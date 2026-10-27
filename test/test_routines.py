"""Tác vụ định kỳ bằng lời (services/routines.py): câu tự nhiên chỉ nhận khi chắc
(không bắt nhầm câu nhắc việc), hỏi lại khi thiếu "mỗi", lệnh /lichtacvu, trần mỗi
người và toàn bot, chạy bù đúng 1 lần, thành viên Zalo mặc định tắt, mục /chuy."""

import datetime as dt
from types import SimpleNamespace

import pytest

import test_background_jobs as _jobs
from core import database as db
from services import background_jobs as bg
from services import channel_chat_service, recurrence, routines, vn_time_parse

jobs = _jobs.jobs
OWNER = _jobs.OWNER
VN = routines.VN_TZ
NOW = dt.datetime(2026, 10, 26, 10, 0, tzinfo=VN)  # Thứ Hai


class FakeRoutines:
    def __init__(self, jobs_db) -> None:
        self.rows: dict[int, dict] = {}
        self.next_id = 1
        self.jobs = jobs_db
        self.runs_today = 0

    async def count(self, uid):
        return sum(1 for r in self.rows.values() if r["telegram_user_id"] == uid)

    async def insert(self, uid, channel, prompt, rule, next_run_at):
        rid = self.next_id
        self.next_id += 1
        self.rows[rid] = {
            "id": rid,
            "telegram_user_id": uid,
            "channel": channel,
            "prompt": prompt,
            "recurrence": rule,
            "status": "active",
            "next_run_at": next_run_at,
            "last_run_at": None,
        }
        return rid

    async def list(self, uid):
        return [dict(r) for r in self.rows.values() if r["telegram_user_id"] == uid]

    async def get(self, uid, rid):
        row = self.rows.get(rid)
        return dict(row) if row and row["telegram_user_id"] == uid else None

    async def delete(self, uid, rid):
        row = self.rows.get(rid)
        if row and row["telegram_user_id"] == uid:
            del self.rows[rid]
            return True
        return False

    async def set_status(self, uid, rid, status, next_run_at=None):
        row = self.rows.get(rid)
        if not row or row["telegram_user_id"] != uid:
            return False
        row["status"] = status
        if next_run_at is not None:
            row["next_run_at"] = next_run_at
        return True

    async def due(self, now, limit=20):
        return [
            dict(r)
            for r in self.rows.values()
            if r["status"] == "active" and r["next_run_at"] and r["next_run_at"] <= now
        ]

    async def fire(self, routine, scheduled, next_run_at, *, enqueue):
        row = self.rows.get(routine["id"])
        if row is None or row["next_run_at"] != scheduled or row["status"] != "active":
            return None
        row["next_run_at"] = next_run_at
        if not enqueue:
            return None
        job = self.jobs.add(
            row["telegram_user_id"],
            row["prompt"],
            kind="routine",
            channel=row["channel"],
            routine_id=row["id"],
            scheduled_for=scheduled,
        )
        return job["id"]

    async def runs_since(self, since):
        return self.runs_today + sum(1 for r in self.jobs.rows.values() if r["kind"] == "routine")

    async def jobs_since(self, uid, since):
        return [
            dict(r)
            for r in self.jobs.rows.values()
            if r["telegram_user_id"] == uid and r["kind"] == "routine"
        ]


@pytest.fixture
def world(jobs, monkeypatch):
    fake = FakeRoutines(jobs.db)
    monkeypatch.setattr(db, "routine_count", fake.count)
    monkeypatch.setattr(db, "routine_insert", fake.insert)
    monkeypatch.setattr(db, "routine_list", fake.list)
    monkeypatch.setattr(db, "routine_get", fake.get)
    monkeypatch.setattr(db, "routine_delete", fake.delete)
    monkeypatch.setattr(db, "routine_set_status", fake.set_status)
    monkeypatch.setattr(db, "routine_due", fake.due)
    monkeypatch.setattr(db, "routine_fire", fake.fire)
    monkeypatch.setattr(db, "routine_runs_since", fake.runs_since)
    monkeypatch.setattr(db, "routine_jobs_since", fake.jobs_since)
    monkeypatch.setattr(routines, "_now", lambda: NOW)
    monkeypatch.setattr(routines, "_pending", routines.OrderedDict())
    monkeypatch.setattr(routines, "_cap_alert_day", None)
    for name in ("ROUTINES_ENABLED", "ROUTINES_MEMBERS", "ROUTINE_MAX_RUNS_PER_DAY"):
        monkeypatch.delenv(name, raising=False)
    alerts: list[str] = []
    monkeypatch.setattr("core.alerts.send", lambda text, **kw: alerts.append(text))
    return SimpleNamespace(routines=fake, jobs=jobs.db, sent=jobs.sent, alerts=alerts)


