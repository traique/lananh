"""Logic trang avatar (services/avatar_service.py) và giọng đọc ``voice_reply.web_clip``:
biểu cảm theo câu trả lời, câu chào theo giờ VN, chọn giọng (cài đặt > env > mặc định
Hoài My), cache giọng có trần, chữ hiển thị không markdown, TTS lỗi/hết quota không
raise, giọng Hoài My qua Edge TTS (giả, không gọi mạng) và đọc lại bằng Gemini khi lỗi."""

import asyncio
import sys
import time
import types
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


@pytest.fixture
def edge(monkeypatch):
    """edge-tts giả: ghi lại tham số; ``state`` chọn MP3 trả về, lỗi hoặc độ trễ."""
    state = {"calls": [], "chunks": [b"\xff\xf3" + bytes(2998), bytes(3000)], "error": None}
    state["delay"] = 0.0

    class Communicate:
        def __init__(self, text, voice, *, rate, volume, pitch, connect_timeout, receive_timeout):
            state["calls"].append(
                {"text": text, "voice": voice, "rate": rate, "pitch": pitch, "volume": volume}
            )

        async def stream(self):
            await asyncio.sleep(state["delay"])
            if state["error"] is not None:
                raise state["error"]
            yield {"type": "SentenceBoundary", "offset": 0, "duration": 1, "text": "x"}
            for data in state["chunks"]:
                yield {"type": "audio", "data": data}

    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    monkeypatch.setattr(voice_reply, "_edge_down_until", 0.0)
    for name in (
        "AVATAR_EDGE_RATE",
        "AVATAR_EDGE_PITCH",
        "AVATAR_EDGE_VOLUME",
        "AVATAR_EDGE_TIMEOUT_SEC",
    ):
        monkeypatch.delenv(name, raising=False)
    return state


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
    # Chưa chọn trên trang, chưa đặt env: Hoài My (Edge); Edge lỗi thì đọc lại bằng Achernar.
    assert avatar_service.default_voice() == "HoaiMy"
    assert await avatar_service.current_voice() == "HoaiMy"
    assert avatar_service.gemini_voice() == "Achernar"
    monkeypatch.setenv("AVATAR_TTS_VOICE", "Sulafat")  # env vẫn thắng mặc định
    assert await avatar_service.current_voice() == "Sulafat"
    assert avatar_service.gemini_voice() == "Sulafat"
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
    assert await avatar_service.set_voice("hoaimy") == "HoaiMy"
    assert settings[avatar_service.VOICE_SETTING_KEY] == "HoaiMy"
    assert await avatar_service.current_voice() == "HoaiMy"
    assert avatar_service.gemini_voice() == "Achernar"  # env là tên lạ -> giọng dự phòng
    settings[avatar_service.VOICE_SETTING_KEY] = "hỏng"  # giá trị hỏng trong DB -> env
    assert await avatar_service.current_voice() == "Kore"
    # Không giữ chung cài đặt /noi giong của Telegram.
    assert not any(key.startswith("voice_name:") for key in settings)


async def test_doc_cai_dat_loi_thi_dung_mac_dinh(settings, monkeypatch):
    async def broken(key):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_setting", broken)
    assert await avatar_service.current_voice() == "HoaiMy"
    assert await avatar_service.current_style() == "photo"


async def test_kieu_anh_chup_mac_dinh_ve_chibi_doi_duoc(settings):
    assert await avatar_service.current_style() == "photo"
    assert await avatar_service.set_style("drawn") == "drawn"
    assert settings[avatar_service.STYLE_SETTING_KEY] == "drawn"
    assert await avatar_service.current_style() == "drawn"
    with pytest.raises(ValueError):
        await avatar_service.set_style("anime")
    settings[avatar_service.STYLE_SETTING_KEY] = "hỏng"  # giá trị lạ trong DB -> ảnh chụp
    assert await avatar_service.current_style() == "photo"


def test_danh_sach_giong_hoai_my_dung_dau_va_phong_cach(settings, monkeypatch):
    voices = avatar_service.voices()
    names = [v["name"] for v in voices[:5]]
    assert names == ["HoaiMy", "Achernar", "Vindemiatrix", "Leda", "Sulafat"]
    assert voices[0]["title"] == "Hoài My"
    assert voices[0]["label"] == "Hoài My · Microsoft · tiếng Việt · miễn phí"
    assert voices[1]["label"] == "Achernar (mềm)" and voices[1]["title"] == "Achernar"
    assert all(v["suggested"] for v in voices[:5]) and not voices[5]["suggested"]
    assert len(voices) == len(voice_reply.VOICES) + 1
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


def test_hoai_my_chi_cho_trang_avatar_khong_lan_sang_noi():
    assert voice_reply.canonical_voice("HoaiMy") is None
    assert voice_reply.canonical_voice("hoaimy") is None
    assert avatar_service.canonical_voice("hoaimy") == "HoaiMy"
    assert avatar_service.canonical_voice("leda") == "Leda"
    assert avatar_service.canonical_voice("GiọngLạ") is None


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


