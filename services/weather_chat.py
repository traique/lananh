"""Trả lời câu hỏi thời tiết bằng code, KHÔNG gọi AI.

Trước đây "mai Đà Lạt có mưa không" đi router tool (1 lượt AI) rồi chat AI soạn
câu trả lời. Module này tự tách địa điểm và số ngày bằng cách bỏ các từ không
phải địa danh (thời gian, từ hỏi, xưng hô...), tra Open-Meteo qua
services/weather.py và trả lời thẳng.

Khi không chắc thì trả None để đi đường cũ (router tool get_weather + chat AI):
- phần còn lại sau khi bỏ từ thừa quá dài (không giống tên địa danh);
- địa danh không có "ở/tại" phía trước mà geocoding không tìm thấy (có thể là
  chữ thừa chứ không phải địa danh).

Thêm:
- Không nói địa điểm -> dùng thành phố mặc định RIÊNG của người hỏi
  (services/weather_home.py, theo internal user id), chưa đặt mới về TP.HCM.
- Buổi trong ngày ("chiều nay", "tối mai", "đêm nay") và "mấy giờ mưa" -> dự
  báo theo giờ (khả năng mưa từng giờ) thay vì xác suất cả ngày.
- Hỏi chất lượng không khí/bụi mịn -> US AQI thật; câu thời tiết hôm nay chỉ
  kèm cảnh báo khi AQI > 100.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from collections import OrderedDict
from zoneinfo import ZoneInfo

from services import weather, weather_home

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
    (r"\b(?:ngay mai|mai|sang mai|trua mai|chieu mai|toi mai|dem mai)\b", (1, 1)),
)
# Buổi trong ngày -> (giờ bắt đầu, giờ kết thúc, tên). Đêm kéo sang 6h sáng hôm sau.
_PARTS = {
    "sang": (6, 11, "sáng"),
    "trua": (11, 14, "trưa"),
    "chieu": (13, 18, "chiều"),
    "toi": (18, 22, "tối"),
    "dem": (22, 30, "đêm"),
}
_PART_RE = re.compile(r"\b(sang|trua|chieu|toi|dem) (nay|mai)\b")
_HOURS_RE = re.compile(r"\b(?:may gio|gio nao|luc nao|khi nao|theo gio|tung gio)\b")
_RAINY_PERCENT = 50
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
    r"sang nay", r"chieu nay", r"toi nay", r"dem nay", r"luc nay", r"trua nay", r"trua mai",
    r"may gio", r"gio nao", r"luc may gio", r"luc nao", r"khi nao", r"theo gio", r"tung gio",
    r"chat luong khong khi", r"khong khi", r"bui min", r"o nhiem", r"aqi", r"pm ?2 5", r"pm25",
    r"sao roi", r"sao", r"roi",
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
_AIR_TIMEOUT_SEC = 8


def known_places() -> list[tuple[str, str]]:
    """(tên đã bỏ dấu, tên giữ dấu) các địa danh quen - dùng chung với weather_home."""
    return list(_KNOWN_FOLDED)


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


def _location(original: str) -> tuple[str, str, bool] | None:
    """(địa danh giữ dấu, câu đã bỏ dấu và bỏ tên địa danh, có nói địa điểm không).

    "" = dùng mặc định (thành phố riêng của người hỏi, chưa đặt thì TP.HCM); cờ
    thứ ba True khi người hỏi NÓI RÕ TP.HCM/Sài Gòn (không lấy thành phố riêng).
    None = không chắc phần còn lại là địa danh."""
    folded = _norm(original)
    for key, name in _KNOWN_FOLDED:
        m = re.search(rf"\b{re.escape(key)}\b", folded)
        if m:
            return name, folded[: m.start()] + folded[m.end() :], True
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
    if not place:
        return "", folded, False
    if weather.is_default_location(weather._clean_location(place)):
        return "", folded, True
    # Chữ lạ chỉ được coi là địa danh khi có "ở/tại" phía trước; nếu không có thể
    # là chữ thừa ("mai đi chơi được không") -> để AI hiểu câu.
    if not _EXPLICIT_PLACE_RE.search(folded) or len(kept) > _MAX_LOCATION_WORDS:
        return None
    return place, folded, True


def _part(t: str) -> tuple[int, int, int, str] | None:
    """(ngày lệch so với hôm nay, giờ bắt đầu, giờ kết thúc, nhãn) khi câu hỏi
    theo buổi ("chiều nay", "tối mai") hoặc theo giờ ("mấy giờ mưa")."""
    m = _PART_RE.search(t)
    if m:
        start_h, end_h, name = _PARTS[m.group(1)]
        offset = 0 if m.group(2) == "nay" else 1
        return offset, start_h, end_h, f"{name} {m.group(2)}"
    if _HOURS_RE.search(t):
        offset = 1 if re.search(r"\b(?:ngay mai|mai)\b", t) else 0
        return offset, 0, 24, "ngày mai" if offset else "hôm nay"
    return None


def _window(
    hours: list[weather.HourPoint],
    day: dt.date,
    start_h: int,
    end_h: int,
    now: dt.datetime | None,
) -> list[weather.HourPoint]:
    begin = dt.datetime.combine(day, dt.time()) + dt.timedelta(hours=start_h)
    end = dt.datetime.combine(day, dt.time()) + dt.timedelta(hours=end_h)
    if now is not None:
        current = now.replace(minute=0, second=0, microsecond=0)
        if begin < current < end:
            begin = current  # buổi đang diễn ra: chỉ xem phần còn lại
    return [h for h in hours if begin <= h.time < end]


def _hour_label(point: weather.HourPoint) -> str:
    return f"{point.time.hour}h"


def _ranges(points: list[weather.HourPoint]) -> list[str]:
    """Gộp các giờ liền nhau: [15h,16h,17h] -> "15h-18h"."""
    out: list[str] = []
    run: list[weather.HourPoint] = []
    for p in points:
        if run and p.time - run[-1].time != dt.timedelta(hours=1):
            out.append(_range_text(run))
            run = []
        run.append(p)
    if run:
        out.append(_range_text(run))
    return out


def _range_text(run: list[weather.HourPoint]) -> str:
    end = (run[-1].time + dt.timedelta(hours=1)).hour
    return f"{run[0].time.hour}h-{end or 24}h"


def rain_line(points: list[weather.HourPoint]) -> str:
    """1 câu tóm tắt mưa theo giờ: khung giờ mưa khả năng cao + giờ cao nhất."""
    peak = max(points, key=lambda p: p.rain_chance or 0)
    peak_text = f"cao nhất {weather._n(peak.rain_chance)}% lúc {_hour_label(peak)}"
    rainy = [p for p in points if (p.rain_chance or 0) >= _RAINY_PERCENT]
    if rainy:
        return f"☔ Dễ mưa (từ {_RAINY_PERCENT}%) khoảng {', '.join(_ranges(rainy))}; {peak_text}."
    return f"🌤 Ít khả năng mưa ({peak_text})."


def _format_hours(data: dict, label: str, start_h: int, end_h: int, points: list) -> str:
    n = weather._n
    span = "theo giờ" if (start_h, end_h) == (0, 24) else f"{start_h}h-{end_h % 24 or 24}h"
    lines = [
        f"🌦 {label[:1].upper()}{label[1:]} ({span}) ở {data.get('location')}",
        rain_line(points),
    ]
    temps = [p.temperature_c for p in points if p.temperature_c is not None]
    rain_mm = sum(p.precip_mm or 0 for p in points)
    if temps:
        detail = f"🌡 Nhiệt độ {n(min(temps))}-{n(max(temps))}°C"
        if rain_mm >= 0.1:
            detail += f", lượng mưa dự kiến ~{rain_mm:.1f} mm".replace(".", ",")
        lines.append(detail + ".")
    step = 1 if len(points) <= 12 else 2
    lines.append(
        "Từng giờ: " + " · ".join(f"{_hour_label(p)} {n(p.rain_chance)}%" for p in points[::step])
    )
    if any((p.rain_chance or 0) >= 60 for p in points):
        lines.append("Nhớ mang áo mưa nha anh ☔")
    lines.append(f"(Nguồn {data.get('source')}, dự báo theo giờ)")
    return "\n".join(lines)


def _format(data: dict, start: int, days: int, today: dt.date, air: dict | None = None) -> str:
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
    warning = weather.air_warning(air) if start == 0 else None
    if warning:
        lines.append(warning)
    lines.append(f"(Nguồn {data.get('source')})")
    return "\n".join(lines)


async def optional_air(place: str) -> dict | None:
    """AQI nếu lấy được, lỗi thì None - chỉ để kèm cảnh báo, không chặn câu trả lời."""
    try:
        return await asyncio.wait_for(weather.get_air_quality(place), _AIR_TIMEOUT_SEC)
    except Exception as exc:  # dữ liệu phụ: nguồn lỗi kiểu gì cũng bỏ qua
        logger.info("Không lấy được AQI (place=%r): %r", place, exc)
        return None


async def answer_coordinates(
    lat: float, lon: float, label: str, today: dt.date | None = None
) -> str:
    """Thời tiết hôm nay + ngày mai tại toạ độ người dùng gửi (tin nhắn vị trí
    Telegram) - bằng code, không geocoding, không gọi AI; kèm cảnh báo AQI > 100."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "❌ Vị trí này không hợp lệ, gửi lại vị trí khác giúp em nha."
    today = today or dt.datetime.now(_VN_TZ).date()

    async def optional_air_at() -> dict | None:
        try:
            return await asyncio.wait_for(
                weather.get_air_quality_at(lat, lon, label), _AIR_TIMEOUT_SEC
            )
        except Exception as exc:  # dữ liệu phụ: lỗi kiểu gì cũng bỏ qua
            logger.info("Không lấy được AQI theo toạ độ: %r", exc)
            return None

    try:
        data, air = await asyncio.gather(
            asyncio.wait_for(weather.get_weather_at(lat, lon, label, 2), _TIMEOUT_SEC),
            optional_air_at(),
        )
    except (weather.WeatherError, TimeoutError):
        logger.warning("Tra thời tiết theo toạ độ lỗi (%.3f, %.3f)", lat, lon, exc_info=True)
        return (
            "❌ Chưa lấy được dữ liệu thời tiết lúc này (Open-Meteo và wttr.in đều lỗi), "
            "thử lại sau nhé."
        )
    return _format(data, 0, 2, today, air)


