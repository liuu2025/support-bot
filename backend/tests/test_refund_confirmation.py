"""Customer authorization is a server boundary, independent of model behavior."""

import asyncio
import json
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

from app import refund_confirmation as confirmation
from app.agent.runner import create_conversation, run_agent_turn
from app.agent.tools import TOOLS, ToolContext, tool_config
from app.db import session as db
from app.db.models import Conversation, Message, Order, ReasoningEvent, Refund, RefundProposal, utcnow
from tests.conftest import admin_auth, auth, tools_by_name
from tests.doubles import ScriptedChatModel, install, tool_call


async def proposal(customer="CUST-001", order="ORD-1001"):
    cid = await create_conversation(customer)
    result = await confirmation.prepare(customer, cid, order)
    return result["proposal"]


async def decide(client, p, decision="confirm", customer="CUST-001", **extra):
    return await client.post(
        f'/api/me/refund-proposals/{p["id"]}/decision',
        headers=auth(customer),
        json={"decision": decision, **extra},
    )


async def ledger_count():
    async with db.SessionLocal() as session:
        return await session.scalar(select(func.count(Refund.id)))


async def test_agent_has_no_executing_or_identity_switching_tools(engine):
    names = set(tools_by_name(TOOLS))
    assert names == {
        "get_my_profile",
        "list_orders",
        "get_order",
        "check_refund_eligibility",
        "prepare_refund",
        "offer_order_choices",
    }
    cid = await create_conversation("CUST-001")
    value = json.loads(
        await tools_by_name(TOOLS)["prepare_refund"].ainvoke(
            {"order_id": "ORD-1001", "user_confirmed": True},
            config=tool_config(ToolContext(cid, "CUST-001")),
        )
    )
    assert value["decision"] == "awaiting_confirmation"
    assert await ledger_count() == 0
    async with db.SessionLocal() as session:
        assert not (await session.get(Order, "ORD-1001")).refunded


def test_model_amount_formatting_uses_yuan_without_changing_the_value():
    from app.agent.runner import display_amounts

    assert (
        display_amounts("金额 ¥129.99，阈值 ￥500，大额 $1,299.00")
        == "金额 129.99 元，阈值 500 元，大额 1,299.00 元"
    )


async def test_consultation_only_checks_policy(client, engine, monkeypatch):
    model = install(
        monkeypatch,
        ScriptedChatModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[tool_call("check_refund_eligibility", {"order_id": "ORD-1001"})],
                ),
                AIMessage(content="这笔订单符合退款条件。如需申请，请告诉我。"),
            ]
        ),
    )
    response = await client.post(
        "/api/chat", headers=auth("CUST-001"), json={"message": "ORD-1001 能退吗？"}
    )
    assert response.status_code == 200 and model.calls == 2
    assert await ledger_count() == 0
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count(RefundProposal.id))) == 0


async def test_denial_keeps_the_explanation_and_negative_refund_status(client, engine, monkeypatch):
    reply = "订单为特价商品，不符合退款条件。\n- 是否**已退款**：**否**\n未生成退款申请。"
    install(
        monkeypatch,
        ScriptedChatModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[tool_call("check_refund_eligibility", {"order_id": "ORD-1001"})],
                ),
                AIMessage(content=reply),
            ]
        ),
    )
    async with db.SessionLocal() as session:
        (await session.get(Order, "ORD-1001")).is_final_sale = True
        await session.commit()
    events = [event async for event in run_agent_turn("CUST-001", None, "申请退款")]
    assert not any(event.get("step_type") == "output_correction" for event in events)
    assert next(event["content"] for event in events if event["kind"] == "message" and event.get("role") == "assistant") == reply
    assert await ledger_count() == 0
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count(RefundProposal.id))) == 0


@pytest.mark.parametrize(
    "customer,order,expected",
    [
        ("CUST-001", "ORD-1001", "确认退款或取消申请"),
        ("CUST-003", "ORD-1003", "确认提交人工审核或取消申请"),
    ],
)
async def test_prepared_card_uses_a_clear_prompt_instead_of_model_debug_details(
    client,
    engine,
    monkeypatch,
    customer,
    order,
    expected,
):
    install(
        monkeypatch,
        ScriptedChatModel(
            [
                AIMessage(
                    content="", tool_calls=[tool_call("prepare_refund", {"order_id": order})]
                ),
                AIMessage(
                    content="申请编号 rp-internal，状态 pending，有效期 2026-10-28 12:00 UTC。"
                ),
            ]
        ),
    )
    events = [event async for event in run_agent_turn(customer, None, "申请退款")]
    content = next(event["content"] for event in events if event["kind"] == "message" and event.get("role") == "assistant")
    assert expected in content and "尚未" in content
    assert not any(value in content for value in ("rp-internal", "pending", "UTC"))
    assert any(event["kind"] == "refund_proposal" for event in events)
    assert await ledger_count() == 0


