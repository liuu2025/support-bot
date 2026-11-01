"""Test doubles for the model service.

Nothing in `app/` fakes a model any more: the project runs on a real
OpenAI-compatible endpoint. Tests must not depend on the network or spend
tokens, so the double lives here — in the test tree, never shipped in the image
— and is installed by patching the factory the agent calls
(`app.agent.graph.get_chat_model`).

Two doubles:

- `ScriptedChatModel` replays a fixed list of `AIMessage`s. Use it when the test
  is about what the *agent* does with a specific model output (jailbroken
  tool calls, malformed replies, ...).
- `HeuristicChatModel` reads the user's message: it calls `prepare_refund` when a
  proposal is requested for an order it can see, and answers in prose otherwise. Use
  it for flow tests (demo sandboxes, archiving) that only need a plausible
  assistant.
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage

STUB_BASE_URL = "https://stub.invalid/v1"
STUB_API_KEY = "test-key"
STUB_MODEL = "stub-model"

_ORDER_ID = re.compile(r"\b[A-Z]{2,}-\d[\w-]*\b")
_REFUND_WORDS = ("refund", "退款", "退钱", "return the", "return it")


class ScriptedChatModel:
    """Replays a list of AIMessages; implements what the graph uses."""

    def __init__(self, messages: list[AIMessage] | None = None):
        self.messages = messages or [AIMessage(content="好的。")]
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        message = self.messages[min(self.calls, len(self.messages) - 1)]
        self.calls += 1
        return message


class HeuristicChatModel:
    """A plausible assistant driven by the user's message.

    Prepares every order id mentioned in a refund request (the deterministic gate
    decides the outcome), and answers in prose otherwise — including after the
    tool results come back, so the loop terminates.
    """

    def __init__(self, reply: str = "好的，我帮你查一下。"):
        self.reply = reply
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        last_human = max(
            (i for i, m in enumerate(messages) if getattr(m, "type", "") == "human"),
            default=-1,
        )
        if last_human < 0:
            return AIMessage(content=self.reply)
        tool_names = [
            getattr(m, "name", "") for m in messages[last_human + 1 :] if getattr(m, "type", "") == "tool"
        ]
        text = str(messages[last_human].content)
        lowered = text.lower()
        order_ids = (
            list(dict.fromkeys(_ORDER_ID.findall(text)))
            if any(word in lowered for word in _REFUND_WORDS)
            else []
        )
        if order_ids and not tool_names:
            # Look the order up first: the metric requires the check → act order.
            return _calls(self.calls, "check_refund_eligibility", order_ids)
        if order_ids and "prepare_refund" not in tool_names:
            # Only refund what the policy check cleared — a good agent stops for
            # an ineligible order instead of hammering the decision tool.
            cleared = [
                order_id
                for order_id in order_ids
                if _verdict(messages, order_id) in ("approved", "escalated")
            ]
            if cleared:
                return _calls(self.calls, "prepare_refund", cleared)
        return AIMessage(content=self.reply)


def _verdict(messages: list, order_id: str) -> str:
    """The decision `check_refund_eligibility` returned for `order_id`, if any."""
    for message in reversed(messages):
        if getattr(message, "type", "") != "tool":
            continue
        try:
            payload = json.loads(str(message.content))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("order_id") == order_id:
            return str(payload.get("decision") or "")
    return ""


def _calls(round_: int, tool: str, order_ids: list[str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": tool,
                "args": {"order_id": order_id},
                "id": f"call-{round_}-{index}",
                "type": "tool_call",
            }
            for index, order_id in enumerate(order_ids)
        ],
    )


def tool_call(name: str, args: dict, call_id: str = "call-1") -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def install(monkeypatch, model=None) -> object:
    """Point the agent at `model` (default: the heuristic double).

    Also pins a stub endpoint in the settings, because `runner` reads the model
    configuration before it asks for a client — without one the turn would be
    refused as "not configured".
    """
    from app.agent import graph as graph_module
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_base_url", STUB_BASE_URL)
    monkeypatch.setattr(settings, "model_api_key", STUB_API_KEY)
    monkeypatch.setattr(settings, "model_name", STUB_MODEL)
    chosen = model if model is not None else HeuristicChatModel()
    monkeypatch.setattr(graph_module, "get_chat_model", lambda cfg: chosen)
    graph_module.clear_graph_cache()
    return chosen
