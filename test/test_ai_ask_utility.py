"""ask() nhận max_tokens/temperature/json_mode (mặc định giữ nguyên hành vi cũ) và
ask_utility() cho việc phụ đầu ra ngắn: chuỗi Groq -> Gemini, max_tokens nhỏ, giai
đoạn "utility" (không đổi active_provider)."""

from types import SimpleNamespace

import pytest

from ai import groq_client, openrouter_client, orchestrator, router9_client
from ai.provider_state import provider_state
from core import config, database as db


@pytest.fixture
def providers(monkeypatch):
    store: dict[str, str] = {}

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    async def no_record(*args, **kwargs):
        return None

    async def preferred():
        return None

    async def api_key_for(idx):
        return "k"

    log = SimpleNamespace(calls=[], fail=set())

    def fake(name):
        async def _call(prompt, **kwargs):
            log.calls.append((name, kwargs))
            if name in log.fail:
                raise RuntimeError(f"{name} lỗi")
            return SimpleNamespace(text=f"{name}-ok")

        return _call

    async def official(idx, prompt, **kwargs):
        log.calls.append((f"api{idx}", kwargs))
        if f"api{idx}" in log.fail:
            raise RuntimeError(f"api{idx} lỗi")
        return SimpleNamespace(text=f"api{idx}-ok")

    from ai import provider_overrides

    monkeypatch.setattr(provider_overrides, "_api_key_cache", {})
    monkeypatch.setattr(provider_overrides, "_enabled_cache", {})
    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(orchestrator, "_record_provider_call", no_record)
    monkeypatch.setattr(router9_client, "get_preferred_model_name", preferred)
    monkeypatch.setattr(router9_client, "generate", fake("router9"))
    monkeypatch.setattr(groq_client, "generate", fake("groq"))
    monkeypatch.setattr(openrouter_client, "generate", fake("openrouter"))
    monkeypatch.setattr(orchestrator.official_client, "generate", official)
    monkeypatch.setattr(orchestrator.official_client, "api_key_for", api_key_for)
    monkeypatch.setattr(config, "GROQ_API_KEY", "g")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "o")
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "groq", "openrouter", "api1", "api2"])
    provider_state.active_provider = "router9"
    provider_state.router9_dead_since = None
    provider_state.router9_enabled = True
    provider_state.api_exhausted_until = {"groq": 0.0, "openrouter": 0.0, "api1": 0.0, "api2": 0.0}
    provider_state._loaded = True
    yield log
    provider_state._loaded = False


async def test_ask_mac_dinh_khong_truyen_tham_so_moi(providers):
    await orchestrator.ask("câu hỏi")
    name, kwargs = providers.calls[0]
    assert name == "router9" and set(kwargs) == {"model"}


async def test_ask_truyen_max_tokens_nhiet_do_va_json_xuong_provider(providers):
    providers.fail = {"router9", "groq", "openrouter"}
    await orchestrator.ask("trả JSON", max_tokens=300, temperature=0.1, json_mode=True)
    by_name = dict(providers.calls)
    assert by_name["router9"] == {"model": None, "max_tokens": 300, "temperature": 0.1}
    # JSON mode thật chỉ ở Groq (json_object) và Gemini (response_mime_type).
    assert by_name["groq"] == {
        "model": None,
        "max_tokens": 300,
        "temperature": 0.1,
        "json_mode": True,
    }
    assert "json_mode" not in by_name["openrouter"]
    assert by_name["api1"]["max_output_tokens"] == 300 and by_name["api1"]["json_mode"] is True
    assert by_name["api1"]["temperature"] == 0.1


async def test_ask_utility_chuoi_ngan_khong_doi_provider_dang_dung(providers, monkeypatch):
    stages = []

    async def record(provider, **kwargs):
        stages.append((provider, orchestrator._call_stage.get()))

    monkeypatch.setattr(orchestrator, "_record_provider_call", record)
    providers.fail = {"groq"}
    response = await orchestrator.ask_utility("viết lại truy vấn")
    assert response.text == "api1-ok"
    assert [name for name, _ in providers.calls] == ["groq", "api1"]  # không đụng 9Router
    assert providers.calls[0][1]["max_tokens"] == orchestrator.UTILITY_MAX_TOKENS
    assert stages == [("groq", "utility"), ("api1", "utility")]
    assert provider_state.active_provider == "router9"  # lượt phụ không đổi /status


async def test_viet_lai_truy_van_tra_web_di_chuoi_phu(monkeypatch):
    from services import web_search

    seen = {}

    async def fake_ask(prompt, **kwargs):
        seen.update(kwargs, stage=orchestrator._call_stage.get())
        return SimpleNamespace(text="giá vàng SJC hôm nay\ngiá vàng 9999")

    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    queries = await web_search._rewrite_and_expand_query("còn vàng thì sao?", "user: giá bạc")
    assert queries == ["giá vàng SJC hôm nay", "giá vàng 9999"]
    assert seen["providers_override"] == list(orchestrator.UTILITY_PROVIDERS)
    assert seen["max_tokens"] == web_search._REWRITE_MAX_TOKENS and seen["stage"] == "utility"


async def test_router_du_phong_qua_chuoi_la_luot_phu_json(monkeypatch):
    from services import tools

    seen = {}

    async def fake_ask(prompt, **kwargs):
        seen.update(kwargs, stage=orchestrator._call_stage.get())
        return SimpleNamespace(text='{"tool": "none", "args": {}}')

    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    assert await tools._route_with_chain("router JSON") == {"tool": "none", "args": {}}
    assert seen["json_mode"] is True and seen["stage"] == "utility"
    assert seen["max_tokens"] == tools._ROUTER_CHAIN_MAX_TOKENS