async def test_compromised_model_cannot_execute_even_if_it_claims_success(
    client, engine, monkeypatch
):
    install(
        monkeypatch,
        ScriptedChatModel(
            [
                AIMessage(
                    content="", tool_calls=[tool_call("issue_refund", {"order_id": "ORD-1001"})]
                ),
                AIMessage(content="已为您退款。"),
            ]
        ),
    )
    events = [event async for event in run_agent_turn("CUST-001", None, "我只是问问能不能退")]
    assert await ledger_count() == 0
    assert any(event.get("step_type") == "output_correction" for event in events)


async def test_confirm_is_durable_idempotent_and_never_calls_model(client, engine, monkeypatch):
    p = await proposal()

    def forbidden(*args, **kwargs):
        raise AssertionError("confirmation must not invoke a model")

    monkeypatch.setattr("app.agent.graph.get_chat_model", forbidden)
    first = await decide(client, p)
    second = await decide(client, p)
    assert first.status_code == second.status_code == 200
    assert first.json()["proposal"]["status"] == "confirmed"
    assert second.json()["replayed"] is True
    assert first.json()["proposal"]["refund_id"] == second.json()["proposal"]["refund_id"]
    assert await ledger_count() == 1
    async with db.SessionLocal() as session:
        assert (await session.get(Order, "ORD-1001")).refunded
    restored = await client.get(
        f'/api/me/conversations/{p["conversation_id"]}', headers=auth("CUST-001")
    )
    assert restored.status_code == 200
    assert any(e["step_type"] == "confirmation" for e in restored.json()["events"])


async def test_cancel_is_not_a_policy_denial_and_prevents_later_confirm(client, engine):
    p = await proposal()
    assert (await decide(client, p, "cancel")).status_code == 200
    assert (await decide(client, p, "cancel")).json()["replayed"] is True
    assert (await decide(client, p)).status_code == 409
    assert await ledger_count() == 0


async def test_proposal_ownership_authentication_and_tampered_fields(client, engine):
    p = await proposal()
    path = f'/api/me/refund-proposals/{p["id"]}/decision'
    assert (await client.post(path, json={"decision": "confirm"})).status_code == 401
    assert (
        await client.post(path, headers=admin_auth(), json={"decision": "confirm"})
    ).status_code == 403
    assert (await decide(client, p, customer="CUST-002")).status_code == 404
    assert (await decide(client, p, amount=0.01, order_id="ORD-1003")).status_code == 422
    assert await ledger_count() == 0
    assert (await decide(client, p)).status_code == 200


@pytest.mark.parametrize(
    "field,value",
    [("amount", 1), ("is_final_sale", True), ("refunded", True), ("product_name", "另一件商品")],
)
async def test_changed_order_requires_a_fresh_confirmation(client, engine, field, value):
    p = await proposal()
    async with db.SessionLocal() as session:
        order = await session.get(Order, "ORD-1001")
        setattr(order, field, value)
        await session.commit()
    assert (await decide(client, p)).status_code == 409
    assert (await confirmation.owned_proposal("CUST-001", p["id"]))["status"] == "invalidated"
    assert await ledger_count() == 0


async def test_expired_and_superseded_cards_cannot_execute(client, engine):
    p = await proposal()
    async with db.SessionLocal() as session:
        row = await session.get(RefundProposal, p["id"])
        row.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    assert (await decide(client, p)).status_code == 409
    p2 = await proposal()
    await confirmation.invalidate_pending("CUST-001", p2["conversation_id"])
    assert (await decide(client, p2)).status_code == 409
    assert await ledger_count() == 0


async def test_changed_policy_requires_a_fresh_confirmation(client, engine, monkeypatch):
    from app.config import get_settings

    p = await proposal()
    monkeypatch.setattr(get_settings(), "escalation_threshold", 50)
    assert (await decide(client, p)).status_code == 409
    assert await ledger_count() == 0


async def test_pending_review_is_created_only_after_customer_confirms(client, engine):
    p = await proposal("CUST-003", "ORD-1003")
    assert p["action"] == "review" and await ledger_count() == 0
    assert (await decide(client, p, customer="CUST-003")).status_code == 200
    async with db.SessionLocal() as session:
        order = await session.get(Order, "ORD-1003")
        refund = await session.scalar(select(Refund).where(Refund.order_id == order.id))
        assert not order.refunded and refund.escalation_status == "pending"


