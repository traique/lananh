"""Ghi DB nền có trần (ai/background_writes.py): dòng provider_calls và trạng thái
provider-chain không còn chặn câu trả lời; lệnh admin chịu được DB chập chờn; mọi
khoá cooldown (kể cả Whisper/TTS) được nạp lại sau restart."""

import asyncio
import time

import pytest

from ai import background_writes, call_log, orchestrator
from ai import provider_state as provider_state_module
from ai.provider_state import ProviderChainState, provider_state
from core import config
from core import database as db


@pytest.fixture
def gate(monkeypatch):
    """DB giả "chậm": mọi lượt ghi chờ tới khi test mở cổng."""
    opened = asyncio.Event()
    rows: list[tuple] = []
    settings: dict[str, str] = {}

    async def record(provider, model, **kwargs):
        await opened.wait()
        rows.append((provider, kwargs.get("outcome")))

    async def set_setting(key, value):
        await opened.wait()
        settings[key] = value

    async def get_setting(key):
        return settings.get(key)

    monkeypatch.setattr(db, "record_provider_call", record)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(db, "get_setting", get_setting)
    yield opened, rows, settings
    opened.set()


async def test_ghi_provider_calls_khong_chan_luot_tra_loi(gate):
    opened, rows, _ = gate
    await asyncio.wait_for(call_log.record("groq", "m", latency_ms=5), 1)
    assert rows == [] and background_writes.pending() == 1
    opened.set()
    await call_log.drain()
    assert rows == [("groq", "ok")] and background_writes.pending() == 0


async def test_so_luot_ghi_nen_co_tran(gate, monkeypatch, caplog):
    opened, rows, _ = gate
    monkeypatch.setattr(background_writes, "MAX_PENDING", 3)
    for _ in range(5):
        await call_log.record("groq", "m")
    assert background_writes.pending() == 3
    assert "lượt ghi DB nền đang chờ" in caplog.text
    opened.set()
    await call_log.drain()
    assert len(rows) == 3


async def test_db_loi_khong_van_len_va_drain_khong_treo(monkeypatch):
    async def boom(*args, **kwargs):
        raise ConnectionError("db sập")

    monkeypatch.setattr(db, "record_provider_call", boom)
    await call_log.record("groq", "m")
    await asyncio.wait_for(call_log.drain(), 1)


async def test_doi_provider_ghi_nen_va_db_giu_gia_tri_moi_nhat(gate):
    opened, _, settings = gate
    state = ProviderChainState()
    state._loaded = True
    await asyncio.wait_for(state.set_active_provider("groq"), 1)
    await state.set_active_provider("api1")
    await state.set_active_provider("api2")
    assert state.active_provider == "api2" and settings == {}
    opened.set()
    await background_writes.drain()
    assert settings[provider_state_module._STATE_ACTIVE_PROVIDER] == "api2"
    assert not state._writing and not state._dirty


async def test_chuoi_provider_tra_loi_truoc_khi_db_ghi_xong(gate, monkeypatch):
    opened, rows, settings = gate
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["api1"])

    async def api_key_for(idx):
        return "k"

    async def api_call(idx):
        return "api1-ok"

    async def router9_call():
        raise AssertionError("không gọi router9")

    monkeypatch.setattr(orchestrator.official_client, "api_key_for", api_key_for)
    monkeypatch.setattr(provider_state, "active_provider", "router9")
    monkeypatch.setattr(provider_state, "api_exhausted_until", {"api1": 0.0})
    monkeypatch.setattr(provider_state, "_loaded", True)
    result = await asyncio.wait_for(
        orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call), 1
    )
    assert result == "api1-ok" and rows == [] and settings == {}
    opened.set()
    await background_writes.drain()
    assert rows == [("api1", "ok")]
    assert settings[provider_state_module._STATE_ACTIVE_PROVIDER] == "api1"


async def test_lenh_admin_chiu_duoc_db_loi(monkeypatch):
    async def broken(key, value):
        raise ConnectionError("Supabase chập chờn")

    monkeypatch.setattr(db, "set_setting", broken)
    state = ProviderChainState()
    state._loaded = True
    state.api_exhausted_until["api1"] = time.time() + 999
    state.cooldown_reasons["api1"] = "429"
    assert await state.set_router9_enabled(False) is False
    assert state.router9_enabled is False
    assert await state.reset_api_cooldown("api1") is False
    assert not state.api_in_cooldown("api1") and "api1" not in state.cooldown_reasons
    await background_writes.drain()


async def test_lenh_admin_bao_da_luu(monkeypatch):
    saved = {}

    async def set_setting(key, value):
        saved[key] = value

    monkeypatch.setattr(db, "set_setting", set_setting)
    state = ProviderChainState()
    assert await state.set_router9_enabled(True) is True
    assert saved[provider_state_module._STATE_ROUTER9_ENABLED] == "1"


async def test_cooldown_whisper_va_tts_nap_lai_sau_restart(monkeypatch):
    until = str(time.time() + 600)
    store = {
        "provider_api_exhausted_until_groq_stt": until,
        "provider_api_exhausted_until_tts_api1": until,
        "provider_cooldown_reason_tts_api1": "hết quota NGÀY",
    }

    async def get_setting(key):
        return store.get(key)

    monkeypatch.setattr(db, "get_setting", get_setting)
    assert orchestrator.STT_COOLDOWN_KEY in provider_state_module._COOLDOWN_PROVIDERS
    for idx in (1, 2):
        key = f"{orchestrator.TTS_COOLDOWN_PREFIX}api{idx}"
        assert key in provider_state_module._COOLDOWN_PROVIDERS
    state = ProviderChainState()
    await state.load()
    assert state.api_in_cooldown("groq_stt") and state.api_in_cooldown("tts_api1")
    assert not state.api_in_cooldown("tts_api2")
    assert state.cooldown_reasons["tts_api1"] == "hết quota NGÀY"
