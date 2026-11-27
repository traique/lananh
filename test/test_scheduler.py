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

    assert await scheduler._process_due_reminders([(101, 1, "uống thuốc")]) == 1

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

    released = await scheduler._process_due_reminders(
        [
            (1, 1, "binh thuong 1"),
            (2, 1, "loi"),
            (3, 1, "binh thuong 2"),
        ]
    )

    assert released == 1
    assert mark_calls == [1, 3]
    assert release_calls == [2]


async def _due_now(lease=None):
    return 0.0


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
    monkeypatch.setattr(scheduler, "_interval", lambda: 0.01)
    monkeypatch.setattr(scheduler, "_seconds_until_next_due", _due_now)
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


# ─── Vòng nhắc: ngủ tới lần đến hạn kế tiếp, thức khi đặt/hoãn nhắc ─────────


@pytest.fixture
def next_due(monkeypatch):
    """Giờ tới nhắc kế tiếp (giả) + claim/giao giả; ghi lại số lần claim."""
    state = {"wait": None, "claims": 0, "due": [], "released": 0, "checks": 0}

    async def seconds_until_next_due(lease=None):
        state["checks"] += 1
        value = state["wait"]
        if isinstance(value, Exception):
            raise value
        return value

    async def claim(**kwargs):
        state["claims"] += 1
        assert kwargs == {"limit": scheduler._CLAIM_LIMIT, "lease": scheduler._CLAIM_LEASE}
        return state["due"]

    async def process(due):
        return state["released"]

    monkeypatch.setattr(scheduler, "_seconds_until_next_due", seconds_until_next_due)
    monkeypatch.setattr(idempotency, "claim_due_reminders", claim)
    monkeypatch.setattr(scheduler, "_process_due_reminders", process)
    monkeypatch.setattr(scheduler.config, "REMINDER_CHECK_INTERVAL_SEC", 30)
    return state


@pytest.mark.asyncio
async def test_luot_nhac_ngu_toi_lan_den_han_ke_tiep(next_due):
    # Không còn nhắc nào chờ: ngủ trọn nhịp kiểm tra, không claim.
    assert await scheduler._reminder_pass() == 30
    # Nhắc kế tiếp sau 12,5 s: ngủ đúng chừng đó (gửi đúng giờ, không trễ tới 30 s).
    next_due["wait"] = 12.5
    assert await scheduler._reminder_pass() == 12.5
    # Còn lâu (1 giờ): nhịp kiểm tra tối đa là lưới an toàn.
    next_due["wait"] = 3600.0
    assert await scheduler._reminder_pass() == 30
    assert next_due["claims"] == 0


@pytest.mark.asyncio
async def test_luot_nhac_toi_han_thi_claim_roi_kiem_tra_lai_ngay(next_due):
    next_due["wait"] = -2.0
    next_due["due"] = [(1, 1, "uống thuốc")]
    assert await scheduler._reminder_pass() == 0.0
    assert next_due["claims"] == 1
    # Giao lỗi đã nhả claim: chờ 1 nhịp rồi mới thử lại, không quay vòng gấp.
    next_due["released"] = 1
    assert await scheduler._reminder_pass() == 30
    # DB báo có nhắc nhưng claim không lấy được (instance khác đang claim): chờ 1 nhịp.
    next_due["released"] = 0
    next_due["due"] = []
    assert await scheduler._reminder_pass() == 30
    # Đọc DB lỗi: log, chờ 1 nhịp.
    next_due["wait"] = ConnectionError("db down")
    assert await scheduler._reminder_pass() == 30


@pytest.mark.asyncio
async def test_dat_nhac_moi_danh_thuc_vong_nhac(next_due, monkeypatch):
    monkeypatch.setattr(scheduler, "_wake_event", asyncio.Event())
    checked = asyncio.Event()

    async def seconds_until_next_due(lease=None):
        next_due["checks"] += 1
        checked.set()
        return None  # không có nhắc: lẽ ra ngủ trọn 30 s

    monkeypatch.setattr(scheduler, "_seconds_until_next_due", seconds_until_next_due)
    task = asyncio.create_task(scheduler._reminder_loop())
    try:
        await asyncio.wait_for(checked.wait(), 1)
        checked.clear()
        await asyncio.sleep(0.01)
        assert next_due["checks"] == 1
        scheduler.wake()
        await asyncio.wait_for(checked.wait(), 1)
        assert next_due["checks"] == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_danh_thuc_trong_luc_dang_kiem_tra_khong_bi_mat(next_due, monkeypatch):
    """Nhắc được ghi + wake() TRONG LÚC lượt kiểm tra đang đọc DB (đọc trước khi dòng
    mới commit nên không thấy): vòng không được ngủ trọn nhịp mà phải kiểm tra lại."""
    monkeypatch.setattr(scheduler, "_wake_event", asyncio.Event())
    second = asyncio.Event()

    async def seconds_until_next_due(lease=None):
        next_due["checks"] += 1
        if next_due["checks"] == 1:
            scheduler.wake()  # reminder_service.create chạy xen giữa lúc đang đọc
            return None
        second.set()
        return None

    monkeypatch.setattr(scheduler, "_seconds_until_next_due", seconds_until_next_due)
    task = asyncio.create_task(scheduler._reminder_loop())
    try:
        await asyncio.wait_for(second.wait(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_wake_khi_chua_start_khong_loi(monkeypatch):
    monkeypatch.setattr(scheduler, "_wake_event", None)
    scheduler.wake()


@pytest.mark.asyncio
async def test_start_stop_tao_va_bo_co_danh_thuc(monkeypatch):
    async def idle_loop():
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "_reminder_loop", idle_loop)
    monkeypatch.setattr(scheduler.config, "ENABLE_DAILY_DIGEST", False)
    monkeypatch.setattr(scheduler, "_reminder_task", None)
    scheduler.start(0)
    assert isinstance(scheduler._wake_event, asyncio.Event)
    await scheduler.stop()
    assert scheduler._wake_event is None and scheduler._reminder_task is None


def test_gio_ngu_toi_da_co_san(monkeypatch):
    monkeypatch.setattr(scheduler.config, "REMINDER_CHECK_INTERVAL_SEC", 0)
    assert scheduler._interval() == scheduler._MIN_INTERVAL_SEC
    monkeypatch.setattr(scheduler.config, "REMINDER_CHECK_INTERVAL_SEC", 300)
    assert scheduler._interval() == 300


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
