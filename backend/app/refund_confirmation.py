"""Prepare / confirm / cancel. Only authenticated HTTP actions can commit a refund.

The model receives `prepare_refund`, never `confirm`. A proposal fixes the order,
amount, policy and customer for 15 minutes. Confirmation revalidates that snapshot
under a transaction; retries return the same receipt without another LLM call.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.config import get_settings
from app.db import session as db
from app.db.models import (
    Conversation,
    Customer,
    Message,
    Order,
    ReasoningEvent,
    Refund,
    RefundProposal,
    utcnow,
)
from app.events import broadcaster
from app.observability import REFUND_DECISIONS
from app.policy.engine import evaluate


class ProposalError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def snapshot(order: Order) -> dict:
    settings = get_settings()
    return {
        "amount": str(order.amount),
        "product_name": order.product_name,
        "customer_id": order.customer_id,
        "status": order.status,
        "delivered_date": aware(order.delivered_date).isoformat() if order.delivered_date else None,
        "is_final_sale": order.is_final_sale,
        "refunded": order.refunded,
        "return_window_days": settings.return_window_days,
        "escalation_threshold": settings.escalation_threshold,
    }


def view(proposal: RefundProposal) -> dict:
    status = proposal.status
    if status == "pending" and aware(proposal.expires_at) <= utcnow():
        status = "expired"
    return {
        "id": proposal.id,
        "conversation_id": proposal.conversation_id,
        "order_id": proposal.order_id,
        "product_name": proposal.product_name,
        "amount": float(proposal.amount),
        "action": proposal.action,
        "status": status,
        "reasons": proposal.reasons,
        "created_at": aware(proposal.created_at).isoformat(),
        "expires_at": aware(proposal.expires_at).isoformat(),
        "refund_id": proposal.refund_id,
    }


async def lock_conversation(session, cid: str, customer_id: str) -> Conversation:
    # The write also serializes SQLite, whose SELECT FOR UPDATE is a no-op.
    found = await session.scalar(
        update(Conversation)
        .where(Conversation.id == cid, Conversation.customer_id == customer_id)
        .values(event_seq=Conversation.event_seq)
        .returning(Conversation.id)
    )
    if found is None:
        raise ProposalError(404, "未找到当前客户的会话。")
    return await session.get(Conversation, cid)


async def invalidate_pending(customer_id: str, cid: str) -> None:
    async with db.SessionLocal() as session:
        await lock_conversation(session, cid, customer_id)
        await session.execute(
            update(RefundProposal)
            .where(
                RefundProposal.customer_id == customer_id,
                RefundProposal.conversation_id == cid,
                RefundProposal.status == "pending",
            )
            .values(status="superseded", resolved_at=utcnow())
        )
        await session.commit()


async def prepare(customer_id: str, cid: str, order_id: str) -> dict:
    async with db.SessionLocal() as session:
        await lock_conversation(session, cid, customer_id)
        order = await session.scalar(
            select(Order)
            .where(
                Order.id == order_id,
                Order.customer_id == customer_id,
            )
            .with_for_update()
        )
        if order is None:
            return {"error": "order_not_found", "order_id": order_id}
        customer = await session.get(Customer, customer_id)
        result = evaluate(order, customer)
        if not result.eligible:
            return {"order_id": order_id, **result.to_dict()}
        prior = await session.scalar(
            select(Refund)
            .where(
                Refund.order_id == order_id,
                Refund.escalation_status.in_(["pending", "denied"]),
            )
            .order_by(Refund.id.desc())
        )
        if prior:
            return {
                "order_id": order_id,
                "decision": "escalated" if prior.escalation_status == "pending" else "denied",
                "already_escalated": True,
                "message": "该订单已有人工审核记录，请查看工单进度。",
            }
        current = await session.scalar(
            select(RefundProposal).where(
                RefundProposal.conversation_id == cid,
                RefundProposal.status == "pending",
            )
        )
        if (
            current
            and current.order_id == order_id
            and current.snapshot == snapshot(order)
            and view(current)["status"] == "pending"
        ):
            return {
                "order_id": order_id,
                "decision": "awaiting_confirmation",
                "proposal": view(current),
            }
        if current:
            current.status = "superseded"
            current.resolved_at = utcnow()
        proposal = RefundProposal(
            id=f"rp-{uuid.uuid4().hex}",
            customer_id=customer_id,
            conversation_id=cid,
            order_id=order.id,
            product_name=order.product_name,
            amount=order.amount,
            action="review" if result.requires_escalation else "refund",
            status="pending",
            reasons=result.reasons,
            snapshot=snapshot(order),
            expires_at=utcnow() + timedelta(minutes=15),
        )
        session.add(proposal)
        await session.commit()
        return {
            "order_id": order_id,
            "decision": "awaiting_confirmation",
            "proposal": view(proposal),
            "message": "仅生成待确认申请，尚未退款或创建人工工单。请客户点击确认卡片。",
        }


async def list_proposals(customer_id: str, cid: str) -> list[dict]:
    async with db.SessionLocal() as session:
        convo = await session.get(Conversation, cid)
        if convo is None or convo.customer_id != customer_id:
            raise ProposalError(404, "未找到当前客户的会话。")
        rows = (
            await session.scalars(
                select(RefundProposal)
                .where(
                    RefundProposal.customer_id == customer_id,
                    RefundProposal.conversation_id == cid,
                )
                .order_by(RefundProposal.created_at)
                .limit(100)
            )
        ).all()
        return [view(row) for row in rows]


async def owned_proposal(customer_id: str, pid: str) -> dict:
    async with db.SessionLocal() as session:
        row = await session.get(RefundProposal, pid)
        if row is None or row.customer_id != customer_id:
            raise ProposalError(404, "未找到待确认申请。")
        return view(row)


async def _receipt(
    session, proposal: RefundProposal, content: str, result: dict
) -> tuple[dict, dict]:
    cid = proposal.conversation_id
    seq = await session.scalar(
        update(Conversation)
        .where(Conversation.id == cid)
        .values(event_seq=Conversation.event_seq + 1)
        .returning(Conversation.event_seq)
    )
    message = Message(conversation_id=cid, role="assistant", content=content)
    step = ReasoningEvent(
        conversation_id=cid,
        seq=seq,
        step_type="confirmation",
        node="customer_action",
        payload={
            "proposal_id": proposal.id,
            "status": proposal.status,
            "order_id": proposal.order_id,
            "result": result,
        },
    )
    session.add_all([message, step])
    await session.flush()
    return (
        {"kind": "message", "role": "assistant", "content": content, "message_id": message.id},
        {
            "kind": "step",
            "id": step.id,
            "seq": seq,
            "step_type": step.step_type,
            "node": step.node,
            "payload": step.payload,
            "created_at": aware(step.created_at).isoformat(),
        },
    )


async def act(customer_id: str, pid: str, action: str) -> dict:
    """Called by the customer HTTP endpoint, never exposed as an agent tool."""
    if action not in ("confirm", "cancel"):
        raise ProposalError(422, "不支持的确认操作。")
    initial = await owned_proposal(customer_id, pid)
    failure = None
    async with db.SessionLocal() as session:
        await lock_conversation(session, initial["conversation_id"], customer_id)
        proposal = await session.get(RefundProposal, pid, populate_existing=True)
        if proposal.status == "confirmed" and action == "confirm":
            return {"proposal": view(proposal), "replayed": True}
        if proposal.status == "cancelled" and action == "cancel":
            return {"proposal": view(proposal), "replayed": True}
        if proposal.status != "pending":
            raise ProposalError(409, "这张申请已结束，请根据最新订单重新发起。")
        if aware(proposal.expires_at) <= utcnow():
            proposal.status = "expired"
            failure = "确认申请已过期，请重新查询订单并生成申请。"
        elif action == "cancel":
            proposal.status = "cancelled"
            content = f"已取消订单 {proposal.order_id} 的本次申请，订单状态未改变。"
        else:
            order = await session.scalar(
                select(Order)
                .where(
                    Order.id == proposal.order_id,
                    Order.customer_id == customer_id,
                )
                .with_for_update()
            )
            customer = await session.get(Customer, customer_id)
            result = evaluate(order, customer) if order else None
            if (
                order is None
                or snapshot(order) != proposal.snapshot
                or not result.eligible
                or ("review" if result.requires_escalation else "refund") != proposal.action
            ):
                proposal.status = "invalidated"
                failure = "订单或退款条件已变化，旧申请未执行。请重新查询并确认最新信息。"
            else:
                existing = await session.scalar(
                    select(Refund).where(
                        Refund.order_id == order.id,
                        (Refund.decision == "approved")
                        | Refund.escalation_status.in_(["pending", "denied"]),
                    )
                )
                if existing:
                    proposal.status = "invalidated"
                    failure = "该订单已有处理记录，旧申请未重复执行。请刷新订单状态。"
                else:
                    review = proposal.action == "review"
                    refund = Refund(
                        order_id=order.id,
                        amount=proposal.amount,
                        decision="escalated" if review else "approved",
                        reason="客户通过确认卡片授权。" + " ".join(result.reasons),
                        decided_by="customer_confirmed",
                        conversation_id=proposal.conversation_id,
                        escalated_at=utcnow() if review else None,
                        escalation_status="pending" if review else None,
                    )
                    session.add(refund)
                    await session.flush()
                    if not review:
                        order.refunded = True
                    proposal.status = "confirmed"
                    proposal.refund_id = refund.id
                    content = (
                        f"订单 {order.id} 的人工审核申请已提交，金额 {proposal.amount:g} 元，等待人工处理；尚未退款。"
                        if review
                        else f"订单 {order.id} 的退款已记录，金额 {proposal.amount:g} 元，订单状态已更新。"
                    )
        proposal.resolved_at = utcnow()
        if failure:
            content = failure
        result_data = {
            "decision": "not_executed"
            if failure
            else "cancelled"
            if action == "cancel"
            else ("escalated" if proposal.action == "review" else "approved"),
            "amount": float(proposal.amount),
            "status": proposal.status,
        }
        message, step = await _receipt(session, proposal, content, result_data)
        await session.commit()
        response = {"proposal": view(proposal), "message": message, "step": step, "replayed": False}
    # Data is durable before notifications. Delivery failure cannot turn an
    # executed refund into a failed HTTP action that invites another payment.
    if proposal.status == "confirmed":
        REFUND_DECISIONS.labels(result_data["decision"]).inc()
    try:
        for event in (message, step, {"kind": "refund_proposal", "proposal": response["proposal"]}):
            await broadcaster.publish(proposal.conversation_id, event)
    except Exception:
        pass
    if failure:
        raise ProposalError(409, failure)
    return response
