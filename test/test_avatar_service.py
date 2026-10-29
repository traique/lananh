"""Logic trang avatar (services/avatar_service.py) và giọng đọc ``voice_reply.web_clip``:
biểu cảm theo câu trả lời, câu chào theo giờ VN, chọn giọng (cài đặt > env > mặc định),
cache giọng có trần, chữ hiển thị không markdown, TTS lỗi/hết quota không raise."""

from datetime import datetime, timezone

import pytest

from ai import orchestrator
from ai.timeouts import turn_deadline
from core import database as db
from services import avatar_service, usage_quota, voice_reply

PCM = b"\x01\x00" * 24000  # 1 giây PCM 16-bit mono 24 kHz


@pytest.fixture
def settings(monkeypatch):
    data: dict[str, str] = {}

    async def get_setting(key):
        return data.get(key)

    async def set_setting(key, value):
        data[key] = value

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    for name in (
        "AVATAR_TTS_VOICE",
        "AVATAR_VOICE_STYLE",
        "VOICE_REPLY_ENABLED",
        "VOICE_REPLY_MAX_CHARS",
        "VOICE_TTS_VOICE",
        "VOICE_STYLE_PROMPT",
    ):
        monkeypatch.delenv(name, raising=False)
    return data


@pytest.fixture
def tts(monkeypatch):
    calls = []

    async def synthesize(text, *, model, voice, timeout_sec, engine="tts", style=""):
        calls.append({"text": text, "voice": voice, "style": style})
        return orchestrator.Speech(PCM, "audio/L16;codec=pcm;rate=24000", "api1")

    monkeypatch.setattr(orchestrator, "synthesize_speech", synthesize)
    monkeypatch.setattr(voice_reply, "ffmpeg_path", lambda: None)
    return calls


# ─── Biểu cảm ────────────────────────────────────────────────────────────────


def test_bieu_cam_du_7_truong_hop():
    cases = {
        "Em thương anh nhiều lắm 🥰": "love",
        "Nhớ anh quá à": "love",
        "Anh khen hoài em ngại á 🙈": "shy",
        "Hihi vui quá đi 😊": "happy",
        "Em buồn ghê 😢": "sad",
        "Hứ, em dỗi rồi đó 😤": "angry",
        "Ơ, thật hả anh? 😲": "surprised",
        "Giá FPT hôm nay 120 nghìn.": "neutral",
        "": "neutral",
    }
    for text, mood in cases.items():
        assert avatar_service.guess_mood(text) == mood, text
    assert set(avatar_service.MOODS) == {*cases.values()}


def test_bieu_cam_ranh_gioi_tu_tieng_viet():
    # "tôi" chứa "ôi", "anh ơi" chứa "ơ", "buồng" chứa "buồn": không phải từ cảm xúc.
    assert avatar_service.guess_mood("Tôi đi làm rồi") == "neutral"
    assert avatar_service.guess_mood("Anh ơi ăn cơm chưa") == "neutral"
    assert avatar_service.guess_mood("Phòng ngủ buồng trong") == "neutral"
    assert avatar_service.guess_mood("Ôi trời ơi") == "surprised"
    assert avatar_service.guess_mood("ƠI, BUỒN QUÁ") == "sad"  # không phân biệt hoa/thường
    # Emoji nặng hơn chữ; hoà điểm thì cảm xúc mạnh trước.
    assert avatar_service.guess_mood("hihi 🥺") == "sad"
    assert avatar_service.guess_mood("Thương anh, hihi") == "love"


# ─── Câu chào ────────────────────────────────────────────────────────────────


def test_cau_chao_theo_khung_gio_viet_nam():
    expected = {0: "khuya", 4: "khuya", 5: "sang", 10: "sang", 11: "trua", 13: "trua"}
    expected |= {14: "chieu", 17: "chieu", 18: "toi", 21: "toi", 22: "khuya", 23: "khuya"}
    for hour, slot in expected.items():
        assert avatar_service.slot_for(hour) == slot, hour
    # 01:30 UTC = 08:30 giờ VN -> buổi sáng.
    morning = avatar_service.greeting(datetime(2026, 10, 29, 1, 30, tzinfo=timezone.utc))
    assert morning.id.startswith("sang:") and morning.text in {
        text for text, _ in avatar_service.GREETINGS["sang"]
    }
    # 16:00 UTC = 23:00 giờ VN -> khuya.
    late = avatar_service.greeting(datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc))
    assert late.id.startswith("khuya:")


