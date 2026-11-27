"""Unit test cho core/database.py: retry logic (_with_reconnect), pool
singleton, và logic ranh giới phiên chat (get_session_messages) - dùng fake
pool thay vì Postgres thật, vì đây là logic Python thuần (không phải test
câu SQL)."""

import asyncio
import inspect
import sys
from pathlib import Path

import asyncpg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import database as db  # noqa: E402
from core.db import pool as dbpool  # noqa: E402


# ─── _with_reconnect ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_reconnect_thu_lai_1_lan_khi_loi_ket_noi_roi_thanh_cong(monkeypatch):
    calls = {"n": 0, "reset_called": 0}

    async def fake_reset_pool(failed_pool):
        calls["reset_called"] += 1

    monkeypatch.setattr(dbpool, "_reset_pool", fake_reset_pool)

    @dbpool._with_reconnect
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

    monkeypatch.setattr(dbpool, "_reset_pool", fake_reset_pool)

    @dbpool._with_reconnect
    async def broken():
        raise ValueError("lỗi logic, không phải lỗi kết nối")

    with pytest.raises(ValueError):
        await broken()


@pytest.mark.asyncio
async def test_with_reconnect_chi_reset_dung_pool_vua_dung(monkeypatch):
    """Pool lỗi là pool get_pool() đã trả cho lượt gọi này, KHÔNG phải pool
    global tại thời điểm bắt lỗi (có thể đã là pool mới của coroutine khác)."""
    old_pool, new_pool = object(), object()
    reset_args = []

    async def fake_reset_pool(failed_pool):
        reset_args.append(failed_pool)

    monkeypatch.setattr(dbpool, "_reset_pool", fake_reset_pool)
    monkeypatch.setattr(dbpool, "_pool", old_pool)

    @dbpool._with_reconnect
    async def uses_pool():
        pool = await db.get_pool()
        if pool is old_pool:
            dbpool._pool = new_pool  # coroutine khác vừa thay pool
            raise asyncpg.InterfaceError("connection is closed")
        return "ok"

    assert await uses_pool() == "ok"
    assert reset_args == [old_pool]
    monkeypatch.setattr(dbpool, "_pool", None)


@pytest.mark.asyncio
async def test_with_reconnect_khong_retry_insert_khi_mat_ket_noi_giua_chung(monkeypatch):
    calls = {"n": 0}

    async def fake_reset_pool(failed_pool):
        return None

    monkeypatch.setattr(dbpool, "_reset_pool", fake_reset_pool)

    @dbpool._with_reconnect(idempotent=False)
    async def insert():
        calls["n"] += 1
        raise asyncpg.ConnectionDoesNotExistError("closed in the middle of operation")

    with pytest.raises(asyncpg.ConnectionDoesNotExistError):
        await insert()
    assert calls["n"] == 1  # không gửi lại INSERT có thể đã commit


