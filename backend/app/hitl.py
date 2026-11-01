"""Human-in-the-loop escalation service.

The agent can hand a refund to a human (`escalate_to_human`), but until a
reviewer acts on it the ticket is just a row: nothing reaches the customer and
nothing is ever paid out. This module closes that loop.

Lifecycle of an escalated refund row:

    decision="escalated", escalation_status="pending"     <- agent
        |
        |  a reviewer (role=admin, unscoped token) resolves it
        v
    decision="approved"|"denied", decided_by="human",
    escalation_status="approved"|"denied",
    resolved_at / resolved_by / resolution_note

On resolution the customer is told in the same conversation: an assistant
message is persisted *and* published to the conversation's SSE channel, so an
open chat page updates without a refresh.

Safety properties, mirroring the rest of the codebase:

- The reviewer cannot invent an outcome: the agent-side policy already decided
  the order is refundable-with-human-approval, so a human may only approve or
  deny that specific ticket.
- A ticket is resolved at most once (`escalation_status="pending"` is required),
  enforced under a row lock *and* by the partial unique index that allows one
  approved refund per order.
- Approval is the only path that sets `orders.refunded`; denial changes nothing
  in the ledger beyond the ticket itself.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import session as db
from app.db.models import Conversation, Customer, Message, Order, Refund, utcnow
from app.events import broadcaster
from app.observability import (
    HITL_QUEUE_DEPTH,
    HITL_RESOLUTION_SECONDS,
    HITL_RESOLUTIONS,
    log_event,
)

log = logging.getLogger(__name__)

DECISION_APPROVED = "approved"
DECISION_DENIED = "denied"
STATUS_PENDING = "pending"

_MAX_NOTE = 500


class EscalationNotFound(Exception):
    """No such ticket, or it is not visible to this caller."""


class EscalationAlreadyResolved(Exception):
    def __init__(self, status: str):
        self.status = status
        super().__init__(f"escalation already resolved as {status}")


class EscalationConflict(Exception):
    """Another approved refund for the same order won the race."""


def _amount(value) -> float:
    return float(value or 0)


def _seconds_since(moment: datetime | None) -> float | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (utcnow() - moment).total_seconds())


def customer_message(order_id: str, amount: float, decision: str, note: str) -> str:
    """What the customer sees when a human finishes the review."""
    if decision == DECISION_APPROVED:
        text = (
            f"【人工复核结果】客服专员已批准订单 {order_id} 的退款 "
            f"{amount:.2f} 元，款项将按原支付方式退回。"
        )
    else:
        text = f"【人工复核结果】客服专员复核后拒绝了订单 {order_id} 的退款申请。"
    note = (note or "").strip()
    if note:
        text += f"\n\n专员备注：{note}"
    return text


async def refresh_queue_depth() -> int:
    """Publish the current pending count to Prometheus (best effort)."""
    try:
        async with db.SessionLocal() as session:
            pending = (
                await session.scalar(
                    select(func.count(Refund.id)).where(
                        Refund.escalation_status == STATUS_PENDING
                    )
                )
                or 0
            )
        HITL_QUEUE_DEPTH.set(pending)
        return int(pending)
    except Exception:  # pragma: no cover - metrics must never break a turn
        log.warning("could not refresh HITL queue depth", exc_info=True)
        return 0


async def list_escalations(
    status: str | None = STATUS_PENDING,
    limit: int = 50,
    customer_id: str | None = None,
) -> list[dict]:
    """The reviewer queue (or a customer's own tickets when `customer_id` is set)."""
    stmt = (
        select(Refund, Order, Customer)
        .join(Order, Order.id == Refund.order_id)
        .outerjoin(Customer, Customer.id == Order.customer_id)
        .where(Refund.escalation_status.is_not(None))
        .order_by(Refund.id.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(Refund.escalation_status == status)
    if customer_id is not None:
        stmt = stmt.where(Order.customer_id == customer_id)
    async with db.SessionLocal() as session:
        rows = (await session.execute(stmt)).all()
    return [_ticket(r, o, c) for r, o, c in rows]


def _ticket(refund: Refund, order: Order, customer: Customer | None) -> dict:
    waiting = _seconds_since(refund.escalated_at)
    return {
        "id": refund.id,
        "order_id": refund.order_id,
        "product_name": order.product_name if order else "",
        "amount": _amount(refund.amount),
        "status": refund.escalation_status,
        "decision": refund.decision,
        "reason": refund.reason,
        "customer_id": order.customer_id if order else None,
        "customer_name": customer.name if customer else None,
        "conversation_id": refund.conversation_id,
        "escalated_at": refund.escalated_at or refund.created_at,
        "resolved_at": refund.resolved_at,
        "resolved_by": refund.resolved_by,
        "resolution_note": refund.resolution_note or "",
        "waiting_seconds": waiting,
        "handled_seconds": _seconds_since(refund.escalated_at)
        if refund.resolved_at
        else None,
    }


async def get_escalation(ticket_id: int, customer_id: str | None = None) -> dict:
    """One ticket. With `customer_id`, tickets of other customers are invisible."""
    async with db.SessionLocal() as session:
        refund = await session.get(Refund, ticket_id)
        if refund is None or refund.escalation_status is None:
            raise EscalationNotFound()
        order = await session.get(Order, refund.order_id)
        if order is None:
            raise EscalationNotFound()
        if customer_id is not None and order.customer_id != customer_id:
            raise EscalationNotFound()
        customer = await session.get(Customer, order.customer_id)
        ticket = _ticket(refund, order, customer)
        transcript = []
        if refund.conversation_id:
            messages = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == refund.conversation_id)
                    .order_by(Message.id)
                    .limit(40)
                )
            ).all()
            transcript = [
                {"role": m.role, "content": m.content, "created_at": m.created_at}
                for m in messages
            ]
    ticket["transcript"] = transcript
    return ticket


