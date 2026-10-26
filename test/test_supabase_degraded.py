"""Chế độ suy giảm khi Supabase chậm/sập: câu trả lời đã sinh không bao giờ bị
mất vì lỗi ghi DB, đọc trí nhớ/lịch sử fail-open, circuit breaker fail nhanh."""

import asyncio
from types import SimpleNamespace

import asyncpg
import pytest

from core import config, database as db


# ─── Circuit breaker trong _with_reconnect ───────────────────────────────────


@pytest.fixture
def no_pool_reset(monkeypatch):
    async def fake_reset_pool(failed_pool):
        return None

    monkeypatch.setattr(db, "_reset_pool", fake_reset_pool)
    monkeypatch.setattr(config, "DB_BREAKER_OPEN_SEC", 30)


@pytest.mark.asyncio
async def test_loi_ket_noi_le_duoc_reconnect_chua_khong_mo_breaker(no_pool_reset):
    calls = []

    @db._with_reconnect
    async def query():
        calls.append(1)
        if len(calls) == 1:
            raise asyncpg.ConnectionDoesNotExistError("connection was closed")
        return "ok"

    assert await query() == "ok"
    assert not db.circuit_open()


@pytest.mark.asyncio
async def test_reconnect_van_mat_ket_noi_thi_mo_breaker_fail_nhanh(no_pool_reset, monkeypatch):
    calls = []

    @db._with_reconnect
    async def query():
        calls.append(1)
        raise ConnectionRefusedError("db down")

    with pytest.raises(ConnectionRefusedError):
        await query()
    assert len(calls) == 2 and db.circuit_open()

    # Breaker mở: báo lỗi ngay, không chạm DB.
    with pytest.raises(db.DatabaseUnavailableError):
        await query()
    assert len(calls) == 2

    # Hết hạn: đúng 1 lệnh thăm dò đi qua; thành công thì đóng breaker.
    monkeypatch.setattr(db, "_breaker_open_until", 1.0)

    @db._with_reconnect
    async def healthy():
        return "ok"

    assert await healthy() == "ok"
    assert not db.circuit_open() and db._breaker_open_until == 0.0


@pytest.mark.asyncio
async def test_lenh_tham_do_long_lenh_con_khong_tu_chan(no_pool_reset, monkeypatch):
    monkeypatch.setattr(db, "_breaker_open_until", 1.0)  # đã hết hạn -> chờ thăm dò

    @db._with_reconnect
    async def inner():
        return "inner"

    @db._with_reconnect
    async def outer():
        return await inner()

    assert await outer() == "inner"
    assert db._breaker_open_until == 0.0


@pytest.mark.asyncio
async def test_command_timeout_khong_mo_breaker(no_pool_reset):
    @db._with_reconnect
    async def slow():
        raise TimeoutError("command timeout")

    with pytest.raises(TimeoutError):
        await slow()
    assert not db.circuit_open()


@pytest.mark.asyncio
async def test_mo_ket_noi_qua_timeout_mo_breaker_va_bao_db_unavailable(monkeypatch):
    captured = {}

    async def slow_create_pool(*args, **kwargs):
        captured.update(kwargs)
        raise TimeoutError()

    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(config, "DB_BREAKER_OPEN_SEC", 30)
    monkeypatch.setattr(asyncpg, "create_pool", slow_create_pool)
    with pytest.raises(db.DatabaseUnavailableError):
        await db.get_pool()
    assert captured["timeout"] == config.DB_CONNECT_TIMEOUT_SEC
    assert db.circuit_open()


# ─── Câu trả lời đã sinh không bị mất ────────────────────────────────────────


@pytest.mark.asyncio
async def test_luu_lich_su_loi_van_tra_cau_tra_loi(monkeypatch):
    from services import channel_chat_service as ccs

    async def telemetry_start(*args, **kwargs):
        return 5

    async def telemetry_noop(*args, **kwargs):
        return None

    async def no_tool(user_id, text):
        return None

    async def no_memory(user_id, query=""):
        from services.memory_service import TurnMemory

        return TurnMemory()

    async def plan(text, *, user_id=None):
        return ccs.web_search.SearchPlan()

    async def fake_chat(*args, **kwargs):
        return SimpleNamespace(text="Dạ em đây.", used_fallback=False)

    async def broken_save(*args, **kwargs):
        raise ConnectionError("supabase down")

    def no_update(*args, **kwargs):
        return None

    monkeypatch.setattr(ccs.telemetry, "start", telemetry_start)
    monkeypatch.setattr(ccs.telemetry, "success", telemetry_noop)
    monkeypatch.setattr(ccs.telemetry, "failure", telemetry_noop)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", no_tool)
    monkeypatch.setattr(ccs.web_search, "plan_search", plan)
    monkeypatch.setattr(ccs.memory_service, "build_turn_memory", no_memory)
    monkeypatch.setattr(ccs.memory_service, "after_exchange", no_update)
    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.conversation_log.db, "add_chat_turn", broken_save)

    turn = await ccs.run_chat_turn(1, "chào em")
    await ccs.stop_background_tasks()
    assert not turn.failed and turn.text == "Dạ em đây."