# ─── Đọc câu ────────────────────────────────────────────────────────────────


def test_moi_sang_thu_2_la_lap_hang_tuan_khong_phai_hang_ngay():
    when = vn_time_parse.parse("mỗi sáng thứ 2 8h tóm tắt tin VNM", NOW)
    assert when.recurrence == {"type": "weekly", "at": "08:00", "days": [0]}
    when = vn_time_parse.parse("nhắc anh mỗi tối thứ 6 9h đổ rác", NOW)
    assert when.recurrence == {"type": "weekly", "at": "21:00", "days": [4]}
    # "mỗi ngày" vẫn là hằng ngày; "mỗi sáng 7h" không kèm thứ cũng vậy.
    assert vn_time_parse.parse("mỗi sáng 7h uống thuốc", NOW).recurrence["type"] == "daily"


@pytest.mark.parametrize(
    "text",
    [
        "nhắc anh mỗi sáng 8h kiểm tra email",  # nhắc việc: gửi câu cố định
        "kiểm tra giúp anh 8h tối nay có mưa không",  # câu hỏi 1 lần
        "tóm tắt bài này cho anh",  # không có mốc thời gian
        "mỗi sáng 8h trả tiền điện cho anh",  # "trả" không phải "tra"
        "hôm qua em tổng kết chi tiêu cho anh chưa",
    ],
)
@pytest.mark.asyncio
async def test_khong_bat_nham_cau_thuong(world, text):
    assert await routines.maybe_handle_chat(OWNER, text, "telegram", now=NOW) is None
    assert world.routines.rows == {}


@pytest.mark.asyncio
async def test_cau_tu_nhien_tao_tac_vu(world):
    reply = await routines.maybe_handle_chat(
        OWNER, "mỗi sáng thứ 2 8h tóm tắt tin về VNM cho anh", "telegram", now=NOW
    )
    assert reply and "#1" in reply and "hằng tuần (T2)" in reply
    row = world.routines.rows[1]
    assert row["prompt"] == "Tóm tắt tin về VNM cho anh"
    assert row["recurrence"] == {"type": "weekly", "at": "08:00", "days": [0]}
    assert row["next_run_at"] == dt.datetime(2026, 11, 2, 8, 0, tzinfo=VN)


@pytest.mark.asyncio
async def test_thieu_moi_thi_hoi_lai_ok_moi_dat(world):
    text = "tối thứ 6 9h tổng kết chi tiêu tuần cho anh"
    question = await routines.maybe_handle_chat(OWNER, text, "telegram", now=NOW)
    assert question and "HẰNG TUẦN" in question and "ok" in question
    assert world.routines.rows == {}
    created = await routines.maybe_handle_chat(OWNER, "ok em", "telegram", now=NOW)
    assert created and "#1" in created
    assert world.routines.rows[1]["recurrence"] == {"type": "weekly", "at": "21:00", "days": [4]}
    # Hỏi lại lần nữa rồi nói "thôi": không đặt.
    await routines.maybe_handle_chat(OWNER, text, "telegram", now=NOW)
    assert "không đặt" in await routines.maybe_handle_chat(OWNER, "thôi", "telegram", now=NOW)
    assert len(world.routines.rows) == 1


@pytest.mark.asyncio
async def test_thieu_gio_thi_hoi_gio(world):
    reply = await routines.maybe_handle_chat(
        OWNER, "mỗi ngày tóm tắt tin vàng cho anh", "telegram", now=NOW
    )
    assert reply and "mấy giờ" in reply and world.routines.rows == {}


@pytest.mark.asyncio
async def test_chat_kenh_dung_chung_dua_cau_vao_tac_vu_truoc_router(world, monkeypatch):
    async def none(*args, **kwargs):
        return None

    async def record(*args, **kwargs):
        return True

    monkeypatch.setattr(channel_chat_service.style_approval, "maybe_handle_reply", none)
    monkeypatch.setattr(channel_chat_service.portfolio_service, "handle_command", none)
    monkeypatch.setattr(channel_chat_service.reminder_service, "maybe_handle_shortcut", none)
    monkeypatch.setattr(channel_chat_service.conversation_log, "record_exchange", record)

    async def no_tool(*args, **kwargs):
        raise AssertionError("không được tới router AI / nhắc việc")

    monkeypatch.setattr(channel_chat_service.tools, "maybe_run_tool", no_tool)
    result = await channel_chat_service.handle_channel_text(
        -6, "mỗi tối thứ 6 9h tổng kết chi tiêu tuần cho anh", True, "zalo"
    )
    assert "tác vụ định kỳ #1" in result.messages[0]
    assert world.routines.rows[1]["telegram_user_id"] == -6


