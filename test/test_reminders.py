"""Nhắc việc v2 (services/reminder_service.py, reminder_commands.py, ngay_nho.py,
scheduler) trên DB giả trong RAM: /nhac, /ngaynho, huỷ/hoãn, lối tắt "hoãn 10p"/
"xong", lần kế tiếp của chuỗi, nhãn nhắc trễ."""

import datetime as dt

import pytest

import scheduler
from core import database as db
from core import idempotency
from services import morning_brief, ngay_nho, reminder_commands, reminder_service
from services.reminder_delivery import NotificationTarget, notification_target

VN = reminder_service.VN_TZ


class FakeDB:
    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.series: dict[int, dict] = {}
        self.next_id = 1
        self.now = dt.datetime.now(VN)

    def _id(self):
        self.next_id += 1
        return self.next_id

    async def add_reminder(self, uid, message, due_at, **kw):
        key = kw.get("event_key")
        for row in self.rows.values():
            if key and row.get("event_key") == key:
                return row["due_at"]
        rid = self._id()
        self.rows[rid] = dict(
            id=rid,
            uid=uid,
            message=message,
            due_at=due_at,
            sent=False,
            series_id=None,
            fired_at=None,
            done_at=None,
            **kw,
        )
        return due_at

    async def count_once(self, uid):
        return sum(
            1
            for r in self.rows.values()
            if r["uid"] == uid and not r["sent"] and not r["series_id"]
        )

    async def count_series(self, uid):
        return sum(1 for s in self.series.values() if s["telegram_user_id"] == uid)

    async def create_series(self, uid, message, *, first, **kw):
        sid = self._id()
        self.series[sid] = dict(id=sid, telegram_user_id=uid, message=message, **kw)
        for due, text in first:
            await self.add_occurrence(self.series[sid], due, text)
        return sid, True

    async def get_series(self, sid):
        return self.series.get(sid)

    async def add_occurrence(self, series, due, text):
        if any(r["series_id"] == series["id"] and r["due_at"] == due for r in self.rows.values()):
            return False
        rid = self._id()
        self.rows[rid] = dict(
            id=rid,
            uid=series["telegram_user_id"],
            message=text,
            due_at=due,
            sent=False,
            series_id=series["id"],
            fired_at=None,
            done_at=None,
            event_key=None,
        )
        return True

    async def list_once(self, uid, limit=20):
        rows = [
            r
            for r in self.rows.values()
            if r["uid"] == uid and not r["sent"] and not r["series_id"]
        ]
        return sorted(rows, key=lambda r: r["due_at"])[:limit]

    async def list_series(self, uid, kind="reminder", limit=50):
        out = []
        for s in self.series.values():
            if s["telegram_user_id"] != uid or s["kind"] != kind:
                continue
            pending = sorted(
                (r for r in self.rows.values() if r["series_id"] == s["id"] and not r["sent"]),
                key=lambda r: r["due_at"],
            )
            nxt = pending[0] if pending else None
            out.append(dict(s, next_id=nxt and nxt["id"], next_due_at=nxt and nxt["due_at"]))
        return out[:limit]

    async def delete(self, uid, rid):
        row = self.rows.get(rid)
        if not row or row["uid"] != uid or row["sent"]:
            return False
        del self.rows[rid]
        return True

    async def delete_series(self, uid, sid):
        if sid not in self.series or self.series[sid]["telegram_user_id"] != uid:
            return False
        del self.series[sid]
        for rid in [r["id"] for r in self.rows.values() if r["series_id"] == sid]:
            del self.rows[rid]
        return True

    async def reschedule(self, uid, rid, due):
        row = self.rows.get(rid)
        if not row or row["uid"] != uid or row["sent"]:
            return False
        row["due_at"] = due
        return True

    async def last_fired(self, uid, within):
        fired = [
            r
            for r in self.rows.values()
            if r["uid"] == uid
            and r["fired_at"]
            and not r["done_at"]
            and r["fired_at"] >= self.now - within
        ]
        return max(fired, key=lambda r: r["fired_at"]) if fired else None

    async def mark_done(self, rid):
        self.rows[rid]["done_at"] = self.now

    async def mark_fired(self, rid):
        self.rows[rid]["fired_at"] = self.rows[rid]["fired_at"] or self.now

    async def mark_sent(self, rid):
        self.rows[rid]["sent"] = True
        await self.mark_fired(rid)

    def fire(self, rid, minutes_ago=1):
        self.rows[rid]["sent"] = True
        self.rows[rid]["fired_at"] = self.now - dt.timedelta(minutes=minutes_ago)


