"""Hỏi thăm chủ động dựa trên trí nhớ (services/proactive_service.py)."""

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core import config, database as db
from services import concurrency, proactive_service as ps

VN = ZoneInfo("Asia/Ho_Chi_Minh")
OWNER = 777


class FakeEventsDB:
    def __init__(self):
        self.settings = {}
        self.events = []
        self.chat = []
        self.recent_at = None  # created_at tin gần nhất (None = chưa chat)
        self.next_id = 1

    def install(self, monkeypatch):
        for name in (
            "get_setting",
            "set_setting",
            "add_user_event",
            "expire_user_events",
            "users_with_due_events",
            "claim_due_user_event",
            "release_user_event",
            "mark_user_event_asked",
            "finish_user_events",
            "list_user_events",
            "user_events_on",
            "get_recent_chat_rows",
            "add_chat_turn",
        ):
            monkeypatch.setattr(db, name, getattr(self, name))

    async def get_setting(self, key):
        return self.settings.get(key)

    async def set_setting(self, key, value):
        self.settings[key] = value

    async def add_user_event(self, uid, title, when_text, event_date):
        if any(
            e["uid"] == uid and e["event_date"] == event_date and e["title"] == title
            for e in self.events
        ):
            return False
        self.events.append(
            {
                "id": self.next_id,
                "uid": uid,
                "title": title,
                "when_text": when_text,
                "event_date": event_date,
                "status": "pending",
                "asked_at": None,
            }
        )
        self.next_id += 1
        return True

    async def expire_user_events(self, before):
        n = 0
        for e in self.events:
            if e["status"] == "pending" and e["event_date"] < before:
                e["status"] = "done"
                n += 1
        return n

    async def users_with_due_events(self, on_or_before, limit=20):
        return sorted(
            {
                e["uid"]
                for e in self.events
                if e["status"] == "pending" and e["event_date"] <= on_or_before
            }
        )

    async def claim_due_user_event(self, uid, on_or_before):
        for e in self.events:
            if e["uid"] == uid and e["status"] == "pending" and e["event_date"] <= on_or_before:
                e["status"] = "asked"
                return dict(e)
        return None

    async def release_user_event(self, event_id):
        for e in self.events:
            if e["id"] == event_id and e["status"] == "asked" and e["asked_at"] is None:
                e["status"] = "pending"

    async def mark_user_event_asked(self, event_id):
        for e in self.events:
            if e["id"] == event_id:
                e["asked_at"] = "now"

    async def finish_user_events(self, uid):
        n = 0
        for e in self.events:
            if e["uid"] == uid and e["status"] == "pending":
                e["status"] = "done"
                n += 1
        return n

    async def list_user_events(self, uid, limit=20):
        return [dict(e) for e in self.events if e["uid"] == uid][:limit]

    async def user_events_on(self, uid, day, limit=5):
        return [
            dict(e)
            for e in self.events
            if e["uid"] == uid and e["event_date"] == day and e["status"] != "done"
        ]

    async def get_recent_chat_rows(self, uid, limit=40, max_chars=1500):
        if self.recent_at is None:
            return []
        return [
            {"id": 1, "role": "model", "kind": "chat", "content": "", "created_at": self.recent_at}
        ]

    async def add_chat_turn(self, uid, user_text, model_text, *, kind="chat"):
        self.chat.append((uid, user_text, model_text, kind))


@pytest.fixture
def fake(monkeypatch):
    f = FakeEventsDB()
    f.install(monkeypatch)
    monkeypatch.setattr(config, "ALLOWED_USER_ID", OWNER)
    monkeypatch.delenv("PROACTIVE_FOLLOWUPS", raising=False)
    return f


def test_tinh_ngay_bang_code_theo_ngay_tin_nhan():
    monday = dt.date(2026, 10, 26)
    assert ps.resolve_when("mai", monday) == dt.date(2026, 10, 27)
    assert ps.resolve_when("sáng mai", monday) == dt.date(2026, 10, 27)
    assert ps.resolve_when("tối nay", monday) == monday
    assert ps.resolve_when("thứ 6 tuần sau", monday) == dt.date(2026, 11, 6)
    assert ps.resolve_when("20/11", monday) == dt.date(2026, 11, 20)
    assert ps.resolve_when("3 ngày nữa", monday) == dt.date(2026, 10, 29)
    assert ps.resolve_when("dạo này", monday) is None


