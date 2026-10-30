"""Trả lời kèm giọng nói (ý từ ethanplusai/jarvis, isair/jarvis) - lệnh /noi.

- /noi on|off|auto theo TỪNG người (mặc định off; ``auto`` = chỉ đọc khi người dùng
  vừa nhắn tin thoại). Cổng toàn bot VOICE_REPLY_ENABLED (mặc định 1, người dùng
  vẫn phải tự bật). Chủ bot Telegram/Zoom dùng chung 1 cài đặt; mỗi Zalo (kể cả
  Zalo của chủ bot) riêng.
- Bản chữ LUÔN gửi như cũ; tin thoại đi kèm SAU bản chữ. Lỗi đọc giọng (TTS hỏng,
  hết quota, hết hạn chót, ffmpeg lỗi) chỉ log - không bao giờ làm hỏng câu chữ.
- Chỉ đọc câu trả lời trò chuyện/tra cứu nhanh (không đọc lệnh "/", báo cáo cổ phiếu,
  câu hỏi duyệt cách trả lời). Chữ được lọc trước khi đọc (``speakable_text``): bỏ
  khối code, bảng, link, markdown, emoji, dòng "⚙️ API"; trần VOICE_REPLY_MAX_CHARS
  (600) - dài hơn thì đọc đoạn đầu (cắt ở cuối câu) và nói rõ phần còn lại xem
  trong tin chữ.
- Tổng hợp giọng (``orchestrator.synthesize_speech``) theo VOICE_ENGINE:
  ``auto`` (mặc định) = Gemini Live trước (ai/live_client.py: đọc NGUYÊN VĂN, bản
  chép lời phải giống văn bản >= LIVE_VOICE_MIN_MATCH%, cooldown quota riêng
  "live_api1/2"), lỗi/quota/đọc lệch thì Gemini TTS; ``live`` = chỉ Live (lỗi thì
  chỉ gửi chữ); ``tts`` = chỉ Gemini TTS (api1 -> api2, cooldown quota TTS riêng
  "tts_api1/2"). Mỗi lượt qua ``ai.timeouts.bounded()`` trong hạn chót của lượt.
  Model TTS VOICE_TTS_MODEL (mặc định gemini-3.8-flash-tts), giọng dựng sẵn
  VOICE_TTS_VOICE (mặc định Sulafat "ấm" - Live dùng cùng bộ giọng, đều đọc được
  tiếng Việt); mỗi người đổi giọng riêng bằng ``/noi giong <tên>``. Chỉ dẫn phong
  cách đọc VOICE_STYLE_PROMPT (giọng nữ trẻ miền Nam, thân mật, có ngắt nghỉ): TTS
  3.8 nhận qua annotation speech_metadata (ai/official_client.py), Live qua chỉ dẫn
  hệ thống. Đầu ra PCM 16-bit mono 24 kHz (thô, hoặc WAV có header với model TTS
  3.8) - ``to_pcm`` nhận cả hai.
- Trước khi đọc, chữ được chuẩn hoá (services/tts_normalize.py + từ điển
  assets/tts_viet_tat.yaml): số tiền, %, ngày giờ, đơn vị, viết tắt, mã cổ phiếu
  đọc từng chữ cái. Live so bản chép lời với bản ĐÃ chuẩn hoá.
- Định dạng (không thêm package/apt): có ffmpeg trên PATH thì Telegram nhận
  OGG/Opus qua sendVoice; không có thì đóng gói WAV bằng module ``wave`` và gửi
  sendDocument (Bot API: sendVoice chỉ nhận OGG/Opus, MP3, M4A; sendAudio chỉ MP3,
  M4A). Zalo (zca-js sendVoice gửi link m4a cho app Zalo tự phát) CHỈ bật khi có
  ffmpeg (đổi sang M4A/AAC) - WAV không chắc phát được trên app Zalo.
- Zoom: chatbot Team Chat không gửi được file/âm thanh -> kèm 1 dòng
  "🔊 Nghe giọng Lan Anh: <link>" tới ``/media/tts/<tên>`` (M4A, cùng kho RAM, cần
  URL công khai WEBHOOK_BASE_URL/RENDER_EXTERNAL_URL).
- Zalo: clip M4A nằm trong kho RAM có trần (``ClipStore``: tối đa 12 file và
  VOICE_REPLY_STORE_MB, hết hạn sau VOICE_REPLY_URL_TTL_MIN phút), tên file ký HMAC
  có hạn (khoá ngẫu nhiên theo process - restart là link cũ hết hiệu lực, đúng như
  kho RAM). Gateway tải qua loopback ``/internal/zalo/tts/<tên>``, tải lên CDN Zalo
  rồi sendVoice; tải lên lỗi thì sendVoice thẳng link công khai ``/media/tts/<tên>``
  (cần WEBHOOK_BASE_URL/RENDER_EXTERNAL_URL) - channels/tts_media.py.
- Thành viên Zalo: mỗi tin thoại tính 1 lượt nặng (services/usage_quota.py); hết
  lượt nặng thì chỉ gửi chữ.
- Trang avatar (/admin/avatar, web_avatar.py): ``web_clip`` đọc với giọng và phong cách
  riêng của trang (không xét /noi hay kênh), ra M4A cho trình duyệt (không có ffmpeg
  thì WAV). Riêng trang avatar có thêm giọng Hoài My của Microsoft Edge TTS (gói
  edge-tts, dịch vụ không chính thức, miễn phí - không tính quota TTS): MP3 phát thẳng
  trên trình duyệt; lỗi/hết giờ/rỗng thì đọc lại bằng giọng Gemini. /noi và
  ``canonical_voice`` vẫn chỉ biết giọng Gemini.

RAM: tối đa 1 lượt tổng hợp + đổi định dạng cùng lúc (semaphore); PCM tối đa
MAX_PCM_BYTES; ffmpeg ghi file tạm trong MEDIA_DIR rồi xoá ngay.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import functools
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
import unicodedata
import wave
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ai.timeouts import TurnTimeoutError, bounded, remaining_turn_seconds
from core import config, memtrim
from core import database as db
from core.envutil import env_int, env_on
from core.text_normalize import fold
from core.text_utils import parse_on_off
from core.urls import URL_RE
from services import (
    personal_settings,
    prompt_redaction,
    reply_audience,
    tts_normalize,
    usage_quota,
)

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_ON = "on"
MODE_AUTO = "auto"
MODES = (MODE_OFF, MODE_ON, MODE_AUTO)
DEFAULT_MODEL = "gemini-3.8-flash-tts"
# Sulafat ("Warm") thay Kore ("Firm" - nghe cứng, "rất AI" theo phản hồi 27/10).
DEFAULT_VOICE = "Sulafat"
# Chỉ dẫn phong cách bằng lời (Gemini TTS: annotation speech_metadata.style; Live:
# chỉ dẫn hệ thống). VOICE_STYLE_PROMPT đổi được; "off" = không gửi phong cách.
DEFAULT_STYLE = (
    "Giọng nữ trẻ miền Nam, ấm áp, thân mật như đang nhắn tin thoại cho người nhà; "
    "nói chuyện tự nhiên, nhịp vừa phải, có ngắt nghỉ ở dấu phẩy và cuối câu, lên "
    "xuống giọng nhẹ nhàng theo ý câu, mỉm cười khi chào hỏi; không đọc đều đều như "
    "máy, không nhấn nhá kiểu phát thanh viên."
)
# 30 giọng dựng sẵn của Gemini TTS (ai.google.dev/gemini-api/docs/speech-generation,
# 10/2026) - Live dùng cùng bộ giọng. Mô tả theo tài liệu, dịch ngắn.
VOICES = {
    "Zephyr": "sáng",
    "Puck": "vui tươi",
    "Charon": "rõ ràng, kiểu thông tin",
    "Kore": "chắc, dứt khoát",
    "Fenrir": "hào hứng",
    "Leda": "trẻ trung",
    "Orus": "chắc",
    "Aoede": "nhẹ nhàng, thoáng",
    "Callirrhoe": "thoải mái",
    "Autonoe": "sáng",
    "Enceladus": "hơi thở nhẹ",
    "Iapetus": "rõ",
    "Umbriel": "thoải mái",
    "Algieba": "mượt",
    "Despina": "mượt",
    "Erinome": "rõ",
    "Algenib": "khàn",
    "Rasalgethi": "kiểu thông tin",
    "Laomedeia": "vui tươi",
    "Achernar": "mềm",
    "Alnilam": "chắc",
    "Schedar": "đều",
    "Gacrux": "chín chắn",
    "Pulcherrima": "thẳng thắn",
    "Achird": "thân thiện",
    "Zubenelgenubi": "bình dân",
    "Vindemiatrix": "dịu dàng",
    "Sadachbia": "sôi nổi",
    "Sadaltager": "hiểu biết",
    "Sulafat": "ấm",
}
# Gợi ý khi gõ /noi giong (giọng nữ hợp persona Lan Anh).
SUGGESTED_VOICES = ("Sulafat", "Leda", "Aoede", "Vindemiatrix", "Achernar", "Despina", "Kore")
_MB = 1024 * 1024
# Một lượt TTS ~600 ký tự mất 5-20 giây; trần riêng để provider treo không ăn hết lượt.
TTS_TIMEOUT_SEC = 45.0
# Trang avatar đọc từng câu: 1 câu Gemini Live quá chừng này thì thôi, chỉ hiện chữ.
WEB_TTS_TIMEOUT_SEC = 25.0
# Còn ít hơn chừng này giây của hạn chót lượt thì thôi đọc (câu chữ đã/ sắp gửi).
MIN_REMAINING_SEC = 8.0
FFMPEG_TIMEOUT_SEC = 20.0
GATE_WAIT_SEC = 10.0
# 600 ký tự đọc ~45-60 giây = ~2,9 MB PCM 24 kHz; trần dư cho câu đọc chậm.
MAX_PCM_BYTES = 8 * _MB
# Giọng Microsoft Edge TTS - chỉ trang avatar dùng (web_clip), /noi không nhận.
EDGE_VOICES = {"HoaiMy": "vi-VN-HoaiMyNeural"}
# edge-tts trả MP3 24 kHz 48 kbps CBR = 6 byte mỗi ms; 600 ký tự ~60 giây ~360 KB.
EDGE_MP3_BYTES_PER_MS = 6
MAX_EDGE_BYTES = 2 * _MB
# Edge lỗi 1 lần thì các câu sau đọc thẳng bằng giọng Gemini chừng này giây, không chờ lại.
EDGE_COOLDOWN_SEC = 600.0
# Trang avatar đọc từng đoạn: đoạn đầu chỉ cần chừng này ký tự để giọng bắt đầu sớm, các
# đoạn sau gom câu tới trần (VOICE_REPLY_MAX_CHARS 600 -> 3-4 đoạn).
WEB_FIRST_CHUNK_CHARS = 40
WEB_CHUNK_CHARS = 220
_SENTENCE_GAP_RE = re.compile(r"(?<=[.!?…])\s+")
_PERCENT = re.compile(r"[+-]\d{1,3}%")
_HERTZ = re.compile(r"[+-]\d{1,3}Hz")
# Kho clip Zalo/Zoom: tin thoại M4A 48 kbps ~0,35 MB/phút -> 12 file / 4 MB đủ cho
# vài người trong 10 phút hiệu lực link (trước 27/10: 20 file / 8 MB).
STORE_MAX_FILES = 12
MIN_SPEAK_CHARS = 2
TELEGRAM_WAV_NAME = "lan-anh-tra-loi.wav"

_gate = asyncio.Semaphore(1)
_ffmpeg_warned = False
_edge_down_until = 0.0


def enabled() -> bool:
    return env_on("VOICE_REPLY_ENABLED")


def max_chars() -> int:
    return env_int("VOICE_REPLY_MAX_CHARS", 600, 100, 2000)


def tts_model() -> str:
    return os.getenv("VOICE_TTS_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def tts_voice() -> str:
    return canonical_voice(os.getenv("VOICE_TTS_VOICE", DEFAULT_VOICE)) or DEFAULT_VOICE


def canonical_voice(name: str) -> str | None:
    """Tên giọng đúng chữ hoa theo VOICES (không phân biệt hoa/thường); None nếu lạ."""
    wanted = (name or "").strip().lower()
    return next((voice for voice in VOICES if voice.lower() == wanted), None)


def style_prompt() -> str:
    """VOICE_STYLE_PROMPT (trống = mặc định; "off"/"0"/"-" = không gửi phong cách)."""
    raw = " ".join(os.getenv("VOICE_STYLE_PROMPT", "").split())
    if raw.lower() in ("off", "0", "-", "none", "tat", "tắt"):
        return ""
    return (raw or DEFAULT_STYLE)[:500]


async def get_voice(user_id: int) -> str:
    """Giọng riêng của người này (/noi giong), không có thì VOICE_TTS_VOICE."""
    try:
        raw = await db.get_setting(personal_settings.voice_name_key(user_id))
    except Exception:
        raw = None
    return canonical_voice(raw or "") or tts_voice()


def voice_engine() -> str:
    """VOICE_ENGINE: auto (Live rồi TTS) | live | tts; giá trị lạ -> auto."""
    raw = os.getenv("VOICE_ENGINE", "auto").strip().lower()
    return raw if raw in ("auto", "live", "tts") else "auto"


def url_ttl_sec() -> int:
    return env_int("VOICE_REPLY_URL_TTL_MIN", 10, 1, 120) * 60


def store_max_bytes() -> int:
    return env_int("VOICE_REPLY_STORE_MB", 4, 1, 64) * _MB


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def public_base() -> str | None:
    base = (config.WEBHOOK_BASE_URL or "").rstrip("/")
    return base if base.startswith("https://") else None


def channel_supported(channel: str) -> bool:
    """Kênh gửi được tin thoại trả lời: Telegram luôn (OGG hoặc file WAV), Zalo chỉ
    khi có ffmpeg (M4A). Zoom: chatbot Team Chat KHÔNG gửi được file/âm thanh (chỉ
    tin chữ/thẻ có link - developers.zoom.us, 10/2026) nên gửi 1 dòng link nghe
    ``/media/tts/<tên>`` - cần URL công khai (WEBHOOK_BASE_URL/RENDER_EXTERNAL_URL)."""
    if channel == "telegram":
        return True
    if channel == "zoom":
        return public_base() is not None
    return channel == "zalo" and ffmpeg_path() is not None


# ─── Cài đặt /noi ────────────────────────────────────────────────────────────


async def get_mode(user_id: int) -> str:
    try:
        raw = (await db.get_setting(personal_settings.voice_reply_key(user_id)) or "").strip()
    except Exception:
        logger.warning("Không đọc được cài đặt /noi của user_id=%s, coi như tắt.", user_id)
        return MODE_OFF
    return raw if raw in MODES else MODE_OFF


def _wants(mode: str, voice_in: bool) -> bool:
    return mode == MODE_ON or (mode == MODE_AUTO and voice_in)


async def wants_voice(user_id: int, *, voice_in: bool = False) -> bool:
    """Lượt này có nên đọc kèm không (cổng toàn bot + cài đặt của người dùng)."""
    return enabled() and _wants(await get_mode(user_id), voice_in)


_VOICE_ARG_RE = re.compile(r"^(?:giong|voice)(?:\s|$)")
_RESET_WORDS = {"mac dinh", "macdinh", "default", "reset", "xoa", "bo"}


async def _voice_command(user_id: int, name: str, me: str, you: str) -> str:
    """/noi giong [tên|mặc định]: xem gợi ý / đổi giọng đọc của CHÍNH người gõ."""
    key = personal_settings.voice_name_key(user_id)
    if name and fold(name) in _RESET_WORDS:
        await db.set_setting(key, "")
        return f"🔊 Đã về giọng mặc định {tts_voice()} ({VOICES.get(tts_voice(), '')})."
    if name:
        voice = canonical_voice(name)
        if voice is None:
            return (
                f"{me.capitalize()} chưa có giọng “{name}”. Gợi ý: "
                + ", ".join(SUGGESTED_VOICES)
                + ". Gõ /noi giong để xem mô tả nha."
            )
        await db.set_setting(key, voice)
        return f"🔊 Từ giờ {me} đọc bằng giọng {voice} ({VOICES[voice]}) cho {you} nha."
    current = await get_voice(user_id)
    lines = [f"🎙️ Giọng đang dùng: {current} ({VOICES.get(current, '')}). Gợi ý giọng nữ:"]
    lines += [f"• {voice} - {VOICES[voice]}" for voice in SUGGESTED_VOICES]
    lines.append(
        "Đổi: /noi giong <tên> (vd /noi giong Leda) · về mặc định: /noi giong mac dinh. "
        f"Dùng được cả {len(VOICES)} giọng Gemini dựng sẵn."
    )
    return "\n".join(lines)


def _parse_mode(argument: str) -> str | None:
    arg = fold(argument or "")
    if arg in {"auto", "tu dong", "tudong"}:
        return MODE_AUTO
    toggle = parse_on_off(arg)
    if toggle is None:
        return None
    return MODE_ON if toggle else MODE_OFF


async def noi_command(user_id: int, argument: str, *, channel: str, is_admin: bool = True) -> str:
    """/noi [on|off|auto]: đặt/xem chế độ trả lời kèm giọng nói của CHÍNH người gõ."""
    you, me = reply_audience.forms()
    if not enabled():
        return "🔇 Trả lời bằng giọng nói đang tắt trên bot (VOICE_REPLY_ENABLED=0)."
    arg = " ".join((argument or "").split())
    if _VOICE_ARG_RE.match(fold(arg)):
        words = arg.split()[1:]
        if words and fold(words[0]) == "doc":  # "/noi giọng đọc Leda"
            words = words[1:]
        return await _voice_command(user_id, " ".join(words), me, you)
    key = personal_settings.voice_reply_key(user_id)
    if arg:
        mode = _parse_mode(arg)
        if mode is None:
            return f"{me.capitalize()} chưa hiểu “{arg}”. Dùng /noi on · /noi auto · /noi off nha."
        await db.set_setting(key, mode)
    mode = await get_mode(user_id)
    state = {
        MODE_ON: f"BẬT - câu trả lời trò chuyện có kèm tin thoại {me} đọc",
        MODE_AUTO: f"TỰ ĐỘNG - {me} chỉ đọc khi {you} vừa gửi tin thoại",
        MODE_OFF: "TẮT - chỉ gửi chữ",
    }[mode]
    lines = [
        f"🔊 Trả lời bằng giọng nói: {state}.",
        f"Bản chữ vẫn gửi như cũ, tin thoại đi kèm sau. {me.capitalize()} đọc tối đa "
        f"{max_chars()} ký tự (bỏ bảng, link, ký hiệu); dài hơn thì đọc đoạn đầu rồi nhắc "
        f"{you} xem phần chữ. Lệnh /, báo cáo cổ phiếu thì {me} không đọc.",
    ]
    if channel == "telegram":
        if ffmpeg_path() is None:
            lines.append(
                f"Máy chủ chưa có ffmpeg nên {me} gửi file WAV kèm theo (có ffmpeg thì thành "
                "tin thoại)."
            )
    elif channel == "zalo":
        if ffmpeg_path() is None:
            lines.append(
                f"⚠️ Trên Zalo cần máy chủ có ffmpeg để gửi tin thoại - hiện chưa có nên {me} "
                "vẫn chỉ gửi chữ."
            )
        if usage_quota.applies(channel, is_admin):
            lines.append("Mỗi tin thoại tính 1 lượt nặng trong ngày.")
    elif public_base() is None:
        lines.append(
            f"Zoom không gửi được tin thoại và máy chủ chưa có link công khai nên {me} chỉ "
            f"gửi chữ - cài đặt này dùng chung với Telegram của {you}."
        )
    else:
        lines.append(
            f"Zoom không nhận tin thoại từ bot nên {me} gửi kèm 1 link nghe (hết hạn sau "
            f"{url_ttl_sec() // 60} phút) - cài đặt này dùng chung với Telegram của {you}."
        )
    lines.append(f"Giọng đang dùng: {await get_voice(user_id)} (xem/đổi: /noi giong).")
    lines.append("Đổi: /noi on · /noi auto · /noi off · /noi giong")
    return "\n".join(lines)


# ─── Lọc chữ để đọc ──────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\([^()\s]+\)")
_TABLE_SEP_RE = re.compile(r"^\s*[-:|+ ]{3,}\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_BULLET_RE = re.compile(r"^\s*(?:[-*+•▪◦‣]|\d{1,2}[.)])\s+")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_INLINE_CODE_RE = re.compile(r"`([^`\n]*)`")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>\n]*>")
_LEFTOVER_RE = re.compile(r"[*#`|~^<>\[\]{}\\]")
_ARROWS = {
    "→": ", ",
    "⇒": ", ",
    "←": ", ",
    "↑": " tăng ",
    "↓": " giảm ",
    "≈": " khoảng ",
    "▲": " tăng ",
    "▼": " giảm ",
    "🔺": " tăng ",
    "🔻": " giảm ",
    "±": " cộng trừ ",
    "~": " khoảng ",
    "≥": " lớn hơn hoặc bằng ",
    "≤": " nhỏ hơn hoặc bằng ",
}
_SENTENCE_END = ".!?…:;,"


def _is_symbol(ch: str) -> bool:
    if ch in "‍️︎⃣":
        return True
    if ch in "°$€":  # đơn vị đọc được (30°C, $5) - services/tts_normalize.py đọc thành chữ
        return False
    category = unicodedata.category(ch)
    return category in ("So", "Sk", "Cs", "Co", "Cn") or (category == "Sm" and ch not in "+=%<>")


def speakable_text(text: str) -> str:
    """Chữ đọc được thành tiếng: bỏ khối code, bảng, link, markdown, emoji, dòng ⚙️."""
    text = _FENCE_RE.sub(" ", text or "")
    pieces: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("⚙️") or line.startswith("⚙"):
            continue
        if line.count("|") >= 2 or _TABLE_SEP_RE.match(line):
            continue  # dòng bảng
        line = _HEADING_RE.sub("", line)
        line = _BULLET_RE.sub("", line)
        line = _MD_LINK_RE.sub(r"\1", line)
        line = URL_RE.sub(" ", line)
        line = _INLINE_CODE_RE.sub(r"\1", line)
        line = _HTML_TAG_RE.sub(" ", line)
        for _ in range(2):
            line = _EMPHASIS_RE.sub(r"\2", line)
        for arrow, spoken in _ARROWS.items():
            line = line.replace(arrow, spoken)
        line = "".join(" " if _is_symbol(ch) else ch for ch in line)
        line = _LEFTOVER_RE.sub(" ", line)
        line = " ".join(line.split()).strip(" -–—")
        if not re.search(r"[^\W_]", line):
            continue
        if line[-1] not in _SENTENCE_END:
            line += "."
        pieces.append(line)
    return " ".join(pieces).strip()


def fit(text: str, limit: int, you: str) -> tuple[str, bool]:
    """(câu đọc, đã cắt?): dài quá ``limit`` thì giữ đoạn đầu (cắt ở cuối câu nếu
    được) và thêm lời nhắc xem phần còn lại trong tin chữ."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    end = max(cut.rfind(mark) for mark in ".!?…")
    if end >= int(limit * 0.4):
        cut = cut[: end + 1]
    else:
        space = cut.rfind(" ")
        cut = (cut[:space] if space > 0 else cut).rstrip(" ,;:") + "…"
    return f"{cut.strip()} Phần còn lại {you} xem trong tin nhắn chữ nha.", True


