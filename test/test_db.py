"""Unit test cho core/database.py: retry logic (_with_reconnect), pool
singleton, và logic ranh giới phiên chat (get_session_messages) - dùng fake
pool thay vì Postgres thật, vì đây là logic Python thuần (không phải test
câu SQL)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import database as db  # noqa: E402


# ─── _with_reconnect ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_with_reconnect_thu_lai_1_lan_khi_loi_ket_noi_roi_thanh_cong(monkeypatch):
    calls = {"n": 0, "reset_called": 0}

    async def fake_reset_pool(failed_pool):
        calls["reset_called"] += 1

    monkeypatch.setattr(db, "_reset_pool", fake_reset_pool)

    @db._with_reconnect
    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncpg.InterfaceError("connection is closed")
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert calls["n"] == 2  # gọi lần 1 lỗi, reset pool, gọi lại lần 2 thành công
    assert calls["reset_called"] == 1


@pytest.mark.asyncio
async def test_with_reconnect_khong_nuot_loi_khac_loi_ket_noi(monkeypatch):
    async def fake_reset_pool(failed_pool):
        pytest.fail("_reset_pool không được gọi cho lỗi không phải lỗi kết nối")

    monkeypatch.setattr(db, "_reset_pool", fake_reset_pool)

    @db._with_reconnect
    async def broken():
        raise ValueError("lỗi logic, không phải lỗi kết nối")

    with pytest.raises(ValueError):
        await broken()


# ─── get_pool: singleton ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_pool_chi_tao_pool_1_lan(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    create_calls = {"n": 0}

    async def fake_create_pool(*args, **kwargs):
        create_calls["n"] += 1
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)

    pool1 = await db.get_pool()
    pool2 = await db.get_pool()

    assert pool1 is pool2
    assert create_calls["n"] == 1
    monkeypatch.setattr(db, "_pool", None)  # dọn lại state global cho test khác


# ─── get_session_messages: ranh giới phiên theo session_timeout_sec ─────────

class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args):
        return self._rows


def _row(role: str, content: str, created_at: datetime) -> dict:
    # asyncpg trả về Record hỗ trợ r["col"] - dict cũng hỗ trợ __getitem__ tương tự.
    return {"role": role, "content": content, "created_at": created_at}


@pytest.mark.asyncio
async def test_get_session_messages_k_khong_duong_tra_rong_khong_dong_pool(monkeypatch):
    async def fail_get_pool():
        pytest.fail("get_pool() không được gọi khi k <= 0 (short-circuit sớm)")

    monkeypatch.setattr(db, "get_pool", fail_get_pool)
    assert await db.get_session_messages(1, 0, 3600) == []
    assert await db.get_session_messages(1, -1, 3600) == []


@pytest.mark.asyncio
async def test_get_session_messages_cat_dung_ranh_gioi_phien(monkeypatch):
    now = datetime.now(timezone.utc)
    # Thứ tự trả về từ DB: MỚI -> CŨ (ORDER BY id DESC), giống hành vi thật.
    rows = [
        _row("model", "tin moi nhat", now - timedelta(seconds=10)),
        _row("user", "tin truoc do 30s", now - timedelta(seconds=40)),
        # Khoảng nghỉ > session_timeout_sec (3600s) tại đây -> phiên cũ dừng lại.
        _row("model", "tin phien truoc, qua cu", now - timedelta(hours=5)),
        _row("user", "tin cang cu hon", now - timedelta(hours=6)),
    ]
    monkeypatch.setattr(db, "get_pool", _fake_get_pool_factory(rows))

    result = await db.get_session_messages(telegram_user_id=1, k=10, session_timeout_sec=3600)

    # Chỉ lấy 2 tin trong cùng phiên (trước điểm nghỉ >1h), theo thứ tự CŨ -> MỚI.
    assert result == [
        ("user", "tin truoc do 30s"),
        ("model", "tin moi nhat"),
    ]


@pytest.mark.asyncio
async def test_get_session_messages_tin_gan_nhat_da_qua_han_tra_rong(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [_row("model", "tin cu", now - timedelta(hours=2))]
    monkeypatch.setattr(db, "get_pool", _fake_get_pool_factory(rows))

    result = await db.get_session_messages(telegram_user_id=1, k=10, session_timeout_sec=3600)
    assert result == []


@pytest.mark.asyncio
async def test_get_session_messages_gioi_han_dung_k(monkeypatch):
    now = datetime.now(timezone.utc)
    # 5 tin cùng phiên (cách nhau 10s, không vượt session_timeout_sec), giới hạn k=2.
    rows = [
        _row("model", f"tin {i}", now - timedelta(seconds=10 * i))
        for i in range(5)
    ]
    monkeypatch.setattr(db, "get_pool", _fake_get_pool_factory(rows))

    result = await db.get_session_messages(telegram_user_id=1, k=2, session_timeout_sec=3600)
    assert len(result) == 2
    assert result == [("model", "tin 1"), ("model", "tin 0")]  # cũ -> mới, trong 2 tin mới nhất


def _fake_get_pool_factory(rows):
    pool = _FakePool(rows)

    async def fake_get_pool():
        return pool

    return fake_get_pool


# ─── add_chat_embedding: insert + trim cùng 1 transaction (#3) ──────────────

class _FakeConn:
    def __init__(self, log: list[tuple]):
        self._log = log

    async def execute(self, query, *args):
        self._log.append((" ".join(query.split()), args))

    def transaction(self):
        return _NullAsyncCtx()


class _NullAsyncCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _FakeAcquirePool:
    def __init__(self, log: list[tuple]):
        self._log = log

    def acquire(self):
        return _AcquireCtx(self._log)


class _AcquireCtx:
    def __init__(self, log: list[tuple]):
        self._log = log

    async def __aenter__(self):
        return _FakeConn(self._log)

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_add_chat_embedding_insert_va_trim_cung_transaction(monkeypatch):
    log: list[tuple] = []
    monkeypatch.setattr(db, "VECTOR_ENABLED", True)

    async def fake_get_pool():
        return _FakeAcquirePool(log)

    monkeypatch.setattr(db, "get_pool", fake_get_pool)

    await db.add_chat_embedding(1, "noi dung", [0.1, 0.2])

    assert len(log) == 2
    assert log[0][0].startswith("INSERT INTO chat_embeddings")
    assert log[1][0].startswith("DELETE FROM chat_embeddings")
    assert log[1][1][-1] == db.CHAT_EMBEDDINGS_RETENTION_LIMIT


@pytest.mark.asyncio
async def test_add_chat_embedding_tat_khi_vector_disabled(monkeypatch):
    log: list[tuple] = []
    monkeypatch.setattr(db, "VECTOR_ENABLED", False)

    async def fail_get_pool():
        pytest.fail("get_pool() không được gọi khi VECTOR_ENABLED=False")

    monkeypatch.setattr(db, "get_pool", fail_get_pool)

    await db.add_chat_embedding(1, "noi dung", [0.1, 0.2])
    assert log == []


# ─── semantic_search: lọc theo max_distance (#9) ─────────────────────────────

class _FakeFetchPool:
    def __init__(self, captured: dict):
        self._captured = captured

    async def fetch(self, query, *args):
        self._captured["query"] = " ".join(query.split())
        self._captured["args"] = args
        return [{"content": "doan cu"}]


@pytest.mark.asyncio
async def test_semantic_search_truyen_dung_nguong_max_distance(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(db, "VECTOR_ENABLED", True)

    async def fake_get_pool():
        return _FakeFetchPool(captured)

    monkeypatch.setattr(db, "get_pool", fake_get_pool)

    result = await db.semantic_search(1, [0.1, 0.2], top_k=3, max_distance=0.4)

    assert result == ["doan cu"]
    assert "embedding <=> $2 < $4" in captured["query"]
    assert captured["args"] == (1, [0.1, 0.2], 3, 0.4)


@pytest.mark.asyncio
async def test_semantic_search_tat_khi_vector_disabled(monkeypatch):
    async def fail_get_pool():
        pytest.fail("get_pool() không được gọi khi VECTOR_ENABLED=False")

    monkeypatch.setattr(db, "VECTOR_ENABLED", False)
    monkeypatch.setattr(db, "get_pool", fail_get_pool)

    assert await db.semantic_search(1, [0.1, 0.2]) == []


# ─── clear_chat_embeddings (#4 - dọn theo /forget) ──────────────────────────

@pytest.mark.asyncio
async def test_clear_chat_embeddings_xoa_dung_user(monkeypatch):
    calls = []

    class _P:
        async def execute(self, query, *args):
            calls.append((query, args))

    async def fake_get_pool():
        return _P()

    monkeypatch.setattr(db, "get_pool", fake_get_pool)

    await db.clear_chat_embeddings(42)

    assert calls[0][1] == (42,)
    assert "DELETE FROM chat_embeddings" in calls[0][0]
