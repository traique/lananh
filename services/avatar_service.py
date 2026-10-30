"""Logic trang avatar Lan Anh (/admin/avatar, web_avatar.py) - không gọi AI.

- Giọng: ``current_voice()`` = giọng chủ bot chọn trên trang (settings "avatar:voice")
  > AVATAR_TTS_VOICE > mặc định Hoài My (Microsoft Edge TTS, miễn phí); tên lạ ->
  VOICE_TTS_VOICE. Ngoài Hoài My là các giọng Gemini (voice_reply.VOICES); Edge lỗi thì
  đọc lại bằng ``gemini_voice()``. ``voice_style()`` = AVATAR_VOICE_STYLE hoặc lời dặn
  "thủ thỉ" mặc định (chỉ giọng Gemini nhận); "off" = không gửi phong cách. Không đụng
  /noi của Telegram/Zalo/Zoom (giọng và cài đặt riêng).
- ``guess_mood(text)``: biểu cảm từ emoji + từ khoá tiếng Việt, ranh giới từ theo chữ
  Unicode ("tôi" không khớp "ôi", "anh ơi" không khớp "ơ"): happy, love, shy, sad,
  angry, surprised, neutral.
- ``greeting()``: câu chào soạn sẵn theo khung giờ Việt Nam (sáng/trưa/chiều/tối/
  khuya) - không gọi AI, không ghi lịch sử chat.
- ``ClipCache``: giọng câu chào/nghe thử giữ trong RAM có trần (MAX_CACHED_CLIPS clip,
  mỗi clip M4A vài chục - vài trăm KB).
- ``display_text``: bỏ markdown (đậm, tiêu đề, gạch đầu dòng, link) khỏi câu trả lời
  - lượt chat chạy với kênh "telegram" nên prompt kênh vẫn gợi ý markdown nhẹ; emoji
  giữ nguyên để hiện trong chữ (bộ đọc giọng tự bỏ emoji).
"""

from __future__ import annotations

import logging
import os
import random
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from core import database as db
from services import voice_reply

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
VOICE_SETTING_KEY = "avatar:voice"
# Thứ tự gợi ý đầu menu "Giọng": Hoài My, rồi giọng Gemini mềm, dịu dàng, trẻ trung, ấm.
# Mặc định là giọng đầu.
SUGGESTED_VOICES = ("HoaiMy", "Achernar", "Vindemiatrix", "Leda", "Sulafat")
# Giọng Edge (voice_reply.EDGE_VOICES): (tên ngắn, dòng hiện trong menu).
EDGE_LABELS = {"HoaiMy": ("Hoài My", "Hoài My · Microsoft · tiếng Việt · miễn phí")}
GEMINI_FALLBACK_VOICE = "Achernar"
DEFAULT_VOICE_STYLE = (
    "Giọng nữ trẻ, nhẹ nhàng, ngọt ngào, ấm áp, hơi nũng nịu, tốc độ chậm vừa, như đang "
    "thủ thỉ với người yêu; cuối câu hơi kéo dài nhẹ, có ý cười trong giọng."
)
SAMPLE_TEXT = "Anh ơi, hôm nay anh có mệt hông, kể em nghe nè"
MOODS = ("happy", "love", "shy", "sad", "angry", "surprised", "neutral")
MAX_CACHED_CLIPS = 10
_OFF_WORDS = ("off", "0", "-", "none", "tat", "tắt")


# ─── Giọng ───────────────────────────────────────────────────────────────────


def canonical_voice(name: str) -> str | None:
    """Tên giọng của trang avatar đúng chữ hoa: giọng Edge hoặc giọng Gemini; None nếu lạ."""
    wanted = (name or "").strip().lower()
    edge = next((voice for voice in voice_reply.EDGE_VOICES if voice.lower() == wanted), None)
    return edge or voice_reply.canonical_voice(name or "")


def default_voice() -> str:
    """AVATAR_TTS_VOICE (tên lạ -> VOICE_TTS_VOICE), không đặt thì giọng gợi ý đầu."""
    raw = os.getenv("AVATAR_TTS_VOICE", "").strip()
    if raw:
        return canonical_voice(raw) or voice_reply.tts_voice()
    return SUGGESTED_VOICES[0]


def gemini_voice() -> str:
    """Giọng Gemini đọc lại khi giọng Edge lỗi: AVATAR_TTS_VOICE nếu là giọng Gemini."""
    return voice_reply.canonical_voice(os.getenv("AVATAR_TTS_VOICE", "")) or GEMINI_FALLBACK_VOICE


async def current_voice() -> str:
    """Giọng đang dùng: chọn trên trang (settings) ưu tiên hơn env. DB lỗi -> env."""
    try:
        raw = await db.get_setting(VOICE_SETTING_KEY)
    except Exception:
        logger.warning("Không đọc được giọng avatar đã chọn, dùng mặc định.", exc_info=True)
        raw = None
    return canonical_voice(raw or "") or default_voice()