async def test_parallel_confirmations_record_exactly_one_refund(client, engine):
    p = await proposal()
    responses = await asyncio.gather(*(decide(client, p) for _ in range(5)))
    assert any(r.status_code == 200 for r in responses)
    assert all(r.status_code in (200, 409) for r in responses)
    assert await ledger_count() == 1


async def test_two_conversations_cannot_refund_the_same_order_twice(client, engine):
    first = await proposal()
    second = await proposal()
    results = await asyncio.gather(decide(client, first), decide(client, second))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert await ledger_count() == 1


async def test_failed_analysis_invalidates_its_unconfirmed_card(engine, monkeypatch):
    class FailsAfterPreparing(ScriptedChatModel):
        async def ainvoke(self, messages):
            if self.calls:
                raise RuntimeError("provider disconnected")
            return await super().ainvoke(messages)

    install(
        monkeypatch,
        FailsAfterPreparing(
            [
                AIMessage(
                    content="", tool_calls=[tool_call("prepare_refund", {"order_id": "ORD-1001"})]
                ),
            ]
        ),
    )
    events = [event async for event in run_agent_turn("CUST-001", None, "申请退款")]
    card = next(event["proposal"] for event in events if event.get("kind") == "refund_proposal")
    assert (await confirmation.owned_proposal("CUST-001", card["id"]))["status"] == "superseded"
    assert await ledger_count() == 0


async def test_prepare_rejects_ineligible_orders_without_a_confirmation_card(engine):
    cid = await create_conversation("CUST-002")
    result = await confirmation.prepare("CUST-002", cid, "ORD-1002")
    assert result["decision"] == "denied" and "proposal" not in result
    assert await ledger_count() == 0


async def test_order_choice_selection_is_scoped_and_persisted(engine):
    cid = await create_conversation("CUST-001")
    async with db.SessionLocal() as session:
        user = Message(conversation_id=cid, role="user", content="哪一笔订单")
        session.add(user)
        await session.flush()
        conversation = await session.get(Conversation, cid)
        conversation.event_seq = 1
        session.add(ReasoningEvent(
            conversation_id=cid, message_id=user.id, seq=1, step_type="tool_result", node="tools",
            payload={"tool": "offer_order_choices", "result": {"orders": [
                {"order_id": "ORD-1001", "choice_label": "A"},
                {"order_id": "ORD-1003", "choice_label": "B"},
            ]}},
        ))
        await session.flush()
        session.add(Message(conversation_id=cid, role="user", content="选择a"))
        await session.commit()
    pending = await confirmation.record_choice_card_pending("CUST-001", cid, user.id)
    assert pending["payload"]["status"] == "pending"
    assert await confirmation.resolve_order_choice_reply("CUST-001", cid, "选择a") == (f"choice:{user.id}", "ORD-1001")
    selected = await confirmation.set_choice_card_state("CUST-001", cid, f"choice:{user.id}", "selected", "ORD-1001")
    assert selected["payload"]["status"] == "selected"
    assert selected["message_id"] != user.id
    assert selected["payload"]["source_message_id"] == user.id
    assert selected["payload"]["by_message_id"] == selected["message_id"]
    with pytest.raises(confirmation.ProposalError):
        await confirmation.set_choice_card_state("CUST-001", cid, f"choice:{user.id}", "selected", "ORD-1003")


async def test_failed_turn_does_not_invalidate_another_conversation_proposal(engine):
    first_cid = await create_conversation("CUST-001")
    second_cid = await create_conversation("CUST-001")
    async with db.SessionLocal() as session:
        first_message = Message(conversation_id=first_cid, role="user", content="申请退款")
        second_message = Message(conversation_id=second_cid, role="user", content="申请退款")
        session.add_all([first_message, second_message])
        await session.flush()
        first_source, second_source = first_message.id, second_message.id
        await session.commit()
    first = (await confirmation.prepare("CUST-001", first_cid, "ORD-1001", first_source))["proposal"]
    second = (await confirmation.prepare("CUST-001", second_cid, "ORD-1001", second_source))["proposal"]
    await confirmation.invalidate_pending("CUST-001", first_cid, first_source)
    assert (await confirmation.owned_proposal("CUST-001", first["id"]))["status"] == "superseded"
    assert (await confirmation.owned_proposal("CUST-001", second["id"]))["status"] == "pending"


async def test_local_session_has_one_identity_and_three_orders_and_is_dev_only(
    client, engine, monkeypatch
):
    response = await client.post("/api/local/session")
    assert response.status_code == 200
    body = response.json()
    assert body["customer"]["name"] == "林晓" and len(body["orders"]) == 3
    assert "admin_token" not in body
    assert {o["scenario"] for o in body["orders"]} == {"refundable", "final_sale", "high_value"}
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "env", "prod")
    assert (await client.post("/api/local/session")).status_code == 404