@pytest.fixture
def fake(monkeypatch):
    store = FakeDB()
    for name, fn in {
        "add_reminder": store.add_reminder,
        "reminder_count_pending_once": store.count_once,
        "reminder_count_series": store.count_series,
        "reminder_create_series": store.create_series,
        "reminder_get_series": store.get_series,
        "reminder_add_occurrence": store.add_occurrence,
        "reminder_list_pending_once": store.list_once,
        "reminder_list_series": store.list_series,
        "reminder_delete": store.delete,
        "reminder_delete_series": store.delete_series,
        "reminder_reschedule": store.reschedule,
        "reminder_last_fired": store.last_fired,
        "reminder_mark_done": store.mark_done,
        "reminder_mark_fired": store.mark_fired,
        "mark_reminder_sent": store.mark_sent,
    }.items():
        monkeypatch.setattr(db, name, fn)
    return store


@pytest.mark.asyncio
async def test_nhac_dat_xem_huy(fake):
    out = await reminder_commands.nhac(1, "8h tối mai gọi mẹ")
    assert out.startswith("✅ Đã đặt nhắc 1 lần lúc 20:00") and out.endswith("Gọi mẹ")
    await reminder_commands.nhac(1, "7h sáng mỗi ngày uống thuốc")

    listing = await reminder_commands.nhac(1, "")
    assert "1. " in listing and "2. " in listing
    assert "07:00 hằng ngày" in listing and "Gọi mẹ" in listing

    assert "Đã huỷ nhắc lặp" in await reminder_commands.nhac(1, "huy uống thuốc")
    assert not fake.series and all(r["series_id"] is None for r in fake.rows.values())
    assert "Đã huỷ nhắc việc: Gọi mẹ" in await reminder_commands.nhac(1, "huỷ 1")
    assert "chưa có nhắc việc" in await reminder_commands.nhac(1, "")


@pytest.mark.asyncio
async def test_nhac_huy_mo_ho_thi_hoi_lai_va_khong_dung_nham_nguoi(fake):
    await reminder_commands.nhac(1, "8h tối mai gọi mẹ")
    await reminder_commands.nhac(1, "9h tối mai gọi mẹ lần nữa")
    assert "Có 2 nhắc việc khớp" in await reminder_commands.nhac(1, "huy gọi mẹ")
    # Người khác (Zalo -5) không thấy/huỷ được nhắc của chủ bot.
    assert "không thấy" in await reminder_commands.nhac(-5, "huy 1")
    assert len(fake.rows) == 2


@pytest.mark.asyncio
async def test_nhac_hoan_doi_gio(fake):
    await reminder_commands.nhac(1, "8h tối mai gọi mẹ")
    out = await reminder_commands.nhac(1, "hoan 1 30p")
    assert "Đã dời sang 20:30" in out
    assert next(iter(fake.rows.values()))["due_at"].strftime("%H:%M") == "20:30"


@pytest.mark.asyncio
async def test_nhac_chu_dau_trung_huy_hoan_khi_bo_dau_van_dat_nhac(fake):
    """bỏ/bố, Huy, đổi/đợi bỏ dấu trùng lệnh huỷ/hoãn: vẫn là đặt nhắc mới, không
    huỷ/dời nhầm 1 nhắc khác."""
    await reminder_commands.nhac(1, "9h tối mai thay bình lọc nước")
    old = {rid: dict(r) for rid, r in fake.rows.items()}
    for argument in (
        "bỏ rác 7h tối",
        "bố uống thuốc 8h tối",
        "Huy gửi hồ sơ 9h sáng mai",
        "đổi bình gas 9h sáng mai",
        "đợi Lan 5h chiều",
    ):
        out = await reminder_commands.nhac(1, argument)
        assert out.startswith("✅ Đã đặt nhắc"), (argument, out)
    assert len(fake.rows) == 6
    for rid, row in old.items():
        assert fake.rows[rid]["due_at"] == row["due_at"]  # nhắc cũ không bị dời/huỷ


