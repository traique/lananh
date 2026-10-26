"""Nút inline Telegram: callback_data whitelist, chỉ chủ bot bấm được, 👍/👎 kèm
provider/model, 🔄/✂️ câu trả lời mới nhất, duyệt bài Facebook, cấp quyền Zalo,
hoãn nhắc việc, /sai và /danhgia."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import scheduler
import test_reminders as _reminders
from core import config
from core import database as db
from handlers import callbacks
from services import action_buttons, answer_feedback, concurrency, reply_audience

fake = _reminders.fake


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    monkeypatch.setattr(config, "TELEGRAM_ANSWER_BUTTONS", True)
    answer_feedback._recent.clear()
    answer_feedback._latest.clear()
    callbacks._answered.clear()
    callbacks._keyboards.clear()
    yield
    answer_feedback._recent.clear()
    answer_feedback._latest.clear()


# ─── services.action_buttons ─────────────────────────────────────────────────


def test_callback_data_whitelist_va_gioi_han_64_byte():
    assert action_buttons.parse("rate:12:u") == action_buttons.Action("rate", "12", "u")
    assert action_buttons.parse("snz:7:am") == action_buttons.Action("snz", "7", "am")
    assert action_buttons.parse("fbok:25") == action_buttons.Action("fbok", "25", "")
    for bad in ("rate:12:x", "rate:abc:u", "exec:1", "fbok:25:extra", "", None, "zpair:1" * 20):
        assert action_buttons.parse(bad) is None, bad
    assert action_buttons.needs_turn_lock("regen:5:r")
    assert not action_buttons.needs_turn_lock("rate:5:u")
    rows = (
        action_buttons.answer_rows(10**17, regenerable=True)
        + action_buttons.zalo_pair_rows("9" * 30)
        + action_buttons.reminder_snooze_rows(10**17)
    )
    for row in rows:
        for _label, data in row:
            assert len(data.encode()) <= 64 and action_buttons.parse(data) is not None
    assert action_buttons.zalo_pair_rows("abc-xyz") is None
    assert len(action_buttons.answer_rows(1, regenerable=False)) == 1


# ─── services.answer_feedback ────────────────────────────────────────────────


def _snapshot(prompt_id=5, **kw):
    base = dict(
        prompt_id=prompt_id,
        user_id=42,
        channel="telegram",
        prompt_text="hỏi gì đó",
        answer_text="trả lời",
        provider="groq",
        model="gpt-oss",
        regenerable=True,
        event_key="telegram:100",
    )
    base.update(kw)
    return answer_feedback.AnswerSnapshot(**base)


async def test_danh_gia_ghi_kem_provider_va_ban_chup(monkeypatch):
    upsert = AsyncMock(return_value=1)
    monkeypatch.setattr(db, "answer_feedback_upsert", upsert)
    monkeypatch.setattr(db, "answer_feedback_turn", AsyncMock(return_value=None))
    answer_feedback.remember_answer(_snapshot(answer_text="x" * 5000))
    assert await answer_feedback.rate(42, 5, -1, channel="telegram") is True
    kwargs = upsert.await_args.kwargs
    assert kwargs["rating"] == -1 and kwargs["provider"] == "groq" and kwargs["model"] == "gpt-oss"
    assert len(kwargs["answer_text"]) == answer_feedback.TEXT_MAX
    # Người khác không đánh giá được câu trả lời của chủ bot; lượt không còn -> False.
    assert await answer_feedback.rate(-3, 5, 1, channel="zalo") is False
    assert await answer_feedback.rate(42, 999, 1, channel="telegram") is False


async def test_danh_gia_lay_tu_db_khi_ram_khong_con(monkeypatch):
    row = {
        "prompt_id": 9,
        "channel": "telegram",
        "prompt_text": "q",
        "answer_text": "a",
        "provider": "api1",
        "model": "gemini",
    }
    monkeypatch.setattr(db, "answer_feedback_turn", AsyncMock(return_value=row))
    upsert = AsyncMock(return_value=1)
    monkeypatch.setattr(db, "answer_feedback_upsert", upsert)
    assert await answer_feedback.rate(42, 9, 1, channel="telegram")
    assert upsert.await_args.kwargs["provider"] == "api1"


async def test_sai_cho_thanh_vien_zalo_theo_cach_goi(monkeypatch):
    row = {"prompt_id": 3, "channel": "zalo", "prompt_text": "q", "answer_text": "a"}
    monkeypatch.setattr(db, "answer_feedback_last_chat", AsyncMock(side_effect=[row, None]))
    upsert = AsyncMock(return_value=1)
    monkeypatch.setattr(db, "answer_feedback_upsert", upsert)
    with reply_audience.use(reply_audience.ReplyAudience(address_form="cô")):
        reply = (await answer_feedback.handle_command("/sai", "dài quá", -7, "zalo"))[0]
        assert reply.startswith("📝 Cháu ghi nhận câu trả lời vừa rồi chưa ổn (lý do: dài quá)")
        assert upsert.await_args.kwargs["user_id"] == -7
        assert upsert.await_args.kwargs["reason"] == "dài quá"
        none = (await answer_feedback.handle_command("/sai", "", -7, "zalo"))[0]
    assert none == "Cháu chưa thấy câu trả lời nào gần đây để ghi nhận nha cô."


async def test_danhgia_chi_chu_bot_va_khong_lo_cau_hoi_thanh_vien(monkeypatch):
    stats = [{"provider": "groq", "model": "gpt-oss", "up": 3, "down": 2}]
    recent = [
        {
            "provider": "groq",
            "prompt_text": "câu của chủ bot",
            "reason": "sai số",
            "created_at": dt.datetime(2026, 10, 25),
        }
    ]
    monkeypatch.setattr(db, "answer_feedback_stats", AsyncMock(return_value=stats))
    recent_mock = AsyncMock(return_value=recent)
    monkeypatch.setattr(db, "answer_feedback_recent_down", recent_mock)
    text = (await answer_feedback.handle_command("/danhgia", "7", 42, "telegram"))[0]
    assert "groq (gpt-oss): 👍 3 · 👎 2" in text and "7 ngày" in text
    assert "câu của chủ bot" in text and "lý do: sai số" in text
    assert recent_mock.await_args.args[0] == 42  # chỉ 👎 của chính người xem
    denied = await answer_feedback.handle_command("/danhgia", "", -7, "zalo")
    assert denied == ["Lệnh này chỉ dành cho chủ bot."]


def test_chi_cau_chat_thuong_moi_lam_lai_duoc():
    assert answer_feedback.can_regenerate("giải thích lạm phát là gì")
    assert not answer_feedback.can_regenerate("nhắc anh 8h tối mai gọi mẹ")
    assert not answer_feedback.can_regenerate("FPT thế nào", grounding="GIÁ FPT")
    answer_feedback.remember_answer(_snapshot(5))
    answer_feedback.remember_answer(_snapshot(6, regenerable=False))
    assert answer_feedback.latest_regenerable(42, 5) is None  # không còn là câu mới nhất
    assert answer_feedback.latest_regenerable(42, 6) is None  # câu có ghi dữ liệu


async def test_lam_lai_xoa_cap_cu_va_chan_tool_ghi(monkeypatch):
    from services import channel_chat_service as ccs
    from services.reminder_delivery import current_event_key

    deleted = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "delete_last_chat_pair", deleted)
    seen = {}

    async def fake_turn(user_id, text, **kwargs):
        seen.update(kwargs, user_id=user_id, text=text, event_key=current_event_key())
        return ccs.ChatTurn("ngắn thôi", prompt_id=6)

    monkeypatch.setattr(ccs, "run_chat_turn", fake_turn)
    turn = await answer_feedback.regenerate(_snapshot(), shorter=True)
    assert turn.text == "ngắn thôi"
    deleted.assert_awaited_once_with(42, "hỏi gì đó", "trả lời")
    assert seen["allow_side_effects"] is False
    assert seen["style_hint"] == answer_feedback.SHORTER_HINT
    assert seen["event_key"] == "telegram:100"  # tool (nếu có) không ghi trùng


# ─── handlers.callbacks ──────────────────────────────────────────────────────


class _Message:
    def __init__(self, message_id=50, chat_id=42):
        self.message_id = message_id
        self.chat_id = chat_id
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs.get("reply_markup")))
        return _Message(self.message_id + len(self.replies), self.chat_id)


class _Query:
    def __init__(self, data, message=None, query_id="q1"):
        self.id = query_id
        self.data = data
        self.message = message or _Message()
        self.answers = []
        self.markups = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)


_query_ids = iter(range(1, 10**6))


def _cb_update(data, user_id=42, message=None):
    query = _Query(data, message, query_id=f"q{next(_query_ids)}")
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id, username="x"),
        effective_message=query.message,
    ), query


class _Bot:
    def __init__(self):
        self.edits = []

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.edits.append((chat_id, message_id, reply_markup))

    async def send_chat_action(self, **kwargs):
        return None


def _labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row] if markup else []


async def test_nguoi_la_bam_nut_bi_bo_qua(monkeypatch):
    rate = AsyncMock()
    monkeypatch.setattr(answer_feedback, "rate", rate)
    update, query = _cb_update("rate:5:u", user_id=999)
    await callbacks.on_callback(update, SimpleNamespace(bot=_Bot()))
    rate.assert_not_awaited()
    assert query.answers == [None]  # vẫn tắt vòng quay, không lộ gì


async def test_bam_danh_gia_ghi_nhan_va_bo_hang_danh_gia(monkeypatch):
    monkeypatch.setattr(answer_feedback, "rate", AsyncMock(return_value=True))
    answer_feedback.remember_answer(_snapshot(5))
    update, query = _cb_update("rate:5:d")
    await callbacks.on_callback(update, SimpleNamespace(bot=_Bot()))
    answer_feedback.rate.assert_awaited_once_with(42, 5, -1, channel="telegram")
    assert query.answers[0].startswith("Em ghi nhận, lần sau em cố hơn")
    assert _labels(query.markups[-1]) == ["🔄 Thử lại", "✂️ Ngắn hơn"]
    # Bấm lại (query khác) vẫn được trả lời; nút hết hạn -> báo nhẹ.
    update, query = _cb_update("rate:abc:u")
    await callbacks.on_callback(update, SimpleNamespace(bot=_Bot()))
    assert query.answers == ["Nút này hết hạn rồi anh ơi."]


async def test_lam_lai_cau_moi_nhat_gui_cau_moi_kem_nut(monkeypatch):
    from services import channel_chat_service as ccs

    async def regenerate(snapshot, *, shorter):
        assert shorter is True
        return ccs.ChatTurn("bản gọn", prompt_id=6, provider="groq", model="m")

    monkeypatch.setattr(answer_feedback, "regenerate", regenerate)
    answer_feedback.remember_answer(_snapshot(5))
    bot = _Bot()
    callbacks._keyboards[42] = 50  # câu trả lời cũ đang mang nút
    update, query = _cb_update("regen:5:s")
    await callbacks.on_callback(update, SimpleNamespace(bot=bot))
    text, markup = query.message.replies[-1]
    assert text == "bản gọn" and _labels(markup) == ["👍", "👎", "🔄 Thử lại", "✂️ Ngắn hơn"]
    assert answer_feedback.latest_regenerable(42, 6) is not None
    assert answer_feedback.latest_regenerable(42, 6).provider == "groq"
    # Nút làm lại của câu cũ bị bỏ ngay khi bấm (chống bấm 2 lần).
    assert _labels(query.markups[0]) == ["👍", "👎"]
    assert bot.edits == [(42, 50, None)]
    # Câu cũ hơn: không làm lại.
    update, query = _cb_update("regen:5:r")
    await callbacks.on_callback(update, SimpleNamespace(bot=bot))
    assert "mới nhất" in query.message.replies[-1][0]


async def test_lam_lai_bi_huy_bang_huy(monkeypatch):
    started = asyncio.Event()

    async def slow(snapshot, *, shorter):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(answer_feedback, "regenerate", slow)
    answer_feedback.remember_answer(_snapshot(5))
    update, query = _cb_update("regen:5:r")
    task = asyncio.create_task(callbacks.on_callback(update, SimpleNamespace(bot=_Bot())))
    await started.wait()
    assert concurrency.cancel_turn(concurrency.OWNER_TURN_KEY)
    await asyncio.wait_for(task, 1)
    assert query.message.replies[-1][0].startswith("🫡 Em dừng rồi nha anh.")


async def test_duyet_bai_facebook_bang_nut(monkeypatch):
    from channels import facebook_commands
    from services.channel_result import ChannelResult

    handled = AsyncMock(return_value=ChannelResult(["✅ Đã đăng #25"]))
    monkeypatch.setattr(facebook_commands, "maybe_handle_facebook_command", handled)
    monkeypatch.setattr(callbacks.zalo_session, "load_account_id", AsyncMock(return_value="acc"))
    update, query = _cb_update("fbok:25")
    await callbacks.on_callback(update, SimpleNamespace(bot=_Bot()))
    handled.assert_awaited_once_with("acc", "/fb_ok 25")
    assert query.markups == [None] and query.message.replies[-1][0] == "✅ Đã đăng #25"
    update, query = _cb_update("fbno:26")
    await callbacks.on_callback(update, SimpleNamespace(bot=_Bot()))
    assert handled.await_args.args == ("acc", "/fb_boqua 26")


async def test_preview_facebook_gui_kem_nut_duyet(monkeypatch):
    from channels import facebook_commands

    sent = []

    async def buttons_callback(text, rows):
        sent.append((text, rows))

    monkeypatch.setattr(facebook_commands, "_auto_rewrite", AsyncMock())
    monkeypatch.setattr(facebook_commands, "_preview", AsyncMock(return_value="PREVIEW"))
    monkeypatch.setattr(
        facebook_commands.facebook_repository, "get_post", AsyncMock(return_value={"id": 25})
    )
    monkeypatch.setenv("ZALO_CONTROLLER_ID", "")
    from channels import zalo_session

    monkeypatch.setattr(zalo_session, "load_controller", AsyncMock(return_value=""))
    facebook_commands.set_admin_notification_callback(AsyncMock())
    facebook_commands.set_admin_buttons_callback(buttons_callback)
    try:
        await facebook_commands.prepare_post("acc", 25)
    finally:
        facebook_commands.set_admin_notification_callback(None)
    assert sent == [("PREVIEW", action_buttons.facebook_review_rows(25))]


async def test_cap_quyen_zalo_bang_nut(monkeypatch):
    from channels import zalo_onboarding, zalo_users

    user = SimpleNamespace(external_id="123", display_name="Mẹ", address_form=None)
    result = zalo_onboarding.PairResult(user, True, False, zalo_onboarding.WELCOME_NONE)
    pair = AsyncMock(return_value=result)
    monkeypatch.setattr(zalo_onboarding, "pair", pair)
    monkeypatch.setitem(zalo_users._notified_unpaired, "123", "Mẹ")
    update, query = _cb_update("zpair:123:a")
    await callbacks.on_callback(update, SimpleNamespace(bot=_Bot()))
    pair.assert_awaited_once_with("123", "Mẹ", admin=True)
    assert query.message.replies[-1][0].startswith("👑 Đã cấp quyền ADMIN cho Zalo id: 123 (Mẹ)")


async def test_canh_bao_zalo_la_kem_nut(monkeypatch):
    from channels import zalo_users

    zalo_users._notified_unpaired.clear()
    sent = []

    async def prompt(text, rows):
        sent.append(rows)

    zalo_users.set_alert_callback(AsyncMock())
    zalo_users.set_pair_prompt_callback(prompt)
    try:
        zalo_users.notify_unpaired("777", "Bạn lạ")
        await asyncio.sleep(0.01)
    finally:
        zalo_users.set_alert_callback(None)
    assert sent == [action_buttons.zalo_pair_rows("777")]
    assert zalo_users.pending_name("777") == "Bạn lạ"


async def test_hoan_nhac_bang_nut(fake, monkeypatch):
    rid = 900
    fake.rows[rid] = dict(
        id=rid,
        uid=42,
        message="gọi mẹ",
        due_at=fake.now,
        sent=True,
        series_id=None,
        fired_at=fake.now,
        done_at=None,
        event_key=None,
    )

    async def reminder_get(uid, reminder_id):
        row = fake.rows.get(reminder_id)
        return row if row and row["uid"] == uid else None

    monkeypatch.setattr(db, "reminder_get", reminder_get)
    update, query = _cb_update(f"snz:{rid}:60")
    await callbacks.on_callback(update, SimpleNamespace(bot=_Bot()))
    reply = query.message.replies[-1][0]
    assert reply.startswith("⏰ Dạ em hoãn tới") and reply.endswith("gọi mẹ")
    new = [r for r in fake.rows.values() if r.get("event_key") == f"snooze:{rid}"]
    assert len(new) == 1 and new[0]["due_at"] > dt.datetime.now(_reminders.VN)
    assert fake.rows[rid]["done_at"] is not None
    # Bấm lại: không đặt thêm nhắc thứ 2.
    update, query = _cb_update(f"snz:{rid}:10")
    await callbacks.on_callback(update, SimpleNamespace(bot=_Bot()))
    assert query.message.replies[-1][0] == "Nhắc việc này đã hoãn hoặc xong rồi nha."
    assert len([r for r in fake.rows.values() if r.get("event_key") == f"snooze:{rid}"]) == 1


async def test_tin_nhac_telegram_kem_nut_hoan(fake):
    from core import idempotency

    sent = []

    async def notify(uid, text, rows):
        sent.append((uid, rows))

    scheduler.set_notify_callback(AsyncMock())
    scheduler.set_reminder_notify_callback(notify)
    try:
        due = idempotency.DueReminder(5, 42, "gọi mẹ", "telegram", "42", "", "", fake.now, None)
        fake.rows[5] = dict(id=5, uid=42, message="gọi mẹ", due_at=fake.now, sent=False)
        await scheduler._process_due_reminders([due])
    finally:
        scheduler._notify_callback = None
        scheduler._reminder_notify_callback = None
    assert sent == [(42, action_buttons.reminder_snooze_rows(5))]
    assert fake.rows[5]["sent"] is True


# ─── web._process_update: khoá lượt theo loại nút ────────────────────────────


async def test_nut_thuong_khong_cho_khoa_nut_lam_lai_thi_cho(monkeypatch):
    import web

    processed = []

    async def process_update(update):
        processed.append(update.callback_query.data)

    monkeypatch.setattr(
        web, "application", SimpleNamespace(process_update=process_update, bot=None)
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def hold():
        async with concurrency.assistant_turn(concurrency.OWNER_TURN_KEY):
            started.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await started.wait()

    def cb_update(data, update_id):
        query = _Query(data, query_id=f"q{update_id}")
        return SimpleNamespace(
            update_id=update_id,
            callback_query=query,
            effective_chat=SimpleNamespace(id=42),
            effective_message=query.message,
        ), query

    rating, _ = cb_update("rate:5:u", 1)
    await asyncio.wait_for(web._process_update(rating), 1)  # không chờ khoá
    regen, regen_query = cb_update("regen:5:r", 2)
    task = asyncio.create_task(web._process_update(regen))
    await asyncio.sleep(0.02)
    assert processed == ["rate:5:u"] and not task.done()
    assert regen_query.answers == ["⏳ Em làm ngay khi xong việc đang dở nha."]
    release.set()
    await holder
    await asyncio.wait_for(task, 1)
    assert processed == ["rate:5:u", "regen:5:r"]


# ─── Provider/model gắn vào câu trả lời ──────────────────────────────────────


async def test_orchestrator_gan_provider_model(monkeypatch):
    from ai import orchestrator
    from ai.openai_compatible import Response

    async def effective(provider):
        return f"{provider}-model"

    monkeypatch.setattr(orchestrator, "_effective_model", effective)
    tagged = await orchestrator._tag_answer(Response("x"), "groq")
    assert (tagged.provider, tagged.model) == ("groq", "groq-model")
    assert await orchestrator._tag_answer("chuỗi", "groq") == "chuỗi"


async def test_telemetry_ghi_provider_vao_ket_qua(monkeypatch):
    from services.telemetry import telemetry

    save = AsyncMock()
    monkeypatch.setattr(db, "save_result", save)
    await telemetry.success(7, "chat", "ok", provider="groq", model="m")
    assert save.await_args.kwargs == {"content_text": "ok", "provider": "groq", "model": "m"}
    await telemetry.success(7, "chat", "ok")
    assert save.await_args.kwargs == {"content_text": "ok"}