def test_chia_doan_doc_doan_dau_ngan_doan_sau_gom_cau(settings, monkeypatch):
    assert voice_reply.web_chunks("**Dạ** anh 🥰") == ["Dạ anh."]
    assert voice_reply.web_chunks("🥰🥰") == []
    text = (
        "Dạ anh. Em đây nè, anh ăn cơm chưa? Hôm nay em nhớ anh nhiều lắm á. "
        "Giá vàng 1.000.000 đồng."
    )
    # Đoạn đầu gom tới đủ 40 ký tự; số "1.000.000" không bị cắt ở dấu chấm.
    assert voice_reply.web_chunks(text) == [
        "Dạ anh. Em đây nè, anh ăn cơm chưa? Hôm nay em nhớ anh nhiều lắm á.",
        "Giá vàng 1.000.000 đồng.",
    ]
    sentence = "Em kể anh nghe chuyện hôm nay ở chỗ làm vui lắm luôn đó anh."
    chunks = voice_reply.web_chunks(" ".join([sentence] * 9))
    assert len(chunks) > 2 and all(len(c) <= voice_reply.WEB_CHUNK_CHARS for c in chunks)
    assert " ".join(chunks).startswith(sentence) and chunks[0] == sentence
    # Dài quá VOICE_REPLY_MAX_CHARS: cắt như web_clip, đoạn cuối nhắc xem tin chữ.
    monkeypatch.setenv("VOICE_REPLY_MAX_CHARS", "200")
    chunks = voice_reply.web_chunks(" ".join([sentence] * 9))
    assert chunks[-1].endswith("xem trong tin nhắn chữ nha.")
    assert len(" ".join(chunks)) < 260


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


# ─── Giọng Hoài My (Edge TTS) ───────────────────────────────────────────────


async def test_web_clip_hoai_my_doc_bang_edge_mien_phi(settings, tts, edge, monkeypatch):
    recorded = []

    async def no_quota(user_id, *, channel, is_admin):
        return False  # hết lượt nặng vẫn đọc được bằng Edge

    async def record(user_id, kind, *, channel, is_admin):
        recorded.append(kind)

    monkeypatch.setattr(usage_quota, "heavy_available", no_quota)
    monkeypatch.setattr(usage_quota, "record_heavy", record)
    clip = await voice_reply.web_clip(
        42, "**Dạ** anh 🥰 150k nha", voice="HoaiMy", style="thủ thỉ", fallback_voice="Achernar"
    )
    assert (clip.mime, clip.ext, clip.voice) == ("audio/mpeg", "mp3", "HoaiMy")
    assert clip.data == b"".join(edge["chunks"]) and clip.duration_ms == 1000  # 48 kbps
    assert edge["calls"] == [
        {
            "text": "Dạ anh một trăm năm mươi nghìn nha.",
            "voice": "vi-VN-HoaiMyNeural",
            "rate": "-6%",
            "pitch": "+12Hz",
            "volume": "+0%",
        }
    ]
    assert tts == [] and recorded == []  # không gọi Gemini, không tính quota TTS
    monkeypatch.setenv("AVATAR_EDGE_RATE", "-10%")
    monkeypatch.setenv("AVATAR_EDGE_PITCH", "+20Hz")
    await voice_reply.web_clip(42, "Dạ anh.", voice="HoaiMy", style="")
    assert (edge["calls"][-1]["rate"], edge["calls"][-1]["pitch"]) == ("-10%", "+20Hz")


async def test_edge_loi_het_gio_rong_thi_doc_lai_bang_gemini(settings, tts, edge, monkeypatch):
    monkeypatch.setattr(voice_reply, "edge_timeout_sec", lambda: 0.05)
    monkeypatch.setattr(voice_reply, "MAX_EDGE_BYTES", 5000)

    def broken():
        edge["error"] = RuntimeError("403 Forbidden")

    def slow():
        edge["delay"] = 1.0

    def silent():
        edge["chunks"] = []

    def too_big():
        edge["chunks"] = [bytes(6000)]

    def missing_package():
        monkeypatch.setitem(sys.modules, "edge_tts", None)

    for make_fail in (broken, slow, silent, too_big, missing_package):
        edge.update(error=None, delay=0.0, chunks=[bytes(600)])
        voice_reply._edge_down_until = 0.0  # mỗi kiểu lỗi thử Edge lại từ đầu
        make_fail()
        clip = await voice_reply.web_clip(
            42, "Dạ anh.", voice="HoaiMy", style="thủ thỉ", fallback_voice="Achernar"
        )
        assert clip is not None and (clip.ext, clip.voice) == ("wav", "Achernar"), make_fail
        assert tts[-1]["voice"] == "Achernar" and tts[-1]["style"] == "thủ thỉ", make_fail
    assert len(tts) == 5
    # Không truyền giọng dự phòng: VOICE_TTS_VOICE.
    voice_reply._edge_down_until = 0.0
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    clip = await voice_reply.web_clip(42, "Dạ anh.", voice="HoaiMy", style="")
    assert clip.voice == "Sulafat"


