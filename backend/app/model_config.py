"""The model service the agent runs on: web-editable, OpenAI-compatible.

Why one protocol instead of a provider per SDK: every provider worth using
(official OpenAI, DeepSeek, GLM, Qwen, SiliconFlow, and any 中转站) exposes the
same OpenAI-compatible surface — `POST {base}/chat/completions` plus
`GET {base}/models`. Storing just (base_url, model_name, api_key) therefore
covers all of them, and the model list can be *fetched* instead of typed by
hand.

What this module owns:

- encryption at rest for the API key (Fernet, key material from
  `CONFIG_SECRET_KEY`; the API never returns the key, only `has_api_key`)
- accepting any http(s) address, including local ones: the usual target is a
  model running on the same machine or network (Ollama, LM Studio, a model
  container), and only an unscoped admin can change it
- reading the effective config (DB row, else env bootstrap, else "not
  configured") and versioning it, so a saved change takes effect on the next
  turn without a restart
- helpers the settings page calls: list models, probe a real tool-calling
  round trip, save, and preset management

The LangChain client itself is built in `app/agent/llm.py`, keyed on
`ModelSettings.version`.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.config import get_settings
from app.db import session as db
from app.db.models import ModelConfig, ModelConfigPreset, utcnow


# A configuration that cannot be used, with a message meant for the admin UI.
class ModelConfigError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ModelSettings:
    """The resolved endpoint a turn runs on."""

    base_url: str
    model_name: str
    api_key: str
    timeout_s: float
    version: int
    source: str  # "db" | "env" | "none"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model_name and self.api_key)

    @property
    def normalized_base_url(self) -> str:
        return normalize_base_url(self.base_url)


NOT_CONFIGURED = "尚未配置模型服务。请到「模型设置」填写服务地址、密钥并拉取模型。"


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def _fernet() -> Fernet:
    secret = get_settings().effective_config_secret
    if not secret:
        raise ModelConfigError("服务端缺少 CONFIG_SECRET_KEY，无法保存密钥。", 500)
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(raw: str) -> str:
    return _fernet().encrypt(raw.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # CONFIG_SECRET_KEY changed since the key was stored: treat as missing
        # so the admin is asked to re-enter it instead of getting a 500.
        return ""


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------
#
# No address filtering: this deployment is self-hosted and the address can only
# be set by an unscoped admin, and the usual case is a *local* model server
# (Ollama on 127.0.0.1:11434, LM Studio, a model container on the compose
# network). Blocking private addresses would break exactly that. If this is ever
# offered as a shared multi-tenant service, add an allow-list of provider hosts.


def normalize_base_url(base_url: str) -> str:
    """Accept `https://host`, `https://host/`, `https://host/v1` alike.

    `/v1` is appended only when the address has no path, so providers that use a
    different prefix (`https://open.bigmodel.cn/api/paas/v4`) are left alone.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if not urlparse(base).path.strip("/"):
        base = f"{base}/v1"
    return base