def test_cau_chao_tra_lai_theo_id_va_mood_hop_le():
    for slot, lines in avatar_service.GREETINGS.items():
        assert len(lines) >= 2
        for index, (text, mood) in enumerate(lines):
            assert mood in avatar_service.MOODS and text
            assert avatar_service.greeting_by_id(f"{slot}:{index}") == avatar_service.Greeting(
                f"{slot}:{index}", text, mood
            )
    for bad in ("", "sang", "sang:", "sang:99", "sang:-1", "dem:0", "sang:1x", None):
        assert avatar_service.greeting_by_id(bad) is None


def test_cache_giong_co_tran_lru():
    cache = avatar_service.ClipCache()
    clip = voice_reply.Clip(b"a", "audio/wav", "wav", 1000, False)
    for index in range(avatar_service.MAX_CACHED_CLIPS + 5):
        cache.put(("Achernar", "", f"câu {index}"), clip)
    assert len(cache) == avatar_service.MAX_CACHED_CLIPS == 10
    assert cache.get(("Achernar", "", "câu 0")) is None  # cũ nhất bị bỏ
    assert cache.get(("Achernar", "", "câu 5")) is clip
    cache.put(("Leda", "", "mới"), clip)  # "câu 5" vừa dùng nên còn, "câu 6" bị bỏ
    assert cache.get(("Achernar", "", "câu 5")) is clip
    assert cache.get(("Achernar", "", "câu 6")) is None


# ─── Giọng ───────────────────────────────────────────────────────────────────


async def test_giong_cai_dat_uu_tien_hon_env_ten_la_ve_mac_dinh(settings, monkeypatch):
    assert avatar_service.default_voice() == "Achernar"
    assert await avatar_service.current_voice() == "Achernar"
    monkeypatch.setenv("AVATAR_TTS_VOICE", "leda")
    assert await avatar_service.current_voice() == "Leda"
    # Tên lạ trong env -> VOICE_TTS_VOICE (không lỗi).
    monkeypatch.setenv("AVATAR_TTS_VOICE", "GiọngLạ")
    monkeypatch.setenv("VOICE_TTS_VOICE", "Kore")
    assert await avatar_service.current_voice() == "Kore"
    assert await avatar_service.set_voice("vindemiatrix") == "Vindemiatrix"
    assert settings[avatar_service.VOICE_SETTING_KEY] == "Vindemiatrix"
    assert await avatar_service.current_voice() == "Vindemiatrix"
    with pytest.raises(ValueError):
        await avatar_service.set_voice("GiọngLạ")
    settings[avatar_service.VOICE_SETTING_KEY] = "hỏng"  # giá trị hỏng trong DB -> env
    assert await avatar_service.current_voice() == "Kore"
    # Không giữ chung cài đặt /noi giong của Telegram.
    assert not any(key.startswith("voice_name:") for key in settings)


async def test_doc_cai_dat_loi_thi_dung_mac_dinh(settings, monkeypatch):
    async def broken(key):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_setting", broken)
    assert await avatar_service.current_voice() == "Achernar"


def test_danh_sach_giong_goi_y_dung_dau_va_phong_cach(settings, monkeypatch):
    voices = avatar_service.voices()
    assert [v["name"] for v in voices[:4]] == ["Achernar", "Vindemiatrix", "Leda", "Sulafat"]
    assert all(v["suggested"] for v in voices[:4]) and not voices[4]["suggested"]
    assert len(voices) == len(voice_reply.VOICES)
    assert avatar_service.voice_style() == avatar_service.DEFAULT_VOICE_STYLE
    monkeypatch.setenv("AVATAR_VOICE_STYLE", "  giọng  vui  ")
    assert avatar_service.voice_style() == "giọng vui"
    monkeypatch.setenv("AVATAR_VOICE_STYLE", "off")
    assert avatar_service.voice_style() == ""
    monkeypatch.setenv("AVATAR_VOICE_STYLE", "x" * 900)
    assert len(avatar_service.voice_style()) == 500


