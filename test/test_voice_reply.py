"""Trả lời kèm giọng nói /noi (services/voice_reply.py, channels/tts_media.py): lọc chữ,
trần độ dài, PCM -> WAV/OGG/M4A, kho RAM link ký HMAC, chọn kênh, quota thành viên,
lỗi TTS không làm hỏng câu chữ."""

import asyncio
import io
import re
import time
import wave
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from ai import orchestrator
from ai.timeouts import turn_deadline
from core import database as db
from services import reply_audience, usage_quota, voice_reply

ROOT = Path(__file__).resolve().parent.parent
PCM = b"\x01\x00" * 24000  # 1 giây PCM 16-bit mono 24 kHz
T = 1_800_000_000.0  # mốc giờ unix cố định cho kho link


@pytest.fixture
def settings(monkeypatch):
    data: dict[str, str] = {}

    async def get_setting(key):
        return data.get(key)

    async def set_setting(key, value):
        data[key] = value

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    for name in ("VOICE_REPLY_ENABLED", "VOICE_REPLY_MAX_CHARS", "VOICE_REPLY_STORE_MB"):
        monkeypatch.delenv(name, raising=False)
    voice_reply.store.clear()
    return data


@pytest.fixture
def tts(monkeypatch):
    """TTS giả: trả PCM thô như model 2.5 ("audio/L16;...;rate=24000")."""
    calls = []

    async def synthesize(text, *, model, voice, timeout_sec, engine="tts"):
        calls.append({"text": text, "model": model, "voice": voice, "engine": engine})
        return orchestrator.Speech(PCM, "audio/L16;codec=pcm;rate=24000", "api1")

    monkeypatch.setattr(orchestrator, "synthesize_speech", synthesize)
    return calls


def _no_ffmpeg(monkeypatch):
    monkeypatch.setattr(voice_reply, "ffmpeg_path", lambda: None)