# ─── Âm thanh ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Clip:
    data: bytes
    mime: str
    ext: str  # "ogg" | "m4a" | "wav" | "mp3"
    duration_ms: int
    truncated: bool = False
    voice: str = ""  # giọng đã đọc thật (giọng Edge lỗi thì là giọng Gemini đọc lại)

    @property
    def duration_sec(self) -> int:
        return max(1, round(self.duration_ms / 1000))


def to_pcm(audio: bytes, mime: str) -> tuple[bytes, int, int]:
    """(PCM 16-bit little-endian, tần số, số kênh) từ đầu ra Gemini TTS: WAV có
    header RIFF (model 3.8) hoặc PCM thô "audio/L16;codec=pcm;rate=24000"."""
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        with wave.open(io.BytesIO(audio), "rb") as reader:
            if reader.getsampwidth() != 2:
                raise ValueError(f"WAV {reader.getsampwidth() * 8}-bit chưa hỗ trợ")
            return (
                reader.readframes(reader.getnframes()),
                reader.getframerate(),
                max(1, reader.getnchannels()),
            )
    match = re.search(r"rate=(\d+)", mime or "")
    rate = int(match.group(1)) if match else 24000
    return audio, rate, 1


def wav_bytes(pcm: bytes, rate: int, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm)
    return buffer.getvalue()