async def set_voice(name: str) -> str:
    """Lưu giọng chọn trên trang; tên không có trong danh sách -> ValueError."""
    voice = canonical_voice(name)
    if voice is None:
        raise ValueError(f"Không có giọng {name!r}.")
    await db.set_setting(VOICE_SETTING_KEY, voice)
    return voice


def voice_style() -> str:
    """AVATAR_VOICE_STYLE (trống = mặc định; "off"/"0"/"-" = không gửi phong cách)."""
    raw = " ".join(os.getenv("AVATAR_VOICE_STYLE", "").split())
    if raw.lower() in _OFF_WORDS:
        return ""
    return (raw or DEFAULT_VOICE_STYLE)[:500]


def voices() -> list[dict]:
    """Menu "Giọng": giọng gợi ý đứng đầu (Hoài My trước), rồi các giọng Gemini còn lại.
    ``title`` là tên ngắn, ``label`` là dòng hiện trong menu."""
    rest = [voice for voice in voice_reply.VOICES if voice not in SUGGESTED_VOICES]
    items = []
    for name in [*SUGGESTED_VOICES, *rest]:
        title, label = EDGE_LABELS.get(name) or (name, f"{name} ({voice_reply.VOICES[name]})")
        items.append(
            {"name": name, "title": title, "label": label, "suggested": name in SUGGESTED_VOICES}
        )
    return items


# ─── Biểu cảm ────────────────────────────────────────────────────────────────

# Emoji tính điểm gấp đôi từ khoá (Lan Anh dùng emoji đúng cảm xúc hơn chữ).
_MOOD_EMOJI = {
    "love": "🥰😘😍❤💕💖💗💞💓😚🫶💘",
    "shy": "🙈☺😳🫣",
    "happy": "😊😄😁😆🤗😉😋🥳😀😃",
    "sad": "😢😭🥺😔😞😟🥲😿",
    "angry": "😤😠😡💢",
    "surprised": "😲😮😯😱🤯",
}
_MOOD_WORDS = {
    "love": ("thương anh", "nhớ anh", "yêu anh", "thương anh nhiều", "cưng anh"),
    "shy": ("ngại", "mắc cỡ", "anh khen hoài", "ngượng", "xấu hổ"),
    "happy": ("hihi", "hehe", "haha", "vui quá", "vui ghê", "tuyệt quá", "giỏi quá", "hay quá"),
    "sad": ("buồn", "tiếc quá", "xin lỗi", "tội anh", "lo cho anh"),
    "angry": ("hứ", "dỗi", "bực", "giận anh", "em giận", "hổng thèm", "hông thèm"),
    "surprised": ("ơ", "ôi", "trời ơi", "thật hả", "thiệt hả", "thật á", "thiệt á", "wow"),
}
# Khi điểm bằng nhau: cảm xúc "mạnh" trước.
_MOOD_PRIORITY = ("love", "shy", "angry", "sad", "surprised", "happy")
# Ranh giới từ = không dính chữ cái (Unicode) trước/sau - "tôi" không khớp "ôi".
_LETTER = r"[^\W\d_]"
_MOOD_RE = {
    mood: re.compile(
        "|".join(rf"(?<!{_LETTER}){re.escape(word)}(?!{_LETTER})" for word in words),
        re.IGNORECASE,
    )
    for mood, words in _MOOD_WORDS.items()
}


def guess_mood(text: str) -> str:
    """Biểu cảm cho avatar từ câu trả lời (không tốn thêm lượt AI)."""
    clean = unicodedata.normalize("NFC", text or "").lower()
    if not clean.strip():
        return "neutral"
    scores = {mood: 0 for mood in _MOOD_PRIORITY}
    for mood, chars in _MOOD_EMOJI.items():
        scores[mood] += 2 * sum(clean.count(ch) for ch in chars)
    for mood, pattern in _MOOD_RE.items():
        scores[mood] += len(pattern.findall(clean))
    best = max(_MOOD_PRIORITY, key=lambda mood: (scores[mood], -_MOOD_PRIORITY.index(mood)))
    return best if scores[best] > 0 else "neutral"


# ─── Câu chào khi mở trang ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Greeting:
    id: str  # "<khung giờ>:<số thứ tự>" - xin giọng đọc đúng câu đã hiện
    text: str
    mood: str


