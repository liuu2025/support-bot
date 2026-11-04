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
        "source_message_id": proposal.source_message_id,
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


async def invalidate_pending(customer_id: str, cid: str, source_message_id: int | None = None) -> None:
    """End only cards created by a failed turn; unrelated cards survive."""
    events: list[dict] = []
    async with db.SessionLocal() as session:
        await lock_conversation(session, cid, customer_id)
        filters = [
            RefundProposal.customer_id == customer_id,
            RefundProposal.conversation_id == cid,
            RefundProposal.status == "pending",
        ]
        if source_message_id is not None:
            filters.append(RefundProposal.source_message_id == source_message_id)
        proposals = (await session.scalars(select(RefundProposal).where(*filters))).all()
        for proposal in proposals:
            proposal.status = "superseded"
            proposal.resolved_at = utcnow()
            source_id = proposal.source_message_id or source_message_id
            if source_id is not None:
                events.append(await _record_card_state(
                    session, cid, source_id, f"proposal:{proposal.id}",
                    "refund_proposal", "superseded", action_message_id=source_message_id,
                ))

        if source_message_id is not None:
            source = await _offer_choice_event(session, cid, source_message_id)
            card_id = f"choice:{source_message_id}"
            if source is not None:
                state = await _latest_card_state(session, cid, card_id)
                if state is None or state.payload.get("status") == "pending":
                    events.append(await _record_card_state(
                        session, cid, source_message_id, card_id, "order_choice", "abandoned",
                        {"by_message_id": source_message_id}, action_message_id=source_message_id,
                    ))
        await session.commit()
    for event in events:
        await broadcaster.publish(cid, event)


async def _record_card_state(
    session, cid: str, source_message_id: int | None, card_id: str, card_type: str, status: str,
    extra: dict | None = None, *, action_message_id: int | None = None,
) -> dict:
    seq = await session.scalar(
        update(Conversation).where(Conversation.id == cid)
        .values(event_seq=Conversation.event_seq + 1).returning(Conversation.event_seq)
    )
    event_message_id = action_message_id if action_message_id is not None else source_message_id
    payload = {"card_id": card_id, "card_type": card_type, "status": status,
               "source_message_id": source_message_id, **(extra or {})}
    event = ReasoningEvent(
        conversation_id=cid, message_id=event_message_id, seq=seq, step_type="card_state",
        node="customer_action", payload=payload,
    )
    session.add(event)
    await session.flush()
    return {"kind": "step", "id": event.id, "message_id": event_message_id, "seq": seq,
            "step_type": event.step_type, "node": event.node, "payload": event.payload,
            "created_at": aware(event.created_at).isoformat()}


async def _latest_card_state(session, cid: str, card_id: str):
    states = (await session.scalars(select(ReasoningEvent).where(
        ReasoningEvent.conversation_id == cid, ReasoningEvent.step_type == "card_state",
    ).order_by(ReasoningEvent.seq.desc()))).all()
    return next((event for event in states if event.payload.get("card_id") == card_id), None)


async def abandon_cards(customer_id: str, cid: str, message_id: int, card_ids: list[str]) -> list[dict]:
    """Persist abandonment only for cards the customer explicitly chose to leave."""
    events: list[dict] = []
    async with db.SessionLocal() as session:
        await lock_conversation(session, cid, customer_id)
        for card_id in dict.fromkeys(card_ids):
            if card_id.startswith("proposal:"):
                proposal_id = card_id.removeprefix("proposal:")
                proposal = await session.get(RefundProposal, proposal_id)
                if proposal is None or proposal.customer_id != customer_id or proposal.conversation_id != cid:
                    continue
                if proposal.status == "pending" and aware(proposal.expires_at) > utcnow():
                    proposal.status = "superseded"
                    proposal.resolved_at = utcnow()
                    events.append(await _record_card_state(
                        session, cid, proposal.source_message_id, card_id, "refund_proposal", "abandoned",
                        {"by_message_id": message_id}, action_message_id=message_id,
                    ))
            elif card_id.startswith("choice:"):
                source_id = card_id.removeprefix("choice:")
                if not source_id.isdigit():
                    continue
                source_id_int = int(source_id)
                source = await _offer_choice_event(session, cid, source_id_int)
                if source is None:
                    continue
                state = await _latest_card_state(session, cid, card_id)
                if state is not None and state.payload.get("status") != "pending":
                    continue
                events.append(await _record_card_state(
                    session, cid, source_id_int, card_id, "order_choice", "abandoned",
                    {"by_message_id": message_id}, action_message_id=message_id,
                ))
        await session.commit()
    for event in events:
        await broadcaster.publish(cid, event)
    return events