@pytest.mark.asyncio
async def test_doc_tri_nho_loi_tra_rong_khong_hong_luot(monkeypatch):
    from services import memory_service

    async def broken(*args, **kwargs):
        raise ConnectionError("db down")

    monkeypatch.setattr(memory_service.db, "get_setting", broken)
    assert await memory_service.build_memory_context(1, "hỏi") == ""


@pytest.mark.asyncio
async def test_doc_lich_su_loi_chat_van_tra_loi(monkeypatch):
    from ai import openai_compatible, orchestrator
    from ai.provider_state import provider_state

    async def broken_history(*args):
        raise ConnectionError("db down")

    captured = {}

    async def fake_router9(prompt, **kwargs):
        captured["history"] = kwargs.get("history")
        return openai_compatible.Response("Dạ.")

    async def preferred():
        return None

    async def no_record(*args, **kwargs):
        return None

    async def no_setting(key):
        return None

    async def ignore_set(key, value):
        return None

    provider_state.router9_dead_since = None
    provider_state.router9_enabled = True
    provider_state._loaded = True
    monkeypatch.setattr(db, "get_setting", no_setting)
    monkeypatch.setattr(db, "set_setting", ignore_set)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "api1", "api2"])
    monkeypatch.setattr(db, "get_session_messages", broken_history)
    monkeypatch.setattr(orchestrator.router9_client, "generate", fake_router9)
    monkeypatch.setattr(orchestrator.router9_client, "get_preferred_model_name", preferred)
    monkeypatch.setattr(orchestrator, "_record_provider_call", no_record)
    response = await orchestrator.chat(1, "chào em")
    assert response.text == "Dạ." and captured["history"] == []


@pytest.mark.asyncio
async def test_luu_cache_idempotency_loi_van_tra_payload():
    from services import channel_dispatch
    from services.channel_result import ChannelResult

    async def no_cache(*args):
        return None

    async def broken_save(*args):
        raise ConnectionError("db down")

    async def chat(user_id, text, is_admin):
        return ChannelResult(["Dạ em trả lời đây."])

    class _Turn:
        def __init__(self, key):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    req = channel_dispatch.DispatchRequest(
        account_id="acc",
        event_id="m1",
        cache_kind="text",
        user_id=-1,
        is_admin=False,
        channel="zalo",
        text="chào",
        turn_key="zalo:1",
    )
    handlers = channel_dispatch.DispatchHandlers(
        chat=chat, get_cached=no_cache, save_cached=broken_save, turn=_Turn
    )
    payload = await channel_dispatch.dispatch(req, handlers)
    assert payload["messages"] == ["Dạ em trả lời đây."]


def test_supervisord_web_co_startretries_20():
    from pathlib import Path

    conf = (Path(__file__).resolve().parents[1] / "supervisord.conf").read_text()
    web_section = conf.split("[program:web]", 1)[1].split("[program:", 1)[0]
    assert "startretries=20" in web_section


@pytest.mark.asyncio
async def test_add_chat_turn_ghi_2_dong_trong_1_transaction(monkeypatch):
    executed = []

    class _Tx:
        async def __aenter__(self):
            executed.append("BEGIN")

        async def __aexit__(self, *exc):
            executed.append("COMMIT")
            return False

    class _Conn:
        def transaction(self):
            return _Tx()

        async def execute(self, query, *args):
            executed.append((query.split()[0], args))

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(db, "get_pool", get_pool)
    await db.add_chat_turn(7, "hỏi", "đáp")
    await asyncio.sleep(0)
    assert executed[0] == "BEGIN" and executed[-1] == "COMMIT"
    insert = executed[1]
    assert insert[0] == "INSERT" and insert[1] == (7, "hỏi", "đáp", "chat")


@pytest.mark.asyncio
async def test_lenh_tham_do_bi_huy_khong_ket_breaker(no_pool_reset, monkeypatch):
    monkeypatch.setattr(db, "_breaker_open_until", 1.0)  # hết hạn, chờ thăm dò
    started = asyncio.Event()

    @db._with_reconnect
    async def hanging():
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(hanging())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db._breaker_probe_inflight is False

    @db._with_reconnect
    async def healthy():
        return "ok"

    assert await healthy() == "ok"