@pytest.mark.asyncio
async def test_su_kien_phai_co_moc_thoi_gian_nguyen_van_va_trong_60_ngay(fake):
    now = dt.datetime.now(dt.timezone.utc)
    rows = [{"role": "user", "content": "mai anh đi phỏng vấn ở công ty X", "created_at": now}]
    events = [
        {"title": "phỏng vấn ở công ty X", "when_text": "mai"},
        {"title": "đi Đà Lạt", "when_text": "thứ 7"},  # không có trong lời người dùng -> bịa
        {"title": "sinh nhật", "when_text": ""},
    ]
    assert await ps.add_extracted_events(OWNER, events, rows) == 1
    assert fake.events[0]["title"] == "phỏng vấn ở công ty X"
    tomorrow = (now.astimezone(VN) + dt.timedelta(days=1)).date()
    assert fake.events[0]["event_date"] == tomorrow
    far = [{"role": "user", "content": "1/1/2099 anh đi du lịch", "created_at": now}]
    # Quá 60 ngày tới: không lưu.
    assert (
        await ps.add_extracted_events(OWNER, [{"title": "du lịch", "when_text": "1/1/2099"}], far)
        == 0
    )
    old = [{"role": "user", "content": "hôm qua anh đi khám", "created_at": now}]
    assert (
        await ps.add_extracted_events(OWNER, [{"title": "khám", "when_text": "hôm qua"}], old) == 0
    )


@pytest.mark.asyncio
async def test_ai_duoc_hoi_tham_chu_bot_mac_dinh_zalo_phai_tu_bat(fake, monkeypatch):
    today = dt.date(2026, 10, 26)
    assert await ps.events_enabled(OWNER, today=today)
    assert not await ps.events_enabled(-5, today=today)  # Zalo tắt mặc định
    monkeypatch.setenv("ZALO_ENABLED", "true")
    monkeypatch.setenv("ZALO_BRIDGE_SECRET", "s")
    assert not await ps.events_enabled(-5, today=today)
    await ps.set_followups(-5, True)
    assert await ps.events_enabled(-5, today=today)
    await ps.opt_out(OWNER, today=today)  # "đừng nhắc nữa" -> tạm dừng 7 ngày
    assert not await ps.events_enabled(OWNER, today=today)
    assert await ps.events_enabled(OWNER, today=today + dt.timedelta(days=ps.PAUSE_DAYS))
    monkeypatch.setenv("PROACTIVE_FOLLOWUPS", "0")
    assert not await ps.events_enabled(OWNER, today=today)
    assert not await ps.add_extracted_events(-6, [{"title": "x", "when_text": "mai"}], [])


@pytest.fixture
def delivery(fake, monkeypatch):
    sent = []

    async def notify(uid, text):
        sent.append((uid, text))

    async def compose(event, now, form):
        return f"{form}: {event['title']} thế nào rồi?"

    monkeypatch.setattr(ps, "_notify_callback", notify)
    monkeypatch.setattr(ps, "_compose", compose)
    return sent


EVENING = dt.datetime(2026, 10, 27, 19, 0, tzinfo=VN)


@pytest.mark.asyncio
async def test_tick_gui_telegram_luu_lich_su_va_co_tran_moi_ngay(fake, delivery, monkeypatch):
    monkeypatch.setenv("PROACTIVE_DAILY_CAP", "1")
    await fake.add_user_event(OWNER, "phỏng vấn", "mai", EVENING.date())
    await fake.add_user_event(OWNER, "đi khám", "mai", EVENING.date())
    assert await ps.run_tick(EVENING) == 1
    assert delivery == [(OWNER, "anh: phỏng vấn thế nào rồi?")]
    assert fake.chat == [(OWNER, ps.FOLLOWUP_USER_LABEL, "anh: phỏng vấn thế nào rồi?", "followup")]
    assert await ps.run_tick(EVENING + dt.timedelta(minutes=30)) == 0  # đủ trần hôm nay
    assert [e["status"] for e in fake.events] == ["asked", "pending"]


