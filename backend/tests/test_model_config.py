"""Model settings: the OpenAI-compatible endpoint edited from the UI.

Covers the things that would hurt if they broke: the API key never leaves the
server, the stored key survives a save that only changes the model, local model
servers (loopback / LAN / compose network) are accepted, and a saved change bumps
the version that cached clients key on (so it takes effect without a restart).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import model_config
from app.auth import ROLE_ADMIN, mint_token
from app.config import get_settings
from app.db import session as db
from app.db.models import ModelConfig, ModelConfigPreset
from tests.conftest import admin_auth, auth

# A literal public IP: no DNS lookup needed, so the HTTP double stays offline.
PUBLIC = "http://8.8.8.8"


class _Response:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def stub_http(monkeypatch, *, payload=None, status: int = 200, text: str = "") -> list:
    """Replace httpx.AsyncClient; returns the recorded (method, url, body) calls."""
    calls: list[tuple] = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            calls.append(("GET", url, None))
            return _Response(status, payload, text)

        async def post(self, url, json=None):
            calls.append(("POST", url, json))
            return _Response(status, payload, text)

    monkeypatch.setattr(model_config.httpx, "AsyncClient", Client)
    return calls


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("https://api.deepseek.com", "https://api.deepseek.com/v1"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("https://proxy.example.com/v1/", "https://proxy.example.com/v1"),
        ("  https://a.example.com/  ", "https://a.example.com/v1"),
        # A path that is not /v1 is kept as typed (e.g. Zhipu's OpenAI-compatible
        # base is /api/paas/v4, and appending /v1 would 404).
        ("https://open.bigmodel.cn/api/paas/v4", "https://open.bigmodel.cn/api/paas/v4"),
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434/v1"),
    ],
)
def test_base_url_is_normalised(given, expected):
    assert model_config.normalize_base_url(given) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",  # Ollama on the same machine
        "http://localhost:1234/v1",  # LM Studio
        "http://192.168.1.10:8000/v1",  # another machine on the LAN
        "http://support-bot-mock-openai:7800/v1",  # a model container on the compose net
        "http://[::1]:8080/v1",
        "https://api.deepseek.com/v1",
    ],
)
def test_any_http_endpoint_is_accepted(url):
    # No address filtering: self-hosted models are normally local, and only an
    # unscoped admin can set this.
    assert model_config.validate_base_url(url)
    assert model_config.validate_base_url(url).startswith(("http://", "https://"))


@pytest.mark.parametrize("url", ["ftp://example.com/v1", "file:///etc/passwd", "example.com/v1", ""])
def test_non_http_urls_are_refused(url):
    with pytest.raises(model_config.ModelConfigError):
        model_config.validate_base_url(url)


async def test_local_model_server_can_be_configured_through_the_api(client, monkeypatch):
    calls = stub_http(monkeypatch, payload={"data": [{"id": "llama3.2"}]})
    res = await client.post(
        "/api/admin/model-config/models",
        json={"base_url": "http://127.0.0.1:11434/v1", "api_key": "k"},
        headers=admin_auth(),
    )
    assert res.status_code == 200
    assert res.json()["models"] == ["llama3.2"]
    assert calls[0][1] == "http://127.0.0.1:11434/v1/models"


# ---------------------------------------------------------------------------
# Listing models (the dropdown)
# ---------------------------------------------------------------------------


async def test_model_list_comes_from_the_endpoint(client, monkeypatch):
    stub_http(
        monkeypatch,
        payload={"object": "list", "data": [{"id": "glm-5.3"}, {"id": "deepseek-chat"}, {"id": "glm-5.3"}]},
    )
    res = await client.post(
        "/api/admin/model-config/models",
        json={"base_url": PUBLIC, "api_key": "sk-test"},
        headers=admin_auth(),
    )
    assert res.status_code == 200
    # Sorted and de-duplicated: it feeds a <select>.
    assert res.json()["models"] == ["deepseek-chat", "glm-5.3"]


async def test_bad_key_is_reported_in_chinese(client, monkeypatch):
    stub_http(monkeypatch, status=401, text="unauthorized")
    res = await client.post(
        "/api/admin/model-config/models",
        json={"base_url": PUBLIC, "api_key": "wrong"},
        headers=admin_auth(),
    )
    assert res.status_code == 400
    assert "API Key" in res.json()["detail"]


async def test_non_openai_response_is_reported(client, monkeypatch):
    stub_http(monkeypatch, payload={"unexpected": True})
    res = await client.post(
        "/api/admin/model-config/models",
        json={"base_url": PUBLIC, "api_key": "k"},
        headers=admin_auth(),
    )
    assert res.status_code == 400
    assert "格式" in res.json()["detail"]


# ---------------------------------------------------------------------------
# Saving: encryption, key handling, version bump
# ---------------------------------------------------------------------------


async def test_saving_encrypts_the_key_and_never_returns_it(client, engine):
    res = await client.put(
        "/api/admin/model-config",
        json={
            "base_url": PUBLIC,
            "model_name": "glm-5.3",
            "api_key": "test-api-key-placeholder",
            "timeout_s": 60,
        },
        headers=admin_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["has_api_key"] is True
    assert "test-api-key-placeholder" not in res.text
    assert body["base_url"] == f"{PUBLIC}/v1"
    assert body["version"] == 1

    async with db.SessionLocal() as session:
        row = await session.scalar(select(ModelConfig).where(ModelConfig.id == 1))
    assert row.api_key_encrypted != "test-api-key-placeholder"
    assert model_config.decrypt_secret(row.api_key_encrypted) == "test-api-key-placeholder"

    # And the read endpoint does not leak it either.
    got = await client.get("/api/admin/model-config", headers=admin_auth())
    assert "test-api-key-placeholder" not in got.text
    assert got.json()["has_api_key"] is True


async def test_blank_key_keeps_the_stored_one_and_bumps_the_version(client, engine):
    await client.put(
        "/api/admin/model-config",
        json={"base_url": PUBLIC, "model_name": "glm-5.3", "api_key": "sk-first"},
        headers=admin_auth(),
    )
    res = await client.put(
        "/api/admin/model-config",
        json={"base_url": PUBLIC, "model_name": "deepseek-chat", "api_key": ""},
        headers=admin_auth(),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["model_name"] == "deepseek-chat"
    assert body["version"] == 2  # cached clients key on this
    async with db.SessionLocal() as session:
        row = await session.scalar(select(ModelConfig).where(ModelConfig.id == 1))
    assert model_config.decrypt_secret(row.api_key_encrypted) == "sk-first"


async def test_first_save_requires_a_key_and_a_model(client, engine):
    missing_key = await client.put(
        "/api/admin/model-config",
        json={"base_url": PUBLIC, "model_name": "glm-5.3"},
        headers=admin_auth(),
    )
    assert missing_key.status_code == 400
    assert "API Key" in missing_key.json()["detail"]

    missing_model = await client.put(
        "/api/admin/model-config",
        json={"base_url": PUBLIC, "model_name": "", "api_key": "sk-1"},
        headers=admin_auth(),
    )
    assert missing_model.status_code == 400
    assert "模型" in missing_model.json()["detail"]


async def test_saved_config_drives_the_agent_and_the_header(client, engine, monkeypatch):
    await client.put(
        "/api/admin/model-config",
        json={"base_url": PUBLIC, "model_name": "glm-5.3", "api_key": "sk-1"},
        headers=admin_auth(),
    )
    # The DB row beats the env bootstrap the test suite pins.
    cfg = await model_config.effective_settings()
    assert (cfg.model_name, cfg.source, cfg.version) == ("glm-5.3", "db", 1)
    meta = (await client.get("/api/meta")).json()
    assert meta["model"] == "openai-compatible"
    assert meta["model_name"] == "glm-5.3"


async def test_deleting_the_config_falls_back_to_the_env_bootstrap(client, engine):
    await client.put(
        "/api/admin/model-config",
        json={"base_url": PUBLIC, "model_name": "glm-5.3", "api_key": "sk-1"},
        headers=admin_auth(),
    )
    assert (await client.delete("/api/admin/model-config", headers=admin_auth())).status_code == 204
    cfg = await model_config.effective_settings()
    assert cfg.source == "env" and cfg.model_name == "stub-model"


# ---------------------------------------------------------------------------
# Test connection (a real tool-calling round trip)
# ---------------------------------------------------------------------------


async def test_probe_accepts_a_model_that_calls_tools(client, engine, monkeypatch):
    await client.put(
        "/api/admin/model-config",
        json={"base_url": PUBLIC, "model_name": "glm-5.3", "api_key": "sk-1"},
        headers=admin_auth(),
    )
    calls = stub_http(
        monkeypatch,
        payload={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "get_order", "arguments": "{}"}}
                        ],
                    }
                }
            ]
        },
    )
    res = await client.post("/api/admin/model-config/test", headers=admin_auth())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["tool_called"] == "get_order"
    # It hit the endpoint configured, not the env default.
    assert calls[0][1] == f"{PUBLIC}/v1/chat/completions"
    assert calls[0][2]["tools"][0]["function"]["name"] == "get_order"


async def test_probe_explains_a_model_without_tool_calling(client, engine, monkeypatch):
    await client.put(
        "/api/admin/model-config",
        json={"base_url": PUBLIC, "model_name": "chat-only", "api_key": "sk-1"},
        headers=admin_auth(),
    )
    stub_http(
        monkeypatch,
        payload={"choices": [{"message": {"role": "assistant", "content": "ORD-1001 已发货"}}]},
    )
    res = await client.post("/api/admin/model-config/test", headers=admin_auth())
    assert res.status_code == 422
    assert "工具调用" in res.json()["detail"]


async def test_probe_without_a_configuration_says_so(client, engine, monkeypatch):
    for name in ("model_base_url", "model_api_key", "model_name"):
        monkeypatch.setattr(get_settings(), name, "")
    res = await client.post("/api/admin/model-config/test", headers=admin_auth())
    assert res.status_code == 409
    assert "模型设置" in res.json()["detail"]


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


async def test_presets_are_saved_applied_and_deleted(client, engine):
    created = await client.post(
        "/api/admin/model-config/presets",
        json={
            "name": "中转站",
            "base_url": PUBLIC,
            "model_name": "glm-5.3",
            "api_key": "sk-preset",
        },
        headers=admin_auth(),
    )
    assert created.status_code == 200, created.text
    preset = created.json()
    assert preset["has_api_key"] is True and "sk-preset" not in created.text

    applied = await client.post(
        f"/api/admin/model-config/presets/{preset['id']}/apply", headers=admin_auth()
    )
    assert applied.status_code == 200
    assert applied.json()["model_name"] == "glm-5.3"
    assert (await model_config.effective_settings()).api_key == "sk-preset"

    renamed = await client.patch(
        f"/api/admin/model-config/presets/{preset['id']}",
        json={"name": "中转站-A"},
        headers=admin_auth(),
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "中转站-A"

    listed = await client.get("/api/admin/model-config/presets", headers=admin_auth())
    assert [p["name"] for p in listed.json()] == ["中转站-A"]

    assert (
        await client.delete(
            f"/api/admin/model-config/presets/{preset['id']}", headers=admin_auth()
        )
    ).status_code == 204
    async with db.SessionLocal() as session:
        assert (await session.scalars(select(ModelConfigPreset))).all() == []


# ---------------------------------------------------------------------------
# Authorization: this sets the model for every customer
# ---------------------------------------------------------------------------


async def test_customers_cannot_change_the_model(client, engine):
    res = await client.put(
        "/api/admin/model-config",
        json={"base_url": PUBLIC, "model_name": "glm-5.3", "api_key": "sk-1"},
        headers=auth("CUST-001"),
    )
    assert res.status_code == 403


async def test_scoped_admin_cannot_change_the_model(client, engine):
    scoped = {"Authorization": f"Bearer {mint_token('admin-9', ROLE_ADMIN, scope='CUST-001')}"}
    res = await client.put(
        "/api/admin/model-config",
        json={"base_url": PUBLIC, "model_name": "glm-5.3", "api_key": "sk-1"},
        headers=scoped,
    )
    assert res.status_code == 403
    assert (await client.get("/api/admin/model-config", headers=scoped)).status_code == 403