def _fake_ffmpeg(monkeypatch, out=b"ENCODED"):
    seen = []

    async def encode(pcm, rate, channels, fmt):
        seen.append(fmt)
        return out

    monkeypatch.setattr(voice_reply, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(voice_reply, "ffmpeg_encode", encode)
    return seen


# ─── Lọc chữ ─────────────────────────────────────────────────────────────────


def test_loc_chu_bo_markdown_link_bang_emoji_va_dong_api():
    text = (
        "## Tóm tắt 📈\n"
        "**Giá vàng** hôm nay tăng → xem [nguồn](https://sjc.com.vn/x) hoặc https://a.vn/b\n"
        "| Mã | Giá |\n|---|---|\n| FPT | 120 |\n"
        "- Ý 1 `code`\n"
        "```python\nprint(1)\n```\n"
        "\n\n⚙️ _Câu trả lời do API dự phòng trả về._"
    )
    spoken = voice_reply.speakable_text(text)
    assert spoken == "Tóm tắt. Giá vàng hôm nay tăng , xem nguồn hoặc. Ý 1 code."
    assert voice_reply.speakable_text("👍🎉") == ""


def test_tran_do_dai_cat_cuoi_cau_va_noi_ro():
    text = "Câu một khá dài để đọc. " * 40
    spoken, truncated = voice_reply.fit(text, 120, "chị")
    assert truncated and spoken.endswith("Phần còn lại chị xem trong tin nhắn chữ nha.")
    assert spoken.split(" Phần còn lại")[0].endswith(".")
    assert len(spoken.split(" Phần còn lại")[0]) <= 120
    assert voice_reply.fit("ngắn thôi.", 120, "anh") == ("ngắn thôi.", False)


# ─── Âm thanh ────────────────────────────────────────────────────────────────


def test_pcm_tu_wav_header_va_pcm_tho():
    wav = voice_reply.wav_bytes(PCM, 24000)
    assert wav[:4] == b"RIFF"
    assert voice_reply.to_pcm(wav, "audio/wav") == (PCM, 24000, 1)
    assert voice_reply.to_pcm(PCM, "audio/L16;codec=pcm;rate=16000") == (PCM, 16000, 1)
    assert voice_reply.duration_ms(PCM, 24000) == 1000
    with wave.open(io.BytesIO(wav)) as reader:
        assert reader.getframerate() == 24000 and reader.getsampwidth() == 2


@pytest.mark.skipif(voice_reply.ffmpeg_path() is None, reason="máy không có ffmpeg")
@pytest.mark.asyncio
async def test_ffmpeg_that_doi_ogg_opus_va_m4a():
    ogg = await voice_reply.ffmpeg_encode(PCM, 24000, 1, "ogg")
    m4a = await voice_reply.ffmpeg_encode(PCM, 24000, 1, "m4a")
    assert ogg is not None and ogg[:4] == b"OggS"
    assert m4a is not None and m4a[4:8] == b"ftyp"


# ─── Kho RAM + link ký HMAC ──────────────────────────────────────────────────


def _clip(size=10, ext="m4a"):
    return voice_reply.Clip(b"x" * size, voice_reply.MIMES[ext], ext, 1000)


def test_kho_link_ky_co_han_va_tran(settings, monkeypatch):
    store = voice_reply.ClipStore()
    name = store.put(_clip(), now=T)
    assert store.get(name, now=T + 1).data == b"x" * 10
    clip_id, expires, sig, ext = name.split(".")
    assert int(expires) == T + 600
    # Sửa hạn, sửa đuôi, sửa chữ ký đều sai chữ ký.
    assert store.get(f"{clip_id}.{int(expires) + 999}.{sig}.{ext}", now=T + 1) is None
    assert store.get(f"{clip_id}.{expires}.{sig}.ogg", now=T + 1) is None
    assert store.get(f"{clip_id}.{expires}.{sig[:-1]}A.{ext}", now=T + 1) is None
    assert store.get("../../etc/passwd", now=T + 1) is None
    assert store.get(name, now=T + 601) is None  # hết hạn
    # Trần số file: file cũ nhất bị đẩy ra.
    names = [store.put(_clip(), now=T + 1000) for _ in range(voice_reply.STORE_MAX_FILES + 3)]
    assert len(store) == voice_reply.STORE_MAX_FILES
    assert store.get(names[0], now=T + 1001) is None and store.get(names[-1], now=T + 1001)
    # Trần tổng MB.
    monkeypatch.setenv("VOICE_REPLY_STORE_MB", "1")
    big = voice_reply.ClipStore()
    first = big.put(_clip(700_000), now=T + 2000)
    big.put(_clip(700_000), now=T + 2000)
    assert big.get(first, now=T + 2001) is None and big.total_bytes() == 700_000
    assert big.put(_clip(2 * 1024 * 1024), now=T + 2000) is None


@pytest.mark.asyncio
async def test_endpoint_cong_khai_va_loopback(settings, monkeypatch):
    from channels import tts_media

    monkeypatch.setenv("ZALO_BRIDGE_SECRET", "s3cret")
    name = voice_reply.store.put(_clip(5))
    app = FastAPI()
    app.include_router(tts_media.public_router)
    app.include_router(tts_media.internal_router)
    public = httpx.ASGITransport(app=app, client=("203.0.113.9", 5000))
    async with httpx.AsyncClient(transport=public, base_url="https://bot") as client:
        got = await client.get(f"/media/tts/{name}")
        assert got.status_code == 200 and got.content == b"xxxxx"
        assert got.headers["content-type"] == "audio/mp4"
        head = await client.head(f"/media/tts/{name}")
        assert head.status_code == 200 and head.headers["content-length"] == "5"
        assert (await client.get("/media/tts/abc.123.zzz.m4a")).status_code == 404
        # Route loopback không mở cho IP ngoài.
        assert (await client.get(f"/internal/zalo/tts/{name}")).status_code == 403
    local = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=local, base_url="http://bridge") as client:
        assert (await client.get(f"/internal/zalo/tts/{name}")).status_code == 403
        ok = await client.get(
            f"/internal/zalo/tts/{name}", headers={"x-zalo-bridge-secret": "s3cret"}
        )
        assert ok.status_code == 200 and ok.content == b"xxxxx"


