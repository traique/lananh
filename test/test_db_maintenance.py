from datetime import datetime

import pytest

from services import db_maintenance as dm

MIB = dm.MIB


def usage(total, live, database=100, posting=0):
    return dm.TableUsage(total * MIB, live * MIB, database * MIB, posting)


def test_runs_only_when_waste_is_large_and_majority():
    assert dm.should_vacuum(usage(200, 20))[0] is True
    assert dm.should_vacuum(usage(60, 30))[0] is False  # lãng phí 30 MB < 50 MB
    assert dm.should_vacuum(usage(300, 200))[0] is False  # lãng phí chưa tới nửa bảng


def test_skips_while_a_post_is_publishing():
    ok, reason = dm.should_vacuum(usage(200, 20, posting=1))
    assert not ok and "POSTING" in reason


def test_skips_when_temp_copy_could_exceed_db_limit():
    ok, reason = dm.should_vacuum(usage(400, 190, database=470))
    assert not ok and "dung lượng" in reason


def test_window_is_sunday_3am_vietnam_by_default():
    assert dm.in_window(datetime(2026, 10, 11, 3, 15, tzinfo=dm.VN_TZ))  # Chủ nhật
    assert not dm.in_window(datetime(2026, 10, 11, 4, 0, tzinfo=dm.VN_TZ))
    assert not dm.in_window(datetime(2026, 10, 12, 3, 0, tzinfo=dm.VN_TZ))  # Thứ 2


@pytest.mark.asyncio
async def test_runs_at_most_once_per_week(monkeypatch):
    store, measured = {}, []

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    async def measure():
        measured.append(1)
        return usage(10, 9)

    monkeypatch.setattr(dm.db, "get_setting", get_setting)
    monkeypatch.setattr(dm.db, "set_setting", set_setting)
    monkeypatch.setattr(dm, "measure", measure)
    sunday = datetime(2026, 10, 11, 3, 5, tzinfo=dm.VN_TZ)
    assert "Bỏ qua" in await dm.run_if_due(sunday)
    assert await dm.run_if_due(sunday.replace(minute=40)) is None
    assert len(measured) == 1


@pytest.mark.asyncio
async def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("DB_AUTO_VACUUM_FULL", "0")
    assert await dm.run_if_due(datetime(2026, 10, 11, 3, 5, tzinfo=dm.VN_TZ)) is None


# ─── Dọn dữ liệu cũ hằng ngày ───────────────────────────────────────────────


class _RetentionPool:
    def __init__(self, batches):
        self.batches = batches  # label -> list số dòng xoá mỗi lô
        self.calls = []

    async def execute(self, sql, interval, batch):
        label = next(r.label for r in dm.RETENTION_RULES if r.sql == sql)
        self.calls.append((label, interval, batch))
        remaining = self.batches.get(label, [])
        return f"DELETE {remaining.pop(0) if remaining else 0}"


@pytest.mark.asyncio
async def test_retention_deletes_in_bounded_batches_with_configured_windows(monkeypatch):
    from datetime import timedelta

    pool = _RetentionPool({"user_calls": [dm._RETENTION_BATCH, 7], "zalo_outbox": [3]})

    async def get_pool():
        return pool

    monkeypatch.setattr(dm.db, "get_pool", get_pool)
    monkeypatch.setenv("RETENTION_PROVIDER_CALLS_DAYS", "0")  # tắt riêng bảng này
    monkeypatch.setenv("RETENTION_REMINDERS_DAYS", "45")
    result = await dm.prune_retention()
    assert result == {
        "user_calls": dm._RETENTION_BATCH + 7,
        "zalo_outbox": 3,
        "zalo_outbox_failed": 0,
        "reminders": 0,
    }
    labels = [label for label, _, _ in pool.calls]
    assert labels.count("user_calls") == 2 and "provider_calls" not in labels
    windows = {label: interval for label, interval, _ in pool.calls}
    assert windows["user_calls"] == timedelta(days=180)
    assert windows["zalo_outbox"] == timedelta(days=30)
    assert windows["reminders"] == timedelta(days=45)


def test_retention_sql_only_touches_old_sent_rows():
    sql = {rule.label: rule.sql for rule in dm.RETENTION_RULES}
    assert "sent_at IS NOT NULL" in sql["zalo_outbox"]
    # Tin đã bỏ cuộc (failed_at, migration 012) cũng được dọn theo cùng số ngày.
    assert "failed_at IS NOT NULL" in sql["zalo_outbox_failed"]
    assert "sent = true" in sql["reminders"]
    assert all("LIMIT $2" in text for text in sql.values())


@pytest.mark.asyncio
async def test_retention_runs_once_per_day_and_a_failing_table_does_not_block_others(monkeypatch):
    store, executed = {}, []

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    class Pool:
        async def execute(self, sql, *args):
            executed.append(sql)
            if "user_calls" in sql:
                raise RuntimeError("lock timeout")
            return "DELETE 0"

    async def get_pool():
        return Pool()

    monkeypatch.setattr(dm.db, "get_setting", get_setting)
    monkeypatch.setattr(dm.db, "set_setting", set_setting)
    monkeypatch.setattr(dm.db, "get_pool", get_pool)
    day = datetime(2026, 10, 11, 9, 0, tzinfo=dm.VN_TZ)
    assert await dm.run_retention_if_due(day) is not None
    assert len(executed) == len(dm.RETENTION_RULES)  # bảng lỗi bị bỏ qua, các bảng kia vẫn chạy
    assert await dm.run_retention_if_due(day.replace(hour=20)) is None
    assert len(executed) == len(dm.RETENTION_RULES)