@pytest.mark.asyncio
async def test_with_reconnect_van_retry_insert_khi_chua_gui_lenh(monkeypatch):
    calls = {"n": 0}

    async def fake_reset_pool(failed_pool):
        return None

    monkeypatch.setattr(dbpool, "_reset_pool", fake_reset_pool)

    @dbpool._with_reconnect(idempotent=False)
    async def insert():
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncpg.InterfaceError("cannot perform operation: connection is closed")
        return 1

    assert await insert() == 1
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_bo_sung_gia_ban_khong_retry_khi_co_the_da_commit(monkeypatch):
    """UPDATE chọn "lệnh bán gần nhất chưa có giá": chạy lại sau khi đã commit sẽ
    điền giá cho lệnh bán CŨ HƠN -> không được retry khi mất kết nối giữa chừng."""
    from core.repositories import portfolio as repo

    calls = {"n": 0}

    async def fake_reset_pool(failed_pool):
        return None

    async def fake_pool():
        return object()

    async def fill(pool, user_id, symbol, price, **kw):
        calls["n"] += 1
        raise asyncpg.ConnectionDoesNotExistError("closed in the middle of operation")

    monkeypatch.setattr(dbpool, "_reset_pool", fake_reset_pool)
    monkeypatch.setattr(db, "get_pool", fake_pool)
    monkeypatch.setattr(repo, "fill_sale_price", fill)
    with pytest.raises(asyncpg.ConnectionDoesNotExistError):
        await db.portfolio_fill_sale_price(1, "HPG", 27000, fee_rate=0.0015, tax_rate=0.001)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_doi_loai_luot_quota_khong_retry_khi_co_the_da_commit(monkeypatch):
    """relabel chọn "lượt chat MỚI NHẤT hôm nay": chạy lại sau khi đã commit sẽ đổi
    cả lượt TRƯỚC đó thành chat_search (lượt nặng +2) -> không retry khi mất kết nối
    giữa chừng; lỗi trước khi gửi lệnh vẫn retry như cũ."""
    from datetime import datetime

    from core.repositories import quota as repo

    calls = {"n": 0}

    async def fake_reset_pool(failed_pool):
        return None

    async def fake_pool():
        return object()

    async def relabel(pool, *args):
        calls["n"] += 1
        raise asyncpg.ConnectionDoesNotExistError("closed in the middle of operation")

    monkeypatch.setattr(dbpool, "_reset_pool", fake_reset_pool)
    monkeypatch.setattr(db, "get_pool", fake_pool)
    monkeypatch.setattr(repo, "relabel_latest", relabel)
    with pytest.raises(asyncpg.ConnectionDoesNotExistError):
        await db.relabel_last_user_call("zalo", -9, "chat", "chat_search", datetime(2026, 10, 26))
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_with_reconnect_chay_lai_1_lan_khi_prepared_statement_cu(monkeypatch):
    """Schema đổi (migration của instance khác) -> InvalidCachedStatementError trong
    transaction: câu lỗi chưa chạy, transaction đã rollback -> chạy lại 1 lần, kể cả
    hàm không idempotent; không reset pool (asyncpg đã xoá cache)."""

    async def fail_reset(failed_pool):
        pytest.fail("không reset pool vì prepared statement cũ")

    monkeypatch.setattr(dbpool, "_reset_pool", fail_reset)
    calls = {"n": 0}

    @dbpool._with_reconnect(idempotent=False)
    async def insert():
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncpg.InvalidCachedStatementError("cached plan must not change result type")
        return "ok"

    assert await insert() == "ok" and calls["n"] == 2

    @dbpool._with_reconnect
    async def always_stale():
        calls["n"] += 1
        raise asyncpg.InvalidCachedStatementError("cached plan must not change result type")

    calls["n"] = 0
    with pytest.raises(asyncpg.InvalidCachedStatementError):
        await always_stale()
    assert calls["n"] == 2  # chỉ chạy lại đúng 1 lần


# ─── _reset_pool / close_pool: không chờ lệnh dài ────────────────────────────


class _HangingPool:
    """Pool có 1 kết nối đang bị mượn rất lâu (VACUUM FULL, migration): close()
    chờ mãi. Đếm số lần expire/terminate."""

    def __init__(self, closing=False):
        self.expired = 0
        self.terminated = 0
        self.closing = closing

    def is_closing(self):
        return self.closing

    async def expire_connections(self):
        self.expired += 1

    async def close(self):
        await asyncio.Event().wait()  # không bao giờ xong

    def terminate(self):
        self.terminated += 1


@pytest.mark.asyncio
async def test_reset_pool_thay_ket_noi_khong_cho_lenh_dang_chay(monkeypatch):
    pool = _HangingPool()
    monkeypatch.setattr(dbpool, "_pool", pool)
    await asyncio.wait_for(dbpool._reset_pool(pool), 1)  # bản cũ treo ở pool.close()
    assert pool.expired == 1 and dbpool._pool is pool  # giữ pool, chỉ thay kết nối
    # Pool không còn là pool hiện tại (coroutine khác đã thay): không đụng tới.
    await dbpool._reset_pool(_HangingPool())
    assert pool.expired == 1
    monkeypatch.setattr(dbpool, "_pool", None)


@pytest.mark.asyncio
async def test_reset_pool_bo_pool_dang_dong(monkeypatch):
    pool = _HangingPool(closing=True)
    monkeypatch.setattr(dbpool, "_pool", pool)
    await asyncio.wait_for(dbpool._reset_pool(pool), 1)
    assert dbpool._pool is None and pool.expired == 0