async def _air_answer(place: str) -> str | None:
    try:
        air = await asyncio.wait_for(weather.get_air_quality(place), _TIMEOUT_SEC)
    except (weather.WeatherError, TimeoutError):
        logger.warning("Tra chất lượng không khí lỗi (place=%r)", place, exc_info=True)
        return "❌ Chưa lấy được dữ liệu chất lượng không khí lúc này, thử lại sau nhé."
    if air.get("error"):
        return None
    lines = [f"🌫 Chất lượng không khí {air.get('location')}: {weather.describe_air(air)}."]
    if int(air["us_aqi"]) > weather.AQI_WARN_THRESHOLD:
        lines.append(
            "😷 Nhóm nhạy cảm (người già, trẻ nhỏ, bệnh hô hấp) nên hạn chế ra ngoài, "
            "ra đường nhớ đeo khẩu trang nha."
        )
    lines.append(f"(Nguồn {weather.AIR_SOURCE} - chỉ để tham khảo)")
    return "\n".join(lines)


async def _hourly_answer(
    place: str, part: tuple[int, int, int, str], today: dt.date, now: dt.datetime
) -> str | None:
    offset, start_h, end_h, label = part
    try:
        data = await asyncio.wait_for(weather.get_hourly(place), _TIMEOUT_SEC)
    except (weather.WeatherError, TimeoutError):
        logger.warning("Dự báo theo giờ lỗi (place=%r), dùng dự báo ngày", place, exc_info=True)
        return None
    if data.get("error"):
        return None
    day = today + dt.timedelta(days=offset)
    points = _window(data["hours"], day, start_h, end_h, now if offset == 0 else None)
    if not points:
        return None
    return _format_hours(data, label, start_h, end_h, points)