def test_chu_hien_thi_bo_markdown_giu_emoji():
    text = (
        "## Tiêu đề\n**Dạ** anh, xem [nguồn](https://a.vn/b) nha 🥰\n"
        "- ý 1\n2. ý *hai*\n\n\n\n`mã` ~~cũ~~"
    )
    assert avatar_service.display_text(text) == (
        "Tiêu đề\nDạ anh, xem nguồn nha 🥰\ný 1\ný hai\n\nmã cũ"
    )
    assert avatar_service.display_text("2*3*4 = 24") == "2*3*4 = 24"


# ─── Giọng đọc web_clip ──────────────────────────────────────────────────────


async def test_web_clip_doc_giong_trang_khong_xet_noi(settings, tts):
    # Chưa bật /noi, VOICE_REPLY_ENABLED=0: trang avatar vẫn đọc (giọng riêng).
    settings["voice_reply:42"] = "off"
    clip = await voice_reply.web_clip(42, "**Dạ** anh 🥰 150k nha", voice="leda", style="thủ thỉ")
    assert clip is not None and clip.ext == "wav" and clip.mime == "audio/wav"
    assert tts == [
        {"text": "Dạ anh một trăm năm mươi nghìn nha.", "voice": "Leda", "style": "thủ thỉ"}
    ]
    await voice_reply.web_clip(42, "Dạ.", voice="TênLạ", style="")
    assert tts[-1]["voice"] == "Sulafat" and tts[-1]["style"] == ""  # tên lạ -> VOICE_TTS_VOICE
    assert await voice_reply.web_clip(42, "🥰🥰", voice="Leda", style="") is None  # không còn chữ


async def test_web_clip_ra_m4a_khi_co_ffmpeg(settings, tts, monkeypatch):
    seen = []

    async def encode(pcm, rate, channels, fmt):
        seen.append(fmt)
        return b"M4A"

    monkeypatch.setattr(voice_reply, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(voice_reply, "ffmpeg_encode", encode)
    clip = await voice_reply.web_clip(42, "Dạ anh.", voice="Leda", style="")
    assert seen == ["m4a"] and (clip.data, clip.mime, clip.ext) == (b"M4A", "audio/mp4", "m4a")


async def test_web_clip_tts_loi_het_han_het_quota_khong_raise(settings, monkeypatch):
    async def broken(text, **kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(orchestrator, "synthesize_speech", broken)
    monkeypatch.setattr(voice_reply, "ffmpeg_path", lambda: None)
    assert await voice_reply.web_clip(42, "Dạ anh.", voice="Leda", style="") is None
    with turn_deadline(3):  # còn < MIN_REMAINING_SEC: không gọi TTS
        assert await voice_reply.web_clip(42, "Dạ anh.", voice="Leda", style="") is None

    calls = []

    async def no_quota(user_id, *, channel, is_admin):
        calls.append((user_id, channel, is_admin))
        return False

    monkeypatch.setattr(usage_quota, "heavy_available", no_quota)
    assert await voice_reply.web_clip(42, "Dạ anh.", voice="Leda", style="") is None
    assert calls == [(42, "telegram", True)]


async def test_web_clip_tinh_quota_tts_nhu_chu_bot(settings, tts, monkeypatch):
    recorded = []

    async def available(user_id, *, channel, is_admin):
        return True

    async def record(user_id, kind, *, channel, is_admin):
        recorded.append((user_id, kind, channel, is_admin))

    monkeypatch.setattr(usage_quota, "heavy_available", available)
    monkeypatch.setattr(usage_quota, "record_heavy", record)
    assert await voice_reply.web_clip(42, "Dạ anh.", voice="Leda", style="") is not None
    assert recorded == [(42, usage_quota.KIND_TTS, "telegram", True)]