async def test_edge_ban_hoac_het_han_chot_thi_khong_doc_lai(settings, tts, edge, monkeypatch):
    # Cổng đọc đang bận (lượt /noi khác): chỉ hiện chữ, không đọc lại bằng Gemini.
    monkeypatch.setattr(voice_reply, "GATE_WAIT_SEC", 0.05)
    await voice_reply._gate.acquire()
    try:
        assert await voice_reply.web_clip(42, "Dạ anh.", voice="HoaiMy", style="") is None
    finally:
        voice_reply._gate.release()
    # Hết hạn chót của lượt trong lúc Edge đang đọc: thôi, không đọc lại.
    monkeypatch.setattr(voice_reply, "MIN_REMAINING_SEC", 0.0)
    edge["delay"] = 1.0
    with turn_deadline(0.1):
        assert await voice_reply.web_clip(42, "Dạ anh.", voice="HoaiMy", style="") is None
    assert tts == []


async def test_edge_loi_thi_10_phut_doc_thang_bang_gemini(settings, tts, edge):
    assert voice_reply.edge_timeout_sec() == 8
    edge["error"] = RuntimeError("403 Forbidden")
    clip = await voice_reply.web_clip(
        42, "Dạ anh.", voice="HoaiMy", style="", fallback_voice="Leda"
    )
    assert clip.voice == "Leda" and len(edge["calls"]) == 1
    left = voice_reply._edge_down_until - time.monotonic()
    assert voice_reply.EDGE_COOLDOWN_SEC - 5 < left <= voice_reply.EDGE_COOLDOWN_SEC
    # Câu sau: không chờ Edge lại, đọc thẳng bằng giọng Gemini.
    edge["error"] = None
    clip = await voice_reply.web_clip(42, "Dạ em.", voice="HoaiMy", style="", fallback_voice="Leda")
    assert clip.voice == "Leda" and len(edge["calls"]) == 1 and len(tts) == 2
    voice_reply._edge_down_until = 0.0  # hết 10 phút
    clip = await voice_reply.web_clip(42, "Dạ em.", voice="HoaiMy", style="", fallback_voice="Leda")
    assert clip.voice == "HoaiMy" and len(edge["calls"]) == 2 and len(tts) == 2


async def test_giong_gemini_tren_avatar_chi_doc_bang_live(settings, monkeypatch):
    calls = []
    failing = False

    async def synthesize(text, *, model, voice, timeout_sec, engine="tts", style=""):
        calls.append((engine, timeout_sec))
        if failing:
            raise RuntimeError("Gemini Live đọc lệch chữ")
        return orchestrator.Speech(PCM, "audio/L16;codec=pcm;rate=24000", "live_api1")

    monkeypatch.setattr(orchestrator, "synthesize_speech", synthesize)
    monkeypatch.setattr(voice_reply, "ffmpeg_path", lambda: None)
    monkeypatch.setenv("VOICE_ENGINE", "auto")  # /noi giữ auto, trang avatar vẫn chỉ Live
    assert await voice_reply.web_clip(42, "Dạ anh.", voice="Leda", style="") is not None
    assert calls == [("live", voice_reply.WEB_TTS_TIMEOUT_SEC)]
    failing = True
    assert await voice_reply.web_clip(42, "Dạ anh.", voice="Leda", style="") is None
    assert len(calls) == 2  # Live lỗi: chỉ hiện chữ, không đọc lại bằng Gemini TTS


def test_tham_so_edge_sai_dinh_dang_thi_dung_mac_dinh(monkeypatch):
    monkeypatch.setenv("AVATAR_EDGE_RATE", "-10%")
    monkeypatch.setenv("AVATAR_EDGE_PITCH", " +5Hz ")
    monkeypatch.setenv("AVATAR_EDGE_VOLUME", "-20%")
    assert voice_reply.edge_prosody() == {"rate": "-10%", "pitch": "+5Hz", "volume": "-20%"}
    for rate, pitch, volume in (
        ("10%", "+12", "loud"),
        ("-6", "12Hz", "+5Hz"),
        ("+5 %", "+1hz", ""),
    ):
        monkeypatch.setenv("AVATAR_EDGE_RATE", rate)
        monkeypatch.setenv("AVATAR_EDGE_PITCH", pitch)
        monkeypatch.setenv("AVATAR_EDGE_VOLUME", volume)
        assert voice_reply.edge_prosody() == {"rate": "-6%", "pitch": "+12Hz", "volume": "+0%"}
