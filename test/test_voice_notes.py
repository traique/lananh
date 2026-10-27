"""Tin nhắn thoại: nhận định dạng, chép lời (Groq -> Gemini), lượt chỉ đọc,
endpoint /internal/zalo/voice và handler Telegram (services/voice_service.py)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from ai import orchestrator
from ai.timeouts import remaining_turn_seconds
from services import voice_service
from services.channel_result import ChannelResult

OGG = b"OggS\x00\x02" + b"\x00" * 40
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 40
ADTS = b"\xff\xf1\x50\x80" + b"\x00" * 40


@pytest.fixture(autouse=True)
def _quiet_telemetry(monkeypatch):
    async def start(*args, **kwargs):
        return None

    async def done(*args, **kwargs):
        return None

    monkeypatch.setattr(voice_service.telemetry, "start", start)
    monkeypatch.setattr(voice_service.telemetry, "success", done)
    monkeypatch.setattr(voice_service.telemetry, "failure", done)

    async def no_symbols(user_id):
        return []

    monkeypatch.setattr(voice_service, "_user_symbols", no_symbols)


# ─── Định dạng, trần, lọc ảo giác ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (OGG, "ogg"),
        (M4A, "m4a"),
        (ADTS, "aac"),
        (b"ID3\x04" + b"\x00" * 20, "mp3"),
        (b"\xff\xfb\x90\x64" + b"\x00" * 20, "mp3"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
        (b"fLaC\x00\x00\x00\x22", "flac"),
        (b"\x1aE\xdf\xa3\x9f", "webm"),
    ],
)
def test_sniff_nhan_dinh_dang_theo_magic_bytes(data, name):
    fmt = voice_service.sniff(data)
    assert fmt is not None and fmt.name == name


def test_sniff_tu_choi_amr_va_anh():
    assert voice_service.sniff(b"#!AMR\n" + b"\x00" * 20) is None
    assert voice_service.sniff(b"\xff\xd8\xff\xe0" + b"\x00" * 20) is None  # JPEG
    assert voice_service.sniff(b"") is None


def test_aac_tho_chi_gui_gemini():
    fmt = voice_service.sniff(ADTS)
    assert fmt is not None and fmt.groq_mime is None and fmt.gemini_mime == "audio/aac"


def test_thoi_luong_int_hoac_timedelta():
    assert voice_service.duration_seconds(12) == 12.0
    assert voice_service.duration_seconds(timedelta(seconds=95)) == 95.0
    assert voice_service.duration_seconds(None) is None


def test_tran_kich_thuoc_va_thoi_luong(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_SECONDS", "60")
    monkeypatch.delenv("VOICE_MAX_BYTES", raising=False)
    assert voice_service.limit_error(1000, 30) is None
    assert voice_service.limit_error(1000, 61) == "too_long"
    assert voice_service.limit_error(6 * 1024 * 1024, None) == "too_big"


def test_loc_cau_ao_giac_cua_whisper():
    classic = "Hãy subscribe cho kênh Ghiền Mì Gõ Để không bỏ lỡ những video hấp dẫn"
    assert voice_service.clean_transcript(classic) == ""
    assert voice_service.clean_transcript("Cảm ơn các bạn đã theo dõi!") == ""
    # Câu thật có từ trùng vẫn giữ.
    assert voice_service.clean_transcript("huỷ subscribe Netflix giúp anh") != ""
    assert voice_service.clean_transcript("gửi anh video tiếp theo nhé") != ""
    assert voice_service.clean_transcript("  mai   Hà Nội mưa không ") == "mai Hà Nội mưa không"


def test_cau_nhac_lai_theo_cach_goi():
    assert voice_service.echo_line("chào em", owner=True) == "🎙️ Em nghe là: «chào em»"
    assert voice_service.echo_line("chào", address_form="cô") == "🎙️ Cháu nghe là: «chào»"
    text = voice_service.error_reply("too_long", owner=False, address_form="chị")
    assert "chị" in text and "anh" not in text


# ─── transcribe() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transcribe_gui_dung_dinh_dang_va_goi_y_ma(monkeypatch):
    seen = {}

    async def symbols(user_id):
        return ["FPT", "HPG"]

    async def fake_stt(audio, **kwargs):
        seen.update(kwargs)
        return orchestrator.Transcription("  phân tích FPT  ", "groq")

    monkeypatch.setattr(voice_service, "_user_symbols", symbols)
    monkeypatch.setattr(voice_service.orchestrator, "transcribe_audio", fake_stt)
    monkeypatch.delenv("VOICE_STT_LANGUAGE", raising=False)
    text = await voice_service.transcribe(M4A, user_id=42)
    assert text == "phân tích FPT"
    assert seen["filename"] == "voice.m4a"
    assert seen["groq_mime"] == "audio/mp4" and seen["gemini_mime"] == "audio/m4a"
    assert seen["language"] == "vi"
    assert "FPT, HPG" in seen["prompt"] and len(seen["prompt"]) <= voice_service.PROMPT_MAX_CHARS


@pytest.mark.asyncio
async def test_transcribe_loi_va_rong(monkeypatch):
    async def boom(audio, **kwargs):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(voice_service.orchestrator, "transcribe_audio", boom)
    with pytest.raises(voice_service.VoiceError) as info:
        await voice_service.transcribe(OGG)
    assert info.value.kind == "failed" and "HTTP 500" in info.value.detail

    async def silent(audio, **kwargs):
        return orchestrator.Transcription("Cảm ơn các bạn đã theo dõi", "groq")

    monkeypatch.setattr(voice_service.orchestrator, "transcribe_audio", silent)
    with pytest.raises(voice_service.VoiceError) as info:
        await voice_service.transcribe(OGG)
    assert info.value.kind == "empty"

    with pytest.raises(voice_service.VoiceError) as info:
        await voice_service.transcribe(b"not audio" * 10)
    assert info.value.kind == "format"

    monkeypatch.setenv("VOICE_STT_ENABLED", "0")
    with pytest.raises(voice_service.VoiceError) as info:
        await voice_service.transcribe(OGG)
    assert info.value.kind == "disabled"


def test_nhan_dien_cau_ghi_du_lieu_tu_tin_thoai():
    assert voice_service.write_statement_kind("đã mua FPT 1000 giá 120") == "portfolio"
    assert voice_service.write_statement_kind("cafe 45k") == "expense"
    assert voice_service.write_statement_kind("có nên mua FPT không") is None
    assert voice_service.write_statement_kind("mai Hà Nội mưa không") is None
    # Câu nhắc việc có số tiền: không phải "khoản chi bị chặn" - để lượt chỉ đọc báo
    # đúng là chưa đặt nhắc (router tool, outcome blocked).
    assert voice_service.write_statement_kind("nhắc anh 9h mai trả 500k") is None
    # Khai báo thành phố thời tiết mặc định cũng là câu ghi dữ liệu.
    assert voice_service.write_statement_kind("anh ở Đà Nẵng") == "weather_home"
    reply = voice_service.write_blocked_reply("weather_home", owner=True)
    assert "thành phố thời tiết" in reply and "không tự ghi" in reply


# ─── Zalo: handle_channel_voice ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zalo_voice_mot_han_chot_va_luot_chi_doc(monkeypatch):
    seen = {}

    async def fake_transcribe(audio, **kwargs):
        seen["stt_budget"] = remaining_turn_seconds()
        seen["duration"] = kwargs.get("duration")
        return "giá vàng hôm nay"

    async def fake_text(user_id, text, is_admin=True, channel="zalo", **kwargs):
        seen["chat_budget"] = remaining_turn_seconds()
        seen.update(user_id=user_id, text=text, is_admin=is_admin, channel=channel, **kwargs)
        return ChannelResult(["Vàng SJC ..."], "api")

    monkeypatch.setattr(voice_service, "transcribe", fake_transcribe)
    monkeypatch.setattr(voice_service.channel_chat_service, "handle_channel_text", fake_text)
    replies, provider = await voice_service.handle_channel_voice(
        -7, OGG, is_admin=False, duration_ms=4200
    )
    assert replies == ["🎙️ Em nghe là: «giá vàng hôm nay»", "Vàng SJC ..."]
    assert provider == "api"
    assert seen["allow_side_effects"] is False and seen["channel"] == "zalo"
    assert seen["duration"] == 4.2
    # Cả bước nghe lẫn lượt chat nằm trong CÙNG 1 hạn chót.
    assert seen["stt_budget"] is not None and seen["chat_budget"] is not None
    assert seen["chat_budget"] <= seen["stt_budget"]


@pytest.mark.asyncio
async def test_zalo_voice_cau_ghi_danh_muc_khong_ghi(monkeypatch):
    recorded = []

    async def fake_transcribe(audio, **kwargs):
        return "đã mua FPT 1000 giá 120"

    async def must_not_chat(*args, **kwargs):
        raise AssertionError("không được chạy nhánh ghi danh mục")

    async def record(user_id, user_text, reply, kind="chat", **kwargs):
        recorded.append((user_id, user_text, kind))
        return True

    monkeypatch.setattr(voice_service, "transcribe", fake_transcribe)
    monkeypatch.setattr(voice_service.channel_chat_service, "handle_channel_text", must_not_chat)
    monkeypatch.setattr(voice_service.conversation_log, "record_exchange", record)
    replies, provider = await voice_service.handle_channel_voice(-7, OGG)
    assert replies[0].startswith("🎙️ Em nghe là")
    assert "không tự ghi" in replies[1] and "danh mục" in replies[1]
    assert provider is None
    assert recorded == [(-7, "đã mua FPT 1000 giá 120", "portfolio")]


@pytest.mark.asyncio
async def test_zalo_voice_loi_chep_loi_tra_cau_giong_lan_anh(monkeypatch):
    from services import reply_audience

    async def fail(audio, **kwargs):
        raise voice_service.VoiceError("failed", "HTTP 503 groq")

    monkeypatch.setattr(voice_service, "transcribe", fail)
    with reply_audience.use(reply_audience.ReplyAudience(address_form="chị")):
        replies, _ = await voice_service.handle_channel_voice(-7, OGG)
    assert "chưa nghe được" in replies[0] and "chị" in replies[0]
    assert "groq" not in replies[0].lower()


# ─── orchestrator.transcribe_audio ───────────────────────────────────────────


@pytest.fixture
def stt_chain(monkeypatch):
    from ai import official_client, provider_overrides
    from ai.provider_state import provider_state
    from core import config

    calls = []

    async def loaded():
        return None

    async def enabled(name):
        return True

    async def usable():
        return [1]

    async def model_for(idx):
        return "gemini-test"

    async def record(*args, **kwargs):
        calls.append(("record", args[0], kwargs.get("outcome"), kwargs.get("stage")))

    monkeypatch.setattr(provider_state, "ensure_loaded", loaded)
    monkeypatch.setattr(provider_overrides, "is_enabled", enabled)
    monkeypatch.setattr(official_client, "usable_indices", usable)
    monkeypatch.setattr(official_client, "model_for", model_for)
    monkeypatch.setattr(orchestrator.db, "record_provider_call", record)
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    provider_state.api_exhausted_until.pop(orchestrator.STT_COOLDOWN_KEY, None)
    yield calls
    provider_state.api_exhausted_until.pop(orchestrator.STT_COOLDOWN_KEY, None)


@pytest.mark.asyncio
async def test_stt_groq_truoc(stt_chain, monkeypatch):
    async def groq(audio, **kwargs):
        stt_chain.append(("groq", kwargs["mime_type"], kwargs["language"]))
        return "xin chào"

    monkeypatch.setattr(orchestrator.groq_client, "transcribe", groq)
    result = await orchestrator.transcribe_audio(
        OGG, filename="v.ogg", groq_mime="audio/ogg", gemini_mime="audio/ogg", language="vi"
    )
    assert result == orchestrator.Transcription("xin chào", "groq")
    assert ("groq", "audio/ogg", "vi") in stt_chain
    assert ("record", "groq", "ok", "stt") in stt_chain


@pytest.mark.asyncio
async def test_stt_groq_429_cooldown_rieng_roi_sang_gemini(stt_chain, monkeypatch):
    from ai import official_client, openai_compatible
    from ai.provider_state import provider_state

    marked = []

    async def groq(audio, **kwargs):
        raise openai_compatible.OpenAICompatibleError(
            "Groq STT HTTP 429: rate", status_code=429, retry_after_sec=40
        )

    async def gemini(idx, audio, mime_type, instruction):
        assert mime_type == "audio/ogg" and "NGUYÊN VĂN" in instruction
        return official_client.FallbackResponse(" anh ơi ")

    async def mark(key, seconds=None, reason=""):
        marked.append(key)

    monkeypatch.setattr(orchestrator.groq_client, "transcribe", groq)
    monkeypatch.setattr(official_client, "transcribe_audio", gemini)
    monkeypatch.setattr(provider_state, "mark_api_exhausted", mark)
    result = await orchestrator.transcribe_audio(
        OGG, filename="v.ogg", groq_mime="audio/ogg", gemini_mime="audio/ogg"
    )
    assert result == orchestrator.Transcription("anh ơi", "api1")
    # Cooldown bucket audio riêng - không khoá Groq chat.
    assert marked == ["groq_stt"]


@pytest.mark.asyncio
async def test_stt_aac_bo_qua_groq_va_bao_loi_khi_het_duong(stt_chain, monkeypatch):
    from ai import official_client

    async def groq_must_not_run(*args, **kwargs):
        raise AssertionError("Groq không nhận AAC thô")

    async def gemini(idx, audio, mime_type, instruction):
        raise RuntimeError("api1 lỗi")

    monkeypatch.setattr(orchestrator.groq_client, "transcribe", groq_must_not_run)
    monkeypatch.setattr(official_client, "transcribe_audio", gemini)
    with pytest.raises(RuntimeError, match="api1 lỗi"):
        await orchestrator.transcribe_audio(
            ADTS, filename="v.aac", groq_mime=None, gemini_mime="audio/aac"
        )


# ─── Groq client: multipart /audio/transcriptions ────────────────────────────


@pytest.mark.asyncio
async def test_groq_transcribe_gui_multipart(monkeypatch):
    from ai import groq_client
    from core import config

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(200, json={"text": " mai mấy giờ họp "})

    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    monkeypatch.setattr(config, "GROQ_STT_MODEL", "whisper-large-v3-turbo")

    async def no_override(name):
        return None

    monkeypatch.setattr(groq_client.provider_overrides, "get_api_key_override", no_override)
    pool = groq_client._pool
    old_client = pool._client
    pool._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        text = await groq_client.transcribe(
            OGG, filename="voice.ogg", mime_type="audio/ogg", language="vi", prompt="FPT"
        )
    finally:
        await pool._client.aclose()
        pool._client = old_client
    assert text == "mai mấy giờ họp"
    assert seen["url"].endswith("/audio/transcriptions")
    assert seen["auth"] == "Bearer gk"
    body = seen["body"]
    assert b'name="model"' in body and b"whisper-large-v3-turbo" in body
    assert b'name="language"' in body and b'name="prompt"' in body
    assert b'filename="voice.ogg"' in body


# ─── Endpoint /internal/zalo/voice ───────────────────────────────────────────

SECRET = "s3cr3t"


class _Turn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def voice_bridge(monkeypatch):
    from channels import router as zalo_router
    from channels import zalo_media

    state = {"cache": {}, "calls": []}
    monkeypatch.setattr(zalo_router, "_secret", lambda: SECRET)
    monkeypatch.delenv("ZALO_BRIDGE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(zalo_media, "assistant_turn", lambda *_a, **_k: _Turn())

    async def get_cached(account, message_id, kind):
        return state["cache"].get((account, message_id, kind))

    async def save_cached(account, message_id, kind, response):
        state["cache"][(account, message_id, kind)] = response

    monkeypatch.setattr(zalo_media.idempotency, "get_zalo_response", get_cached)
    monkeypatch.setattr(zalo_media.idempotency, "save_zalo_response", save_cached)

    class User:
        is_active = True
        is_admin = False
        internal_user_id = -9
        address_form = None

    async def resolve(external_id):
        return User() if external_id == "friend" else None

    monkeypatch.setattr(zalo_media.zalo_users, "resolve", resolve)
    monkeypatch.setattr(zalo_media.zalo_users, "notify_unpaired", lambda *a, **k: None)

    async def fake_voice(user_id, audio, *, is_admin=False, duration_ms=None):
        state["calls"].append((user_id, len(audio), is_admin, duration_ms))
        return ["🎙️ Em nghe là: «chào»", "Dạ em chào"], None

    monkeypatch.setattr(zalo_media.voice_service, "handle_channel_voice", fake_voice)
    return state


async def _post_voice(body: bytes, sender: str = "friend", **headers):
    from channels import zalo_media

    app = FastAPI()
    app.include_router(zalo_media.router)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        return await client.post(
            "/internal/zalo/voice",
            content=body,
            headers={
                "x-zalo-bridge-secret": SECRET,
                "x-zalo-sender-id": sender,
                "x-zalo-message-id": "m1",
                "content-type": "audio/mp4",
                **headers,
            },
        )


@pytest.mark.asyncio
async def test_endpoint_voice_tra_loi_va_cache(voice_bridge):
    response = await _post_voice(M4A, **{"x-zalo-duration-ms": "3500"})
    assert response.status_code == 200
    assert response.json()["messages"] == ["🎙️ Em nghe là: «chào»", "Dạ em chào"]
    assert voice_bridge["calls"] == [(-9, len(M4A), False, 3500)]
    # Gửi lại cùng message_id: trả cache, không nghe lại.
    again = await _post_voice(M4A)
    assert again.json() == response.json()
    assert len(voice_bridge["calls"]) == 1
    assert ("zalo-bot", "m1", "voice") in voice_bridge["cache"]


@pytest.mark.asyncio
async def test_endpoint_voice_nguoi_la_im_lang_va_body_qua_lon(voice_bridge, monkeypatch):
    stranger = await _post_voice(M4A, sender="stranger")
    assert stranger.json()["messages"] == []
    assert stranger.json()["paired"] is False
    monkeypatch.setenv("VOICE_MAX_BYTES", str(64 * 1024))
    too_big = await _post_voice(b"x" * (64 * 1024 + 1))
    assert too_big.status_code == 413
    assert voice_bridge["calls"] == []


def test_request_limits_cho_phep_body_media_voice_va_file():
    from services import request_limits

    assert request_limits._limits("/internal/zalo/voice", 0) == (8 * 1024 * 1024, True)
    assert request_limits._limits("/internal/zalo/file", 0) == (8 * 1024 * 1024, True)


# ─── Telegram: voice_msg + route_text chỉ đọc ────────────────────────────────


class _Voice:
    def __init__(self, duration=timedelta(seconds=8), size=20_000):
        self.duration = duration
        self.file_size = size
        self.mime_type = "audio/ogg"


class _Message:
    def __init__(self, voice):
        self.voice = voice
        self.audio = None
        self.message_id = 77
        self.text = None
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _Update:
    def __init__(self, voice):
        self.message = _Message(voice)
        self.effective_message = self.message
        self.effective_user = type("U", (), {"id": 42, "username": "owner"})()
        self.effective_chat = type("C", (), {"id": 42})()


class _Context:
    def __init__(self):
        async def send_chat_action(**kwargs):
            return None

        self.bot = type("B", (), {"send_chat_action": staticmethod(send_chat_action)})()


@pytest.fixture
def telegram_voice(monkeypatch, tmp_path):
    from core import config
    from handlers import voice_handler

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    state = {"downloads": 0, "routed": []}

    async def download(media, path):
        state["downloads"] += 1
        path.write_bytes(OGG)

    async def route(update, context, text, *, allow_side_effects=True, voice_in=False):
        assert voice_in is True  # /noi auto đọc kèm câu trả lời của tin thoại
        state["routed"].append((text, allow_side_effects, remaining_turn_seconds() is not None))

    monkeypatch.setattr(voice_handler.common, "download_telegram_photo_with_retry", download)
    monkeypatch.setattr(voice_handler.chat_router, "route_text", route)
    return voice_handler, state


@pytest.mark.asyncio
async def test_telegram_voice_nhac_lai_roi_chat_chi_doc(telegram_voice, monkeypatch, tmp_path):
    voice_handler, state = telegram_voice

    async def fake_transcribe(audio, **kwargs):
        assert audio == OGG
        return "mai Hà Nội mưa không"

    monkeypatch.setattr(voice_handler.voice_service, "transcribe", fake_transcribe)
    update = _Update(_Voice())
    await voice_handler.voice_msg(update, _Context())
    assert update.message.replies == ["🎙️ Em nghe là: «mai Hà Nội mưa không»"]
    assert state["routed"] == [("mai Hà Nội mưa không", False, True)]
    assert list(tmp_path.iterdir()) == []  # file tạm đã xoá


@pytest.mark.asyncio
async def test_telegram_voice_qua_dai_khong_tai(telegram_voice, monkeypatch):
    voice_handler, state = telegram_voice
    monkeypatch.setenv("VOICE_MAX_SECONDS", "60")
    update = _Update(_Voice(duration=timedelta(minutes=5)))
    await voice_handler.voice_msg(update, _Context())
    assert state["downloads"] == 0 and state["routed"] == []
    assert "60 giây" in update.message.replies[0]


@pytest.mark.asyncio
async def test_route_text_chi_doc_bo_qua_nhanh_ghi(monkeypatch):
    from core import config
    from handlers import chat_router, stock_handler
    from services import channel_chat_service as ccs

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    seen = {}

    async def must_not_write(*args, **kwargs):
        raise AssertionError("lượt chỉ đọc không được chạy nhánh ghi")

    async def none(*args, **kwargs):
        return None

    async def route(update, user_id, text):
        return stock_handler.StockRouteResult(handled=False, grounding="")

    async def fake_turn(user_id, text, **kwargs):
        seen.update(kwargs)
        return ccs.ChatTurn("dạ")

    async def reply_long(message, text):
        seen["sent"] = text

    monkeypatch.setattr(chat_router.reminder_service, "maybe_handle_shortcut", must_not_write)
    monkeypatch.setattr(
        chat_router.portfolio_service, "maybe_handle_natural_language", must_not_write
    )
    monkeypatch.setattr(chat_router.expense_service, "maybe_handle_chat", must_not_write)

    async def daily(text, user_id=None):
        seen["daily_user_id"] = user_id  # None = không lưu thành phố thời tiết mặc định
        return None

    for name in ("article_reader", "lunar_chat"):
        monkeypatch.setattr(getattr(chat_router, name), "maybe_handle", none)
    monkeypatch.setattr(chat_router.vn_daily_chat, "maybe_handle", daily)
    monkeypatch.setattr(chat_router.weather_chat, "maybe_handle", none)
    monkeypatch.setattr(chat_router.stock_handler, "maybe_handle", route)
    monkeypatch.setattr(chat_router.channel_chat_service, "run_chat_turn", fake_turn)
    monkeypatch.setattr(chat_router.common, "reply_long_text", reply_long)
    update = _Update(_Voice())
    await chat_router.route_text(update, _Context(), "nhắc anh 8h họp", allow_side_effects=False)
    assert seen["allow_side_effects"] is False and seen["sent"] == "dạ"
    assert seen["daily_user_id"] is None


def test_bot_app_dang_ky_voice_audio():
    text = (Path(__file__).resolve().parent.parent / "bot_app.py").read_text(encoding="utf-8")
    assert "filters.VOICE | filters.AUDIO, voice_handler.voice_msg" in text