async def _offer_choice_event(session, cid: str, source_id: int):
    candidates = (await session.scalars(select(ReasoningEvent).where(
        ReasoningEvent.conversation_id == cid, ReasoningEvent.message_id == source_id,
        ReasoningEvent.step_type == "tool_result",
    ).order_by(ReasoningEvent.seq.desc()))).all()
    return next((event for event in candidates if event.payload.get("tool") == "offer_order_choices"), None)


async def record_choice_card_pending(customer_id: str, cid: str, source_id: int) -> dict | None:
    card_id = f"choice:{source_id}"
    async with db.SessionLocal() as session:
        await lock_conversation(session, cid, customer_id)
        if await _latest_card_state(session, cid, card_id) is not None:
            return None
        event = await _record_card_state(session, cid, source_id, card_id, "order_choice", "pending")
        await session.commit()
    await broadcaster.publish(cid, event)
    return event


async def record_proposal_card_pending(customer_id: str, cid: str, proposal_id: str, source_id: int | None) -> dict | None:
    if source_id is None:
        return None
    card_id = f"proposal:{proposal_id}"
    async with db.SessionLocal() as session:
        await lock_conversation(session, cid, customer_id)
        if await _latest_card_state(session, cid, card_id) is not None:
            return None
        proposal = await session.get(RefundProposal, proposal_id)
        if proposal is None or proposal.customer_id != customer_id or proposal.conversation_id != cid:
            return None
        event = await _record_card_state(session, cid, source_id, card_id, "refund_proposal", "pending")
        await session.commit()
    await broadcaster.publish(cid, event)
    return event


async def resolve_order_choice_reply(customer_id: str, cid: str, text: str) -> tuple[str, str] | None:
    """Resolve a bare contextual label against the newest pending choice card."""
    import re

    match = re.fullmatch(r"(?:选择|选|第)?\s*([a-z]|\d{1,2})(?:号|个)?", text.strip().casefold())
    if not match:
        return None
    label = match.group(1)
    async with db.SessionLocal() as session:
        latest_user = await session.scalar(select(Message).where(
            Message.conversation_id == cid, Message.role == "user",
        ).order_by(Message.id.desc()).limit(1))
        if latest_user is None:
            return None
        previous_user = await session.scalar(select(Message).where(
            Message.conversation_id == cid, Message.role == "user", Message.id < latest_user.id,
        ).order_by(Message.id.desc()).limit(1))
        if previous_user is None:
            return None
        events = (await session.scalars(select(ReasoningEvent).where(
            ReasoningEvent.conversation_id == cid,
            ReasoningEvent.message_id == previous_user.id,
            ReasoningEvent.step_type == "tool_result",
        ).order_by(ReasoningEvent.seq.desc()))).all()
        event = next((item for item in events if item.payload.get("tool") == "offer_order_choices"), None)
        if event is None or event.message_id is None:
            return None
        card_id = f"choice:{event.message_id}"
        state = await _latest_card_state(session, cid, card_id)
        # Missing pending state is reconciled as pending for the narrow crash
        # window between the offer event and its state event.
        if state is not None and state.payload.get("status") != "pending":
            return None
        choices = (event.payload.get("result") or {}).get("orders", [])
        matches = [
            row for index, row in enumerate(choices)
            if isinstance(row, dict)
            and str(row.get("choice_label", chr(ord("A") + index) if index < 26 else index + 1)).casefold() == label
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("order_id"), str):
            return None
        owned = await session.scalar(select(Order.id).where(
            Order.id == matches[0]["order_id"], Order.customer_id == customer_id,
        ))
        if owned is None:
            return None
        return card_id, matches[0]["order_id"]
    return None


