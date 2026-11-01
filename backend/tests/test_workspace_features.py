"""Workspace contracts: visible policy, local amounts, safe endpoint switching."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app import model_config
from app.agent.prompts import get_system_prompt, prompt_version
from app.config import get_settings
from app.hitl import customer_message
from tests.conftest import admin_auth


async def test_meta_and_prompt_use_the_current_policy(client, engine, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "return_window_days", 14)
    monkeypatch.setattr(settings, "escalation_threshold", 800.5)
    response = await client.get("/api/meta")
    assert response.json()["refund_policy"] == {
        "return_window_days": 14,
        "escalation_threshold": 800.5,
    }
    get_system_prompt.cache_clear()
    prompt_version.cache_clear()
    try:
        prompt = get_system_prompt()
        assert "14 天" in prompt and "800.5 元" in prompt
        assert "$500" not in prompt and "Example Shop" not in prompt
    finally:
        get_system_prompt.cache_clear()
        prompt_version.cache_clear()


def test_human_result_uses_yuan():
    text = customer_message("ORD-1003", 1299, "approved", "已核实")
    assert "1299.00 元" in text and "$" not in text


async def test_changing_endpoint_never_forwards_the_saved_key(client, engine, monkeypatch):
    await model_config.save_config("https://first.invalid", "model-a", "secret-a", 60)
    calls = []

    async def record(*args):
        calls.append(args)
        return ["model-b"]

    monkeypatch.setattr(model_config, "list_models", record)
    response = await client.post(
        "/api/admin/model-config/models",
        headers=admin_auth(),
        json={"base_url": "https://second.invalid", "api_key": ""},
    )
    assert response.status_code == 400
    assert "重新" in response.json()["detail"] or "请填写" in response.json()["detail"]
    assert calls == []
    assert "secret-a" not in response.text

    response = await client.put(
        "/api/admin/model-config",
        headers=admin_auth(),
        json={"base_url": "https://second.invalid", "model_name": "model-b", "api_key": ""},
    )
    assert response.status_code == 400
    assert (await model_config.effective_settings()).base_url == "https://first.invalid/v1"

    response = await client.post(
        "/api/admin/model-config/models",
        headers=admin_auth(),
        json={"base_url": "https://second.invalid", "api_key": "new-key"},
    )
    assert response.status_code == 200
    assert calls == [("https://second.invalid", "new-key")]


async def test_new_preset_inherits_key_only_for_the_same_endpoint(client, engine):
    await model_config.save_config("https://first.invalid", "model-a", "secret-a", 60)
    preset = await model_config.save_preset(
        "本地方案", "https://first.invalid/v1/", "model-b", "", 60
    )
    assert preset["has_api_key"] is True
    applied = await model_config.apply_preset(preset["id"])
    assert applied.api_key == "secret-a"
    with pytest.raises(model_config.ModelConfigError):
        await model_config.save_preset("错误方案", "https://second.invalid", "model-b", "", 60)


@pytest.mark.parametrize(
    "url",
    ["https://user:pass@a.invalid", "https://a.invalid?token=secret", "https://a.invalid/#key"],
)
def test_model_address_cannot_embed_credentials_or_parameters(url):
    with pytest.raises(model_config.ModelConfigError):
        model_config.validate_base_url(url)


def test_catalog_migration_preserves_user_edits_and_supports_rollback(tmp_path):
    migration = (
        Path(__file__).resolve().parents[1] / "alembic/versions/0005_localize_sample_catalog.py"
    )
    spec = importlib.util.spec_from_file_location("local_catalog", migration)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE customers (id TEXT, name TEXT, email TEXT)"))
        connection.execute(sa.text("CREATE TABLE orders (id TEXT, product_name TEXT)"))
        connection.execute(
            sa.text(
                "INSERT INTO customers VALUES ('CUST-001', 'Alice Johnson', 'alice@example.com'), ('CUST-002', '我的客户', 'local@example.test')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO orders VALUES ('ORD-1001', 'Wireless Headphones'), ('ORD-1002', '我的商品')"
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            assert (
                connection.execute(
                    sa.text("SELECT name FROM customers WHERE id='CUST-001'")
                ).scalar_one()
                == "林晓"
            )
            assert (
                connection.execute(
                    sa.text("SELECT name FROM customers WHERE id='CUST-002'")
                ).scalar_one()
                == "我的客户"
            )
            assert (
                connection.execute(
                    sa.text("SELECT product_name FROM orders WHERE id='ORD-1002'")
                ).scalar_one()
                == "我的商品"
            )
            module.downgrade()
            assert (
                connection.execute(
                    sa.text("SELECT name FROM customers WHERE id='CUST-001'")
                ).scalar_one()
                == "Alice Johnson"
            )
    engine.dispose()