# ─── Lệnh /lichtacvu ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lenh_tao_xem_tam_dung_tiep_huy(world):
    cmd = routines.handle_command
    created = await cmd("/lichtacvu", "mỗi ngày 7h kiểm tra giá vàng", OWNER, "telegram", now=NOW)
    assert "#1" in created[0]
    listing = (await cmd("/lichtacvu", "", OWNER, "telegram", now=NOW))[0]
    assert "#1" in listing and "07:00 hằng ngày" in listing and "lần tới" in listing
    assert "tạm dừng" in (await cmd("/lichtacvu", "tam dung 1", OWNER, "telegram", now=NOW))[0]
    assert world.routines.rows[1]["status"] == "paused"
    assert "⏸" in (await cmd("/lichtacvu", "", OWNER, "telegram", now=NOW))[0]
    resumed = (await cmd("/lichtacvu", "tiếp 1", OWNER, "telegram", now=NOW))[0]
    assert "chạy lại" in resumed and world.routines.rows[1]["status"] == "active"
    assert world.routines.rows[1]["next_run_at"] == dt.datetime(2026, 10, 27, 7, 0, tzinfo=VN)
    assert "không thấy" in (await cmd("/lichtacvu", "huy 1", -5, "zalo", is_admin=True))[0]
    assert "huỷ" in (await cmd("/lichtacvu", "huỷ 1", OWNER, "telegram", now=NOW))[0]
    assert world.routines.rows == {}


@pytest.mark.asyncio
async def test_lenh_khong_co_moc_lap_lai_thi_huong_dan(world):
    reply = await routines.handle_command(
        "/lichtacvu", "8h tối nay tóm tắt tin", OWNER, "telegram", now=NOW
    )
    assert "mốc lặp lại" in reply[0] and "/viec" in reply[0] and not world.routines.rows


@pytest.mark.asyncio
async def test_tran_10_tac_vu_moi_nguoi(world):
    for i in range(routines.MAX_PER_USER):
        await routines.handle_command(
            "/lichtacvu", f"mỗi ngày {i + 1}h kiểm tra việc {i}", OWNER, "telegram", now=NOW
        )
    assert len(world.routines.rows) == routines.MAX_PER_USER
    refused = await routines.handle_command(
        "/lichtacvu", "mỗi ngày 23h kiểm tra thêm", OWNER, "telegram", now=NOW
    )
    assert "tối đa 10" in refused[0] and len(world.routines.rows) == routines.MAX_PER_USER


def test_khoang_cach_toi_thieu_1_gio():
    assert routines.interval_ok({"type": "daily", "at": "08:00"}, NOW)
    assert routines.interval_ok({"type": "weekly", "days": [0, 1], "at": "08:00"}, NOW)


@pytest.mark.asyncio
async def test_thanh_vien_zalo_mac_dinh_tat(world, monkeypatch):
    text = "mỗi sáng 8h tóm tắt tin vàng cho chị"
    assert await routines.maybe_handle_chat(-5, text, "zalo", now=NOW) is None
    refused = await routines.handle_command("/lichtacvu", text, -5, "zalo")
    assert "chưa mở" in refused[0] and world.routines.rows == {}
    # Admin Zalo dùng được; ROUTINES_MEMBERS=1 mở cho thành viên.
    assert "#1" in await routines.maybe_handle_chat(-6, text, "zalo", is_admin=True, now=NOW)
    monkeypatch.setenv("ROUTINES_MEMBERS", "1")
    assert "#2" in await routines.maybe_handle_chat(-5, text, "zalo", now=NOW)


# ─── Vòng chạy ──────────────────────────────────────────────────────────────


async def _add(world, uid=OWNER, *, rule=None, next_run_at, channel="telegram"):
    rule = rule or {"type": "daily", "at": "08:00"}
    return await world.routines.insert(uid, channel, "tóm tắt tin vàng", rule, next_run_at)


