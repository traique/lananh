import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import openai_compatible, router9_client
from core import config, database as db


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://router.example.com", "https://router.example.com"),
        ("https://router.example.com/", "https://router.example.com"),
        ("https://router.example.com/v1/", "https://router.example.com/v1"),
        ("http://localhost:8080/v1", "http://localhost:8080/v1"),
    ],
)
def test_normalize_router9_base_url_accepts_valid_urls(raw, expected):
    assert router9_client.normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "router.example.com/v1",
        "ftp://router.example.com/v1",
        "https://",
        "https://user:pass@router.example.com/v1",
        "https://router.example.com/v1?token=x",
        "https://router.example.com/v1#frag",
        "https://router.example.com:bad/v1",
        "https://router.example.com/a b",
        "https://router.example.com/v1/chat/completions",
        "https://router.example.com/v1/models",
    ],
)
def test_normalize_router9_base_url_rejects_invalid_urls(raw):
    assert router9_client.normalize_base_url(raw) is None


@pytest.mark.asyncio
async def test_invalid_override_is_cleared_and_falls_back_to_render_env(monkeypatch):
    settings = {router9_client._SETTING_BASE_URL: "https://old.example/v1"}

    async def fake_get_setting(key):
        return settings.get(key)

    async def fake_set_setting(key, value):
        settings[key] = value

    monkeypatch.setattr(db, "get_setting", fake_get_setting)
    monkeypatch.setattr(db, "set_setting", fake_set_setting)
    monkeypatch.setattr(config, "ROUTER9_BASE_URL", "https://render.example/v1")

    valid = await router9_client.set_base_url_override("not-a-url")

    assert valid is False
    assert settings[router9_client._SETTING_BASE_URL] == ""
    assert await router9_client.get_base_url() == "https://render.example/v1"


@pytest.mark.asyncio
async def test_generate_uses_router9_base_url_override(monkeypatch):
    captured = {}

    async def fake_api_key():
        return "key"

    async def fake_base_url():
        return "https://custom.example/v1"

    async def fake_post(client, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(router9_client, "_api_key", fake_api_key)
    monkeypatch.setattr(router9_client, "get_base_url", fake_base_url)
    monkeypatch.setattr(openai_compatible, "post_chat_completion", fake_post)

    result = await router9_client.generate("hello", model="model-x")

    assert result.text == "ok"
    assert captured["base_url"] == "https://custom.example/v1"
