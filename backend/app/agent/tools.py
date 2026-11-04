"""Read-only agent tools plus preparation of a customer-confirmed proposal.

The graph has no tool that refunds or creates review tickets. Those actions
require an authenticated confirmation request outside the model loop.
Stored free text is sanitized before it reaches the model; identity always
comes from the verified request token rather than model arguments.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select

from app.agent.guard import detect_injection
from app.db import session as db
from app.db.models import Customer, Order
from app.policy.engine import evaluate

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    conversation_id: str
    verified_customer_id: str | None = None
    user_message_id: int | None = None


UNTRUSTED_PLACEHOLDER = "[text removed: looked like instructions]"
ESCALATION_REASON_PREFIX = "[客服智能体转述的客户诉求，内容未经核实] "


def untrusted_text(value: str | None) -> str | None:
    """A free-text field from the database, or a placeholder if it reads like
    instructions to the model (indirect prompt injection)."""
    if value and detect_injection(value):
        log.warning("instruction-like text removed from tool output", extra={"text": value[:200]})
        return UNTRUSTED_PLACEHOLDER
    return value


def _order_view(order: Order) -> dict:
    return {
        "order_id": order.id,
        "product_name": untrusted_text(order.product_name),
        "category": untrusted_text(order.category),
        "amount": float(order.amount),
        "status": order.status,
        "delivered_date": order.delivered_date.date().isoformat()
        if order.delivered_date
        else None,
        "is_final_sale": order.is_final_sale,
        "already_refunded": order.refunded,
    }


def tool_config(ctx: ToolContext) -> dict:
    """RunnableConfig fragment that carries `ctx` to the tools."""
    return {"configurable": {"tool_ctx": ctx}}


def _ctx(config: RunnableConfig) -> ToolContext:
    ctx = (config or {}).get("configurable", {}).get("tool_ctx")
    return ctx if isinstance(ctx, ToolContext) else ToolContext(conversation_id="")


def _build_tools() -> list:
    def _require_identity(ctx: ToolContext) -> str | None:
        if ctx.verified_customer_id is None:
            return json.dumps(
                {
                    "error": "identity_not_verified",
                    "message": "该会话没有通过认证的客户身份。",
                }
            )
        return None

    async def _get_owned_order(
        ctx: ToolContext, session, order_id: str, for_update: bool = False
    ) -> tuple[Order | None, str | None]:
        stmt = select(Order).where(Order.id == order_id)
        if for_update:
            stmt = stmt.with_for_update()
        order = await session.scalar(stmt)
        if order is None:
            return None, json.dumps(
                {"error": "order_not_found", "order_id": order_id}
            )
        if order.customer_id != ctx.verified_customer_id:
            # Ownership guard: never reveal or act on another customer's order.
            # The reply is identical to a missing order's, so the agent can't
            # confirm that someone else's order exists (found by the red team).
            # Operators still see the attempt.
            log.warning(
                "cross-customer order access refused",
                extra={"order_id": order_id, "customer_id": ctx.verified_customer_id},
            )
            return None, json.dumps({"error": "order_not_found", "order_id": order_id})
        return order, None

    @tool
    async def get_my_profile(config: RunnableConfig) -> str:
        """Load the authenticated customer's profile (name, email, loyalty tier).

        The customer is already signed in; you never need to ask who they are.
        """
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        async with db.SessionLocal() as session:
            customer = await session.get(Customer, ctx.verified_customer_id)
        if customer is None:
            return json.dumps({"found": False, "message": "未找到客户资料。"})
        return json.dumps(
            {
                "found": True,
                "customer_id": customer.id,
                "name": untrusted_text(customer.name),
                "email": untrusted_text(customer.email),
                "loyalty_tier": customer.loyalty_tier,
            }
        )

    @tool
    async def list_orders(config: RunnableConfig) -> str:
        """List all orders belonging to the signed-in customer."""
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        async with db.SessionLocal() as session:
            orders = (
                await session.scalars(
                    select(Order)
                    .where(Order.customer_id == ctx.verified_customer_id)
                    .order_by(Order.order_date.desc())
                    .limit(50)
                )
            ).all()
        return json.dumps({"orders": [_order_view(o) for o in orders]})

    @tool
    async def offer_order_choices(config: RunnableConfig) -> str:
        """Offer a clickable list of the authenticated customer's orders when the intended order is ambiguous.

        This is a read-only disambiguation step. It neither selects an order nor
        checks refund eligibility, prepares a proposal, nor authorizes an action.
        The customer can click an order or continue describing it in chat.
        """
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        async with db.SessionLocal() as session:
            orders = (
                await session.scalars(
                    select(Order)
                    .where(Order.customer_id == ctx.verified_customer_id)
                    .order_by(Order.order_date.desc(), Order.id)
                    .limit(50)
                )
            ).all()
            customer = await session.get(Customer, ctx.verified_customer_id)
            if customer is None:
                return json.dumps({"error": "identity_not_verified"})
            choices = []
            for index, order in enumerate(orders):
                item = _order_view(order)
                item["choice_label"] = chr(ord("A") + index) if index < 26 else str(index + 1)
                item["eligibility"] = evaluate(order, customer).to_dict()
                choices.append(item)
        return json.dumps({"orders": choices}, ensure_ascii=False)

    @tool
    async def get_order(order_id: str, config: RunnableConfig) -> str:
        """Get the details of a single order belonging to the signed-in customer."""
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        async with db.SessionLocal() as session:
            order, err = await _get_owned_order(ctx, session, order_id)
        if err:
            return err
        return json.dumps(_order_view(order))

    @tool
    async def check_refund_eligibility(order_id: str, config: RunnableConfig) -> str:
        """Check whether an order is eligible for a refund WITHOUT issuing one.

        Runs the deterministic refund policy and returns eligibility, whether the
        refund requires human escalation, and the reasons. Use this before
        deciding what to tell the customer.
        """
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        async with db.SessionLocal() as session:
            order, err = await _get_owned_order(ctx, session, order_id)
            if err:
                return err
            customer = await session.get(Customer, ctx.verified_customer_id)
            result = evaluate(order, customer)
        return json.dumps({"order_id": order_id, **result.to_dict()})

    @tool
    async def prepare_refund(order_id: str, config: RunnableConfig) -> str:
        """Prepare a refund or review proposal ONLY after an explicit request.

        Does NOT refund, approve or create a reviewer ticket. For questions such
        as 'can I return it?', use check_refund_eligibility instead. A real
        customer must click the resulting card; chat text is never confirmation.
        """
        ctx = _ctx(config)
        guard = _require_identity(ctx)
        if guard:
            return guard
        from app.refund_confirmation import ProposalError, prepare

        try:
            result = await prepare(ctx.verified_customer_id, ctx.conversation_id, order_id, ctx.user_message_id)
        except ProposalError:
            return json.dumps({"error": "conversation_not_found"})
        if result.get("proposal"):
            result["proposal"]["product_name"] = untrusted_text(result["proposal"]["product_name"])
        return json.dumps(result, ensure_ascii=False)

    return [get_my_profile, list_orders, offer_order_choices, get_order, check_refund_eligibility, prepare_refund]


TOOLS: list = _build_tools()