@pytest.mark.asyncio
async def test_tick_ngoai_gio_hoat_dong_truoc_gio_hoi_va_khi_vua_chat_thi_bo_qua(fake, delivery):
    await fake.add_user_event(OWNER, "phỏng vấn", "mai", EVENING.date())
    assert await ps.run_tick(EVENING.replace(hour=22, minute=30)) == 0  # ngoài 08:00-21:30
    assert await ps.run_tick(EVENING.replace(hour=10)) == 0  # sự kiện hôm nay: chờ tới 18h
    fake.recent_at = dt.datetime.now(dt.timezone.utc)  # vừa chat < PROACTIVE_IDLE_MIN
    assert await ps.run_tick(EVENING) == 0
    assert delivery == [] and fake.events[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_tick_bo_qua_khi_dang_co_luot_chay(fake, delivery):
    await fake.add_user_event(OWNER, "phỏng vấn", "mai", EVENING.date())
    async with concurrency.assistant_turn(concurrency.OWNER_TURN_KEY):
        assert concurrency.is_busy(concurrency.OWNER_TURN_KEY)
        assert await ps.run_tick(EVENING) == 0
    assert not concurrency.is_busy(concurrency.OWNER_TURN_KEY)
    assert fake.events[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_no_reply_khong_gui_gui_loi_tra_claim(fake, delivery, monkeypatch):
    await fake.add_user_event(OWNER, "chuyện buồn", "mai", EVENING.date())

    async def no_reply(event, now, form):
        return None

    monkeypatch.setattr(ps, "_compose", no_reply)
    assert await ps.run_tick(EVENING) == 0
    assert fake.events[0]["asked_at"] == "now" and delivery == []

    await fake.add_user_event(OWNER, "phỏng vấn", "mai", EVENING.date())

    async def compose(event, now, form):
        return "Phỏng vấn sao rồi anh?"

    async def broken_notify(uid, text):
        raise RuntimeError("telegram lỗi")

    monkeypatch.setattr(ps, "_compose", compose)
    monkeypatch.setattr(ps, "_notify_callback", broken_notify)
    assert await ps.run_tick(EVENING) == 0
    assert fake.events[1]["status"] == "pending"  # trả claim, lượt sau thử lại


@pytest.mark.asyncio
async def test_zalo_gui_qua_outbox_cua_chinh_nguoi_do_khong_sang_telegram(
    fake, delivery, monkeypatch
):
    from channels import zalo_repository, zalo_users

    monkeypatch.setenv("ZALO_ENABLED", "true")
    monkeypatch.setenv("ZALO_BRIDGE_SECRET", "s")
    await ps.set_followups(-5, True)
    await fake.add_user_event(-5, "thi lái xe", "mai", EVENING.date())
    outbox = []

    async def users():
        return [
            SimpleNamespace(
                internal_user_id=-5, external_id="z5", is_active=True, address_form="chị"
            )
        ]

    async def account():
        return "acc"

    async def enqueue(account_id, recipient, text):
        outbox.append((account_id, recipient, text))

    monkeypatch.setattr(zalo_users, "list_users", users)
    monkeypatch.setattr(zalo_repository, "resolve_active_account_id", account)
    monkeypatch.setattr(zalo_repository, "enqueue_outbox", enqueue)
    assert await ps.run_tick(EVENING) == 1
    assert outbox == [("acc", "z5", "chị: thi lái xe thế nào rồi?")]
    assert delivery == []  # không bao giờ giao sự kiện Zalo sang Telegram


@pytest.mark.asyncio
async def test_dung_nhac_nua_sau_tin_hoi_tham_tat_tam(fake, monkeypatch):
    from services import channel_chat_service as ccs

    await fake.add_user_event(OWNER, "đi khám", "thứ 6", dt.date(2026, 10, 30))
    seen = {}

    async def telemetry_start(*args, **kwargs):
        return 1

    async def noop(*args, **kwargs):
        return None

    async def plan(text, user_id=None):
        return ccs.web_search.SearchPlan()

    async def memory(user_id, query=""):
        return ccs.memory_service.TurnMemory()

    async def last_row(user_id):
        return {"kind": "followup", "content": "Phỏng vấn sao rồi anh?"}

    async def fake_chat(user_id, text, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(text="Dạ em biết rồi ạ.", used_fallback=False)

    async def no_daily(text):
        return ccs.vn_daily_chat.DailyGrounding()

    monkeypatch.setattr(ccs.telemetry, "start", telemetry_start)
    monkeypatch.setattr(ccs.telemetry, "success", noop)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", noop)
    monkeypatch.setattr(ccs.web_search, "plan_search", plan)
    monkeypatch.setattr(ccs.memory_service, "build_turn_memory", memory)
    monkeypatch.setattr(ccs.memory_service, "after_exchange", lambda *a, **k: None)
    monkeypatch.setattr(ccs.conversation_log, "last_model_row", last_row)
    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.vn_daily_chat, "grounding_for", no_daily)
    monkeypatch.setattr(ccs.chat_context, "kb_grounding", noop)
    await ccs.run_chat_turn(OWNER, "thôi em đừng hỏi nữa nhé")
    assert ps.OPT_OUT_NOTE in seen["grounding"]
    assert fake.events[0]["status"] == "done"
    assert fake.settings[f"followup:{OWNER}"].startswith("pause:")
    assert ps.is_opt_out("Đừng nhắc chuyện đó nữa") and not ps.is_opt_out("nhắc anh 8h họp")


@pytest.mark.asyncio
async def test_lenh_hoithan_va_muc_ban_tin_sang(fake, monkeypatch):
    from services import memory_commands, morning_brief

    await fake.add_user_event(OWNER, "phỏng vấn", "mai", dt.date(2026, 10, 27))
    status = (await memory_commands.handle("/hoithan", "", OWNER, channel="telegram")).messages[0]
    assert "BẬT" in status and "27/10: phỏng vấn" in status
    off = (await memory_commands.handle("/hoithan", "off", OWNER, channel="telegram")).messages[0]
    assert "Đã tắt" in off and fake.events[0]["status"] == "done"
    zalo = (await memory_commands.handle("/hoithan", "on", -5, channel="zalo")).messages[0]
    assert "Zalo chưa bật" in zalo
    await fake.add_user_event(OWNER, "đi khám", "mai", dt.date(2026, 10, 28))
    section = next(s for s in morning_brief.sections() if s.key == "followups")
    ctx = morning_brief.BriefContext(
        user_id=OWNER,
        channel="telegram",
        now=dt.datetime(2026, 10, 28, 7, 0, tzinfo=VN),
        settings=morning_brief.BriefSettings(on=True),
        is_owner=True,
    )
    assert await section.build(ctx) == "📅 Việc hôm nay đã kể với em:\n• đi khám"