@pytest.mark.asyncio
async def test_nhac_lenh_huy_hoan_co_dau_va_khong_dau(fake):
    for text in ("8h tối mai gọi mẹ", "9h tối mai uống thuốc", "10h tối mai tắt máy"):
        await reminder_commands.nhac(1, text)
    assert "Đã dời sang 20:30" in await reminder_commands.nhac(1, "hoan 1 30p")
    assert "Đã dời sang 08:00" in await reminder_commands.nhac(1, "hoãn 2 sáng mai")
    # Sau khi dời: 1. uống thuốc 08:00, 2. gọi mẹ 20:30, 3. tắt máy 22:00.
    assert "Đã huỷ nhắc việc: Tắt máy" in await reminder_commands.nhac(1, "Huy 3")
    assert "Đã huỷ nhắc việc: Gọi mẹ" in await reminder_commands.nhac(1, "bỏ 2")
    assert "Đã huỷ nhắc việc: Uống thuốc" in await reminder_commands.nhac(1, "xoá uống thuốc")
    assert "chưa có nhắc việc" in await reminder_commands.nhac(1, "")


@pytest.mark.asyncio
async def test_tran_so_nhac_dang_cho(fake, monkeypatch):
    async def full(uid):
        return reminder_service.MAX_PENDING_ONCE

    monkeypatch.setattr(db, "reminder_count_pending_once", full)
    out = await reminder_commands.nhac(1, "8h tối mai gọi mẹ")
    assert out.startswith("❌") and "100" in out


@pytest.mark.asyncio
async def test_nhac_khong_doc_duoc_gio_thi_huong_dan(fake):
    out = await reminder_commands.nhac(1, "gọi mẹ")
    assert "chưa thấy thời điểm" in out and not fake.rows


@pytest.mark.asyncio
async def test_loi_tat_hoan_va_xong(fake):
    await reminder_service.create(1, "uống thuốc", fake.now + dt.timedelta(minutes=5))
    rid = next(iter(fake.rows))
    assert await reminder_service.maybe_handle_shortcut(1, "xong") is None  # chưa nhắc

    fake.fire(rid)
    with notification_target(NotificationTarget("telegram", "1", event_key="telegram:9")):
        reply = await reminder_service.maybe_handle_shortcut(1, "hoãn 10p")
    assert reply.startswith("⏰ Dạ em hoãn tới") and "uống thuốc" in reply
    snoozed = [r for r in fake.rows.values() if not r["sent"]]
    assert len(snoozed) == 1 and snoozed[0]["event_key"] == "telegram:9:hoan"
    assert fake.rows[rid]["done_at"] is not None
    # Lần nhắc gốc đã "xử lý" -> "xong" ngay sau không áp vào nó nữa.
    assert await reminder_service.maybe_handle_shortcut(1, "xong") is None

    fake.fire(snoozed[0]["id"])
    assert "ghi nhận xong: uống thuốc" in await reminder_service.maybe_handle_shortcut(
        1, "xong rồi"
    )


@pytest.mark.asyncio
async def test_loi_tat_hoan_vuot_tran_hoac_loi_db_thi_tra_loi_ro(fake, monkeypatch):
    await reminder_service.create(1, "uống thuốc", fake.now + dt.timedelta(minutes=5))
    rid = next(iter(fake.rows))
    fake.fire(rid)

    async def full(uid):
        return reminder_service.MAX_PENDING_ONCE

    monkeypatch.setattr(db, "reminder_count_pending_once", full)
    reply = await reminder_service.maybe_handle_shortcut(1, "hoãn 10p")
    assert reply.startswith("❌") and "100" in reply
    assert fake.rows[rid]["done_at"] is None  # chưa hoãn được -> lần nhắc gốc chưa "xong"

    async def broken(*args, **kwargs):
        raise ConnectionError("DB rớt mô phỏng")

    monkeypatch.setattr(db, "reminder_count_pending_once", broken)
    reply = await reminder_service.maybe_handle_shortcut(1, "hoãn 10p")
    assert reply.startswith("⚠️") and "chưa hoãn được" in reply
    assert fake.rows[rid]["done_at"] is None

    monkeypatch.setattr(db, "reminder_mark_done", broken)
    reply = await reminder_service.maybe_handle_shortcut(1, "xong")
    assert reply.startswith("⚠️") and "chưa ghi nhận" in reply


