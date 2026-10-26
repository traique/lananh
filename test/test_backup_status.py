"""Trạng thái sao lưu DB: cảnh báo 1 lần/ngày khi bản mới nhất quá BACKUP_STALE_DAYS
ngày; bảng trống (chưa thiết lập) thì im lặng; job bảo trì gọi kiểm tra này."""

from datetime import datetime, timedelta

import pytest

from services import backup_status, db_maintenance

VN = backup_status.VN_TZ


@pytest.fixture
def env(monkeypatch):
    store: dict[str, str] = {}
    runs: dict[str, dict | None] = {"backup": None, "drill": None}
    sent: list[tuple[str, str | None]] = []

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    async def latest_backup_run(kind="backup"):
        return runs[kind]

    def send(text, *, key=None, cooldown=None):
        sent.append((text, key))
        return True

    monkeypatch.setattr(backup_status.db, "get_setting", get_setting)
    monkeypatch.setattr(backup_status.db, "set_setting", set_setting)
    monkeypatch.setattr(backup_status.db, "latest_backup_run", latest_backup_run)
    monkeypatch.setattr(backup_status.alerts, "send", send)
    monkeypatch.delenv("BACKUP_STALE_DAYS", raising=False)
    return store, runs, sent


NOW = datetime(2026, 10, 26, 9, 0, tzinfo=VN)


@pytest.mark.asyncio
async def test_chua_thiet_lap_thi_khong_bao(env):
    _, _, sent = env
    assert await backup_status.check_stale_if_due(NOW) is None
    assert sent == []
    status = await backup_status.status(NOW)
    assert status["configured"] is False and status["stale"] is False


@pytest.mark.asyncio
async def test_backup_cu_thi_bao_1_lan_moi_ngay(env):
    store, runs, sent = env
    runs["backup"] = {"run_key": "1-1-backup", "finished_at": NOW - timedelta(days=4)}
    text = await backup_status.check_stale_if_due(NOW)
    assert text is not None and "4 ngày" in text and "Enable workflow" in text
    assert sent == [(text, "backup_stale")]
    assert await backup_status.check_stale_if_due(NOW + timedelta(hours=3)) is None
    assert len(sent) == 1
    assert await backup_status.check_stale_if_due(NOW + timedelta(days=1)) is not None


@pytest.mark.asyncio
async def test_backup_moi_hoac_tat_kiem_tra_thi_im_lang(env, monkeypatch):
    _, runs, sent = env
    runs["backup"] = {"run_key": "1-1-backup", "finished_at": NOW - timedelta(hours=30)}
    assert await backup_status.check_stale_if_due(NOW) is None
    runs["backup"]["finished_at"] = NOW - timedelta(days=30)
    monkeypatch.setenv("BACKUP_STALE_DAYS", "0")
    assert await backup_status.check_stale_if_due(NOW + timedelta(days=1)) is None
    assert sent == []


@pytest.mark.asyncio
async def test_status_cho_admin(env):
    _, runs, _ = env
    runs["backup"] = {
        "run_key": "9-1-backup",
        "finished_at": NOW - timedelta(days=5),
        "size_bytes": 3 * 1024 * 1024,
    }
    runs["drill"] = {"run_key": "9-1-drill", "finished_at": NOW - timedelta(days=50)}
    status = await backup_status.status(NOW)
    assert status["configured"] and status["stale"] and status["stale_days"] == 3
    assert status["size_mib"] == 3.0 and status["backup_age_days"] == 5.0
    assert status["last_backup_at"].startswith("2026-10-21T09:00")
    assert status["drill_stale"] is True


@pytest.mark.asyncio
async def test_job_bao_tri_goi_kiem_tra_backup_va_loi_khong_chan_vong(monkeypatch):
    calls = []

    async def ok(*args, **kwargs):
        calls.append("ok")

    async def broken(*args, **kwargs):
        calls.append("backup")
        raise RuntimeError("bảng chưa có")

    async def stop_sleep(seconds):
        raise KeyboardInterrupt  # thoát vòng lặp sau 1 lượt

    monkeypatch.setattr(db_maintenance, "run_retention_if_due", ok)
    monkeypatch.setattr(db_maintenance, "run_if_due", ok)
    monkeypatch.setattr(db_maintenance.backup_status, "check_stale_if_due", broken)
    monkeypatch.setattr(db_maintenance.asyncio, "sleep", stop_sleep)
    with pytest.raises(KeyboardInterrupt):
        await db_maintenance._loop()
    assert calls == ["ok", "ok", "backup"]
