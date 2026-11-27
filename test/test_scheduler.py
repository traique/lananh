"""Tests for reminder delivery ordering and retry leases."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scheduler  # noqa: E402
from core import database as db  # noqa: E402
from core import idempotency  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_notify_callback():
    yield
    scheduler._notify_callback = None


@pytest.mark.asyncio
async def test_notify_tra_true_khi_gui_thanh_cong():
    async def fake_callback(uid, text):
        return None

    scheduler.set_notify_callback(fake_callback)
    assert await scheduler._notify(1, "xin chao") is True


@pytest.mark.asyncio
async def test_notify_tra_false_khi_gui_loi():
    async def fake_callback(uid, text):
        raise RuntimeError("mô phỏng lỗi Telegram")

    scheduler.set_notify_callback(fake_callback)
    assert await scheduler._notify(1, "xin chao") is False


@pytest.mark.asyncio
async def test_notify_tra_false_khi_chua_dang_ky_callback():
    assert await scheduler._notify(1, "xin chao") is False


@pytest.mark.asyncio
async def test_reminder_gui_that_bai_duoc_release_de_thu_lai(monkeypatch):
    mark_calls = []
    release_calls = []

    async def fake_mark_reminder_sent(reminder_id):
        mark_calls.append(reminder_id)

    async def fake_release(reminder_id):
        release_calls.append(reminder_id)

    async def failing_callback(uid, text):
        raise RuntimeError("Telegram tạm thời lỗi")

    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark_reminder_sent)
    monkeypatch.setattr(idempotency, "release_reminder_claim", fake_release)
    scheduler.set_notify_callback(failing_callback)

    await scheduler._process_due_reminders([(101, 1, "uống thuốc")])

    assert mark_calls == []
    assert release_calls == [101]


@pytest.mark.asyncio
async def test_reminder_gui_thanh_cong_moi_mark_sent(monkeypatch):
    mark_calls = []

    async def fake_mark_reminder_sent(reminder_id):
        mark_calls.append(reminder_id)

    async def ok_callback(uid, text):
        return None

    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark_reminder_sent)
    scheduler.set_notify_callback(ok_callback)

    await scheduler._process_due_reminders([(102, 1, "họp lúc 3h")])

    assert mark_calls == [102]


@pytest.mark.asyncio
async def test_reminder_nhieu_reminder_doc_lap_nhau(monkeypatch):
    mark_calls = []
    release_calls = []

    async def fake_mark_reminder_sent(reminder_id):
        mark_calls.append(reminder_id)

    async def fake_release(reminder_id):
        release_calls.append(reminder_id)

    async def flaky_callback(uid, text):
        if "loi" in text:
            raise RuntimeError("mô phỏng lỗi")

    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark_reminder_sent)
    monkeypatch.setattr(idempotency, "release_reminder_claim", fake_release)
    scheduler.set_notify_callback(flaky_callback)

    await scheduler._process_due_reminders(
        [
            (1, 1, "binh thuong 1"),
            (2, 1, "loi"),
            (3, 1, "binh thuong 2"),
        ]
    )

    assert mark_calls == [1, 3]
    assert release_calls == [2]


@pytest.mark.asyncio
async def test_dong_lech_cot_chi_bo_qua_dong_do(monkeypatch, caplog):
    """1 dòng dữ liệu lệch cột (DueReminder(*item) raise) không được chặn các nhắc
    khác trong cùng lượt claim."""
    mark_calls = []

    async def fake_mark_reminder_sent(reminder_id):
        mark_calls.append(reminder_id)

    async def ok_callback(uid, text):
        return None

    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark_reminder_sent)
    scheduler.set_notify_callback(ok_callback)
    too_many = tuple(range(20))
    await scheduler._process_due_reminders([too_many, (7,), (8, 1, "uống thuốc")])
    assert mark_calls == [8]
    assert "reminder id=0" in caplog.text and "reminder id=7" in caplog.text


@pytest.mark.asyncio
async def test_vong_nhac_song_sot_loi_bat_ngo(monkeypatch):
    """Claim lỗi, dòng lệch cột, lỗi bất ngờ khi xử lý: task nhắc việc vẫn sống và
    gửi được nhắc ở lượt sau."""
    monkeypatch.setattr(scheduler.config, "REMINDER_CHECK_INTERVAL_SEC", 0)
    batches = [
        RuntimeError("supabase chập chờn"),
        [("không", "phải", "dòng", "nhắc", 1, 2, 3, 4, 5, 6, 7)],
        [(9, 1, "lượt gặp lỗi chưa lường trước")],
        [(10, 1, "họp")],
    ]
    sent = []
    delivered = asyncio.Event()

    async def claim(**kwargs):
        batch = batches.pop(0) if batches else []
        if isinstance(batch, Exception):
            raise batch
        return batch

    real_process = scheduler._process_due_reminders

    async def process(due):
        if due and due[0][0] == 9:
            raise RuntimeError("lỗi ngoài vòng từng nhắc")
        await real_process(due)

    async def mark_sent(reminder_id):
        sent.append(reminder_id)
        delivered.set()

    async def ok_callback(uid, text):
        return None

    monkeypatch.setattr(idempotency, "claim_due_reminders", claim)
    monkeypatch.setattr(scheduler, "_process_due_reminders", process)
    monkeypatch.setattr(db, "mark_reminder_sent", mark_sent)
    scheduler.set_notify_callback(ok_callback)
    task = asyncio.create_task(scheduler._reminder_loop())
    try:
        await asyncio.wait_for(delivered.wait(), 2)
        assert sent == [10] and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_watchdog_chay_lai_vong_nhac_da_chet(monkeypatch):
    started = []

    async def fake_loop():
        started.append(1)
        await asyncio.Event().wait()

    async def crash():
        raise KeyError("cột lạ")

    dead = asyncio.create_task(crash())
    await asyncio.gather(dead, return_exceptions=True)
    monkeypatch.setattr(scheduler, "_reminder_loop", fake_loop)
    monkeypatch.setattr(scheduler, "_reminder_task", dead)
    try:
        assert scheduler.revive_reminder_loop() == "KeyError"
        revived = scheduler._reminder_task
        assert revived is not dead
        await asyncio.sleep(0)
        assert started == [1] and not revived.done()
        assert scheduler.revive_reminder_loop() is None  # đang sống: không làm gì
    finally:
        scheduler._reminder_task.cancel()
        await asyncio.gather(scheduler._reminder_task, return_exceptions=True)
    # Bị huỷ lúc tắt app hoặc chưa start: không chạy lại.
    assert scheduler.revive_reminder_loop() is None
    monkeypatch.setattr(scheduler, "_reminder_task", None)
    assert scheduler.revive_reminder_loop() is None


# ─── Daily digest: guard "đã gửi hôm nay" + gửi bù khi lỡ giờ ───────────────


def test_digest_due_theo_khung_gio_va_guard_ngay(monkeypatch):
    from datetime import datetime

    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    monkeypatch.setattr(scheduler.config, "DAILY_DIGEST_HOUR_VN", 8)
    assert scheduler.digest_due(datetime(2026, 5, 4, 7, 59, tzinfo=tz), None) is False
    assert scheduler.digest_due(datetime(2026, 5, 4, 8, 0, tzinfo=tz), None) is True
    # Lỡ giờ (Render ngủ) -> gửi bù trong 3 giờ.
    assert scheduler.digest_due(datetime(2026, 5, 4, 10, 30, tzinfo=tz), None) is True
    assert scheduler.digest_due(datetime(2026, 5, 4, 11, 1, tzinfo=tz), None) is False
    # Hôm nay đã gửi -> không gửi lại dù restart.
    assert scheduler.digest_due(datetime(2026, 5, 4, 8, 30, tzinfo=tz), "2026-05-04") is False
    assert scheduler.digest_due(datetime(2026, 5, 4, 8, 30, tzinfo=tz), "2026-05-03") is True


def test_digest_bo_qua_ngay_khong_co_phien(monkeypatch):
    from datetime import datetime

    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    monkeypatch.setattr(scheduler.config, "DAILY_DIGEST_HOUR_VN", 8)
    monkeypatch.setattr(scheduler.config, "DIGEST_ON_NON_TRADING_DAYS", False)
    assert scheduler.digest_due(datetime(2026, 5, 9, 8, 30, tzinfo=tz), None) is False  # thứ Bảy
    assert scheduler.digest_due(datetime(2026, 4, 30, 8, 30, tzinfo=tz), None) is False  # lễ
    monkeypatch.setattr(scheduler.config, "DIGEST_ON_NON_TRADING_DAYS", True)
    assert scheduler.digest_due(datetime(2026, 5, 9, 8, 30, tzinfo=tz), None) is True


@pytest.mark.asyncio
async def test_run_daily_digest_once_danh_dau_sau_khi_gui(monkeypatch):
    saved = {}

    async def fake_get_setting(key):
        return saved.get(key)

    async def fake_set_setting(key, value):
        saved[key] = value

    async def fake_build(user_id):
        return "digest"

    sent = []

    async def notify(user_id, text):
        sent.append(text)

    monkeypatch.setattr(db, "get_setting", fake_get_setting)
    monkeypatch.setattr(db, "set_setting", fake_set_setting)
    monkeypatch.setattr(scheduler, "_build_portfolio_digest", fake_build)
    monkeypatch.setattr(scheduler, "digest_due", lambda now, last: last is None)
    scheduler.set_notify_callback(notify)

    assert await scheduler.run_daily_digest_once(1) is True
    assert sent == ["digest"] and scheduler._DIGEST_LAST_SENT_KEY in saved
    # Lượt kế trong cùng ngày: guard trong DB chặn.
    assert await scheduler.run_daily_digest_once(1) is False
    assert sent == ["digest"]
