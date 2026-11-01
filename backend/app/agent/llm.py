"""Chat model factory: one OpenAI-compatible endpoint, chosen at runtime.

The endpoint (base URL + model + key) comes from `app.model_config`, which the
admin edits in the settings page — not from a provider enum here. That is why
there is a single builder: official APIs, DeepSeek/Qwen/GLM and 中转站 all speak
the same `chat/completions` protocol.

Caching: clients are cached per (url, model, key, timeout, **version**). Saving
a new configuration bumps `version`, so the next call builds a fresh client
(and `graph.get_agent` rebuilds the compiled graph, because it keys on the
client object). No restart, no cross-process broadcast.
"""

from __future__ import annotations

from functools import lru_cache

import httpx
from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.model_config import NOT_CONFIGURED, ModelSettings


class ModelNotConfigured(RuntimeError):
    """No usable model endpoint: the admin has not configured one yet."""


@lru_cache(maxsize=8)
def _build(
    base_url: str, model_name: str, api_key: str, timeout_s: float, version: int
) -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=model_name,
        # OpenAI-compatible: every path below already ends in /v1.
        base_url=base_url,
        api_key=api_key,
        max_tokens=settings.max_output_tokens,
        timeout=httpx.Timeout(timeout_s, connect=settings.llm_connect_timeout_s),
        max_retries=settings.llm_max_retries,
    )


def get_chat_model(cfg: ModelSettings) -> ChatOpenAI:
    if not cfg.configured:
        raise ModelNotConfigured(NOT_CONFIGURED)
    return _build(
        cfg.normalized_base_url, cfg.model_name, cfg.api_key, cfg.timeout_s, cfg.version
    )


def reset_model_cache() -> None:
    """Drop cached clients (tests, and callers that just saved a configuration)."""
    _build.cache_clear()