async def resolve(
    ticket_id: int,
    decision: str,
    reviewer: str,
    note: str = "",
    customer_id: str | None = None,
) -> dict:
    """Record a human's decision and tell the customer.

    Raises EscalationNotFound (unknown/foreign ticket), EscalationAlreadyResolved
    (someone got there first) or EscalationConflict (an approved refund for that
    order already exists).
    """
    if decision not in (DECISION_APPROVED, DECISION_DENIED):
        raise ValueError("decision must be 'approved' or 'denied'")
    note = (note or "").strip()[:_MAX_NOTE]

    async with db.SessionLocal() as session:
        refund = await session.get(Refund, ticket_id, with_for_update=True)
        if refund is None or refund.escalation_status is None:
            raise EscalationNotFound()
        order = await session.get(Order, refund.order_id)
        if order is None or (customer_id is not None and order.customer_id != customer_id):
            raise EscalationNotFound()
        if refund.escalation_status != STATUS_PENDING:
            raise EscalationAlreadyResolved(refund.escalation_status)

        waited = _seconds_since(refund.escalated_at or refund.created_at) or 0.0
        refund.decision = decision
        refund.escalation_status = decision
        refund.decided_by = "human"
        refund.resolved_at = utcnow()
        refund.resolved_by = reviewer
        refund.resolution_note = note
        if decision == DECISION_APPROVED:
            order.refunded = True
        try:
            await session.commit()
        except IntegrityError as exc:
            # uq_refunds_one_approved_per_order: somebody already approved one.
            await session.rollback()
            raise EscalationConflict() from exc

        order_id = order.id
        amount = _amount(refund.amount)
        conversation_id = refund.conversation_id

    HITL_RESOLUTIONS.labels(decision).inc()
    HITL_RESOLUTION_SECONDS.observe(waited)
    log_event(
        log,
        "escalation resolved by human",
        ticket_id=ticket_id,
        order_id=order_id,
        decision=decision,
        reviewer=reviewer,
        waited_s=round(waited, 1),
    )

    await notify_customer(conversation_id, order_id, amount, decision, note, ticket_id)
    await refresh_queue_depth()
    return await get_escalation(ticket_id, customer_id=customer_id)


async def notify_customer(
    conversation_id: str | None,
    order_id: str,
    amount: float,
    decision: str,
    note: str,
    ticket_id: int,
) -> None:
    """Persist the outcome into the chat and stream it to an open chat page."""
    text = customer_message(order_id, amount, decision, note)
    if not conversation_id:
        return
    async with db.SessionLocal() as session:
        # A deleted conversation (purged demo sandbox) has nobody to notify.
        if await session.get(Conversation, conversation_id) is None:
            return
        session.add(Message(conversation_id=conversation_id, role="assistant", content=text))
        await session.commit()
    try:
        await broadcaster.publish(
            conversation_id,
            {"kind": "message", "role": "assistant", "content": text},
        )
        await broadcaster.publish(
            conversation_id,
            {
                "kind": "escalation",
                "status": decision,
                "order_id": order_id,
                "ticket_id": ticket_id,
                "note": note,
            },
        )
    except Exception:  # pragma: no cover - the message is already durable
        log.warning("could not publish escalation outcome", exc_info=True)


async def stats() -> dict:
    """Queue depth plus resolution timings, for the console and the workbench."""
    async with db.SessionLocal() as session:
        pending = (
            await session.scalar(
                select(func.count(Refund.id)).where(
                    Refund.escalation_status == STATUS_PENDING
                )
            )
            or 0
        )
        rows = (
            await session.execute(
                select(Refund.escalation_status, func.count(Refund.id))
                .where(Refund.escalation_status.is_not(None))
                .group_by(Refund.escalation_status)
            )
        ).all()
        resolved = (
            await session.execute(
                select(Refund.escalated_at, Refund.resolved_at).where(
                    Refund.resolved_at.is_not(None)
                )
            )
        ).all()

    waits = [
        _seconds_since(escalated) or 0.0
        for escalated, _resolved_at in resolved
        if escalated is not None
    ]
    HITL_QUEUE_DEPTH.set(int(pending))
    return {
        "pending": int(pending),
        "by_status": {status: count for status, count in rows},
        "resolved": len(waits),
        "avg_resolution_seconds": round(sum(waits) / len(waits), 1) if waits else None,
    }


def dumps(payload: dict) -> str:
    """Small helper for logging structured ticket payloads."""
    return json.dumps(payload, default=str)
