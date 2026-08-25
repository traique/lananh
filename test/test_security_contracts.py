import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import orchestrator, provider_overrides
from core import config, crypto, database as db
from core.repositories import settings as settings_repository


def _valid_base_config(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 1)
    monkeypatch.setattr(config, "ROUTER9_API_KEY", "router9-key")
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://example")


def test_config_rejects_missing_encryption_key(monkeypatch):
    _valid_base_config(monkeypatch)
    monkeypatch.setattr(config, "SETTINGS_ENC_KEY", None)
    with pytest.raises(RuntimeError, match="SETTINGS_ENC_KEY"):
        config.validate()


def test_config_rejects_invalid_encryption_key(monkeypatch):
    _valid_base_config(monkeypatch)
    monkeypatch.setattr(config, "SETTINGS_ENC_KEY", "not-a-fernet-key")
    with pytest.raises(RuntimeError, match="không hợp lệ"):
        config.validate()


def test_crypto_refuses_plaintext_when_key_missing(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet", None)
    with pytest.raises(RuntimeError, match="từ chối lưu"):
        crypto.encrypt("secret")


def test_crypto_round_trip(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet", Fernet(Fernet.generate_key()))
    encrypted = crypto.encrypt("secret")
    assert encrypted.startswith("enc:")
    assert encrypted != "secret"
    assert crypto.decrypt(encrypted) == "secret"


@pytest.mark.asyncio
async def test_strict_search_fails_without_official_api_key(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", None)
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", None)
    monkeypatch.setattr(provider_overrides, "_api_key_cache", {})
    async def fake_get_setting(key):
        return None

    monkeypatch.setattr(db, "get_setting", fake_get_setting)
    with pytest.raises(orchestrator.RealSearchUnavailableError):
        await orchestrator._search_only_providers()


@pytest.mark.asyncio
async def test_strict_search_keeps_only_configured_official_providers(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", "key-1")
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", None)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "api2", "api1"])
    monkeypatch.setattr(provider_overrides, "_api_key_cache", {})
    async def fake_get_setting(key):
        return None

    monkeypatch.setattr(db, "get_setting", fake_get_setting)
    assert await orchestrator._search_only_providers() == ["api1"]


@pytest.mark.asyncio
async def test_ask_strict_search_forces_tool_and_directive(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", "key-1")
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", None)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "api1", "api2"])

    async def preferred_model():
        return None

    captured = {}

    async def fake_generate(idx, prompt, **kwargs):
        captured.update(idx=idx, prompt=prompt, kwargs=kwargs)
        return "ok"

    async def fake_chain(*, router9_call, api_call, providers_override, groq_call=None, openrouter_call=None):
        captured["providers"] = providers_override
        return await api_call(1)

    monkeypatch.setattr(orchestrator.router9_client, "get_preferred_model_name", preferred_model)
    monkeypatch.setattr(orchestrator.official_client, "generate", fake_generate)
    monkeypatch.setattr(orchestrator, "_run_provider_chain", fake_chain)

    result = await orchestrator.ask("giá vàng", require_real_search=True)
    assert result == "ok"
    assert captured["providers"] == ["api1"]
    assert captured["kwargs"]["enable_search"] is True
    assert "BẮT BUỘC dùng Google Search" in captured["prompt"]


@pytest.mark.asyncio
async def test_settings_repository_round_trip():
    class FakePool:
        def __init__(self):
            self.value = None

        async def fetchrow(self, query, key):
            return {"value": self.value} if self.value is not None else None

        async def execute(self, query, key, value):
            self.value = value

    pool = FakePool()
    await settings_repository.set(pool, "key", "encrypted-value")
    assert await settings_repository.get(pool, "key") == "encrypted-value"