# Câu hỏi nối tiếp ngay sau 1 câu trả lời thời tiết ("thế còn ngày mai?", "còn
# Đà Lạt thì sao") không có từ khoá thời tiết: nhớ địa điểm vừa tra của từng
# người (có trần số người + TTL) để tra dự báo thật thay vì để AI tự bịa.
_RECENT_MAX = 256
_RECENT_TTL_SEC = 900
_recent: OrderedDict[int, tuple[float, str]] = OrderedDict()
_FOLLOW_UP_START_RE = re.compile(r"^(?:the con|vay con|con|the|vay|thu|con ve)\b")
_FOLLOW_UP_END_RE = re.compile(r"\b(?:thi sao|the nao|ra sao|sao|nhi|khong)$")
_FOLLOW_UP_WHEN_RE = re.compile(
    r"\b(?:hom nay|ngay mai|mai|ngay kia|ngay mot|cuoi tuan|tuan (?:nay|toi|sau)|"
    r"(?:sang|trua|chieu|toi|dem) (?:nay|mai)|\d+ ngay (?:toi|nua))\b"
)
_FOLLOW_UP_MAX_WORDS = 8


def remember_place(user_id: int, place: str, *, now: float | None = None) -> None:
    _recent[user_id] = (time.monotonic() if now is None else now, place)
    _recent.move_to_end(user_id)
    while len(_recent) > _RECENT_MAX:
        _recent.popitem(last=False)