@pytest.mark.asyncio
async def test_loi_ket_noi_trong_luc_co_lenh_dai_khong_treo_luot_khac(monkeypatch):
    pool = _HangingPool()
    monkeypatch.setattr(dbpool, "_pool", pool)
    calls = {"n": 0}

    @dbpool._with_reconnect
    async def query():
        await db.get_pool()
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncpg.ConnectionDoesNotExistError("connection was closed")
        return "ok"

    assert await asyncio.wait_for(query(), 1) == "ok"
    assert pool.expired == 1
    monkeypatch.setattr(dbpool, "_pool", None)


@pytest.mark.asyncio
async def test_close_pool_co_han_roi_cat_han(monkeypatch):
    pool = _HangingPool()
    monkeypatch.setattr(dbpool, "_pool", pool)
    await asyncio.wait_for(db.close_pool(grace_sec=0.05), 1)
    assert pool.terminated == 1 and dbpool._pool is None

    class _Quick:
        closed = False

        async def close(self):
            self.closed = True

    quick = _Quick()
    monkeypatch.setattr(dbpool, "_pool", quick)
    await db.close_pool()
    assert quick.closed and dbpool._pool is None
    await db.close_pool()  # chưa có pool: không làm gì


@pytest.mark.asyncio
async def test_ap_migration_xong_thi_thay_ket_noi_pool(monkeypatch):
    """Schema vừa đổi -> mọi kết nối được thay (không còn prepared statement cũ);
    không áp gì thì giữ nguyên."""
    from core import migrations

    pool = _HangingPool()

    async def get_pool():
        return pool

    applied = iter([2, 0])

    async def run(p):
        assert p is pool
        return next(applied)

    monkeypatch.setattr(db, "get_pool", get_pool)
    monkeypatch.setattr(migrations, "run", run)
    await db.ensure_migrations()
    assert pool.expired == 1
    await db.ensure_migrations()
    assert pool.expired == 1


# ─── get_pool: singleton ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pool_chi_tao_pool_1_lan(monkeypatch):
    monkeypatch.setattr(dbpool, "_pool", None)
    create_calls = {"n": 0}

    async def fake_create_pool(*args, **kwargs):
        create_calls["n"] += 1
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)

    pool1 = await db.get_pool()
    pool2 = await db.get_pool()

    assert pool1 is pool2
    assert create_calls["n"] == 1
    monkeypatch.setattr(dbpool, "_pool", None)  # dọn lại state global cho test khác


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("postgresql://u.ref:p%40ss@aws-0-ap.pooler.supabase.com:5432/postgres", 100),
        ("postgresql://u.ref:p@aws-0-ap.pooler.supabase.com:6543/postgres?sslmode=require", 0),
        ("postgresql://u:p@db.local/postgres", 100),  # không ghi cổng
        ("postgresql://u:p@host:abc/postgres", 100),  # cổng hỏng: asyncpg tự báo lỗi
    ],
)
def test_statement_cache_theo_cong(monkeypatch, dsn, expected):
    """Session Pooler (5432) giữ prepared statement -> cache như mặc định asyncpg;
    transaction pooler (6543) thì không."""
    monkeypatch.setattr(dbpool.config, "DATABASE_URL", dsn)
    monkeypatch.setattr(dbpool.config, "DB_STATEMENT_CACHE_SIZE", None)
    assert db.statement_cache_size() == expected
    monkeypatch.setattr(dbpool.config, "DB_STATEMENT_CACHE_SIZE", 7)  # env đè
    assert db.statement_cache_size() == 7


