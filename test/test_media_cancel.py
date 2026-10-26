"""Tích hợp gói UX Telegram (/huy, nút inline) với gói tin thoại/ảnh/tài liệu:
thứ tự handler PTB thật, /huy huỷ được lượt nghe tin thoại, hỏi đáp ảnh, viết
prompt từ ảnh, đọc tài liệu (Telegram) và /internal/zalo/voice|file (Zalo)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from telegram import Update, User
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from core import config
from services import concurrency, turn_control

OGG = b"OggS\x00\x02" + b"\x00" * 40
CANCELLED = turn_control.cancelled_text(owner=True)


# ─── Thứ tự handler (bot_app.build_application, filter PTB thật) ─────────────


@pytest.fixture(scope="module")
def app():
    import bot_app

    old = config.TELEGRAM_TOKEN
    config.TELEGRAM_TOKEN = "123:ABC"
    try:
        application = bot_app.build_application()
    finally:
        config.TELEGRAM_TOKEN = old
    application.bot._bot_user = User(id=1, is_bot=True, first_name="Lan Anh", username="lananh_bot")
    return application


def _name(handler) -> str:
    return getattr(handler.callback, "__name__", str(handler.callback))


def test_thu_tu_handler_lenh_nut_vitri_thoai_tailieu_anh_chu_catchall(app):
    handlers = app.handlers[0]
    kinds = [type(h) for h in handlers]
    first_non_command = kinds.index(CallbackQueryHandler)
    assert all(k is CommandHandler for k in kinds[:first_non_command])
    rest = handlers[first_non_command:]
    assert isinstance(rest[0], CallbackQueryHandler) and _name(rest[0]) == "on_callback"
    assert all(isinstance(h, MessageHandler) for h in rest[1:])
    assert [_name(h) for h in rest[1:]] == [
        "location_msg",
        "voice_msg",
        "document_msg",
        "photo_msg",
        "photo_msg",
        "chat_msg",
        "unknown_cmd",
        "unsupported_msg",
    ]


def _first(app, payload: dict) -> str | None:
    update = Update.de_json({"update_id": 1, **payload}, app.bot)
    for handler in app.handlers[0]:
        if handler.check_update(update):
            return _name(handler)
    return None


def test_huy_va_nut_bam_khong_roi_vao_catch_all(app):
    base = {
        "message_id": 1,
        "date": 0,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "A"},
    }
    huy = {**base, "text": "/huy", "entities": [{"type": "bot_command", "offset": 0, "length": 4}]}
    assert _first(app, {"message": huy}) == "huy_cmd"
    query = {
        "id": "q1",
        "from": {"id": 42, "is_bot": False, "first_name": "A"},
        "chat_instance": "c",
        "data": "rate:5:u",
        "message": {**base, "text": "câu trả lời"},
    }
    assert _first(app, {"callback_query": query}) == "on_callback"


# ─── Telegram: /huy giữa lượt tin thoại / tài liệu / ảnh ─────────────────────


class _Msg:
    def __init__(self, log, **fields):
        self.log = log
        self.message_id = 77
        self.chat_id = 42
        self.text = None
        self.caption = None
        self.voice = None
        self.audio = None
        self.photo = None
        self.document = None
        self.__dict__.update(fields)

    async def reply_text(self, text, **kwargs):
        self.log.append(text)
        return _Msg(self.log, text=text)

    async def edit_text(self, text, **kwargs):
        self.log.append(text)
        return self


class _Update:
    def __init__(self, **fields):
        self.log: list[str] = []
        self.message = _Msg(self.log, **fields)
        self.effective_message = self.message
        self.effective_user = SimpleNamespace(id=42, username="owner")
        self.effective_chat = SimpleNamespace(id=42)
        self.update_id = 9


class _Context:
    def __init__(self):
        async def send_chat_action(**kwargs):
            return None

        self.bot = SimpleNamespace(send_chat_action=send_chat_action)


@pytest.fixture
def owner(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(config, "TELEGRAM_STREAM_DRAFTS", False)
    monkeypatch.setattr(config, "TELEGRAM_STATUS_DELAY_SEC", 60)

    async def download(file_obj, path):
        path.write_bytes(OGG)

    from handlers import common

    monkeypatch.setattr(common, "download_telegram_photo_with_retry", download)
    return tmp_path


async def _cancel_when(started: asyncio.Event, task: asyncio.Task) -> None:
    await asyncio.wait_for(started.wait(), 1)
    assert concurrency.cancel_turn(concurrency.OWNER_TURN_KEY)
    await asyncio.wait_for(task, 1)
    assert not concurrency.has_running(concurrency.OWNER_TURN_KEY)


def _voice():
    return SimpleNamespace(duration=timedelta(seconds=8), file_size=2000, mime_type="audio/ogg")


async def test_huy_luc_dang_nghe_tin_thoai(owner, monkeypatch):
    from handlers import voice_handler

    started = asyncio.Event()
    routed = []

    async def slow_transcribe(audio, **kwargs):
        started.set()
        await asyncio.sleep(30)

    async def route(*args, **kwargs):
        routed.append(args)

    monkeypatch.setattr(voice_handler.voice_service, "transcribe", slow_transcribe)
    monkeypatch.setattr(voice_handler.chat_router, "route_text", route)
    update = _Update(voice=_voice())
    task = asyncio.create_task(voice_handler.voice_msg(update, _Context()))
    await _cancel_when(started, task)
    assert update.log == [CANCELLED]
    assert routed == []
    assert list(owner.iterdir()) == []  # file tạm đã xoá


async def test_huy_luot_chat_sau_tin_thoai_long_nhau_va_khong_luu_lich_su(owner, monkeypatch):
    """Tin thoại = task con (nghe) + route_text -> _ai_reply (task con lồng, cùng
    khoá chủ bot): /huy lúc đang trả lời huỷ đúng lượt chat, 1 câu "dừng rồi"."""
    from handlers import chat_router, stock_handler, voice_handler

    started = asyncio.Event()
    recorded = []
    seen = {}

    async def transcribe(audio, **kwargs):
        return "phân tích giúp anh thị trường hôm nay"

    async def none(*args, **kwargs):
        return None

    async def route(update, user_id, text):
        return stock_handler.StockRouteResult(handled=False, grounding="")

    async def slow_turn(user_id, text, **kwargs):
        seen.update(kwargs)
        started.set()
        await asyncio.sleep(30)

    async def record(*args, **kwargs):
        recorded.append(args)

    monkeypatch.setattr(voice_handler.voice_service, "transcribe", transcribe)
    for name in ("article_reader", "weather_chat", "lunar_chat", "vn_daily_chat"):
        monkeypatch.setattr(getattr(chat_router, name), "maybe_handle", none)
    monkeypatch.setattr(chat_router.stock_handler, "maybe_handle", route)
    monkeypatch.setattr(chat_router.channel_chat_service, "run_chat_turn", slow_turn)
    monkeypatch.setattr(chat_router.conversation_log, "record_exchange", record)
    monkeypatch.setattr(voice_handler.conversation_log, "record_exchange", record)
    update = _Update(voice=_voice())
    task = asyncio.create_task(voice_handler.voice_msg(update, _Context()))
    await _cancel_when(started, task)
    assert seen["allow_side_effects"] is False  # lượt từ tin thoại vẫn chỉ đọc
    assert update.log == ["🎙️ Em nghe là: «phân tích giúp anh thị trường hôm nay»", CANCELLED]
    assert recorded == []


async def test_huy_luc_dang_doc_tai_lieu(owner, monkeypatch):
    from handlers import document_handler

    started = asyncio.Event()

    async def slow_file(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(document_handler.document_service, "handle_file", slow_file)
    document = SimpleNamespace(
        file_size=1000, file_name="hop-dong.pdf", mime_type="application/pdf"
    )
    update = _Update(document=document, caption="tóm tắt giúp anh")
    task = asyncio.create_task(document_handler.document_msg(update, _Context()))
    await _cancel_when(started, task)
    assert update.log == [CANCELLED]
    assert list(owner.iterdir()) == []


async def test_huy_luc_dang_hoi_dap_anh(owner, monkeypatch):
    from handlers import media_handler

    started = asyncio.Event()

    async def slow_answer(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(media_handler.vision_service, "answer", slow_answer)
    update = _Update(photo=[SimpleNamespace(file_id="p")], caption="ảnh này là gì?")
    task = asyncio.create_task(media_handler.photo_msg(update, _Context()))
    await _cancel_when(started, task)
    assert update.log == [CANCELLED]
    assert list(owner.iterdir()) == []


async def test_huy_luc_dang_viet_prompt_tu_anh(owner, monkeypatch):
    from handlers import media_handler

    started = asyncio.Event()
    failures = []

    async def slow_analyze(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)

    async def failure(prompt_id, kind, exc):
        failures.append((prompt_id, kind, type(exc).__name__))

    monkeypatch.setattr(media_handler.orchestrator, "analyze_image", slow_analyze)
    monkeypatch.setattr(media_handler.telemetry, "start", AsyncMock(return_value=5))
    monkeypatch.setattr(media_handler.telemetry, "failure", failure)
    update = _Update(photo=[SimpleNamespace(file_id="p")], caption="")
    task = asyncio.create_task(media_handler.photo_msg(update, _Context()))
    await _cancel_when(started, task)
    assert update.log == [CANCELLED]
    assert failures == [(5, "promptify", "TurnCancelled")]
    assert list(owner.iterdir()) == []


# ─── Zalo: "/huy" ngoài hàng đợi huỷ lượt /internal/zalo/voice|file ──────────

SECRET = "s3cr3t"


@pytest.fixture
def zalo(monkeypatch):
    from channels import router as zalo_router
    from channels import zalo_media

    cache: dict = {}
    monkeypatch.setattr(zalo_router, "_secret", lambda: SECRET)
    monkeypatch.delenv("ZALO_BRIDGE_ALLOWED_HOSTS", raising=False)
    user = SimpleNamespace(is_active=True, is_admin=False, internal_user_id=-9, address_form="chị")
    # zalo_media dùng chung module channels.zalo_users với router.
    monkeypatch.setattr(zalo_router.zalo_users, "resolve", AsyncMock(return_value=user))

    async def get_cached(account, message_id, kind):
        return cache.get((account, message_id, kind))

    async def save_cached(account, message_id, kind, response):
        cache[(account, message_id, kind)] = response

    for module in (zalo_router, zalo_media):
        monkeypatch.setattr(module.idempotency, "get_zalo_response", get_cached)
        monkeypatch.setattr(module.idempotency, "save_zalo_response", save_cached)
    return zalo_router, zalo_media, cache


async def _post(zalo_media, path: str, body: bytes, **headers):
    api = FastAPI()
    api.include_router(zalo_media.router)
    transport = httpx.ASGITransport(app=api, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        return await client.post(
            path,
            content=body,
            headers={
                "x-zalo-bridge-secret": SECRET,
                "x-zalo-sender-id": "z9",
                "x-zalo-message-id": "m1",
                **headers,
            },
        )


@pytest.mark.parametrize(
    ("path", "target", "headers", "kind"),
    [
        ("/internal/zalo/voice", "voice_service.handle_channel_voice", {}, "voice"),
        (
            "/internal/zalo/file",
            "document_service.handle_channel_file",
            {"x-zalo-file-name": "a.pdf", "content-type": "application/pdf"},
            "file",
        ),
    ],
)
async def test_zalo_huy_huy_luot_thoai_va_file(zalo, monkeypatch, path, target, headers, kind):
    from channels.contracts import ZaloMessageRequest

    zalo_router, zalo_media, cache = zalo
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)

    module_name, func = target.split(".")
    monkeypatch.setattr(getattr(zalo_media, module_name), func, slow)
    turn = asyncio.create_task(_post(zalo_media, path, OGG, **headers))
    await asyncio.wait_for(started.wait(), 1)
    huy = ZaloMessageRequest(
        account_id="zalo-bot", sender_id="z9", conversation_id="z9", message_id="m2", text="/huy"
    )
    cancel_reply = await asyncio.wait_for(zalo_router.receive(huy, SECRET), 1)
    assert cancel_reply.messages == []
    response = await asyncio.wait_for(turn, 1)
    expected = ["🫡 Em dừng rồi nha chị. Cần gì cứ nhắn lại em nhé."]
    assert response.status_code == 200 and response.json()["messages"] == expected
    # Gateway gửi lại cùng message_id: trả câu đã huỷ trong cache, không chạy lại.
    assert cache[("zalo-bot", "m1", kind)]["messages"] == expected
    assert not concurrency.has_running("zalo:-9")
