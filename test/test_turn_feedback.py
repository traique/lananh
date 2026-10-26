"""Phản hồi lượt dài và /huy: task con huỷ được, lối nhanh trước inbox/khoá lượt,
dòng trạng thái, reaction, typing, Zalo /huy ngoài khoá lượt."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import config
from services import concurrency, turn_control, turn_progress
from services.channel_result import ChannelResult

# ─── services.concurrency: task con huỷ được ─────────────────────────────────


async def test_run_cancellable_tra_ket_qua_va_don_registry():
    async def work():
        return 7

    assert await concurrency.run_cancellable("k", work) == 7
    assert not concurrency._running and not concurrency.has_running("k")
    assert concurrency.cancel_turn("k") is False


async def test_huy_task_con_nha_khoa_va_slot(monkeypatch):
    monkeypatch.setattr(concurrency, "_turn_slots", asyncio.Semaphore(1))
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(30)

    async def turn():
        async with concurrency.assistant_turn("zalo:1"):
            with pytest.raises(concurrency.TurnCancelled):
                await concurrency.run_cancellable("zalo:1", slow)
            return "handled"

    task = asyncio.create_task(turn())
    await started.wait()
    # /huy không lấy khoá lượt: gọi được trong lúc lượt đang giữ khoá.
    assert concurrency.cancel_turn("zalo:1") is True
    assert await asyncio.wait_for(task, 1) == "handled"
    assert not concurrency._turn_locks and not concurrency._running
    # Slot toàn cục đã nhả: lượt khác vào ngay.
    async with asyncio.timeout(1):
        async with concurrency.assistant_turn("zalo:2"):
            pass


async def test_task_cha_bi_huy_thi_khong_thanh_turn_cancelled():
    started = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def slow():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    task = asyncio.create_task(concurrency.run_cancellable("owner", slow))
    await started.wait()
    task.cancel()  # shutdown / mất lease inbox
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child_cancelled.is_set()
    assert not concurrency._running


async def test_inbox_lượt_bi_huy_van_finish_khong_thu_lai(monkeypatch):
    """Huỷ đúng task con: sự kiện inbox kết thúc DONE, không bị coi là mất lease."""
    from core import webhook_inbox

    finish, retry = AsyncMock(), AsyncMock()
    monkeypatch.setattr(webhook_inbox, "finish", finish)
    monkeypatch.setattr(webhook_inbox, "retry", retry)
    started = asyncio.Event()
    replies = []

    async def processor(channel, payload):
        async def slow():
            started.set()
            await asyncio.sleep(30)

        async with concurrency.assistant_turn(concurrency.OWNER_TURN_KEY):
            try:
                await concurrency.run_cancellable(concurrency.OWNER_TURN_KEY, slow)
            except concurrency.TurnCancelled:
                replies.append(turn_control.cancelled_text(owner=True))

    row = {"channel": "telegram", "event_id": "1", "payload": "{}", "lease_token": "t"}
    row["attempts"] = 1
    delivery = asyncio.create_task(webhook_inbox.deliver(row, processor))
    await started.wait()
    assert concurrency.cancel_turn(concurrency.OWNER_TURN_KEY)
    await asyncio.wait_for(delivery, 1)
    finish.assert_awaited_once_with(row)
    retry.assert_not_awaited()
    assert replies == ["🫡 Em dừng rồi nha anh. Cần gì cứ nhắn lại em nhé."]


# ─── services.turn_control ───────────────────────────────────────────────────


def test_phan_loai_lenh():
    assert turn_control.is_cancel("/huy") and turn_control.is_cancel("/HUY@lananh_bot")
    assert not turn_control.is_cancel("huỷ đi")
    for text in ("/help", "/notes cafe", "/lich rằm", "/memory", "/nhac", "/kymon", "/history"):
        assert turn_control.is_readonly(text), text
    for text in ("/memory off", "/nhac 8h mai họp", "/status", "/gia iphone", "chào em", ""):
        assert not turn_control.is_readonly(text), text


def test_cau_dung_theo_cach_goi():
    from services import reply_audience

    assert turn_control.cancelled_text(owner=True).startswith("🫡 Em dừng rồi nha anh.")
    assert " anh" not in turn_control.cancelled_text(owner=False)
    with reply_audience.use(reply_audience.ReplyAudience(address_form="cô")):
        assert turn_control.cancelled_text(owner=False).startswith("🫡 Cháu dừng rồi nha cô.")
        assert turn_control.nothing_to_cancel_text(owner=False).endswith("nha cô.")


# ─── services.turn_progress ──────────────────────────────────────────────────


async def test_bao_giai_doan_tu_task_con_toi_tracker():
    tracker = turn_progress.Tracker()

    async def search():
        turn_progress.report(turn_progress.SEARCH)

    async def other():
        turn_progress.report_tool("rag_search")

    with turn_progress.tracking(tracker):
        await asyncio.gather(search())
        assert turn_progress.current_phase() == turn_progress.SEARCH
        await asyncio.gather(other())
    assert tracker.phase == turn_progress.RAG
    assert tracker.history == [turn_progress.SEARCH, turn_progress.RAG]
    turn_progress.report(turn_progress.READ)  # ngoài lượt: không làm gì
    assert turn_progress.current_phase() is None


# ─── handlers.common.typing_keepalive / turn_feedback.StatusLine ─────────────


async def test_typing_gui_lai_dinh_ky_va_dung_khi_xong():
    from handlers import common

    calls = []

    async def send_chat_action(**kwargs):
        calls.append(kwargs)

    bot = SimpleNamespace(send_chat_action=send_chat_action)
    async with common.typing_keepalive(bot, 5, interval=0.01, max_refreshes=100):
        await asyncio.sleep(0.05)
    count = len(calls)
    assert count >= 3 and calls[0] == {"chat_id": 5, "action": "typing"}
    await asyncio.sleep(0.03)
    assert len(calls) == count  # đã dừng


class _Msg:
    def __init__(self, text="hỏi", log=None):
        self.text = text
        self.log = [] if log is None else log
        self.chat_id = 42
        self.message_id = 1

    async def reply_text(self, text, **kwargs):
        sent = _Msg(text, self.log)
        sent.message_id = len(self.log) + 100
        self.log.append(("reply", text))
        return sent

    async def edit_text(self, text, **kwargs):
        self.log.append(("edit", self.text, text))
        self.text = text
        return self


async def test_dong_trang_thai_theo_giai_doan():
    from handlers import turn_feedback

    tracker = turn_progress.Tracker()
    tracker.set(turn_progress.SEARCH)
    message = _Msg()
    status = turn_feedback.StatusLine(message, tracker, 0.01)
    status.start()
    await asyncio.sleep(0.05)
    sent = await status.stop()
    assert sent is not None and sent.text == turn_feedback.STATUS_TEXTS[turn_progress.SEARCH]
    # Xong sớm (trước hạn) thì không gửi gì.
    quick = turn_feedback.StatusLine(_Msg(), tracker, 5)
    quick.start()
    assert await quick.stop() is None


# ─── Telegram chat_msg: dòng trạng thái thành câu trả lời, /huy ──────────────


class _Update:
    def __init__(self, text):
        self.message = _Msg(text)
        self.effective_message = self.message
        self.effective_user = SimpleNamespace(id=42, username="owner")
        self.effective_chat = SimpleNamespace(id=42)


class _Context:
    def __init__(self):
        async def send_chat_action(**kwargs):
            return None

        self.bot = SimpleNamespace(send_chat_action=send_chat_action)


@pytest.fixture
def tg_chat(monkeypatch):
    from handlers import chat_router, stock_handler

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    monkeypatch.setattr(config, "TELEGRAM_STREAM_DRAFTS", False)

    async def none(*args, **kwargs):
        return None

    async def route(update, user_id, text):
        return stock_handler.StockRouteResult(handled=False, grounding="")

    for name in ("article_reader", "weather_chat", "lunar_chat"):
        monkeypatch.setattr(getattr(chat_router, name), "maybe_handle", none)
    monkeypatch.setattr(chat_router.vn_daily_chat, "maybe_handle", none)
    monkeypatch.setattr(chat_router.reminder_service, "maybe_handle_shortcut", none)
    monkeypatch.setattr(chat_router.expense_service, "maybe_handle_chat", none)
    monkeypatch.setattr(chat_router.portfolio_service, "maybe_handle_natural_language", none)
    monkeypatch.setattr(chat_router.stock_handler, "maybe_handle", route)
    return chat_router


async def test_luot_lau_gui_trang_thai_roi_sua_thanh_cau_tra_loi(tg_chat, monkeypatch):
    from services import channel_chat_service as ccs

    monkeypatch.setattr(config, "TELEGRAM_STATUS_DELAY_SEC", 0.01)

    async def slow_turn(user_id, text, **kwargs):
        turn_progress.report(turn_progress.SEARCH)
        await asyncio.sleep(0.08)
        return ccs.ChatTurn("**Dạ** đây ạ", prompt_id=5)

    monkeypatch.setattr(tg_chat.channel_chat_service, "run_chat_turn", slow_turn)
    update = _Update("tin mới nhất về FPT?")
    await tg_chat.chat_msg(update, _Context())
    log = update.message.log
    assert log[0] == ("reply", "🔎 Em đang tra web xíu nha anh…")
    assert log[1] == ("edit", "🔎 Em đang tra web xíu nha anh…", "<b>Dạ</b> đây ạ")
    assert len(log) == 2


async def test_ban_chup_cau_tra_loi_giu_id_cap_lich_su_va_cau_hoi_day_du(tg_chat, monkeypatch):
    """🔄/✂️ cần id cặp chat_messages của lượt (xoá đúng cặp sau khi làm lại) và
    câu hỏi đầy đủ (> 2000 ký tự vẫn hỏi lại nguyên văn)."""
    from services import answer_feedback
    from services import channel_chat_service as ccs

    async def turn(user_id, text, **kwargs):
        return ccs.ChatTurn("Dạ đây ạ", prompt_id=77, history_ids=(3, 4))

    monkeypatch.setattr(config, "TELEGRAM_ANSWER_BUTTONS", False)
    monkeypatch.setattr(tg_chat.channel_chat_service, "run_chat_turn", turn)
    monkeypatch.setattr(answer_feedback, "_recent", type(answer_feedback._recent)())
    monkeypatch.setattr(answer_feedback, "_latest", type(answer_feedback._latest)())
    question = ("giải thích giúp anh " + "lạm phát " * 230).strip()
    await tg_chat.chat_msg(_Update(question), _Context())
    snapshot = answer_feedback.latest_regenerable(42, 77)
    assert snapshot is not None
    assert snapshot.history_ids == (3, 4) and snapshot.question == question


async def test_huy_giua_luot_tra_loi_em_dung_va_khong_luu_lich_su(tg_chat, monkeypatch):
    started = asyncio.Event()
    recorded = []

    async def slow_turn(user_id, text, **kwargs):
        started.set()
        await asyncio.sleep(30)

    async def record(*args, **kwargs):
        recorded.append(args)

    monkeypatch.setattr(config, "TELEGRAM_STATUS_DELAY_SEC", 60)
    monkeypatch.setattr(tg_chat.channel_chat_service, "run_chat_turn", slow_turn)
    monkeypatch.setattr(tg_chat.conversation_log, "record_exchange", record)
    update = _Update("phân tích dài")
    task = asyncio.create_task(tg_chat.chat_msg(update, _Context()))
    await started.wait()
    assert concurrency.cancel_turn(concurrency.OWNER_TURN_KEY)
    await asyncio.wait_for(task, 1)
    assert update.message.log == [("reply", turn_control.cancelled_text(owner=True))]
    assert recorded == []


# ─── Lối nhanh trước inbox (Telegram) và reaction ────────────────────────────


class _Bot:
    def __init__(self):
        self.reactions = []

    async def set_message_reaction(self, chat_id, message_id, reaction=None):
        self.reactions.append((chat_id, message_id, reaction))


def _tg_update(text, update_id=1, user_id=42):
    message = _Msg(text)
    message.message_id = 10 + update_id
    return SimpleNamespace(
        update_id=update_id,
        message=message,
        effective_user=SimpleNamespace(id=user_id),
    )


@pytest.fixture
def fast(monkeypatch):
    from handlers import turn_feedback

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    monkeypatch.setattr(config, "TELEGRAM_BUSY_REACTIONS", True)
    turn_feedback._fast_seen.clear()
    turn_feedback._reacted.clear()
    return turn_feedback


async def _hold_owner_lock():
    started, release = asyncio.Event(), asyncio.Event()

    async def hold():
        async with concurrency.assistant_turn(concurrency.OWNER_TURN_KEY):
            started.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await started.wait()
    return task, release


async def test_huy_o_loi_nhanh_khong_vao_inbox(fast):
    bot = _Bot()
    run = AsyncMock()
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(30)

    turn = asyncio.create_task(concurrency.run_cancellable(concurrency.OWNER_TURN_KEY, slow))
    await started.wait()
    update = _tg_update("/huy", update_id=5)
    assert fast.maybe_fast_path(update, bot, run) is True
    with pytest.raises(concurrency.TurnCancelled):
        await turn
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert bot.reactions == [(42, update.message.message_id, "🫡")]
    # Telegram gửi lại cùng update: không xử lý lần 2.
    assert fast.maybe_fast_path(update, bot, run) is True
    run.assert_not_awaited()


async def test_huy_khi_ranh_bao_khong_co_viec(fast):
    update = _tg_update("/huy", update_id=6)
    assert fast.maybe_fast_path(update, _Bot(), AsyncMock()) is True
    await asyncio.sleep(0.01)
    assert update.message.log == [("reply", turn_control.nothing_to_cancel_text(owner=True))]


async def test_lenh_chi_doc_chay_ngay_khi_khoa_ban(fast):
    run = AsyncMock()
    assert fast.maybe_fast_path(_tg_update("/help", 7), _Bot(), run) is False  # rảnh: qua inbox
    holder, release = await _hold_owner_lock()
    try:
        update = _tg_update("/notes", 8)
        assert fast.maybe_fast_path(update, _Bot(), run) is True
        await asyncio.sleep(0.01)
        run.assert_awaited_once_with(update)
        # Lệnh có AI/ghi dữ liệu vẫn đi inbox, người lạ không đi lối nhanh.
        assert fast.maybe_fast_path(_tg_update("/gia iphone", 9), _Bot(), run) is False
        assert fast.maybe_fast_path(_tg_update("/huy", 10, user_id=7), _Bot(), run) is False
    finally:
        release.set()
        await holder


async def test_reaction_cho_tin_phai_xep_hang(fast):
    bot = _Bot()
    update = _tg_update("câu hỏi 2", 11)
    fast.mark_queued(update, bot)  # khoá rảnh: không thả gì
    holder, release = await _hold_owner_lock()
    try:
        fast.mark_queued(update, bot)
        await asyncio.sleep(0.01)
        assert bot.reactions == [(42, update.message.message_id, "👀")]
        fast.finish_queued(11, bot, ok=False, final=False)  # inbox còn thử lại: giữ 👀
        fast.finish_queued(11, bot, ok=True)
        await asyncio.sleep(0.01)
        assert bot.reactions[-1] == (42, update.message.message_id, "👌")
        fast.finish_queued(11, bot, ok=True)  # đã xong: không đặt lại
        await asyncio.sleep(0.01)
        assert len(bot.reactions) == 2
    finally:
        release.set()
        await holder


async def test_webhook_huy_khong_luu_inbox(monkeypatch, fast):
    import web
    from core import webhook_inbox

    enqueue = AsyncMock()
    monkeypatch.setattr(webhook_inbox, "enqueue", enqueue)
    monkeypatch.setattr(web, "application", SimpleNamespace(bot=_Bot()))
    monkeypatch.setattr(web.config, "WEBHOOK_SECRET", "secret")
    payload = {
        "update_id": 77,
        "message": {
            "message_id": 3,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "A"},
            "text": "/huy",
        },
    }
    req = SimpleNamespace(
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json=AsyncMock(return_value=payload),
    )
    monkeypatch.setattr(fast, "_ack_cancel", AsyncMock())
    response = await web.telegram_webhook(req)
    assert response.status_code == 200
    enqueue.assert_not_awaited()


# ─── Zalo/Zoom: dispatch huỷ được, lệnh chỉ đọc ngoài khoá, /huy ở bridge ───


def _dispatch_parts(chat, *, saved=None):
    from services import channel_dispatch

    async def get_cached(*args):
        return None

    async def save_cached(account_id, event_id, kind, payload):
        if saved is not None:
            saved.append(payload)

    handlers = channel_dispatch.DispatchHandlers(
        chat=chat, get_cached=get_cached, save_cached=save_cached
    )

    def request(text, event="e1"):
        return channel_dispatch.DispatchRequest(
            account_id="acc",
            event_id=event,
            cache_kind="text",
            user_id=-3,
            is_admin=False,
            channel="zalo",
            text=text,
            turn_key="zalo:-3",
        )

    return channel_dispatch, handlers, request


async def test_dispatch_bi_huy_tra_cau_dung_va_ghi_cache():
    saved = []
    started = asyncio.Event()

    async def chat(user_id, text, is_admin):
        started.set()
        await asyncio.sleep(30)

    dispatch, handlers, request = _dispatch_parts(chat, saved=saved)
    task = asyncio.create_task(dispatch.dispatch(request("phân tích dài"), handlers))
    await started.wait()
    assert concurrency.cancel_turn("zalo:-3")
    payload = await asyncio.wait_for(task, 1)
    assert payload["messages"] == [turn_control.cancelled_text(owner=False)]
    assert saved == [payload]  # gateway gửi lại cùng tin: trả cache, không chạy lại
    assert not concurrency._turn_locks


async def test_lenh_chi_doc_khong_cho_khoa_luot():
    started, release = asyncio.Event(), asyncio.Event()

    async def chat(user_id, text, is_admin):
        if text == "/help":
            return ChannelResult(["HELP"])
        started.set()
        await release.wait()
        return ChannelResult(["xong"])

    dispatch, handlers, request = _dispatch_parts(chat)
    long_turn = asyncio.create_task(dispatch.dispatch(request("dài", "e1"), handlers))
    await started.wait()
    payload = await asyncio.wait_for(dispatch.dispatch(request("/help", "e2"), handlers), 1)
    assert payload["messages"] == ["HELP"]
    release.set()
    assert (await long_turn)["messages"] == ["xong"]


async def test_zalo_huy_ngoai_hang_doi_huy_luot_dang_chay(monkeypatch):
    from channels import router as zalo_router
    from channels.contracts import ZaloMessageRequest

    monkeypatch.setattr(zalo_router, "_secret", lambda: "s3cr3t")
    user = SimpleNamespace(is_active=True, is_admin=False, internal_user_id=-9, address_form="chị")
    monkeypatch.setattr(zalo_router.zalo_users, "resolve", AsyncMock(return_value=user))
    monkeypatch.setattr(zalo_router.idempotency, "get_zalo_response", AsyncMock(return_value=None))
    monkeypatch.setattr(zalo_router.idempotency, "save_zalo_response", AsyncMock())
    started = asyncio.Event()

    async def slow_chat(user_id, text, is_admin=True, **kwargs):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(zalo_router, "handle_channel_text", slow_chat)

    def request(text, message_id):
        return ZaloMessageRequest(
            account_id="acc",
            sender_id="z9",
            conversation_id="z9",
            message_id=message_id,
            text=text,
        )

    long_turn = asyncio.create_task(zalo_router.receive(request("phân tích", "m1"), "s3cr3t"))
    await started.wait()
    cancel_reply = await asyncio.wait_for(zalo_router.receive(request("/huy", "m2"), "s3cr3t"), 1)
    assert cancel_reply.messages == []
    turn_reply = await asyncio.wait_for(long_turn, 1)
    assert turn_reply.messages == ["🫡 Em dừng rồi nha chị. Cần gì cứ nhắn lại em nhé."]
    # Không còn gì chạy: /huy báo không có việc dở, theo cách gọi riêng.
    idle = await zalo_router.receive(request("/huy", "m3"), "s3cr3t")
    assert idle.messages == ["Em đang không làm dở việc gì để dừng cả nha chị."]


async def test_zoom_huy_o_webhook(monkeypatch, fast):
    import web

    monkeypatch.setattr(web.db, "zoom_get_pairing", AsyncMock(return_value=("jid-1", "Chủ")))
    sent = []

    async def send_message(to_jid, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(web.zoom, "send_message", send_message)
    event = SimpleNamespace(
        text="/huy", sender_jid="jid-1", event_id="ev1", reply_jid="r", account_id="a"
    )
    assert await web._zoom_fast_path(event) is True
    await asyncio.sleep(0.01)
    assert sent == [turn_control.nothing_to_cancel_text(owner=True)]
    stranger = SimpleNamespace(
        text="/huy", sender_jid="la", event_id="ev2", reply_jid="r", account_id="a"
    )
    assert await web._zoom_fast_path(stranger) is False
    chat = SimpleNamespace(text="chào", sender_jid="jid-1", event_id="ev3")
    assert await web._zoom_fast_path(chat) is False