def validate_base_url(base_url: str) -> str:
    """Check the address is a usable http(s) endpoint and return it normalized."""
    raw = (base_url or "").strip()
    if not raw:
        raise ModelConfigError("服务地址不能为空。")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ModelConfigError("服务地址必须以 http:// 或 https:// 开头。")
    if not parsed.hostname:
        raise ModelConfigError("服务地址缺少主机名。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelConfigError("服务地址不能包含用户名、密码、查询参数或片段。")
    return normalize_base_url(raw)


def resolve_api_key(base_url: str, new_key: str, current: ModelSettings) -> str:
    """Reuse a saved credential only for its original endpoint."""
    normalized = validate_base_url(base_url)
    if new_key.strip():
        return new_key.strip()
    if current.api_key and normalized != normalize_base_url(current.base_url):
        raise ModelConfigError("服务地址已更改，请填写该地址对应的 API Key。")
    return current.api_key


# ---------------------------------------------------------------------------
# Reading / writing the configuration
# ---------------------------------------------------------------------------


def _from_row(row: ModelConfig) -> ModelSettings:
    return ModelSettings(
        base_url=row.base_url,
        model_name=row.model_name,
        api_key=decrypt_secret(row.api_key_encrypted),
        timeout_s=float(row.timeout_s or 60),
        version=int(row.version or 1),
        source="db",
    )


async def get_row() -> ModelConfig | None:
    async with db.SessionLocal() as session:
        return await session.scalar(select(ModelConfig).where(ModelConfig.id == 1))


async def effective_settings() -> ModelSettings:
    """The endpoint a turn should use: saved row, else env bootstrap, else none."""
    row = await get_row()
    if row is not None:
        return _from_row(row)
    s = get_settings()
    if s.bootstrap_model_configured:
        return ModelSettings(
            base_url=s.model_base_url,
            model_name=s.model_name,
            api_key=s.model_api_key,
            timeout_s=s.model_timeout_s,
            version=0,
            source="env",
        )
    return ModelSettings("", "", "", s.model_timeout_s, 0, "none")


def public_config(settings: ModelSettings) -> dict:
    """Shape for the admin UI: never includes the key itself."""
    return {
        "base_url": settings.base_url,
        "model_name": settings.model_name,
        "timeout_s": settings.timeout_s,
        "version": settings.version,
        "source": settings.source,
        "configured": settings.configured,
        "has_api_key": bool(settings.api_key),
    }


async def save_config(
    base_url: str,
    model_name: str,
    api_key: str,
    timeout_s: int,
    updated_by: str | None = None,
) -> ModelSettings:
    """Upsert the row and bump its version (which invalidates client caches).

    An empty `api_key` keeps the stored one, so the UI can show a masked field
    and only send a key when the admin types a new one.
    """
    normalized = validate_base_url(base_url)
    model_name = (model_name or "").strip()
    if not model_name:
        raise ModelConfigError("请先选择模型。")
    if not 5 <= int(timeout_s) <= 600:
        raise ModelConfigError("超时时间需要在 5~600 秒之间。")

    fallback = await effective_settings()
    async with db.SessionLocal() as session:
        row = await session.scalar(select(ModelConfig).where(ModelConfig.id == 1))
        existing = _from_row(row) if row is not None else None
        key = resolve_api_key(normalized, api_key, existing or fallback)
        if not key:
            raise ModelConfigError("请填写 API Key。")

        if row is None:
            row = ModelConfig(id=1)
            session.add(row)
            row.base_url = normalized
            row.model_name = model_name
            row.api_key_encrypted = encrypt_secret(key)
            row.timeout_s = int(timeout_s)
            row.version = 1
        else:
            row.base_url = normalized
            row.model_name = model_name
            if api_key.strip():
                row.api_key_encrypted = encrypt_secret(key)
            row.timeout_s = int(timeout_s)
            row.version = int(row.version or 1) + 1
        row.updated_at = utcnow()
        row.updated_by = updated_by
        await session.commit()
        saved = _from_row(row)
    return saved


async def clear_config() -> None:
    async with db.SessionLocal() as session:
        row = await session.scalar(select(ModelConfig).where(ModelConfig.id == 1))
        if row is not None:
            await session.delete(row)
            await session.commit()


# ---------------------------------------------------------------------------
# Talking to the endpoint (settings page + probe)
# ---------------------------------------------------------------------------


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def list_models(base_url: str, api_key: str, timeout_s: float = 30) -> list[str]:
    """GET {base}/models — the dropdown the settings page fills from the server."""
    url = validate_base_url(base_url)
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, headers=_headers(api_key), follow_redirects=False
        ) as client:
            response = await client.get(f"{url}/models")
    except httpx.HTTPError as exc:
        raise ModelConfigError(
            f"无法连接模型服务（{type(exc).__name__}），请检查地址和网络。"
        ) from exc
    if response.status_code == 401:
        raise ModelConfigError("API Key 无效或已过期（HTTP 401）。")
    if response.status_code != 200:
        raise ModelConfigError(
            f"获取模型列表失败（HTTP {response.status_code}）：{response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError:
        raise ModelConfigError("模型列表返回的不是 JSON，确认地址是否是 OpenAI 兼容接口。") from None
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ModelConfigError("模型列表返回格式不正确（缺少 data 数组）。")
    models = []
    for item in items:
        identifier = item.get("id") if isinstance(item, dict) else item
        if isinstance(identifier, str) and identifier:
            models.append(identifier)
    return sorted(set(models))


async def probe(settings: ModelSettings) -> dict:
    """Send one real tool-calling round trip.

    The agent only works if the model calls tools, so "测试连接" checks exactly
    that instead of pinging the host: a model that answers but never calls
    tools would break every refund turn.
    """
    if not settings.configured:
        raise ModelConfigError(NOT_CONFIGURED, 409)
    payload = {
        "model": settings.model_name,
        "messages": [
            {"role": "system", "content": "你是一个测试探针，必须调用工具。"},
            {"role": "user", "content": "查询订单 ORD-1001 的状态。"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_order",
                    "description": "查询订单详情",
                    "parameters": {
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                        "required": ["order_id"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "max_tokens": 256,
    }
    started = utcnow()
    try:
        async with httpx.AsyncClient(
            timeout=settings.timeout_s, headers=_headers(settings.api_key)
        ) as client:
            response = await client.post(
                f"{settings.normalized_base_url}/chat/completions", json=payload
            )
    except httpx.HTTPError as exc:
        raise ModelConfigError(
            f"无法连接模型服务（{type(exc).__name__}），请检查地址和网络。", 502
        ) from exc
    elapsed_ms = round((utcnow() - started).total_seconds() * 1000)

    if response.status_code == 401:
        raise ModelConfigError("API Key 无效或已过期（HTTP 401）。")
    if response.status_code == 404:
        raise ModelConfigError(
            "接口返回 404：地址可能缺少服务商要求的路径前缀（如 /v1），或该服务不提供 chat/completions。"
        )
    if response.status_code == 429:
        raise ModelConfigError("模型服务限流（HTTP 429），请稍后重试。", 429)
    if response.status_code != 200:
        raise ModelConfigError(
            f"模型服务返回错误（HTTP {response.status_code}）：{response.text[:200]}", 502
        )

    try:
        message = response.json()["choices"][0]["message"]
    except (KeyError, IndexError, TypeError, ValueError):
        raise ModelConfigError("返回格式不是 OpenAI 兼容的 chat.completion。", 502) from None

    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        raise ModelConfigError(
            "这个模型没有调用工具（function calling），无法驱动本项目的智能体。"
            "请换一个支持工具调用的模型。",
            422,
        )
    name = (tool_calls[0].get("function") or {}).get("name", "")
    return {
        "ok": True,
        "detail": f"连接成功：{settings.model_name} 正确调用了 {name} 工具（{elapsed_ms} ms）。",
        "tool_called": name,
        "latency_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def _preset_out(row: ModelConfigPreset) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "base_url": row.base_url,
        "model_name": row.model_name,
        "timeout_s": row.timeout_s,
        "has_api_key": bool(row.api_key_encrypted),
        "updated_at": row.updated_at,
    }


async def list_presets() -> list[dict]:
    async with db.SessionLocal() as session:
        rows = (
            await session.scalars(
                select(ModelConfigPreset).order_by(ModelConfigPreset.updated_at.desc())
            )
        ).all()
    return [_preset_out(r) for r in rows]


async def save_preset(
    name: str,
    base_url: str,
    model_name: str,
    api_key: str,
    timeout_s: int,
    updated_by: str | None = None,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ModelConfigError("方案名称不能为空。")
    normalized = validate_base_url(base_url)
    if not (model_name or "").strip():
        raise ModelConfigError("请先选择模型。")
    if not 5 <= int(timeout_s) <= 600:
        raise ModelConfigError("超时时间需要在 5~600 秒之间。")
    current = await effective_settings()
    async with db.SessionLocal() as session:
        row = await session.scalar(select(ModelConfigPreset).where(ModelConfigPreset.name == name))
        if row is not None and not api_key.strip():
            current = ModelSettings(row.base_url, row.model_name, decrypt_secret(row.api_key_encrypted), row.timeout_s, 0, "db")
        key = resolve_api_key(normalized, api_key, current)
        if not key:
            raise ModelConfigError("请填写 API Key。")
        if row is None:
            row = ModelConfigPreset(name=name)
            session.add(row)
        row.base_url = normalized
        row.model_name = (model_name or "").strip()
        row.api_key_encrypted = encrypt_secret(key)
        row.timeout_s = int(timeout_s)
        row.updated_at = utcnow()
        row.updated_by = updated_by
        await session.commit()
        return _preset_out(row)


async def rename_preset(preset_id: int, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ModelConfigError("方案名称不能为空。")
    async with db.SessionLocal() as session:
        row = await session.get(ModelConfigPreset, preset_id)
        if row is None:
            raise ModelConfigError("方案不存在。", 404)
        row.name = name
        row.updated_at = utcnow()
        await session.commit()
        return _preset_out(row)


async def delete_preset(preset_id: int) -> None:
    async with db.SessionLocal() as session:
        row = await session.get(ModelConfigPreset, preset_id)
        if row is None:
            raise ModelConfigError("方案不存在。", 404)
        await session.delete(row)
        await session.commit()


async def apply_preset(preset_id: int, updated_by: str | None = None) -> ModelSettings:
    async with db.SessionLocal() as session:
        row = await session.get(ModelConfigPreset, preset_id)
        if row is None:
            raise ModelConfigError("方案不存在。", 404)
        base_url, model_name = row.base_url, row.model_name
        api_key = decrypt_secret(row.api_key_encrypted)
        timeout_s = row.timeout_s
    return await save_config(base_url, model_name, api_key, timeout_s, updated_by)