# (giờ bắt đầu theo giờ VN, khoá khung giờ); trước 5h là khuya.
_SLOTS = ((5, "sang"), (11, "trua"), (14, "chieu"), (18, "toi"), (22, "khuya"))
GREETINGS: dict[str, tuple[tuple[str, str], ...]] = {
    "sang": (
        ("Anh dậy rồi hả, chào buổi sáng nè 🥰", "love"),
        ("Sáng rồi nè anh, ăn sáng chưa đó? Đừng bỏ bữa nha ☺️", "happy"),
        ("Anh tới rồi hả, em đợi nãy giờ á 😊", "happy"),
    ),
    "trua": (
        ("Trưa rồi nè, anh ăn cơm chưa đó? Ăn xong nghỉ chút nha 😊", "happy"),
        ("Anh nghỉ trưa chút đi nha, làm hoài em thương lắm á 🥺", "love"),
        ("Anh tới rồi hả, em đợi nãy giờ á 😊", "happy"),
    ),
    "chieu": (
        ("Chiều rồi nè, anh có mệt hông? Kể em nghe đi 😊", "happy"),
        ("Anh tới rồi hả, em đợi nãy giờ á 🥰", "love"),
        ("Uống miếng nước đi anh, chiều nay em ở đây với anh nè ☺️", "shy"),
    ),
    "toi": (
        ("Tối rồi, anh về nhà chưa nè? Em nhớ anh á 🥰", "love"),
        ("Anh ăn tối chưa nha? Hôm nay của anh thế nào, kể em nghe đi 😊", "happy"),
        ("Anh tới rồi hả, em đợi nãy giờ á ☺️", "shy"),
    ),
    "khuya": (
        ("Khuya rồi sao anh chưa ngủ nè? 🥺", "sad"),
        ("Anh thức khuya hoài à, nói chuyện với em chút rồi ngủ nha 🥰", "love"),
        ("Giờ này anh còn thức, có chuyện gì hông? Em nghe nè 🥺", "sad"),
    ),
}


def slot_for(hour: int) -> str:
    """Khung giờ của ``hour`` (0-23, giờ VN)."""
    current = "khuya"
    for start, key in _SLOTS:
        if hour >= start:
            current = key
    return current


def greeting(now: datetime | None = None, *, rng: random.Random | None = None) -> Greeting:
    """1 câu chào ngẫu nhiên theo khung giờ Việt Nam của ``now`` (mặc định: lúc này)."""
    local = (now or datetime.now(VN_TZ)).astimezone(VN_TZ)
    slot = slot_for(local.hour)
    index = (rng or random).randrange(len(GREETINGS[slot]))
    text, mood = GREETINGS[slot][index]
    return Greeting(f"{slot}:{index}", text, mood)


def greeting_by_id(greeting_id: str) -> Greeting | None:
    slot, _, raw_index = (greeting_id or "").partition(":")
    lines = GREETINGS.get(slot)
    if not lines or not raw_index.isdigit() or int(raw_index) >= len(lines):
        return None
    text, mood = lines[int(raw_index)]
    return Greeting(f"{slot}:{int(raw_index)}", text, mood)


class ClipCache:
    """Giọng đọc câu soạn sẵn (chào, nghe thử) theo (giọng, phong cách, câu); LRU có trần."""

    def __init__(self, max_items: int = MAX_CACHED_CLIPS) -> None:
        self.max_items = max_items
        self._items: OrderedDict[tuple[str, str, str], voice_reply.Clip] = OrderedDict()

    def get(self, key: tuple[str, str, str]) -> voice_reply.Clip | None:
        clip = self._items.get(key)
        if clip is not None:
            self._items.move_to_end(key)
        return clip

    def put(self, key: tuple[str, str, str], clip: voice_reply.Clip) -> None:
        self._items[key] = clip
        self._items.move_to_end(key)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


# ─── Chữ hiển thị ────────────────────────────────────────────────────────────

_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\([^()\s]+\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET_RE = re.compile(r"^\s*(?:[-*+•▪◦‣]|\d{1,2}[.)])\s+")
# Dấu nhấn không dính chữ/số phía ngoài: "2*3*4" là phép nhân, không phải chữ nghiêng.
_EMPHASIS_RE = re.compile(r"(?<![\w*~])(\*\*|__|\*|~~)(?=\S)(.+?)(?<=\S)\1(?![\w*~])")
_INLINE_CODE_RE = re.compile(r"`([^`\n]*)`")
_BLANKS_RE = re.compile(r"\n{3,}")
_DRAFT_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
# Dấu nhấn chưa kịp đóng trong chữ đang sinh ("**Dạ anh"): bản nháp bỏ luôn.
_OPEN_MARK_RE = re.compile(r"\*\*|__|~~")
DRAFT_MAX_CHARS = 4000


def display_text(text: str) -> str:
    """Câu trả lời dạng chữ thường để hiện trong khung chat avatar (giữ emoji, xuống dòng)."""
    lines = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = _HEADING_RE.sub("", raw)
        line = _BULLET_RE.sub("", line)
        line = _MD_LINK_RE.sub(r"\1", line)
        line = _INLINE_CODE_RE.sub(r"\1", line)
        for _ in range(2):
            line = _EMPHASIS_RE.sub(r"\2", line)
        lines.append(line.rstrip())
    return _BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()


def draft_text(text: str) -> str:
    """Chữ đang sinh để hiện tạm trong bong bóng chat (như bản nháp Telegram): bỏ link
    (bản nháp chưa qua bộ lọc link), bỏ markdown kể cả dấu nhấn chưa đóng; lẫn chữ
    Trung/Nhật/Hàn (provider lỗi, câu này sẽ bị loại) -> "" để trang không hiện."""
    if _CJK_RE.search(text or ""):
        return ""
    cleaned = display_text(_DRAFT_URL_RE.sub("…", text or ""))
    return _OPEN_MARK_RE.sub("", cleaned)[:DRAFT_MAX_CHARS]