async def set_choice_card_state(
    customer_id: str, cid: str, card_id: str, status: str, order_id: str | None = None,
    *, action_message_id: int | None = None,
) -> dict:
    if status not in {"selected", "skipped"} or not card_id.startswith("choice:"):
        raise ProposalError(422, "无效的订单选择卡操作。")
    if status == "skipped" and order_id is not None:
        raise ProposalError(422, "继续描述时不能附带订单号。")
    try:
        source_id = int(card_id.removeprefix("choice:"))
    except ValueError:
        raise ProposalError(404, "订单选择卡不存在。") from None
    async with db.SessionLocal() as session:
        await lock_conversation(session, cid, customer_id)
        source = await _offer_choice_event(session, cid, source_id)
        if source is None:
            raise ProposalError(404, "订单选择卡不存在。")
        previous = await _latest_card_state(session, cid, card_id)
        if previous is None:
            # Reconcile a crash between storing the offer event and its initial
            # pending-state event while keeping the transition atomic.
            await _record_card_state(session, cid, source_id, card_id, "order_choice", "pending")
        elif previous.payload.get("status") != "pending":
            raise ProposalError(409, "这张订单选择卡已处理或已关闭。")
        if status == "selected":
            allowed = {row.get("order_id") for row in (source.payload.get("result") or {}).get("orders", []) if isinstance(row, dict)}
            if order_id not in allowed:
                raise ProposalError(404, "所选订单不在这张卡片中。")
            owned = await session.scalar(select(Order.id).where(
                Order.id == order_id, Order.customer_id == customer_id,
            ))
            if owned is None:
                raise ProposalError(404, "所选订单不在这张卡片中。")
        action_message = None
        if action_message_id is None:
            source_message = await session.get(Message, source_id)
            source_text = source_message.content if source_message is not None else ""
            if status == "selected":
                content = f"我选择订单 {order_id}。关于‘{source_text}’，请继续帮我处理。"
            else:
                content = f"继续描述，不选择订单。此前我问的是：‘{source_text}’。"
            message = Message(conversation_id=cid, role="user", content=content)
            session.add(message)
            await session.flush()
            action_message_id = message.id
            action_message = {
                "kind": "message", "role": "user", "content": content,
                "message_id": message.id,
            }
        event = await _record_card_state(
            session, cid, source_id, card_id, "order_choice", status,
            {"by_message_id": action_message_id, **({"order_id": order_id} if order_id else {})},
            action_message_id=action_message_id,
        )
        if action_message is not None:
            event["action_message"] = action_message
        await session.commit()
    await broadcaster.publish(cid, event)
    if event.get("action_message"):
        await broadcaster.publish(cid, event["action_message"])
    return event


async def prepare(customer_id: str, cid: str, order_id: str, source_message_id: int | None = None) -> dict:
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
                RefundProposal.customer_id == customer_id,
                RefundProposal.conversation_id == cid,
                RefundProposal.order_id == order_id,
                RefundProposal.status == "pending",
            ).order_by(RefundProposal.created_at.desc())
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
            source_message_id=source_message_id,
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
) -> tuple[dict, dict, dict]:
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
    step.message_id = message.id
    state = await _record_card_state(
        session, cid, proposal.source_message_id, f"proposal:{proposal.id}",
        "refund_proposal", proposal.status,
        {"by_message_id": message.id}, action_message_id=message.id,
    )
    return (
        {"kind": "message", "role": "assistant", "content": content, "message_id": message.id},
        {
            "kind": "step",
            "id": step.id,
            "message_id": message.id,
            "seq": seq,
            "step_type": step.step_type,
            "node": step.node,
            "payload": step.payload,
            "created_at": aware(step.created_at).isoformat(),
        },
        state,
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
        message, step, card_state = await _receipt(session, proposal, content, result_data)
        await session.commit()
        response = {"proposal": view(proposal), "message": message, "step": step, "card_state": card_state, "replayed": False}
    # Data is durable before notifications. Delivery failure cannot turn an
    # executed refund into a failed HTTP action that invites another payment.
    if proposal.status == "confirmed":
        REFUND_DECISIONS.labels(result_data["decision"]).inc()
    try:
        for event in (message, step, response["card_state"], {"kind": "refund_proposal", "proposal": response["proposal"]}):
            await broadcaster.publish(proposal.conversation_id, event)
    except Exception:
        pass
    if failure:
        raise ProposalError(409, failure)
    return response
