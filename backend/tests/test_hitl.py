"""Human-in-the-loop: escalation tickets, the reviewer queue, and the payout.

These tests pin the behaviour that makes "转人工" a closed loop rather than a
dead-end row:

- the agent's escalation is a *ticket* the reviewer queue can see;
- a human decision is the only thing that flips an escalated order to paid;
- the customer is told in the same conversation, and their own endpoint agrees;
- a ticket resolves once, and a scoped token cannot resolve anything.
"""

import json

from sqlalchemy import select

from app.agent.tools import TOOLS, ToolContext, tool_config
from app.auth import ROLE_ADMIN, mint_token
from app.db import session as db
from app.db.models import Conversation, Message, Order, Refund
from tests.conftest import admin_auth, auth, tools_by_name

CONV = "conv-hitl"


async def _call(ctx, name, **kwargs):
    return json.loads(await tools_by_name(TOOLS)[name].ainvoke(kwargs, config=tool_config(ctx)))


async def _open_ticket(order_id: str = "ORD-1003", customer: str = "CUST-003"):
    """Escalate an over-the-limit order the way a chat turn would."""
    async with db.SessionLocal() as session:
        session.add(Conversation(id=CONV, customer_id=customer))
        await session.commit()
    ctx = ToolContext(conversation_id=CONV, verified_customer_id=customer)
    result = await _call(ctx, "prepare_refund", order_id=order_id)
    assert result["decision"] == "awaiting_confirmation"
    # Explicitly simulate the customer pressing the confirmation button.
    from app.refund_confirmation import act
    await act(customer, result["proposal"]["id"], "confirm")
    async with db.SessionLocal() as session:
        return await session.scalar(
            select(Refund).where(
                Refund.order_id == order_id, Refund.escalation_status == "pending"
            )
        )


async def test_policy_escalation_lands_in_the_reviewer_queue(client, engine):
    ticket = await _open_ticket()
    assert ticket.escalated_at is not None
    # The ticket carries the policy's own wording for the reviewer.
    assert "转人工" in ticket.reason

    res = await client.get("/api/agent/escalations", headers=admin_auth())
    assert res.status_code == 200
    queue = res.json()
    assert [t["id"] for t in queue] == [ticket.id]
    assert queue[0]["status"] == "pending"
    assert queue[0]["customer_name"]
    assert queue[0]["waiting_seconds"] is not None


