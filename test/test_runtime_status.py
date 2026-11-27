"""/status có dòng pool kết nối DB (services/runtime_status.py)."""

from core import database as db
from services import runtime_status


def _stats(**overrides):
    base = {
        "ready": True,
        "size": 3,
        "idle": 1,
        "in_use": 2,
        "min": 2,
        "max": 8,
        "statement_cache": 100,
        "acquires": 40,
        "exhausted": 0,
        "slow_waits": 0,
        "max_wait_ms": 12,
        "last_slow_wait_at": None,
    }
    return {**base, **overrides}


def test_pool_chua_mo(monkeypatch):
    monkeypatch.setattr(db, "pool_stats", lambda: _stats(ready=False, size=None, in_use=None))
    assert runtime_status.db_pool_line() == "🗄️ DB pool: chưa mở (tối đa 8 kết nối)"


def test_pool_dang_dung_va_lan_cho_cham(monkeypatch):
    monkeypatch.setattr(db, "pool_stats", lambda: _stats())
    assert runtime_status.db_pool_line() == (
        "🗄️ DB pool: 2/3 kết nối đang dùng (tối đa 8, cache câu lệnh 100)"
    )
    monkeypatch.setattr(db, "pool_stats", lambda: _stats(slow_waits=3, max_wait_ms=1800))
    assert runtime_status.db_pool_line().endswith("; 3 lần chờ > 1 giây, lâu nhất 1800 ms")


def test_pool_stats_that_khong_cham_db():
    line = runtime_status.db_pool_line()
    assert line.startswith("🗄️ DB pool:")