@pytest.mark.asyncio
async def test_nut_hoan_vuot_tran_thi_tra_loi_ro(fake, monkeypatch):
    await reminder_service.create(1, "uống thuốc", fake.now + dt.timedelta(minutes=5))
    rid = next(iter(fake.rows))
    fake.fire(rid)

    async def get(uid, reminder_id):
        return fake.rows.get(reminder_id)

    async def full(uid):
        return reminder_service.MAX_PENDING_ONCE

    monkeypatch.setattr(db, "reminder_get", get)
    monkeypatch.setattr(db, "reminder_count_pending_once", full)
    reply = await reminder_service.snooze_delivered(1, rid, "10")
    assert reply.startswith("❌") and "100" in reply
    assert fake.rows[rid]["done_at"] is None


@pytest.mark.asyncio
async def test_loi_tat_khong_bat_nham_cau_thuong(fake):
    await reminder_service.create(1, "uống thuốc", fake.now + dt.timedelta(minutes=5))
    fake.fire(next(iter(fake.rows)))
    for text in ("hoàn thành báo cáo chưa em", "hoãn chuyến đi Đà Lạt", "xong chưa em", "ok"):
        assert await reminder_service.maybe_handle_shortcut(1, text) is None, text
    # Quá 30 phút sau khi nhắc: lối tắt không áp dụng.
    fake.rows[next(iter(fake.rows))]["fired_at"] = fake.now - dt.timedelta(minutes=45)
    assert await reminder_service.maybe_handle_shortcut(1, "xong") is None


@pytest.mark.asyncio
async def test_loi_tat_xung_ho_thanh_vien(fake):
    from services import reply_audience

    with notification_target(NotificationTarget("zalo", "z-me", "acc")):
        await reminder_service.create(-5, "uống thuốc", fake.now + dt.timedelta(minutes=5))
    fake.fire(next(iter(fake.rows)))
    with reply_audience.use(reply_audience.ReplyAudience(address_form="cô")):
        reply = await reminder_service.maybe_handle_shortcut(-5, "xong")
    assert reply.startswith("✅ Dạ cháu ghi nhận xong")


# ─── Scheduler: lần kế tiếp của chuỗi, nhãn nhắc trễ ───────────────────────


@pytest.mark.asyncio
async def test_scheduler_chen_lan_ke_tiep_va_nhan_nhac_tre(fake, monkeypatch):
    sent = []

    async def notify(uid, text):
        sent.append(text)

    scheduler.set_notify_callback(notify)
    try:
        rule = {"type": "daily", "at": "07:00"}
        old_due = dt.datetime.combine(fake.now.date() - dt.timedelta(days=3), dt.time(7), tzinfo=VN)
        sid, _ = await fake.create_series(
            1,
            "uống thuốc",
            kind="reminder",
            category="",
            recurrence=rule,
            lead_days=0,
            channel="telegram",
            recipient_id="1",
            account_id="",
            user_jid="",
            event_key=None,
            first=[(old_due, "uống thuốc")],
        )
        rid = next(r["id"] for r in fake.rows.values())
        due = idempotency.DueReminder(rid, 1, "uống thuốc", "telegram", "1", "", "", old_due, sid)
        await scheduler._process_due_reminders([due])
        await scheduler._process_due_reminders([due])  # claim lại: không sinh thêm lần nào
    finally:
        scheduler._notify_callback = None

    pending = [r for r in fake.rows.values() if not r["sent"]]
    # Máy chủ ngủ 3 ngày: chỉ đúng 1 lần kế tiếp, sau bây giờ (không dồn tin bù).
    assert len(pending) == 1 and pending[0]["due_at"] > dt.datetime.now(VN)
    assert pending[0]["due_at"].strftime("%H:%M") == "07:00"
    assert f"(nhắc trễ, lịch gốc 07:00 {old_due:%d/%m})" in sent[0]
    assert sent[0].endswith(reminder_service.REPLY_HINT)
    assert fake.rows[rid]["sent"] and fake.rows[rid]["fired_at"]