def recent_place(user_id: int, *, now: float | None = None) -> str | None:
    item = _recent.get(user_id)
    if item is None:
        return None
    stamp, place = item
    if (time.monotonic() if now is None else now) - stamp > _RECENT_TTL_SEC:
        _recent.pop(user_id, None)
        return None
    return place


def _follow_up(text: str, user_id: int | None) -> str | None:
    """Địa điểm vừa tra nếu ``text`` là câu hỏi nối tiếp về thời tiết."""
    if user_id is None:
        return None
    place = recent_place(user_id)
    if place is None:
        return None
    folded = _norm(text)
    if not folded or len(folded.split()) > _FOLLOW_UP_MAX_WORDS:
        return None
    if not (_FOLLOW_UP_START_RE.search(folded) or _FOLLOW_UP_END_RE.search(folded)):
        return None
    mentions_place = any(re.search(rf"\b{re.escape(key)}\b", folded) for key, _ in _KNOWN_FOLDED)
    if not (_FOLLOW_UP_WHEN_RE.search(folded) or mentions_place):
        return None
    return place


_LEAD_WORDS = {"the", "vay", "con", "thu", "ve"}
_TAIL_WORDS = {"thi", "sao", "the", "nao", "ra", "nhi", "khong", "a"}


def _strip_follow_up_words(text: str) -> str:
    """Bỏ "thế còn"/"vậy còn" đầu câu và "thì sao"/"thế nào" cuối câu (giữ dấu
    phần còn lại) để phần tách địa danh không coi chúng là tên địa điểm."""
    words = re.sub(r"[?!.,;:]+", " ", text).split()
    while words and weather.fold(words[0]) in _LEAD_WORDS:
        words.pop(0)
    while words and weather.fold(words[-1]) in _TAIL_WORDS:
        words.pop()
    return " ".join(words)


async def answer(
    text: str,
    today: dt.date | None = None,
    *,
    user_id: int | None = None,
    now: dt.datetime | None = None,
) -> str | None:
    """Câu trả lời thời tiết, hoặc None nếu không phải/không chắc (để AI lo).

    ``user_id``: câu không nói địa điểm thì dùng thành phố mặc định riêng của
    người này (weather_home); ngay sau 1 câu trả lời thời tiết, câu nối tiếp
    ("thế còn ngày mai?") dùng lại địa điểm vừa tra. ``now`` (giờ VN, không tz)
    để test cố định giờ."""
    return await _answer(text, today, user_id=user_id, now=now)


async def _answer(
    text: str,
    today: dt.date | None,
    *,
    user_id: int | None,
    now: dt.datetime | None,
) -> str | None:
    is_weather = weather.looks_like_weather_question(text)
    follow_place = None
    if not is_weather and not weather.looks_like_air_question(text):
        follow_place = _follow_up(text, user_id)
        if follow_place is None:
            return None
        text = f"thời tiết {_strip_follow_up_words(text)}"
        is_weather = True
    if now is None:
        now = (
            dt.datetime.combine(today, dt.time())
            if today is not None
            else dt.datetime.now(_VN_TZ).replace(tzinfo=None)
        )
    today = today or now.date()
    loc = _location(text)
    if loc is None:
        return None
    place, rest, explicit = loc
    if not place and not explicit and follow_place:
        place = follow_place
    elif not place and not explicit and user_id is not None:
        place = await weather_home.get(user_id)
    if not is_weather:
        return await _air_answer(place)
    part = _part(rest)
    if part is not None:
        reply = await _hourly_answer(place, part, today, now)
        if reply is not None:
            if user_id is not None:
                remember_place(user_id, place)
            return reply
    start, days = _when(rest, today)
    weather_call = asyncio.wait_for(weather.get_weather(place, start + days), _TIMEOUT_SEC)
    air: dict | None = None
    try:
        if start == 0:
            data, air = await asyncio.gather(weather_call, optional_air(place))
        else:
            data = await weather_call
    except (weather.WeatherError, TimeoutError):
        logger.warning("Tra thời tiết lỗi (place=%r)", place, exc_info=True)
        return "❌ Chưa lấy được dữ liệu thời tiết lúc này (Open-Meteo và wttr.in đều lỗi), thử lại sau nhé."
    if data.get("error"):
        return None  # không tìm thấy địa danh -> để AI hiểu câu / hỏi lại
    if user_id is not None:
        remember_place(user_id, place)
    return _format(data, start, days, today, air)


maybe_handle = answer
