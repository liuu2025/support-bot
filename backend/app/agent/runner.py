"""Drives one agent turn and emits reasoning events.

`run_agent_turn` is an async generator: it runs the LangGraph agent over the
conversation, and for every step (model thoughts, tool calls, tool results,
decisions) it persists a ReasoningEvent, publishes it to the live broadcaster
(Redis pub/sub, for the admin dashboard), and yields it to the caller (the
chat SSE stream). The deterministic policy gate lives in the tools; this layer
only observes.

The customer id comes from the verified JWT (see `app/auth.py`) and the
conversation has already been ownership-checked by the endpoint; both are
re-checked here as defence in depth.

DB sessions are opened per write and never held across LLM calls, so a pod's
connection pool is not consumed by idle, long-running streams.

Guardrails enforced here (see `docs/安全设计.md`):
- A per-conversation **token budget** (`max_conversation_tokens`) and a
  per-customer **daily** budget (`customer_daily_token_budget`): once exceeded,
  the next turn short-circuits with a polite refusal — no model call. The
  public demo has a demo-wide daily cap as well; reaching it refuses new turns
  the same way (there is no cheaper local model to fall back to).
- The LangGraph **recursion limit** (`agent_recursion_limit`) bounds the
  tool-call loop, and `turn_timeout_s` bounds wall-clock time.
- An **output sanitizer**: model turns cannot execute refunds. An affirmative
  claim of execution is replaced by a correction, with an `output_correction`
  event. Customer confirmation runs through a separate deterministic endpoint.

When Opik is configured (`app/tracing.py`) the turn is also traced there, and a
final `trace` step links the timeline to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import insert, select, update

from app import model_config, tracing
from app import redis as shared
from app.agent.graph import get_agent
from app.agent.guard import detect_injection
from app.agent.llm import ModelNotConfigured
from app.agent.prompts import get_system_prompt
from app.agent.tools import UNTRUSTED_PLACEHOLDER, ToolContext, tool_config
from app.config import get_settings
from app.db import session as db
from app.db.models import Conversation, Message, ReasoningEvent, utcnow
from app.events import broadcaster
from app.observability import (
    INJECTION_FLAGS,
    LLM_TOKENS,
    TOOL_CALLS,
    TURN_LATENCY,
    TURNS,
    TURNS_IN_FLIGHT,
    conversation_id_var,
    log_event,
)

logger = logging.getLogger(__name__)

# How many prior messages are replayed to the model (bounded prompt size).
_HISTORY_LIMIT = 40

_ERROR_MESSAGE = (
    "抱歉，处理这条消息时我们内部出错了，请稍后重试。"
)

_DECISION_TOOLS = {"prepare_refund"}

# Phrases that make an *affirmative* claim a refund was granted. If the agent's
# final reply contains one of these, the sanitizer replaces the unsupported claim.
#
# These must be affirmative success phrasings — NOT bare words like "approved"
# or "refunded" — so that legitimate denials ("this order has already been
# refunded") and escalations ("it cannot be approved automatically") don't trip
# the sanitizer. The intervening-word structure also naturally excludes negated
# forms ("has not been approved"), since the negation breaks "has been approved".
_APPROVAL_CLAIM = re.compile(
    r"(?:"
    # "your/the refund [of $X] has been | is | was approved|processed|issued|…"
    r"(?:your |the )?refund(?:\s+of\s+\$[\d,.]+)?\s+(?:has been|is|was)\s+"
    r"(?:approved|processed|issued|completed|refunded)"
    # "successfully | now approved|processed|issued|refunded"
    r"|(?:successfully|now)\s+(?:approved|processed|issued|refunded)"
    # "I've | I have approved|processed|issued (your|the) refund"
    r"|i(?:'ve| have)\s+(?:approved|processed|issued)\s+(?:your |the )?refund"
    # "approved|processed|issued your refund"
    r"|(?:approved|processed|issued)\s+your\s+refund"
    r")",
    re.I,
)

# A claim preceded by a negation ("No refund was issued", "not a single refund
# has been processed") is a denial, not a claim.
_NEGATED = re.compile(r"\b(?:no|not|never|without|nor)\b[\s\w,'-]{0,12}$", re.I)
# So is a claim the agent explicitly declines to make ("I can't bypass the limit
# or confirm that a refund was issued"; found by the red team). Only a refusal
# verb directly governing the claim counts, so "Even though I can't normally,
# your refund has been approved" is still a claim.
_DECLINED = re.compile(
    r"\b(?:can[’']?t|cannot|can not|won[’']t|will not|unable to|not able to)\b"
    r"[^.!?\n]{0,60}?\b(?:confirm|say|state|claim|pretend|report|promise|guarantee|tell you|include|write)\s+"
    # "that the $89 ", "that another **$59** ", "“Your second " — up to three
    # short words, Markdown emphasis included
    r"(?:that\s+)?[“\"']?(?:[\w$,.*]+\s+){0,3}$",
    re.I,
)


# Chinese mirror of the same idea: "退款已批准" / "已为您退款" / "退款已到账".
_APPROVAL_CLAIM_ZH = re.compile(
    r"(?:退款|款项|货款)[^。！？\n]{0,10}(?:已经|已)(?:被)?(?:批准|通过|到账|办理完成)"
    r"|(?:已经|已)(?:为您|给你|给你方)?(?:办理|完成|处理)?(?:全额)?退款"
    r"|(?:批准|同意)了?(?:你的?|您的)?(?:全额)?退款"
)
_NEGATED_ZH = re.compile(r"(?:无法|不能|不可以|不予|不支持|尚未|没有|未能?)[^。！？\n]{0,6}$")
_STATUS_QUESTION_ZH = re.compile(r"是否[\s*_`]*$")
_NEGATIVE_STATUS_ZH = re.compile(r"^[\s*_`]*[:：][\s*_`]*(?:否|没有|未退款|false\b|no\b)", re.I)


def claims_approval(text: str) -> bool:
    """True if `text` affirmatively says a refund was granted."""
    if any(
        not _NEGATED.search(text[max(0, m.start() - 24) : m.start()])
        and not _DECLINED.search(text[max(0, m.start() - 100) : m.start()])
        for m in _APPROVAL_CLAIM.finditer(text)
    ):
        return True
    # Chinese mirror ("退款已批准" / "已为您退款" / "退款已到账").
    return any(
        not _NEGATED_ZH.search(text[max(0, m.start() - 12) : m.start()])
        # A structured negative field ("是否已退款：否") is not an execution claim.
        and not (
            _STATUS_QUESTION_ZH.search(text[max(0, m.start() - 12) : m.start()])
            and _NEGATIVE_STATUS_ZH.match(text[m.end() :])
        )
        for m in _APPROVAL_CLAIM_ZH.finditer(text)
    )


_BUDGET_MESSAGE = (
    "抱歉，本次对话今天的额度已用完。你可以新建一个对话，"
    "或联系人工客服继续处理。"
)

# Shown when nobody has configured a model service yet (fresh install).
_NO_MODEL_MESSAGE = (
    "系统还没有配置模型服务，暂时无法回答。"
    "请管理员在「模型设置」里填写服务地址与 API Key 后重试。"
)

_CORRECTION_PREFIX = (
    "_系统提示：本轮未执行退款，订单状态不会因聊天中的回复而改变。_\n\n"
)


class ConversationNotFound(Exception):
    pass


def new_conversation_id() -> str:
    return f"conv-{uuid.uuid4().hex}"


async def create_conversation(customer_id: str, cid: str | None = None) -> str:
    cid = cid or new_conversation_id()
    async with db.SessionLocal() as session:
        session.add(Conversation(id=cid, customer_id=customer_id))
        await session.commit()
    return cid


async def load_owned_conversation(customer_id: str, conversation_id: str) -> Conversation:
    """Return the conversation if it belongs to `customer_id`, else raise.

    Unknown and foreign conversations are indistinguishable to the caller, so
    ids can't be probed.
    """
    async with db.SessionLocal() as session:
        convo = await session.get(Conversation, conversation_id)
    if convo is None or convo.customer_id != customer_id:
        raise ConversationNotFound(conversation_id)
    return convo


async def _history(conversation_id: str) -> list:
    async with db.SessionLocal() as session:
        rows = (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.desc())
                .limit(_HISTORY_LIMIT)
            )
        ).all()
    return [
        HumanMessage(m.content) if m.role == "user" else AIMessage(m.content)
        for m in reversed(rows)
    ]


def _system_message() -> SystemMessage:
    return SystemMessage(get_system_prompt())


def _step_type_for_tool(name: str) -> str:
    if name == "check_refund_eligibility":
        return "policy_eval"
    if name in _DECISION_TOOLS:
        return "proposal"
    return "tool_result"


async def _save_message(cid: str, role: str, content: str) -> int:
    async with db.SessionLocal() as session:
        msg = Message(conversation_id=cid, role=role, content=content)
        session.add(msg)
        await session.commit()
        return msg.id


def display_amounts(text: str) -> str:
    """The local ledger uses yuan; normalize model formatting without conversion."""
    return re.sub(r"[$¥￥]\s*(\d[\d,]*(?:\.\d+)?)", r"\1 元", text)


async def run_agent_turn(
    customer_id: str, conversation_id: str | None, user_text: str, *, selected_order_id: str | None = None,
) -> AsyncIterator[dict]:
    settings = get_settings()
    started = time.perf_counter()
    if conversation_id is None:
        conversation_id = await create_conversation(customer_id)
    convo = await load_owned_conversation(customer_id, conversation_id)
    cid = convo.id
    from app.refund_confirmation import invalidate_pending
    await invalidate_pending(customer_id, cid)
    conversation_id_var.set(cid)

    # Persist the user's message first so it survives reloads.
    user_msg_id = await _save_message(cid, "user", user_text)
    # Mirror it to live admin subscribers (not yielded to the chat stream, which
    # already renders the user's own message locally).
    await broadcaster.publish(cid, {"kind": "message", "role": "user", "content": user_text})

    async def emit(step_type: str, node: str, payload: dict, tokens: int = 0) -> dict:
        # Two Core statements in one short transaction (no ORM unit-of-work:
        # this runs for every reasoning step, so it's on the hot path).
        async with db.SessionLocal() as session:
            # Atomic per-conversation sequence number (safe across pods).
            values = {"event_seq": Conversation.event_seq + 1}
            if tokens:
                values["tokens_used"] = Conversation.tokens_used + tokens
            seq = await session.scalar(
                update(Conversation)
                .where(Conversation.id == cid)
                .values(**values)
                .returning(Conversation.event_seq)
            )
            created = utcnow()
            row_id = await session.scalar(
                insert(ReasoningEvent)
                .values(
                    conversation_id=cid,
                    message_id=user_msg_id,
                    seq=seq,
                    step_type=step_type,
                    node=node,
                    payload=payload,
                    created_at=created,
                )
                .returning(ReasoningEvent.id)
            )
            await session.commit()
        event = {
            "kind": "step",
            "id": row_id,
            "seq": seq,
            "step_type": step_type,
            "node": node,
            "payload": payload,
            "created_at": created.isoformat(),
        }
        await broadcaster.publish(cid, event)
        return event

    async def finish(final_text: str, outcome: str) -> list[dict]:
        final_text = display_amounts(final_text)
        mid = await _save_message(cid, "assistant", final_text)
        final_event = {"kind": "message", "role": "assistant", "content": final_text, "message_id": mid}
        await broadcaster.publish(cid, final_event)
        TURNS.labels(outcome).inc()
        TURN_LATENCY.observe(time.perf_counter() - started)
        return [final_event, {"kind": "done"}]

    yield {"kind": "conversation", "conversation_id": cid}

    # Surface (but do not rely on) suspected manipulation.
    flags = detect_injection(user_text)
    if flags:
        INJECTION_FLAGS.inc()
        yield await emit("injection_flag", "guard", {"patterns": flags, "text": user_text})

    # Token budgets. Checked before any LLM call.
    prior_tokens = convo.tokens_used or 0
    today_tokens = await shared.customer_tokens_today(customer_id)
    over_budget = (
        prior_tokens >= settings.max_conversation_tokens
        or today_tokens >= settings.customer_daily_token_budget
    )
    if settings.is_demo:
        # The public demo has a demo-wide daily cap on top of the per-customer
        # one; reaching it refuses the turn (no cheaper local model exists).
        global_cap = settings.demo_global_daily_token_budget
        over_budget = over_budget or bool(
            global_cap and await shared.global_tokens_today() >= global_cap
        )
    if over_budget:
        yield await emit(
            "budget_exhausted",
            "guard",
            {
                "prior_tokens": prior_tokens,
                "limit": settings.max_conversation_tokens,
                "customer_tokens_today": today_tokens,
                "customer_daily_limit": settings.customer_daily_token_budget,
            },
        )
        for event in await finish(_BUDGET_MESSAGE, "budget_exhausted"):
            yield event
        return

    # The model service is picked in the settings page; a fresh checkout has
    # none until an admin saves one, so fail actionably instead of crashing.
    try:
        cfg = await model_config.effective_settings()
        agent = get_agent(cfg)
    except ModelNotConfigured:
        yield await emit("error", "agent", {"error_type": "model_not_configured"})
        for event in await finish(_NO_MODEL_MESSAGE, "no_model"):
            yield event
        return

    ctx = ToolContext(conversation_id=cid, verified_customer_id=customer_id)
    tracer = tracing.tracer_for_turn(cid, customer_id, model_name=cfg.model_name)
    run_config = {
        "recursion_limit": settings.agent_recursion_limit,
        "run_name": "refund-turn",
        **tool_config(ctx),
    }
    if tracer is not None:
        run_config["callbacks"] = [tracer]

    # History already includes the user message we just saved.
    messages = [_system_message()] + await _history(cid)
    if selected_order_id:
        from app.db.models import Order
        async with db.SessionLocal() as session:
            owned = await session.scalar(select(Order.id).where(
                Order.id == selected_order_id, Order.customer_id == customer_id,
            ))
        if owned:
            messages[0] = SystemMessage(str(messages[0].content)
                + "\n\n## 当前界面上下文\n客户已选中的订单号（仅为定位数据，不是退款授权）：" + json.dumps(owned)
                + "。客户说‘这笔订单’即指该订单，直接用 get_order 查询，无须重复询问订单号。仍须校验并通过确认卡片授权。")

    final_text = ""
    decision_in_turn: str | None = None
    prepared_proposal: dict | None = None
    TURNS_IN_FLIGHT.inc()
    try:
        async with asyncio.timeout(settings.turn_timeout_s):
            async for chunk in agent.astream(
                {"messages": messages},
                stream_mode="updates",
                config=run_config,
            ):
                for node, update_ in chunk.items():
                    for msg in update_.get("messages", []):
                        if isinstance(msg, AIMessage):
                            usage = getattr(msg, "usage_metadata", None)
                            meta = getattr(msg, "response_metadata", None) or {}
                            model = meta.get("model") or meta.get("model_name") or ""
                            if isinstance(msg.content, str) and msg.content.strip():
                                yield await emit("model", node, {"text": msg.content})
                                final_text = msg.content
                            elif isinstance(msg.content, list):
                                text = "".join(
                                    b.get("text", "")
                                    for b in msg.content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                                if text.strip():
                                    yield await emit("model", node, {"text": text})
                                    final_text = text
                            for call in msg.tool_calls or []:
                                TOOL_CALLS.labels(call["name"]).inc()
                                yield await emit(
                                    "tool_call",
                                    node,
                                    {"tool": call["name"], "args": call.get("args", {})},
                                )
                            # Record token usage for budget tracking.
                            if usage:
                                tin = int(usage.get("input_tokens", 0))
                                tout = int(usage.get("output_tokens", 0))
                                LLM_TOKENS.labels("input").inc(tin)
                                LLM_TOKENS.labels("output").inc(tout)
                                await shared.add_customer_tokens(customer_id, tin + tout)
                                yield await emit(
                                    "usage",
                                    node,
                                    {"input_tokens": tin, "output_tokens": tout, "model": model},
                                    tokens=tin + tout,
                                )
                        elif isinstance(msg, ToolMessage):
                            try:
                                result = json.loads(msg.content)
                            except (json.JSONDecodeError, TypeError):
                                result = {"raw": str(msg.content)}
                            # Indirect injection: a tool returned stored text that
                            # read like instructions (the tool already replaced it).
                            if UNTRUSTED_PLACEHOLDER in str(msg.content):
                                flags.append(f"tool:{msg.name}")
                                INJECTION_FLAGS.inc()
                                yield await emit(
                                    "injection_flag",
                                    "guard",
                                    {
                                        "patterns": ["instructions_in_tool_output"],
                                        "source": "tool",
                                        "tool": msg.name,
                                    },
                                )
                            # A prepared proposal is a trace outcome, not execution.
                            if msg.name in _DECISION_TOOLS and isinstance(result, dict):
                                decision = result.get("decision")
                                if decision:
                                    decision_in_turn = decision
                            yield await emit(
                                _step_type_for_tool(msg.name),
                                node,
                                {"tool": msg.name, "result": result},
                            )
                            if msg.name == "prepare_refund" and isinstance(result, dict) and result.get("proposal"):
                                prepared_proposal = result["proposal"]
                                event = {"kind": "refund_proposal", "proposal": result["proposal"]}
                                await broadcaster.publish(cid, event)
                                yield event
    except asyncio.CancelledError:
        await invalidate_pending(customer_id, cid)
        raise
    except Exception as exc:  # model/provider errors, timeouts, recursion limit
        await invalidate_pending(customer_id, cid)
        # Full detail goes to logs; the client gets a generic message.
        log_event(
            logger,
            "agent turn failed",
            level=logging.ERROR,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
        TURNS.labels("error").inc()
        TURN_LATENCY.observe(time.perf_counter() - started)
        yield await emit("error", "agent", {"error_type": type(exc).__name__})
        trace = tracing.finish_turn(
            tracer,
            tracing.TurnOutcome(
                decision=decision_in_turn,
                injection_flagged=bool(flags),
                error=True,
            ),
        )
        if trace:
            yield await emit("trace", "opik", trace)
        yield {"kind": "error", "message": _ERROR_MESSAGE}
        return
    finally:
        TURNS_IN_FLIGHT.dec()

    if not final_text:
        final_text = "抱歉，这个请求我没能处理完成。"

    # Output sanitizer: don't let the assistant *claim* an approval that the
    # refund system did not actually record.
    corrected = claims_approval(final_text)
    if corrected:
        yield await emit(
            "output_correction",
            "sanitizer",
            {
                "reason": "助手宣称退款已批准，但本轮模型只有准备申请的权限，未执行退款。",
                "original": final_text,
            },
        )
        final_text = _CORRECTION_PREFIX + "退款需要先生成待确认申请，再由你点击确认卡片授权。本轮未执行退款。"
    elif decision_in_turn == "awaiting_confirmation" and prepared_proposal:
        # The card holds the authoritative details. Keep the accompanying text
        # clear even when a provider repeats internal ids, raw status or UTC dates.
        final_text = (
            "人工审核申请已准备好。请核对下方卡片，选择确认提交人工审核或取消申请；当前尚未提交工单，也未退款。"
            if prepared_proposal["action"] == "review"
            else "退款申请已准备好。请核对下方卡片中的订单、商品和金额，选择确认退款或取消申请；当前尚未退款。"
        )

    trace = tracing.finish_turn(
        tracer,
        tracing.TurnOutcome(
            decision=decision_in_turn,
            corrected=corrected,
            injection_flagged=bool(flags),
        ),
    )
    if trace:
        yield await emit("trace", "opik", trace)

    for event in await finish(final_text, "ok"):
        yield event