def test_nhan_tre_chi_khi_tre_qua_10_phut():
    now = dt.datetime(2026, 10, 26, 8, 0, tzinfo=VN)
    assert reminder_service.late_label(now - dt.timedelta(minutes=5), now) == ""
    assert reminder_service.late_label(None, now) == ""
    assert reminder_service.late_label(now - dt.timedelta(minutes=45), now) == (
        " (nhắc trễ, lịch gốc 07:15)"
    )
    text = reminder_service.delivery_text("🗓 Hôm nay là Giỗ ông", "ngay_nho", now, now)
    assert text.startswith("🗓 Hôm nay là Giỗ ông\n")  # ngày nhớ không thêm "⏰ Nhắc việc:"


# ─── Ngày nhớ ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ngaynho_them_xem_xoa(fake):
    out = await reminder_commands.ngaynho(1, "giỗ ông nội 12/3 âm trước 3 ngày")
    assert out.startswith("✅ Đã lưu ngày nhớ: Giỗ ông nội - 12/3 âm lịch")
    assert "báo trước 3 ngày" in out and "tháng thường" in out
    series = next(iter(fake.series.values()))
    assert series["kind"] == "ngay_nho" and series["category"] == "gio"
    first = next(iter(fake.rows.values()))
    assert first["message"].startswith("🗓 Còn 3 ngày nữa là Giỗ ông nội")

    await reminder_commands.ngaynho(1, "sinh nhật vợ 20/11/1990")
    listing = await reminder_commands.ngaynho(1, "")
    assert "Giỗ ông nội" in listing and "Sinh nhật vợ" in listing and "tròn" in listing
    # Ngày nhớ không lẫn vào danh sách /nhac.
    assert "Giỗ" not in await reminder_commands.nhac(1, "")

    assert "Đã xoá ngày nhớ" in await reminder_commands.ngaynho(1, "xoa giỗ")
    assert len(fake.series) == 1


@pytest.mark.asyncio
async def test_ngaynho_thieu_ngay_thi_huong_dan(fake):
    out = await reminder_commands.ngaynho(1, "giỗ ông nội")
    assert out.startswith("❌") and "ngày-tháng" in out and not fake.series


def test_ngaynho_lan_bao_truoc_roi_dung_ngay():
    series = {
        "message": "Sinh nhật vợ",
        "category": "sinh_nhat",
        "recurrence": {"type": "yearly", "month": 11, "day": 20, "at": "08:00", "year": 1990},
        "lead_days": 2,
    }
    before = dt.datetime(2026, 11, 1, tzinfo=VN)
    due, text = ngay_nho.next_row(series, before)
    assert due == dt.datetime(2026, 11, 18, 8, tzinfo=VN)
    assert text == "🗓 Còn 2 ngày nữa là Sinh nhật vợ (20/11, Thứ Sáu 20/11/2026) - tròn 36 tuổi."
    due, text = ngay_nho.next_row(series, due)
    assert due == dt.datetime(2026, 11, 20, 8, tzinfo=VN)
    assert text.startswith("🗓 Hôm nay là Sinh nhật vợ")
    due, _ = ngay_nho.next_row(series, due)
    assert due == dt.datetime(2027, 11, 18, 8, tzinfo=VN)


@pytest.mark.asyncio
async def test_ban_tin_sang_co_muc_ngay_nho(fake):
    today = dt.datetime.now(VN).date()
    soon = today + dt.timedelta(days=3)
    spec = ngay_nho.SpecialDate(
        "Sinh nhật mẹ",
        "sinh_nhat",
        {"type": "yearly", "month": soon.month, "day": soon.day, "at": "08:00"},
        1,
    )
    with notification_target(NotificationTarget("zalo", "z-me", "acc")):
        await ngay_nho.add(-5, spec)
    section = next(s for s in morning_brief.sections() if s.key == "ngay_nho")
    ctx = morning_brief.BriefContext(
        user_id=-5,
        channel="zalo",
        now=dt.datetime.now(VN),
        settings=morning_brief.BriefSettings(on=True),
        is_owner=False,
    )
    text = await section.build(ctx)
    assert text.startswith("🗓 Ngày nhớ: Sinh nhật mẹ còn 3 ngày")
    other = morning_brief.BriefContext(1, "telegram", ctx.now, ctx.settings, True)
    assert await section.build(other) is None  # dữ liệu Zalo không sang Telegram
