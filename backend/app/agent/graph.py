"""The agent graph: a two-node ReAct loop (agent ⇄ tools).

One compiled graph per model configuration per process. Compiling per request
was a major CPU cost under load, so the compiled graph is cached and only
rebuilt when the model client changes (i.e. when someone saves a new model
configuration, which bumps its version).

Per-request state (the verified customer) is passed at invoke time through
`config["configurable"]["tool_ctx"]` — see `app.agent.tools.tool_config`.

There is no fallback provider: the project runs on exactly one OpenAI-compatible
endpoint. If that endpoint errors, the turn fails and the caller reports it —
the deterministic refund gate means a failed turn can never pay out money.
"""

from __future__ import annotations

import logging
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agent.llm import get_chat_model
from app.agent.tools import TOOLS
from app.model_config import ModelSettings
from app.observability import log_event

logger = logging.getLogger(__name__)


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


# One compiled graph per configuration signature, per process.
_cache: dict[tuple, object] = {}


def get_agent(cfg: ModelSettings):
    """Return the compiled agent graph for this model configuration."""
    model = get_chat_model(cfg)
    key = (cfg.normalized_base_url, cfg.model_name, cfg.version)
    cached = _cache.get(key)
    if cached is not None and cached[0] is model:
        return cached[1]
    agent = _compile(model)
    _cache[key] = (model, agent)
    log_event(
        logger,
        "compiled agent graph",
        model=cfg.model_name,
        base_url=cfg.normalized_base_url,
        version=cfg.version,
        cached_graphs=len(_cache),
    )
    return agent


def clear_graph_cache() -> None:
    _cache.clear()


def _compile(model):
    tools = TOOLS
    bound = model.bind_tools(tools)
    tool_node = ToolNode(tools)

    async def agent_node(state: AgentState) -> dict:
        return {"messages": [await bound.ainvoke(state["messages"])]}

    def should_continue(state: AgentState):
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()
