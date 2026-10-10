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