# ─── /noi ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noi_dat_che_do_theo_tung_nguoi(settings, monkeypatch):
    _no_ffmpeg(monkeypatch)
    text = await voice_reply.noi_command(42, "", channel="telegram")
    assert "TẮT" in text and "file WAV" in text  # mặc định tắt
    assert "BẬT" in await voice_reply.noi_command(42, "on", channel="telegram")
    assert settings["voice_reply:42"] == "on"
    assert "TỰ ĐỘNG" in await voice_reply.noi_command(-5, "auto", channel="zalo", is_admin=False)
    assert settings["voice_reply:-5"] == "auto" and settings["voice_reply:42"] == "on"
    with reply_audience.use(reply_audience.ReplyAudience(address_form="chị")):
        zalo = await voice_reply.noi_command(-5, "", channel="zalo", is_admin=False)
    assert "cần máy chủ có ffmpeg" in zalo and "1 lượt nặng" in zalo and "chị vừa gửi" in zalo
    assert "chưa hiểu" in await voice_reply.noi_command(42, "to lên", channel="telegram")
    assert "Zoom chưa gửi được" in await voice_reply.noi_command(42, "tắt", channel="zoom")
    assert settings["voice_reply:42"] == "off"
    monkeypatch.setenv("VOICE_REPLY_ENABLED", "0")
    assert "tắt trên bot" in await voice_reply.noi_command(42, "on", channel="telegram")


@pytest.mark.asyncio
async def test_noi_qua_lenh_zalo_va_xoa_cung_du_lieu(settings):
    from services import channel_command_service, personal_settings

    outputs, _ = await channel_command_service.maybe_handle_command(-5, "/noi on", False, "zalo")
    assert "BẬT" in outputs[0] and settings["voice_reply:-5"] == "on"
    assert "voice_reply:-5" in personal_settings.zalo_user_keys(-5)


# ─── Tạo clip ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clip_chi_khi_nguoi_dung_bat(settings, tts, monkeypatch):
    _no_ffmpeg(monkeypatch)
    assert await voice_reply.make_clip(42, "Dạ chào anh.", channel="telegram") is None
    settings["voice_reply:42"] = "auto"
    assert await voice_reply.make_clip(42, "Dạ chào anh.", channel="telegram") is None
    clip = await voice_reply.make_clip(42, "Dạ chào anh.", channel="telegram", voice_in=True)
    assert clip.ext == "wav" and clip.data[:4] == b"RIFF" and clip.duration_ms == 1000
    assert tts == [
        {"text": "Dạ chào anh.", "model": "gemini-3.8-flash-tts", "voice": "Kore", "engine": "auto"}
    ]
    monkeypatch.setenv("VOICE_REPLY_ENABLED", "0")
    assert await voice_reply.make_clip(42, "Dạ.", channel="telegram", voice_in=True) is None


@pytest.mark.asyncio
async def test_telegram_ogg_khi_co_ffmpeg_zalo_chi_khi_co_ffmpeg(settings, tts, monkeypatch):
    settings["voice_reply:42"] = settings["voice_reply:-5"] = "on"
    _no_ffmpeg(monkeypatch)
    assert await voice_reply.make_clip(-5, "Dạ chào chị.", channel="zalo") is None
    assert tts == []  # Zalo không có ffmpeg: không tốn lượt TTS
    seen = _fake_ffmpeg(monkeypatch)
    tg = await voice_reply.make_clip(42, "Dạ chào anh.", channel="telegram")
    assert (tg.ext, tg.mime, tg.data) == ("ogg", "audio/ogg", b"ENCODED")
    zalo = await voice_reply.zalo_voice(-5, "Dạ chào chị.")
    assert seen == ["ogg", "m4a"] and zalo["mime"] == "audio/mp4"
    assert re.fullmatch(r"tts/[A-Za-z0-9_.-]+\.m4a", zalo["path"])
    assert voice_reply.store.get(zalo["path"].removeprefix("tts/")).data == b"ENCODED"
    assert await voice_reply.make_clip(42, "x", channel="zoom") is None