async def test_approving_pays_out_and_tells_the_customer(client, engine):
    ticket = await _open_ticket()

    res = await client.post(
        f"/api/agent/escalations/{ticket.id}/resolve",
        json={"decision": "approved", "note": "已核实物流破损"},
        headers=admin_auth(),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "approved"
    assert body["resolved_by"] == "admin-1"
    assert body["resolved_at"] is not None

    async with db.SessionLocal() as session:
        refund = await session.get(Refund, ticket.id)
        order = await session.get(Order, "ORD-1003")
        messages = (
            await session.scalars(select(Message).where(Message.conversation_id == CONV))
        ).all()
    # The ledger says a human signed this off.
    assert refund.decision == "approved"
    assert refund.decided_by == "human"
    assert refund.resolved_by == "admin-1"
    assert refund.resolution_note == "已核实物流破损"
    assert order.refunded is True
    # The customer was told, in the conversation, with the reviewer's note.
    assert any("人工复核结果" in m.content and "已批准" in m.content for m in messages)
    assert any("已核实物流破损" in m.content for m in messages)
    assert all(m.role == "assistant" for m in messages)

    # ...and their own view agrees.
    mine = await client.get("/api/me/escalations", headers=auth("CUST-003"))
    assert mine.status_code == 200
    assert [t["status"] for t in mine.json()] == ["approved"]


async def test_denying_leaves_the_order_unrefunded(client, engine):
    ticket = await _open_ticket()

    res = await client.post(
        f"/api/agent/escalations/{ticket.id}/resolve",
        json={"decision": "denied", "note": "照片证据不足"},
        headers=admin_auth(),
    )
    assert res.status_code == 200
    assert res.json()["status"] == "denied"

    async with db.SessionLocal() as session:
        order = await session.get(Order, "ORD-1003")
        refund = await session.get(Refund, ticket.id)
        messages = (
            await session.scalars(select(Message).where(Message.conversation_id == CONV))
        ).all()
    assert order.refunded is False
    assert refund.decision == "denied"
    assert any("拒绝" in m.content for m in messages)


async def test_a_ticket_is_resolved_at_most_once(client, engine):
    ticket = await _open_ticket()
    first = await client.post(
        f"/api/agent/escalations/{ticket.id}/resolve",
        json={"decision": "approved"},
        headers=admin_auth(),
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/agent/escalations/{ticket.id}/resolve",
        json={"decision": "denied"},
        headers=admin_auth(),
    )
    assert second.status_code == 409
    # The first decision stands.
    async with db.SessionLocal() as session:
        refund = await session.get(Refund, ticket.id)
    assert refund.decision == "approved"


async def test_customer_tokens_cannot_review(client, engine):
    ticket = await _open_ticket()
    res = await client.post(
        f"/api/agent/escalations/{ticket.id}/resolve",
        json={"decision": "approved"},
        headers=auth("CUST-003"),
    )
    assert res.status_code == 403
    listed = await client.get("/api/agent/escalations", headers=auth("CUST-003"))
    assert listed.status_code == 403


async def test_scoped_admin_token_cannot_review(client, engine):
    """A demo visitor's scoped token may read its own tickets, not rule on them."""
    ticket = await _open_ticket()
    scoped = {"Authorization": f"Bearer {mint_token('visitor-1', ROLE_ADMIN, scope='CUST-003')}"}

    assert (await client.get("/api/agent/escalations", headers=scoped)).status_code == 403
    res = await client.post(
        f"/api/agent/escalations/{ticket.id}/resolve",
        json={"decision": "approved"},
        headers=scoped,
    )
    assert res.status_code == 403
    async with db.SessionLocal() as session:
        refund = await session.get(Refund, ticket.id)
    assert refund.escalation_status == "pending"


async def test_a_customer_only_sees_their_own_tickets(client, engine):
    await _open_ticket()
    other = await client.get("/api/me/escalations", headers=auth("CUST-001"))
    assert other.status_code == 200
    assert other.json() == []
    # ...and cannot even open the stream of a conversation they don't own.
    foreign_stream = await client.get(
        "/api/me/conversations/conv-hitl/stream", headers=auth("CUST-001")
    )
    assert foreign_stream.status_code == 404


async def test_unknown_ticket_is_404(client, engine):
    res = await client.get("/api/agent/escalations/9999", headers=admin_auth())
    assert res.status_code == 404
    missing = await client.post(
        "/api/agent/escalations/9999/resolve",
        json={"decision": "approved"},
        headers=admin_auth(),
    )
    assert missing.status_code == 404


async def test_agent_does_not_reopen_a_human_denial(client, engine):
    ticket = await _open_ticket()
    await client.post(
        f"/api/agent/escalations/{ticket.id}/resolve",
        json={"decision": "denied", "note": "不符政策"},
        headers=admin_auth(),
    )

    ctx = ToolContext(conversation_id=CONV, verified_customer_id="CUST-003")
    again = await _call(ctx, "prepare_refund", order_id="ORD-1003")
    assert again["decision"] == "denied"
    assert "人工审核记录" in again["message"]

    async with db.SessionLocal() as session:
        rows = (await session.scalars(select(Refund).where(Refund.order_id == "ORD-1003"))).all()
    assert len(rows) == 1


async def test_approved_escalation_blocks_a_second_ticket(client, engine):
    ticket = await _open_ticket()
    await client.post(
        f"/api/agent/escalations/{ticket.id}/resolve",
        json={"decision": "approved"},
        headers=admin_auth(),
    )

    ctx = ToolContext(conversation_id=CONV, verified_customer_id="CUST-003")
    again = await _call(ctx, "prepare_refund", order_id="ORD-1003")
    # The order is already refunded, so the policy itself refuses a new ticket.
    assert again["decision"] == "denied"
    async with db.SessionLocal() as session:
        approved = (
            await session.scalars(
                select(Refund).where(
                    Refund.order_id == "ORD-1003", Refund.decision == "approved"
                )
            )
        ).all()
    assert len(approved) == 1


async def test_queue_stats_track_the_backlog(client, engine):
    ticket = await _open_ticket()
    pending = await client.get("/api/agent/escalations/stats", headers=admin_auth())
    assert pending.status_code == 200
    assert pending.json()["pending"] == 1

    await client.post(
        f"/api/agent/escalations/{ticket.id}/resolve",
        json={"decision": "approved"},
        headers=admin_auth(),
    )
    after = await client.get("/api/agent/escalations/stats", headers=admin_auth())
    body = after.json()
    assert body["pending"] == 0
    assert body["resolved"] == 1
    assert body["avg_resolution_seconds"] is not None

    # The console's headline numbers carry the same queue depth.
    stats = await client.get("/api/admin/stats", headers=admin_auth())
    assert stats.status_code == 200
    assert stats.json()["escalations_pending"] == 0
    assert stats.json()["escalations_resolved"] == 1