def duration_ms(pcm: bytes, rate: int, channels: int = 1) -> int:
    return int(len(pcm) * 1000 / max(1, 2 * channels * rate))


_FFMPEG_ARGS = {
    "ogg": ("-c:a", "libopus", "-b:a", "32k", "-application", "voip"),
    "m4a": ("-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart"),
}
MIMES = {"ogg": "audio/ogg", "m4a": "audio/mp4", "wav": "audio/wav", "mp3": "audio/mpeg"}


def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_and_remove(path: str) -> bytes:
    try:
        with open(path, "rb") as file:
            return file.read()
    finally:
        _remove_quietly(path)


async def ffmpeg_encode(pcm: bytes, rate: int, channels: int, fmt: str) -> bytes | None:
    """PCM -> OGG/Opus hoặc M4A/AAC bằng ffmpeg trên PATH; None nếu không có ffmpeg,
    quá hạn hoặc lỗi. Đầu ra ghi file tạm trong MEDIA_DIR (MP4 cần ghi tua lại)."""
    global _ffmpeg_warned
    binary = ffmpeg_path()
    if binary is None or fmt not in _FFMPEG_ARGS:
        return None
    config.ensure_media_dir()
    fd, path = tempfile.mkstemp(prefix="tts_", suffix=f".{fmt}", dir=str(config.MEDIA_DIR))
    os.close(fd)
    remaining = remaining_turn_seconds()
    timeout = (
        FFMPEG_TIMEOUT_SEC if remaining is None else max(1.0, min(FFMPEG_TIMEOUT_SEC, remaining))
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(rate),
            "-ac",
            str(channels),
            "-i",
            "pipe:0",
            *_FFMPEG_ARGS[fmt],
            path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(pcm), timeout)
        except (TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            if not _ffmpeg_warned:
                _ffmpeg_warned = True
                logger.warning(
                    "ffmpeg đổi %s lỗi (mã %s): %s",
                    fmt,
                    proc.returncode,
                    (stderr or b"").decode("utf-8", "replace")[:300],
                )
            return None
        data = await asyncio.to_thread(_read_and_remove, path)
        return data or None
    except TimeoutError:
        logger.warning("ffmpeg đổi %s quá %.0fs, bỏ tin thoại.", fmt, timeout)
        return None
    except OSError:
        logger.warning("Không chạy được ffmpeg.", exc_info=True)
        return None
    finally:
        await asyncio.to_thread(_remove_quietly, path)


async def _encode(pcm: bytes, rate: int, channels: int, channel: str) -> tuple[bytes, str] | None:
    """(dữ liệu, đuôi) theo kênh: Telegram OGG (ffmpeg) hoặc WAV; Zalo M4A (ffmpeg);
    Zoom (link nghe trên trình duyệt) và trang avatar ("web") M4A - trình duyệt nào
    cũng phát được, không có ffmpeg thì WAV."""
    if channel == "zalo":
        data = await ffmpeg_encode(pcm, rate, channels, "m4a")
        return (data, "m4a") if data else None
    if channel in ("zoom", "web"):
        data = await ffmpeg_encode(pcm, rate, channels, "m4a")
        if data:
            return data, "m4a"
        return await asyncio.to_thread(wav_bytes, pcm, rate, channels), "wav"
    data = await ffmpeg_encode(pcm, rate, channels, "ogg")
    if data:
        return data, "ogg"
    return await asyncio.to_thread(wav_bytes, pcm, rate, channels), "wav"


async def make_clip(
    user_id: int,
    text: str,
    *,
    channel: str,
    voice_in: bool = False,
    is_admin: bool = True,
) -> Clip | None:
    """Tin thoại đọc ``text`` nếu người này muốn (/noi) và kênh gửi được; None nếu
    không cần/không làm được. KHÔNG raise (kể cả hết hạn chót lượt)."""
    try:
        if not await wants_voice(user_id, voice_in=voice_in):
            return None
        if not channel_supported(channel):
            return None
        you, _ = reply_audience.forms()
        # Câu trả lời có thể chép lại bí mật code đã khôi phục (vd ghi chú mật khẩu
        # wifi): che như mọi prompt gửi provider (services/prompt_redaction.py).
        hidden = prompt_redaction.Redactor().text(text)
        speech, truncated = fit(speakable_text(hidden), max_chars(), you)
        # Viết tắt, số tiền, ngày giờ, mã cổ phiếu -> chữ đọc được (tin chữ không đổi).
        # Live so bản chép lời với CHÍNH bản đã chuẩn hoá này.
        speech = tts_normalize.normalize(speech)
        if len(speech) < MIN_SPEAK_CHARS:
            return None
        remaining = remaining_turn_seconds()
        if remaining is not None and remaining < MIN_REMAINING_SEC:
            logger.info("Còn %.1fs của lượt - bỏ tin thoại trả lời.", remaining)
            return None
        if not await usage_quota.heavy_available(user_id, channel=channel, is_admin=is_admin):
            return None
        clip = await _synthesize(speech, channel, truncated, voice=await get_voice(user_id))
        if clip is not None:
            await usage_quota.record_heavy(
                user_id, usage_quota.KIND_TTS, channel=channel, is_admin=is_admin
            )
        return clip
    except TurnTimeoutError:
        logger.info("Hết hạn chót lượt khi đọc giọng - chỉ gửi chữ.")
        return None
    except Exception:
        logger.warning("Không tạo được tin thoại trả lời - chỉ gửi chữ.", exc_info=True)
        return None


def web_chunks(text: str) -> list[str]:
    """Câu đọc của 1 câu trả lời trên trang avatar, chia theo câu để đọc từng đoạn bằng
    ``web_clip`` (đọc đoạn này trong lúc tải đoạn sau). Che bí mật, bỏ phần không đọc được
    và cắt theo VOICE_REPLY_MAX_CHARS như ``web_clip``. Chỉ cắt ở dấu câu có khoảng trắng
    theo sau (số "1.000.000" giữ nguyên); [] = không có gì để đọc."""
    you, _ = reply_audience.forms()
    hidden = prompt_redaction.Redactor().text(text)
    speech, _ = fit(speakable_text(hidden), max_chars(), you)
    chunks: list[str] = []
    for sentence in _SENTENCE_GAP_RE.split(speech):
        if not sentence:
            continue
        if chunks:
            joined = f"{chunks[-1]} {sentence}"
            # Đoạn đầu gom tới khi đủ dài (không đọc lẻ "Dạ."), đoạn sau gom tới trần.
            short = len(chunks) > 1 or len(chunks[0]) < WEB_FIRST_CHUNK_CHARS
            if short and len(joined) <= WEB_CHUNK_CHARS:
                chunks[-1] = joined
                continue
        chunks.append(sentence)
    return chunks


async def web_clip(
    user_id: int, text: str, *, voice: str, style: str, fallback_voice: str | None = None
) -> Clip | None:
    """Giọng đọc cho trang avatar (/admin/avatar): như ``make_clip`` nhưng không xét /noi
    và kênh - giọng ``voice`` và chỉ dẫn đọc ``style`` do trang chọn. Vẫn che bí mật,
    lọc chữ đọc được, cắt theo VOICE_REPLY_MAX_CHARS, chuẩn hoá số/viết tắt.

    ``voice`` trong EDGE_VOICES: đọc bằng Edge TTS (miễn phí, không tính quota TTS);
    Edge lỗi/hết giờ/rỗng thì đọc lại bằng giọng Gemini ``fallback_voice`` và EDGE_COOLDOWN_SEC
    tiếp theo đọc thẳng bằng giọng đó. Giọng Gemini (tên lạ -> VOICE_TTS_VOICE) chỉ đọc bằng
    Gemini Live, tính quota TTS như chủ bot Telegram; Live lỗi/đọc lệch/bận thì không có
    giọng (không chờ thêm Gemini TTS như VOICE_ENGINE=auto của /noi). KHÔNG raise."""
    global _edge_down_until
    try:
        you, _ = reply_audience.forms()
        hidden = prompt_redaction.Redactor().text(text)
        speech, truncated = fit(speakable_text(hidden), max_chars(), you)
        speech = tts_normalize.normalize(speech)
        if len(speech) < MIN_SPEAK_CHARS:
            return None
        remaining = remaining_turn_seconds()
        if remaining is not None and remaining < MIN_REMAINING_SEC:
            logger.info("Còn %.1fs của lượt - bỏ giọng đọc avatar.", remaining)
            return None
        if voice in EDGE_VOICES and time.monotonic() < _edge_down_until:
            voice = fallback_voice or tts_voice()
        elif voice in EDGE_VOICES:
            try:
                return await _synthesize_edge(speech, truncated, voice)
            except TurnTimeoutError:
                raise
            except Exception as exc:
                _edge_down_until = time.monotonic() + EDGE_COOLDOWN_SEC
                logger.info(
                    "Giọng Edge %s lỗi (%s: %s) - đọc bằng giọng %s trong %d phút tới.",
                    voice,
                    type(exc).__name__,
                    str(exc)[:200],
                    fallback_voice or tts_voice(),
                    EDGE_COOLDOWN_SEC // 60,
                )
                voice = fallback_voice or tts_voice()
        if not await usage_quota.heavy_available(user_id, channel="telegram", is_admin=True):
            return None
        try:
            clip = await _synthesize(
                speech,
                "web",
                truncated,
                voice=canonical_voice(voice) or tts_voice(),
                style=style,
                engine="live",
                timeout_sec=WEB_TTS_TIMEOUT_SEC,
            )
        except TurnTimeoutError:
            raise
        except Exception as exc:
            logger.info(
                "Gemini Live không đọc được giọng avatar (%s: %s) - chỉ hiện chữ.",
                type(exc).__name__,
                str(exc)[:200],
            )
            return None
        if clip is not None:
            await usage_quota.record_heavy(
                user_id, usage_quota.KIND_TTS, channel="telegram", is_admin=True
            )
        return clip
    except TurnTimeoutError:
        logger.info("Hết hạn chót khi đọc giọng avatar - chỉ hiện chữ.")
        return None
    except Exception:
        logger.warning("Không tạo được giọng đọc avatar - chỉ hiện chữ.", exc_info=True)
        return None


async def _synthesize(
    speech: str,
    channel: str,
    truncated: bool,
    *,
    voice: str | None = None,
    style: str | None = None,
    engine: str | None = None,
    timeout_sec: float = TTS_TIMEOUT_SEC,
) -> Clip | None:
    from ai import orchestrator

    remaining = remaining_turn_seconds()
    wait = GATE_WAIT_SEC if remaining is None else max(0.5, min(GATE_WAIT_SEC, remaining - 5))
    try:
        await asyncio.wait_for(_gate.acquire(), wait)
    except TimeoutError:
        logger.info("Đang có lượt đọc giọng khác - bỏ tin thoại lần này.")
        return None
    try:
        result = await orchestrator.synthesize_speech(
            speech,
            model=tts_model(),
            voice=voice or tts_voice(),
            timeout_sec=timeout_sec,
            engine=engine or voice_engine(),
            style=style_prompt() if style is None else style,
        )
        pcm, rate, channels = to_pcm(result.audio, result.mime)
        if not pcm or len(pcm) > MAX_PCM_BYTES:
            logger.warning("TTS trả %d byte PCM - bỏ.", len(pcm))
            return None
        encoded = await _encode(pcm, rate, channels, channel)
        if encoded is None:
            return None
        data, ext = encoded
        return Clip(
            data, MIMES[ext], ext, duration_ms(pcm, rate, channels), truncated, voice or tts_voice()
        )
    finally:
        _gate.release()
        # PCM vài MB + tiến trình ffmpeg: trả heap rảnh về hệ điều hành (core/memtrim.py).
        memtrim.request("tin thoại")


def edge_timeout_sec() -> int:
    return env_int("AVATAR_EDGE_TIMEOUT_SEC", 8, 3, 60)


@functools.lru_cache(maxsize=16)
def _edge_setting(name: str, raw: str, default: str, pattern: re.Pattern) -> str:
    # lru_cache: giá trị sai chỉ ghi log 1 lần thay vì mỗi câu đọc.
    raw = raw.strip()
    if not raw:
        return default
    if pattern.fullmatch(raw):
        return raw
    logger.warning("%s=%r sai định dạng edge-tts - dùng %s.", name, raw, default)
    return default


def edge_prosody() -> dict[str, str]:
    """Tốc độ/cao độ/âm lượng cho Edge TTS (Edge không nhận chỉ dẫn phong cách bằng lời):
    AVATAR_EDGE_RATE "-6%", AVATAR_EDGE_PITCH "+12Hz", AVATAR_EDGE_VOLUME "+0%" - giọng
    chậm và cao hơn chút cho nhẹ nhàng. Sai định dạng (cần dấu +/- và % hoặc Hz) -> mặc định."""
    return {
        "rate": _edge_setting(
            "AVATAR_EDGE_RATE", os.getenv("AVATAR_EDGE_RATE", ""), "-6%", _PERCENT
        ),
        "pitch": _edge_setting(
            "AVATAR_EDGE_PITCH", os.getenv("AVATAR_EDGE_PITCH", ""), "+12Hz", _HERTZ
        ),
        "volume": _edge_setting(
            "AVATAR_EDGE_VOLUME", os.getenv("AVATAR_EDGE_VOLUME", ""), "+0%", _PERCENT
        ),
    }


async def _synthesize_edge(speech: str, truncated: bool, voice: str) -> Clip | None:
    """MP3 của giọng Edge ``voice`` (EDGE_VOICES) - trình duyệt phát thẳng, không qua
    ffmpeg, không ghi file tạm. None = đang có lượt đọc khác (chỉ hiện chữ); Edge lỗi,
    hết giờ (AVATAR_EDGE_TIMEOUT_SEC), trả rỗng hoặc quá MAX_EDGE_BYTES -> raise để
    ``web_clip`` đọc lại bằng giọng Gemini."""
    # Nạp lười: edge-tts kéo theo aiohttp (~10 MB RAM) - chỉ khi có người chọn giọng Edge.
    import edge_tts

    remaining = remaining_turn_seconds()
    wait = GATE_WAIT_SEC if remaining is None else max(0.5, min(GATE_WAIT_SEC, remaining - 5))
    try:
        await asyncio.wait_for(_gate.acquire(), wait)
    except TimeoutError:
        logger.info("Đang có lượt đọc giọng khác - bỏ giọng avatar lần này.")
        return None
    try:
        timeout = edge_timeout_sec()

        async def collect() -> bytes:
            prosody = edge_prosody()
            communicate = edge_tts.Communicate(
                speech,
                EDGE_VOICES[voice],
                rate=prosody["rate"],
                volume=prosody["volume"],
                pitch=prosody["pitch"],
                connect_timeout=min(5, timeout),
                receive_timeout=timeout,
            )
            audio = bytearray()
            async for chunk in communicate.stream():
                data = chunk.get("data")
                if chunk["type"] == "audio" and data:
                    audio += data
                    if len(audio) > MAX_EDGE_BYTES:
                        raise ValueError(f"Edge TTS trả quá {MAX_EDGE_BYTES} byte")
            return bytes(audio)

        data = await bounded(collect, timeout)
        if not data:
            raise ValueError("Edge TTS không trả âm thanh")
        duration = len(data) // EDGE_MP3_BYTES_PER_MS
        return Clip(data, MIMES["mp3"], "mp3", duration, truncated, voice)
    finally:
        _gate.release()
        memtrim.request("giọng avatar")


# ─── Kho RAM cho Zalo (link ký HMAC có hạn) ──────────────────────────────────

_NAME_RE = re.compile(r"^([A-Za-z0-9_-]{8,32})\.(\d{9,11})\.([A-Za-z0-9_-]{16,64})\.(m4a|ogg|wav)$")


@dataclass(frozen=True)
class StoredClip:
    data: bytes
    mime: str
    ext: str
    expires_at: float


class ClipStore:
    """Clip chờ gateway/ app Zalo tải: trần số file + tổng byte, hết hạn theo TTL.
    Tên file = "<id>.<hết hạn>.<chữ ký HMAC>.<đuôi>" - đoán/sửa được hạn là sai chữ ký."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, StoredClip] = OrderedDict()
        self._secret = secrets.token_bytes(32)

    def _sign(self, clip_id: str, expires: int, ext: str) -> str:
        digest = hmac.new(
            self._secret, f"{clip_id}.{expires}.{ext}".encode(), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest[:24]).decode().rstrip("=")

    def _prune(self, now: float) -> None:
        for name in [n for n, item in self._items.items() if item.expires_at <= now]:
            self._items.pop(name, None)

    def total_bytes(self) -> int:
        return sum(len(item.data) for item in self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def put(self, clip: Clip, *, now: float | None = None) -> str | None:
        """Tên file đã ký, hoặc None nếu clip lớn hơn cả trần kho."""
        now = time.time() if now is None else now
        limit = store_max_bytes()
        if len(clip.data) > limit:
            return None
        self._prune(now)
        while self._items and (
            len(self._items) >= STORE_MAX_FILES or self.total_bytes() + len(clip.data) > limit
        ):
            self._items.popitem(last=False)
        clip_id = secrets.token_urlsafe(12)
        expires = int(now) + url_ttl_sec()
        name = f"{clip_id}.{expires}.{self._sign(clip_id, expires, clip.ext)}.{clip.ext}"
        self._items[name] = StoredClip(clip.data, clip.mime, clip.ext, float(expires))
        return name

    def get(self, name: str, *, now: float | None = None) -> StoredClip | None:
        now = time.time() if now is None else now
        match = _NAME_RE.match(name or "")
        if not match:
            return None
        clip_id, expires_raw, signature, ext = match.groups()
        expected = self._sign(clip_id, int(expires_raw), ext)
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            return None
        if int(expires_raw) <= now:
            self._items.pop(name, None)
            return None
        return self._items.get(name)

    def clear(self) -> None:
        self._items.clear()


store = ClipStore()


def public_url(name: str) -> str | None:
    base = public_base()
    return f"{base}/media/tts/{name}" if base else None


ZOOM_LINK_PREFIX = "🔊 Nghe giọng Lan Anh:"


async def with_zoom_voice(user_id: int, text: str, result):
    """ChannelResult của lượt Zoom + dòng link nghe (nếu /noi bật) - không đọc lệnh
    "/", tin cổ phiếu, ảnh (cùng điều kiện tin thoại Zalo). Lỗi -> chỉ chữ."""
    if (
        not result.messages
        or (text or "").lstrip().startswith("/")
        or result.market
        or result.image_b64
        or result.image_url
    ):
        return result
    line = await zoom_voice_line(user_id, speakable_reply(list(result.messages)))
    if not line:
        return result
    return dataclasses.replace(result, messages=[*result.messages, line])


async def zoom_voice_line(user_id: int, text: str, *, voice_in: bool = False) -> str | None:
    """Dòng "🔊 Nghe giọng Lan Anh: <link>" cho Zoom (chatbot không gửi được âm thanh):
    clip vào kho RAM có trần, link công khai ký HMAC hết hạn sau
    VOICE_REPLY_URL_TTL_MIN phút (channels/tts_media.py). None nếu không cần/không làm được."""
    clip = await make_clip(user_id, text, channel="zoom", voice_in=voice_in)
    if clip is None:
        return None
    name = store.put(clip)
    url = public_url(name) if name else None
    return f"{ZOOM_LINK_PREFIX} {url}" if url else None


async def zalo_voice(
    user_id: int, text: str, *, voice_in: bool = False, is_admin: bool = True
) -> dict | None:
    """Payload ``voice`` của ZaloMessageResponse ({path, url, duration_ms, mime}) hoặc
    None. ``path`` tương đối với ZALO_BRIDGE_BASE_URL của gateway (loopback)."""
    clip = await make_clip(user_id, text, channel="zalo", voice_in=voice_in, is_admin=is_admin)
    if clip is None:
        return None
    name = store.put(clip)
    if name is None:
        return None
    return {
        "path": f"tts/{name}",
        "url": public_url(name),
        "duration_ms": clip.duration_ms,
        "mime": clip.mime,
    }


# Lượt tin thoại Zalo (channels/zalo_media.py) chạy trong task con huỷ được và trả
# (tin, provider): clip đi ra qua 1 hộp đặt sẵn trong ContextVar (task con thừa hưởng
# cùng hộp) thay vì đổi kiểu trả về.
_capture: ContextVar[list | None] = ContextVar("voice_reply_capture", default=None)


@contextmanager
def capture() -> Iterator[list]:
    box: list = []
    token = _capture.set(box)
    try:
        yield box
    finally:
        _capture.reset(token)


def offer(voice: dict | None) -> None:
    box = _capture.get()
    if box is not None and voice is not None:
        box[:] = [voice]


def speakable_reply(messages: list[str]) -> str:
    """Câu cần đọc của 1 kết quả kênh: tin đầu (câu trả lời) - tin sau là câu hỏi
    duyệt cách trả lời, câu báo phụ."""
    return messages[0] if messages else ""
