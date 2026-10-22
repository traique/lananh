"""Trả lời câu hỏi thời tiết bằng code, KHÔNG gọi AI.

Trước đây "mai Đà Lạt có mưa không" đi router tool (1 lượt AI) rồi chat AI soạn
câu trả lời. Module này tự tách địa điểm và số ngày bằng cách bỏ các từ không
phải địa danh (thời gian, từ hỏi, xưng hô...), tra Open-Meteo qua
services/weather.py và trả lời thẳng.

Khi không chắc thì trả None để đi đường cũ (router tool get_weather + chat AI):
- phần còn lại sau khi bỏ từ thừa quá dài (không giống tên địa danh);
- địa danh không có "ở/tại" phía trước mà geocoding không tìm thấy (có thể là
  chữ thừa chứ không phải địa danh).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from zoneinfo import ZoneInfo

from services import weather

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_TIMEOUT_SEC = 20
_MAX_LOCATION_WORDS = 5

# (regex trên chữ đã bỏ dấu, ngày bắt đầu tính từ hôm nay, số ngày cần lấy)
_WHEN = (
    (r"\b(?:\d+|mot|hai|ba|bon|nam|sau|bay) ngay (?:toi|nua|sap toi)\b", None),
    (r"\b(?:tuan nay|tuan toi|tuan sau|7 ngay|ca tuan)\b", (0, 7)),
    (r"\bcuoi tuan\b", "weekend"),
    (r"\b(?:ngay kia|ngay mot|mot)\b", (2, 1)),
    (r"\b(?:ngay mai|mai|sang mai|chieu mai|toi mai|dem mai)\b", (1, 1)),
)
_NUM_WORDS = {"mot": 1, "hai": 2, "ba": 3, "bon": 4, "nam": 5, "sau": 6, "bay": 7}

# Từ/cụm không phải địa danh (đã bỏ dấu), bỏ đi trước khi lấy địa điểm.
_FILLER = [
    r"cho (?:em|anh|chi|minh|toi|tao|tui) hoi", r"(?:em|anh|chi|minh|toi) muon biet",
    r"xem giup", r"tra giup", r"check giup", r"cho biet", r"bao (?:em|anh|chi)",
    r"du bao thoi tiet", r"thoi tiet", r"du bao", r"nhiet do", r"do am(?: khong khi)?",
    r"nang nong", r"(?:co|se|con|bi|dang) (?:mua|nang)", r"mua (?:khong|ko|hong|chua|to|lon|nhieu)",
    r"troi (?:co |se |dang )?(?:mua|nang|nong|lanh|ram)", r"mua", r"nang", r"nong", r"lanh",
    r"hom nay", r"bay gio", r"hien tai", r"ngay mai", r"sang mai", r"chieu mai", r"toi mai",
    r"dem mai", r"ngay kia", r"ngay mot", r"cuoi tuan(?: nay| sau| toi)?", r"tuan (?:nay|toi|sau)",
    r"(?:\d+|mot|hai|ba|bon|nam|sau|bay) ngay (?:toi|nua|sap toi)", r"7 ngay", r"ca tuan",
    r"sang nay", r"chieu nay", r"toi nay", r"dem nay", r"luc nay",
    r"the nao", r"ra sao", r"nhu the nao", r"bao nhieu do", r"bao nhieu", r"khong", r"ko", r"hong",
    r"co", r"se", r"dang", r"troi", r"o", r"tai", r"thi", r"la", r"vay", r"nhi", r"nhe", r"nha",
    r"a", r"oi", r"day", r"di", r"duoc", r"ha", r"em", r"anh", r"chi", r"minh", r"ban", r"mai",
    r"nay", r"xem", r"tra", r"check", r"giup", r"voi", r"cho",
    # Hoạt động hay hỏi kèm ("mai đi chơi được không"): không phải địa danh.
    r"di choi", r"di lam", r"di hoc", r"ra ngoai", r"phoi do", r"giat do", r"chay bo",
    r"da bong", r"di bien", r"can", r"mang", r"ao mua", r"o du", r"choi", r"lam", r"hoc",
]  # fmt: skip
_FILLER_RE = re.compile(r"\b(?:" + "|".join(sorted(_FILLER, key=len, reverse=True)) + r")\b")
_EXPLICIT_PLACE_RE = re.compile(r"\b(?:o|tai)\s+(?=\S)")

# Địa danh hay hỏi: khớp trước tiên, tránh việc bỏ từ thừa cắt mất chữ của tên
# ("nha" trong Nha Trang, "mai" trong Mai Châu). Tên khác vẫn tra geocoding.
KNOWN_PLACES = [
    "Hà Nội", "Hải Phòng", "Đà Nẵng", "Huế", "Cần Thơ", "Hồ Chí Minh",
    "An Giang", "Bà Rịa", "Vũng Tàu", "Bạc Liêu", "Bắc Giang", "Bắc Kạn", "Bắc Ninh",
    "Bến Tre", "Bình Định", "Bình Dương", "Bình Phước", "Bình Thuận", "Cà Mau", "Cao Bằng",
    "Đắk Lắk", "Đắk Nông", "Điện Biên", "Đồng Nai", "Đồng Tháp", "Gia Lai", "Hà Giang",
    "Hà Nam", "Hà Tĩnh", "Hải Dương", "Hậu Giang", "Hòa Bình", "Hưng Yên", "Khánh Hòa",
    "Kiên Giang", "Kon Tum", "Lai Châu", "Lâm Đồng", "Lạng Sơn", "Lào Cai", "Long An",
    "Nam Định", "Nghệ An", "Ninh Bình", "Ninh Thuận", "Phú Thọ", "Phú Yên", "Quảng Bình",
    "Quảng Nam", "Quảng Ngãi", "Quảng Ninh", "Quảng Trị", "Sóc Trăng", "Sơn La", "Tây Ninh",
    "Thái Bình", "Thái Nguyên", "Thanh Hóa", "Tiền Giang", "Trà Vinh", "Tuyên Quang",
    "Vĩnh Long", "Vĩnh Phúc", "Yên Bái",
    "Đà Lạt", "Sa Pa", "Sapa", "Hạ Long", "Phú Quốc", "Nha Trang", "Mũi Né", "Phan Thiết",
    "Hội An", "Mai Châu", "Mộc Châu", "Tam Đảo", "Côn Đảo", "Quy Nhơn", "Buôn Ma Thuột",
    "Pleiku", "Biên Hòa", "Thủ Dầu Một", "Long Xuyên", "Rạch Giá", "Bảo Lộc", "Tuy Hòa",
    "Đồng Hới", "Vinh", "Hà Tiên", "Châu Đốc", "Mỹ Tho", "Cam Ranh", "Tam Kỳ", "Đông Hà",
    "Sầm Sơn", "Cửa Lò", "Cát Bà", "Tràng An", "Thủ Đức", "Nhơn Trạch", "Long Thành",
]  # fmt: skip
_KNOWN_FOLDED = sorted(((weather.fold(p), p) for p in KNOWN_PLACES), key=lambda x: -len(x[0]))


def _today() -> dt.date:
    return dt.datetime.now(_VN_TZ).date()


def _norm(text: str) -> str:
    t = weather.fold(text)
    t = re.sub(r"[?!.,;:]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _when(t: str, today: dt.date) -> tuple[int, int]:
    """(ngày bắt đầu tính từ hôm nay, số ngày) mà câu hỏi muốn xem."""
    for pattern, span in _WHEN:
        m = re.search(pattern, t)
        if not m:
            continue
        if span is None:
            word = m.group(0).split()[0]
            n = int(word) if word.isdigit() else _NUM_WORDS.get(word, 3)
            return 0, max(1, min(n, 7))
        if span == "weekend":
            saturday = (5 - today.weekday()) % 7
            if re.search(r"\bcuoi tuan (?:sau|toi)\b", t):
                saturday += 7
            if saturday > 5:  # Chủ Nhật hỏi "cuối tuần" -> còn mỗi hôm nay
                return 0, 1
            return saturday, 2
        return span
    return 0, 1


def _location(original: str) -> tuple[str, str] | None:
    """(địa danh giữ dấu, câu đã bỏ dấu và bỏ tên địa danh). "" = không nói địa
    điểm (dùng mặc định TP.HCM). None = không chắc phần còn lại là địa danh."""
    folded = _norm(original)
    for key, name in _KNOWN_FOLDED:
        m = re.search(rf"\b{re.escape(key)}\b", folded)
        if m:
            return name, folded[: m.start()] + folded[m.end() :]
    # Lấy chữ gốc (giữ dấu) tương ứng các từ không bị coi là từ thừa.
    words_orig = re.sub(r"[?!.,;:]+", " ", original).split()
    words_fold = folded.split()
    if len(words_orig) != len(words_fold):
        return None
    remaining = _FILLER_RE.sub(lambda m: " " * len(m.group(0)), folded)
    kept, pos = [], 0
    for word_orig, word in zip(words_orig, words_fold, strict=True):
        start = folded.index(word, pos)
        pos = start + len(word)
        if remaining[start:pos].strip():
            kept.append(word_orig)
    place = " ".join(kept)
    if not place or weather.is_default_location(weather._clean_location(place)):
        return "", folded
    # Chữ lạ chỉ được coi là địa danh khi có "ở/tại" phía trước; nếu không có thể
    # là chữ thừa ("mai đi chơi được không") -> để AI hiểu câu.
    if not _EXPLICIT_PLACE_RE.search(folded) or len(kept) > _MAX_LOCATION_WORDS:
        return None
    return place, folded


def _format(data: dict, start: int, days: int, today: dt.date) -> str:
    now = data.get("now") or {}
    daily = data.get("daily") or []
    n = weather._n  # làm tròn số như describe_weather
    lines = [f"🌤 Thời tiết {data.get('location')}"]
    if start == 0:
        lines.append(
            f"Bây giờ: {now.get('description')}, {n(now.get('temperature_c'))}°C "
            f"(cảm giác {n(now.get('feels_like_c'))}°C), độ ẩm {n(now.get('humidity_percent'))}%, "
            f"gió {n(now.get('wind_kmh'))} km/h."
        )
    for d in daily[start : start + days]:
        day = dt.date.fromisoformat(d["date"])
        name = {0: "Hôm nay", 1: "Ngày mai"}.get((day - today).days, d["weekday"])
        rain = d["rain_chance_percent"]
        umbrella = " ☔ nhớ mang áo mưa" if rain is not None and rain >= 60 else ""
        lines.append(
            f"• {name} {day:%d/%m}: {d['description']}, {n(d['min_c'])}-{n(d['max_c'])}°C, "
            f"khả năng mưa {n(rain)}%{umbrella}."
        )
    lines.append(f"(Nguồn {data.get('source')})")
    return "\n".join(lines)


async def answer(text: str, today: dt.date | None = None) -> str | None:
    """Câu trả lời thời tiết, hoặc None nếu không phải/không chắc (để AI lo)."""
    if not weather.looks_like_weather_question(text):
        return None
    today = today or _today()
    loc = _location(text)
    if loc is None:
        return None
    place, rest = loc
    start, days = _when(rest, today)
    try:
        data = await asyncio.wait_for(weather.get_weather(place, start + days), _TIMEOUT_SEC)
    except (weather.WeatherError, TimeoutError):
        logger.warning("Tra thời tiết lỗi (place=%r)", place, exc_info=True)
        return "❌ Chưa lấy được dữ liệu thời tiết lúc này (Open-Meteo và wttr.in đều lỗi), thử lại sau nhé."
    if data.get("error"):
        return None  # không tìm thấy địa danh -> để AI hiểu câu / hỏi lại
    return _format(data, start, days, today)


maybe_handle = answer