@pytest.mark.asyncio
async def test_link_cong_khai_chi_khi_co_https(settings, tts, monkeypatch):
    from core import config

    settings["voice_reply:-5"] = "on"
    _fake_ffmpeg(monkeypatch)
    monkeypatch.setattr(config, "WEBHOOK_BASE_URL", "https://bot.onrender.com/")
    voice = await voice_reply.zalo_voice(-5, "Dạ.")
    assert voice["url"] == "https://bot.onrender.com/media/" + voice["path"]
    monkeypatch.setattr(config, "WEBHOOK_BASE_URL", "")
    assert (await voice_reply.zalo_voice(-5, "Dạ."))["url"] is None


@pytest.mark.asyncio
async def test_loi_tts_va_het_han_lan_khong_raise(settings, monkeypatch):
    _no_ffmpeg(monkeypatch)
    settings["voice_reply:42"] = "on"

    async def broken(text, **kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(orchestrator, "synthesize_speech", broken)
    assert await voice_reply.make_clip(42, "Dạ.", channel="telegram") is None
    with turn_deadline(3):  # còn < MIN_REMAINING_SEC: thôi đọc, không gọi TTS
        assert await voice_reply.make_clip(42, "Dạ.", channel="telegram") is None


@pytest.mark.asyncio
async def test_thanh_vien_zalo_tinh_luot_nang(settings, tts, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    settings["voice_reply:-5"] = "on"
    state = {"heavy": 4, "recorded": []}

    async def caps():
        return usage_quota.Caps(40, 5)

    async def calls_since(channel, uid, start, heavy_kinds):
        assert "tts" in heavy_kinds
        return 10, state["heavy"]

    async def record_call(channel, uid, kind):
        state["recorded"].append((channel, uid, kind))

    usage_quota.reset()
    monkeypatch.setattr(usage_quota, "caps", caps)
    monkeypatch.setattr(db, "user_calls_since", calls_since)
    monkeypatch.setattr(db, "record_user_call", record_call)
    assert await voice_reply.make_clip(-5, "Dạ.", channel="zalo", is_admin=False) is not None
    assert state["recorded"] == [("zalo", -5, "tts")]
    # Lượt nặng thứ 5 vừa dùng: lần sau chỉ gửi chữ, không gọi TTS.
    assert await voice_reply.make_clip(-5, "Dạ.", channel="zalo", is_admin=False) is None
    assert len(tts) == 1
    # Admin Zalo không bị tính.
    settings["voice_reply:-6"] = "on"
    assert await voice_reply.make_clip(-6, "Dạ.", channel="zalo", is_admin=True) is not None
    assert len(state["recorded"]) == 1
    usage_quota.reset()


# ─── Provider ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tts_het_quota_api1_sang_api2_cooldown_rieng(monkeypatch):
    from ai import official_client, provider_overrides
    from ai.provider_state import provider_state

    tried = []

    async def key(idx):
        return f"k{idx}"

    async def on(name):
        return True

    async def speech(idx, text, *, model, voice):
        tried.append(idx)
        if idx == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
        return PCM, "audio/L16;codec=pcm;rate=24000"

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(official_client, "api_key_for", key)
    monkeypatch.setattr(provider_overrides, "is_enabled", on)
    monkeypatch.setattr(official_client, "synthesize_speech", speech)
    monkeypatch.setattr(official_client, "is_quota_exhausted_error", lambda exc: "429" in str(exc))
    monkeypatch.setattr(orchestrator.call_log, "record", nothing)
    monkeypatch.setattr(provider_state, "ensure_loaded", nothing)
    marked = []

    async def mark(name, seconds=None, reason=""):
        marked.append(name)

    monkeypatch.setattr(provider_state, "mark_api_exhausted", mark)
    result = await orchestrator.synthesize_speech("Dạ.", model="m", voice="Kore", timeout_sec=5)
    assert result.provider == "api2" and tried == [1, 2]
    assert marked == ["tts_api1"]  # quota chat api1 không bị đụng


# ─── Kênh ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zalo_chat_kem_voice_bo_qua_lenh_va_co_phieu(monkeypatch):
    from channels import router
    from services.channel_result import ChannelResult

    asked = []

    async def zalo_voice(user_id, text, *, voice_in=False, is_admin=True):
        asked.append(text)
        return {"path": "tts/a.m4a", "url": None, "duration_ms": 1, "mime": "audio/mp4"}

    monkeypatch.setattr(router.voice_reply, "zalo_voice", zalo_voice)
    chat = ChannelResult(["Dạ chào chị", "Câu hỏi duyệt"])
    result = await router._with_voice(-5, "chào em", chat, False)
    assert result.voice["path"] == "tts/a.m4a" and asked == ["Dạ chào chị"]
    for text, res in (
        ("/help", ChannelResult(["..."])),
        ("FPT", ChannelResult(["giá"], market=True)),
        ("vẽ", ChannelResult([], image_b64="AAA")),
    ):
        assert (await router._with_voice(-5, text, res, False)).voice is None
    from services.channel_dispatch import payload_from_result

    assert payload_from_result(result, list)["voice"]["path"] == "tts/a.m4a"
    assert "voice" not in payload_from_result(chat, list)


def test_hop_dong_voice_khop_gateway():
    from channels.contracts import ZaloMessageResponse, ZaloVoice

    pattern = ZaloVoice.model_fields["path"].metadata[0].pattern
    ts = (ROOT / "zalo-gateway" / "src" / "voice.ts").read_text(encoding="utf-8")
    assert "/^tts\\/[A-Za-z0-9_.-]{1,200}$/" in ts and pattern == r"^tts/[A-Za-z0-9_.-]{1,200}$"
    assert "reply.voice" in (ROOT / "zalo-gateway" / "src" / "index.ts").read_text("utf-8")
    parsed = ZaloMessageResponse.model_validate(
        {"messages": ["a"], "voice": {"path": "tts/x.m4a", "duration_ms": 5}}
    )
    assert parsed.voice.path == "tts/x.m4a"
    with pytest.raises(ValueError):
        ZaloVoice(path="../session")


class _Msg:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def reply_voice(self, voice, duration):
        if self.fail:
            raise RuntimeError("Telegram từ chối")
        self.sent.append(("voice", voice.filename, duration))

    async def reply_document(self, document, caption):
        self.sent.append(("document", document.filename, caption))


class _Update:
    def __init__(self, fail=False):
        self.message = _Msg(fail)
        self.effective_chat = type("C", (), {"id": 42})()


class _Context:
    def __init__(self):
        async def send_chat_action(**kwargs):
            return None

        self.bot = type("B", (), {"send_chat_action": staticmethod(send_chat_action)})()


@pytest.mark.asyncio
async def test_telegram_gui_voice_ogg_hoac_file_wav_loi_chi_log(settings, tts, monkeypatch):
    from handlers import chat_router

    settings["voice_reply:42"] = "on"
    _no_ffmpeg(monkeypatch)
    update = _Update()
    await chat_router._speak(update, _Context(), 42, "Dạ chào anh.", voice_in=False)
    assert update.message.sent == [("document", "lan-anh-tra-loi.wav", "🔊 Bản đọc câu trả lời")]
    _fake_ffmpeg(monkeypatch)
    update = _Update()
    await chat_router._speak(update, _Context(), 42, "Dạ chào anh.", voice_in=False)
    assert update.message.sent == [("voice", "lan-anh.ogg", 1)]
    await chat_router._speak(
        _Update(fail=True), _Context(), 42, "Dạ.", voice_in=False
    )  # không raise


@pytest.mark.asyncio
async def test_telegram_cau_tra_loi_nhanh_duoc_doc_kem(settings, tts, monkeypatch):
    from handlers import chat_router

    spoken = []

    async def quick(update, user_id, text, *, allow_side_effects=True):
        return "Giá vàng hôm nay 85 triệu."

    async def speak(update, context, user_id, text, *, voice_in):
        spoken.append((text, voice_in))

    monkeypatch.setattr(chat_router, "_quick_reply", quick)
    monkeypatch.setattr(chat_router, "_speak", speak)

    class _U(_Update):
        effective_user = type("U", (), {"id": 42})()

    await chat_router.route_text(_U(), _Context(), "giá vàng", voice_in=True)
    assert spoken == [("Giá vàng hôm nay 85 triệu.", True)]


@pytest.mark.asyncio
async def test_semaphore_khong_cho_lau(settings, tts, monkeypatch):
    _no_ffmpeg(monkeypatch)
    settings["voice_reply:42"] = "on"
    monkeypatch.setattr(voice_reply, "GATE_WAIT_SEC", 0.05)
    await voice_reply._gate.acquire()
    try:
        started = time.monotonic()
        assert await voice_reply.make_clip(42, "Dạ.", channel="telegram") is None
        assert time.monotonic() - started < 2
    finally:
        voice_reply._gate.release()
    assert not voice_reply._gate.locked()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_bi_mat_trong_cau_tra_loi_duoc_che_truoc_khi_gui_tts(settings, tts, monkeypatch):
    _no_ffmpeg(monkeypatch)
    settings["voice_reply:42"] = "on"
    await voice_reply.make_clip(
        42, "Dạ em ghi rồi: mật khẩu wifi là Abc@12345xyz nha anh.", channel="telegram"
    )
    assert "Abc@12345xyz" not in tts[0]["text"] and "đã ẩn" in tts[0]["text"]


@pytest.mark.asyncio
async def test_tin_thoai_zalo_tra_voice_qua_hop_capture(monkeypatch):
    """Lượt tin thoại Zalo chạy trong task con huỷ được: clip đi ra qua hộp capture()."""
    from services import voice_service
    from services.channel_result import ChannelResult

    async def transcribe(audio, **kwargs):
        return "hôm nay trời thế nào"

    async def chat(user_id, text, is_admin, **kwargs):
        return ChannelResult(["Dạ trời nắng ạ."])

    seen = []

    async def zalo_voice(user_id, text, *, voice_in=False, is_admin=True):
        seen.append((user_id, text, voice_in, is_admin))
        return {"path": "tts/v.m4a", "url": None, "duration_ms": 9, "mime": "audio/mp4"}

    monkeypatch.setattr(voice_service, "transcribe", transcribe)
    monkeypatch.setattr(voice_service, "write_statement_kind", lambda text: None)
    monkeypatch.setattr(voice_service.channel_chat_service, "handle_channel_text", chat)
    monkeypatch.setattr(voice_reply, "zalo_voice", zalo_voice)
    with voice_reply.capture() as box:
        replies, _ = await asyncio.create_task(
            voice_service.handle_channel_voice(-5, b"OggS....", is_admin=False)
        )
    assert replies[-1] == "Dạ trời nắng ạ."
    assert seen == [(-5, "Dạ trời nắng ạ.", True, False)] and box[0]["path"] == "tts/v.m4a"
    voice_reply.offer({"path": "tts/x.m4a"})  # ngoài capture(): bỏ qua, không lỗi