@pytest.mark.asyncio
async def test_tao_pool_theo_cau_hinh_va_do_thoi_gian_muon(monkeypatch):
    captured = {}
    real_create_pool = asyncpg.create_pool

    async def fake_create_pool(dsn, **kwargs):
        captured.update(kwargs)
        # Pool asyncpg thật, min_size=0 nên khởi tạo không mở kết nối nào.
        return await real_create_pool("postgresql://u:p@127.0.0.1:1/x", min_size=0, max_size=2)

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(dbpool.config, "DATABASE_URL", "postgresql://u:p@host:6543/postgres")
    monkeypatch.setattr(dbpool.config, "DB_STATEMENT_CACHE_SIZE", None)
    monkeypatch.setattr(dbpool.config, "DB_POOL_MIN_SIZE", 2)
    monkeypatch.setattr(dbpool.config, "DB_POOL_MAX_SIZE", 8)
    pool = await dbpool._create_pool()
    try:
        assert captured["statement_cache_size"] == 0
        assert (captured["min_size"], captured["max_size"]) == (2, 8)
        assert isinstance(pool, dbpool._MeteredPool)

        async def slow_acquire(self, _timeout):
            await asyncio.sleep(0.02)
            return "conn"

        monkeypatch.setattr(asyncpg.pool.Pool, "_acquire", slow_acquire)
        monkeypatch.setattr(dbpool, "_SLOW_ACQUIRE_SEC", 0.01)
        monkeypatch.setattr(dbpool, "_acquire_stats", dbpool._AcquireStats())
        assert await pool._acquire(None) == "conn"
        stats = dbpool._acquire_stats
        assert stats.acquires == 1 and stats.slow == 1 and stats.max_wait >= 0.02
        assert stats.last_slow_at is not None
    finally:
        await pool.close()


def test_acquire_cua_asyncpg_con_dung_chu_ky():
    """_MeteredPool đè Pool._acquire(self, timeout) - nâng asyncpg mà đổi thì báo ngay."""
    params = list(inspect.signature(asyncpg.pool.Pool._acquire).parameters)
    assert params == ["self", "timeout"]


@pytest.mark.asyncio
async def test_pool_stats(monkeypatch):
    monkeypatch.setattr(dbpool, "_pool", None)
    monkeypatch.setattr(dbpool, "_acquire_stats", dbpool._AcquireStats())
    stats = db.pool_stats()
    assert stats["ready"] is False and stats["size"] is None and stats["in_use"] is None
    assert stats["max"] == dbpool.config.DB_POOL_MAX_SIZE and stats["slow_waits"] == 0

    pool = await asyncpg.create_pool("postgresql://u:p@127.0.0.1:1/x", min_size=0, max_size=3)
    try:
        monkeypatch.setattr(dbpool, "_pool", pool)
        dbpool._record_acquire(pool, 2.5, True)
        dbpool._record_acquire(pool, 0.2, False)
        stats = db.pool_stats()
        assert (stats["ready"], stats["size"], stats["idle"], stats["in_use"]) == (True, 0, 0, 0)
        assert stats["acquires"] == 2 and stats["exhausted"] == 1 and stats["slow_waits"] == 1
        assert stats["max_wait_ms"] == 2500 and stats["last_slow_wait_at"] is not None
    finally:
        await pool.close()
        monkeypatch.setattr(dbpool, "_pool", None)


# ─── get_session_messages: ranh giới phiên tính trong SQL ───────────────────
# Ngữ nghĩa (cắt ở khoảng nghỉ, tin mới nhất quá cũ -> rỗng, đúng k dòng) được kiểm
# trên Postgres thật ở test/test_db_sql.py; ở đây chỉ kiểm phần Python.


@pytest.mark.asyncio
async def test_get_session_messages_k_khong_duong_tra_rong_khong_dong_pool(monkeypatch):
    async def fail_get_pool():
        pytest.fail("get_pool() không được gọi khi k <= 0 (short-circuit sớm)")

    monkeypatch.setattr(db, "get_pool", fail_get_pool)
    assert await db.get_session_messages(1, 0, 3600) == []
    assert await db.get_session_messages(1, -1, 3600) == []


@pytest.mark.asyncio
async def test_get_session_messages_doc_dung_k_dong_va_giu_thu_tu_sql(monkeypatch):
    calls = []

    class Pool:
        async def fetch(self, query, *args):
            calls.append((query, args))
            # SQL đã cắt phiên và sắp CŨ -> MỚI; Python chỉ đổi sang (role, content).
            return [
                {"role": "user", "content": "hỏi"},
                {"role": "model", "content": "đáp"},
            ]

    pool = Pool()

    async def get_pool():
        return pool

    monkeypatch.setattr(db, "get_pool", get_pool)
    result = await db.get_session_messages(telegram_user_id=7, k=20, session_timeout_sec=3600)
    assert result == [("user", "hỏi"), ("model", "đáp")]
    query, args = calls[0]
    assert args == (7, 20, 3600)  # LIMIT đúng k, không còn đọc dư max(k*4, 40) dòng
    assert "lag(created_at)" in query