@pytest.mark.asyncio
async def test_toi_gio_thi_xep_viec_nen_va_doi_lich(world):
    rid = await _add(world, next_run_at=NOW - dt.timedelta(minutes=1))
    assert await routines.run_tick(NOW) == 1
    job = world.jobs.rows[1]
    assert job["kind"] == "routine" and job["routine_id"] == rid
    assert world.routines.rows[rid]["next_run_at"] == dt.datetime(2026, 10, 27, 8, 0, tzinfo=VN)
    assert await routines.run_tick(NOW) == 0  # đã dời lịch: không xếp lần 2


@pytest.mark.asyncio
async def test_lo_gio_chi_chay_bu_1_lan(world):
    # Bot ngủ 3 tiếng qua giờ hẹn (trong cửa sổ chạy bù): chạy đúng 1 lần.
    rid = await _add(world, next_run_at=NOW - dt.timedelta(hours=3))
    assert await routines.run_tick(NOW) == 1
    assert world.routines.rows[rid]["next_run_at"] > NOW
    assert await routines.run_tick(NOW) == 0
    assert len(world.jobs.rows) == 1


@pytest.mark.asyncio
async def test_lo_gio_qua_lau_thi_bo_lan_do(world):
    rid = await _add(world, next_run_at=NOW - dt.timedelta(days=2))
    assert await routines.run_tick(NOW) == 0
    assert world.jobs.rows == {} and world.routines.rows[rid]["next_run_at"] > NOW


@pytest.mark.asyncio
async def test_tran_luot_chay_moi_ngay_toan_bot(world, monkeypatch):
    monkeypatch.setenv("ROUTINE_MAX_RUNS_PER_DAY", "2")
    for _ in range(3):
        await _add(world, next_run_at=NOW - dt.timedelta(minutes=1))
    assert await routines.run_tick(NOW) == 2
    assert len(world.jobs.rows) == 2 and len(world.alerts) == 1
    assert all(r["next_run_at"] > NOW for r in world.routines.rows.values())


@pytest.mark.asyncio
async def test_thanh_vien_het_quyen_thi_bo_lan_chay(world, monkeypatch):
    await _add(world, -5, next_run_at=NOW - dt.timedelta(minutes=1), channel="zalo")
    assert await routines.run_tick(NOW) == 0  # ROUTINES_MEMBERS=0 (mặc định)
    monkeypatch.setenv("ROUTINES_MEMBERS", "1")

    async def exhausted(user_id, **kwargs):
        return True

    monkeypatch.setattr("services.usage_quota.heavy_exhausted", exhausted)
    await _add(world, -5, next_run_at=NOW - dt.timedelta(minutes=1), channel="zalo")
    assert await routines.run_tick(NOW) == 0 and world.jobs.rows == {}


@pytest.mark.asyncio
async def test_tac_vu_chay_qua_bo_chay_nen_va_gui_normal(world, monkeypatch):
    seen = {}

    async def run_chat_turn(user_id, text, **kwargs):
        seen.update(kwargs)
        return _jobs._turn("Giá vàng SJC hôm nay 120 triệu")

    monkeypatch.setattr("services.channel_chat_service.run_chat_turn", run_chat_turn)
    await _add(world, next_run_at=NOW - dt.timedelta(minutes=1))
    await routines.run_tick(NOW)
    job = await world.jobs.claim(bg.LEASE_SEC)
    outcome = await bg.run_job(job)
    assert outcome.status == bg.SUCCEEDED
    assert seen["allow_side_effects"] is False and seen["telemetry_kind"] == "routine"
    assert "NO_REPLY" in seen["style_hint"]
    assert world.sent[0]["source"] == "routine"
    assert "Tác vụ định kỳ #1" in world.sent[0]["text"]


# ─── /chuy ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_muc_chuy_tac_vu_hom_nay(world):
    from services import attention

    await _add(world, next_run_at=NOW.replace(hour=21))
    done = await _add(world, next_run_at=NOW.replace(hour=8))
    world.jobs.add(
        OWNER,
        "tóm tắt tin vàng",
        kind="routine",
        routine_id=done,
        scheduled_for=NOW.replace(hour=8),
        status="succeeded",
        no_reply=True,
    )
    ctx = attention.AttentionContext(OWNER, "telegram", NOW, True, True)
    text = await routines._attention_routines(ctx)
    assert text and "Tác vụ định kỳ hôm nay" in text
    assert "08:00 #2" in text and "không có gì đáng báo" in text
    assert "21:00 #1" in text and "sắp chạy" in text
    assert text.index("08:00") < text.index("21:00")


def test_mo_ta_lich_dung_bo_lap_san_co():
    assert recurrence.describe({"type": "weekly", "days": [0], "at": "08:00"}) == (
        "08:00 hằng tuần (T2)"
    )
