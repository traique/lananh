"""Thời tiết từ Open-Meteo (miễn phí, không cần API key), dự phòng wttr.in.

Chuyển từ gói vn-info (MIT License, Copyright (c) 2026), sửa cho hợp repo:
- Dùng HTTP client chung (services.http_client) thay vì tự mở client riêng.
- Cache toạ độ và cache dự báo đều có trần số mục (RAM 512 MB trên Render).
- Timeout ngắn, không retry dài: đây là grounding cho 1 lượt chat còn chịu hạn
  chót CHAT_TURN_TIMEOUT_SEC.
- Open-Meteo lỗi mà wttr.in trả được: dữ liệu có ``fallback=True`` (chỉ 3 ngày dự
  báo) và chủ bot nhận 1 cảnh báo nhẹ (core.alerts, tối đa 1 lần mỗi 6 giờ). Mô tả
  của wttr.in lấy theo mã thời tiết sang tiếng Việt, không để lọt chữ tiếng Anh.
- Tên địa điểm từ geocoding được sửa chữ: "Ð" (chữ Iceland) -> "Đ", tên tỉnh không
  dấu ("Lam Dong") -> có dấu.
- ``reverse_geocode``: tên nơi của 1 toạ độ (vị trí hiện tại trên web) qua Nominatim của
  OpenStreetMap, theo chính sách dùng của OSMF: tối đa 1 lượt/giây cho cả tiến trình,
  User-Agent riêng của app, cache có trần, lỗi/bị chặn thì nghỉ 10 phút; trang hiển thị
  tên phải ghi "© OpenStreetMap" (dữ liệu ODbL).

Không nói địa điểm (hoặc nói Sài Gòn/HCM...) -> mặc định TP. Hồ Chí Minh
(người dùng tự đặt thành phố mặc định riêng qua services/weather_home.py).

    data = await get_weather("Hà Nội", days=3)
    describe_weather(data)

Thêm (gọi riêng, chỉ khi cần):
- ``get_hourly``: khả năng mưa/nhiệt độ theo giờ hôm nay tới ngày kia (câu "chiều
  nay có mưa không", "mấy giờ mưa", "đêm mai" kéo tới 6h ngày kia).
- ``get_air_quality``: US AQI + PM2.5 từ air-quality-api.open-meteo.com (dữ liệu
  CAMS toàn cầu ~45 km, 3 giờ/lần -> chỉ là ước tính thô, luôn ghi nguồn).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from collections import OrderedDict
from typing import NamedTuple

import httpx

from core import alerts, config
from core.text_normalize import fold
from services import http_client

logger = logging.getLogger(__name__)

DEFAULT_LABEL = "TP. Hồ Chí Minh"
DEFAULT_LAT = 10.7769
DEFAULT_LON = 106.7009
_TIMEZONE = "Asia/Ho_Chi_Minh"
_MAX_DAYS = 7
_TIMEOUT = httpx.Timeout(6.0, connect=4.0)

# Tên gọi khác của TP.HCM (đã bỏ dấu, bỏ khoảng trắng) -> dùng toạ độ mặc định,
# khỏi gọi geocoding (Open-Meteo không biết "Sài Gòn"/"HCM").
_DEFAULT_ALIASES = {
    "hochiminh",
    "tphochiminh",
    "thanhphohochiminh",
    "tphcm",
    "hcm",
    "hcmc",
    "saigon",
    "sg",
}
_PREFIX_RE = re.compile(r"^\s*(?:thành phố|thanh pho|tp\.?|tỉnh|tinh)\s+", re.IGNORECASE)
# Tỉnh/thành có dấu (weather_chat dùng lại làm địa danh quen). Open-Meteo hay trả tên
# tỉnh (admin1) không dấu như "Lam Dong": đổi về tên có dấu theo bảng này.
VN_PROVINCES = (
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
)  # fmt: skip
_PROVINCE_NAMES = {
    re.sub(r"[^a-z0-9]", "", fold(name)): name
    for name in (*VN_PROVINCES, "Thừa Thiên Huế", "Bà Rịa - Vũng Tàu")
}
# Mã thời tiết của wttr.in (WorldWeatherOnline) -> tiếng Việt cùng giọng với bảng WMO.
# Không dùng weatherDesc: luôn là tiếng Anh ("Patchy rain nearby").
_WWO = {
    113: "trời quang", 116: "ít mây", 119: "có mây", 122: "nhiều mây, u ám",
    143: "sương mờ", 176: "có mưa rải rác", 179: "có tuyết rải rác",
    182: "có mưa tuyết rải rác", 185: "có mưa phùn lạnh rải rác", 200: "có thể có dông",
    227: "tuyết thổi", 230: "bão tuyết", 248: "sương mù", 260: "sương mù đóng băng",
    263: "mưa phùn nhẹ rải rác", 266: "mưa phùn nhẹ", 281: "mưa phùn lạnh",
    284: "mưa phùn lạnh dày", 293: "mưa nhỏ rải rác", 296: "mưa nhỏ",
    299: "lúc có mưa vừa", 302: "mưa vừa", 305: "lúc có mưa to", 308: "mưa to",
    311: "mưa lạnh nhỏ", 314: "mưa lạnh vừa đến to", 317: "mưa tuyết nhỏ",
    320: "mưa tuyết vừa đến to", 323: "tuyết nhẹ rải rác", 326: "tuyết nhẹ",
    329: "tuyết vừa rải rác", 332: "tuyết vừa", 335: "tuyết dày rải rác", 338: "tuyết dày",
    350: "mưa đá nhỏ", 353: "mưa rào nhẹ", 356: "mưa rào vừa đến to",
    359: "mưa rào rất to", 362: "mưa tuyết rào nhẹ", 365: "mưa tuyết rào vừa đến to",
    368: "tuyết rào nhẹ", 371: "tuyết rào vừa đến to", 374: "mưa đá rào nhẹ",
    377: "mưa đá rào vừa đến to", 386: "mưa nhỏ kèm dông", 389: "mưa vừa đến to kèm dông",
    392: "tuyết nhẹ kèm dông", 395: "tuyết vừa đến to kèm dông",
}  # fmt: skip
WTTR_MAX_DAYS = 3
# Báo chủ bot khi Open-Meteo lỗi và web/chat đang chạy wttr.in (cảnh báo nhẹ, gom).
_FALLBACK_ALERT_KEY = "weather:open-meteo-fallback"
_FALLBACK_ALERT_COOLDOWN_SEC = 6 * 3600

_GEO_CACHE_MAX = 128
# Nominatim (tra ngược toạ độ -> tên): https://operations.osmfoundation.org/policies/nominatim/
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_INTERVAL_SEC = 1.0
_NOMINATIM_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
_NOMINATIM_COOLDOWN_SEC = 600
_REVERSE_CACHE_MAX = 256
# (lat, lon) làm tròn 2 số lẻ -> tên, hoặc None (biển, nơi không có tên: khỏi tra lại)
_reverse_cache: OrderedDict[tuple[float, float], str | None] = OrderedDict()
_reverse_lock = asyncio.Lock()
_reverse_state = {"last": 0.0, "down_until": 0.0}
# Tiền tố hành chính bỏ đi ở cấp tỉnh/thành ("Thành phố Đà Lạt" -> "Đà Lạt"); cấp phường
# xã giữ nguyên ("Phường 1" mà bỏ tiền tố thì chỉ còn "1").
_ADMIN_PREFIX_RE = re.compile(r"^(?:Thành phố|Tỉnh|Thị xã)\s+", re.IGNORECASE)
_FORECAST_CACHE_MAX = 32
_FORECAST_TTL_SEC = 600
_HOURLY_CACHE_MAX = 16
_AIR_CACHE_MAX = 16
_AIR_TTL_SEC = 1800
# Hôm nay + ngày mai + ngày kia: "đêm mai" (22h-6h) kéo sang sáng ngày kia.
_HOURLY_DAYS = 3
# tên đã chuẩn hoá -> (nhãn, lat, lon) hoặc None (không tìm thấy, tránh gọi lại)
_geo_cache: OrderedDict[str, tuple[str, float, float] | None] = OrderedDict()
_forecast_cache: OrderedDict[tuple[float, float, int], tuple[float, dict]] = OrderedDict()
# (lat, lon) -> (monotonic, list[HourPoint]) / (monotonic, dict AQI)
_hourly_cache: OrderedDict[tuple[float, float], tuple[float, list]] = OrderedDict()
_air_cache: OrderedDict[tuple[float, float], tuple[float, dict]] = OrderedDict()

# Ngưỡng cảnh báo bụi mịn: chỉ nhắc khi US AQI > 100 (mức "kém" trở lên).
AQI_WARN_THRESHOLD = 100
AIR_SOURCE = "CAMS qua Open-Meteo, ước tính thô ~45 km"


class HourPoint(NamedTuple):
    """1 giờ dự báo (giờ địa phương VN). Tuple gọn để cache 48 giờ tốn ít RAM."""

    time: dt.datetime
    rain_chance: int | None
    precip_mm: float | None
    temperature_c: float | None
    code: int | None


WMO = {
    0: "trời quang",
    1: "ít mây",
    2: "có mây",
    3: "nhiều mây, u ám",
    45: "sương mù",
    48: "sương mù đóng băng",
    51: "mưa phùn nhẹ",
    53: "mưa phùn",
    55: "mưa phùn dày",
    56: "mưa phùn lạnh",
    57: "mưa phùn lạnh dày",
    61: "mưa nhỏ",
    63: "mưa vừa",
    65: "mưa to",
    66: "mưa lạnh",
    67: "mưa lạnh to",
    71: "tuyết nhẹ",
    73: "tuyết",
    75: "tuyết dày",
    77: "hạt tuyết",
    80: "mưa rào nhẹ",
    81: "mưa rào",
    82: "mưa rào rất to",
    85: "mưa tuyết",
    86: "mưa tuyết to",
    95: "dông",
    96: "dông kèm mưa đá",
    99: "dông mạnh kèm mưa đá",
}
WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]


class WeatherError(RuntimeError):
    """Không nguồn thời tiết nào trả được dữ liệu."""


# Câu hỏi thời tiết: tool get_weather (services/tools.py) lấy số liệu thật,
# web_search bỏ qua để khỏi tốn Tavily và lẫn số liệu cũ từ web. "bão" không
# nằm đây: tin bão (đường đi, thiệt hại) vẫn cần tra web. Chỉ khớp cụm rõ nghĩa
# thời tiết: "mưa"/"nắng" trần dính "kem chống nắng"... - câu đó vẫn tra web.
_WEATHER_QUESTION_RE = re.compile(
    r"thời tiết|thoi tiet|dự báo mưa|nắng nóng|độ ẩm (?:không khí|hôm nay|ngày mai|ở|tại)|"
    r"nhiệt độ (?:hôm nay|ngày mai|bây giờ|hiện tại|ngoài trời|ở|tại)|"
    r"trời (?:có |sẽ |đang )?(?:mưa|nắng|nóng|lạnh|râm)|"
    r"(?:có|sẽ|còn|bị|đang) mưa|mưa (?:không|ko|hông|chưa|to|lớn|nhiều)|"
    r"mấy giờ (?:thì |sẽ |trời )?mưa|mưa (?:vào |từ )?(?:lúc )?mấy giờ|giờ nào (?:có |trời )?mưa",
    re.IGNORECASE,
)
# Hỏi chất lượng không khí/bụi mịn: trả AQI thật (Open-Meteo air quality).
_AIR_QUESTION_RE = re.compile(
    r"chất lượng không khí|chat luong khong khi|bụi mịn|bui min|ô nhiễm không khí|"
    r"o nhiem khong khi|\baqi\b|pm ?2[.,]?5",
    re.IGNORECASE,
)


def looks_like_weather_question(text: str) -> bool:
    return bool(_WEATHER_QUESTION_RE.search(text or ""))


def looks_like_air_question(text: str) -> bool:
    return bool(_AIR_QUESTION_RE.search(text or ""))


def _clean_location(location: str) -> str:
    return _PREFIX_RE.sub("", (location or "").strip(" .,?!")).strip()


def _vi_place(part: str, typed: str = "") -> str:
    """Sửa tên địa điểm Open-Meteo trả về: "Ð" (U+00D0, chữ Iceland) -> "Đ"; tên tỉnh
    không dấu -> có dấu; tên không dấu mà người dùng gõ có dấu thì lấy chữ người gõ."""
    part = part.replace("\u00d0", "Đ").replace("\u00f0", "đ")
    key = re.sub(r"[^a-z0-9]", "", fold(part))
    if key in _PROVINCE_NAMES:
        return _PROVINCE_NAMES[key]
    # fold() hạ chữ thường: so với bản chữ thường để biết tên có dấu hay không.
    if typed and fold(part) == part.lower() and fold(typed) != typed.lower():
        if re.sub(r"[^a-z0-9]", "", fold(typed)) == key:
            return typed.strip().title()
    return part


def forecast_cached(lat: float, lon: float, days: int) -> bool:
    """Dự báo tại toạ độ này còn trong cache (chưa quá TTL) - tra lại không gọi mạng."""
    cached = _forecast_cache.get((round(lat, 3), round(lon, 3), max(1, min(days, _MAX_DAYS))))
    return cached is not None and time.monotonic() - cached[0] < _FORECAST_TTL_SEC


def location_cached(location: str) -> bool:
    """Tra địa điểm này có cần gọi geocoding không: TP.HCM (toạ độ sẵn) hoặc đã có
    trong cache toạ độ - kể cả kết quả "không tìm thấy" - thì không."""
    name = _clean_location(location)
    return is_default_location(name) or fold(name) in _geo_cache


def is_default_location(location: str) -> bool:
    key = re.sub(r"[^a-z0-9]", "", fold(location))
    return not key or key in _DEFAULT_ALIASES


def _cache_put(cache: OrderedDict, key, value, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


async def _get(url: str, params: dict) -> httpx.Response:
    resp = await http_client.get_client().get(url, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp


async def _geocode(name: str) -> tuple[str, float, float] | None:
    key = fold(name)
    if key in _geo_cache:
        _geo_cache.move_to_end(key)
        return _geo_cache[key]
    result = None
    # Có dấu trước (khớp tên tiếng Việt tốt hơn), rồi bản bỏ dấu; ưu tiên Việt Nam.
    for query, extra in (
        (name, {"countryCode": "VN"}),
        (fold(name), {"countryCode": "VN"}),
        (name, {}),
    ):
        resp = await _get(
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": query, "count": 1, "language": "vi", **extra},
        )
        found = (resp.json() or {}).get("results") or []
        if found:
            g = found[0]
            parts: list[str] = []
            for part in (g.get("name"), g.get("admin1"), g.get("country")):
                part = _vi_place(part, name) if part else part
                if part and fold(part).replace(" ", "") not in {
                    fold(p).replace(" ", "") for p in parts
                }:
                    parts.append(part)
            result = (", ".join(parts), float(g["latitude"]), float(g["longitude"]))
            break
    _cache_put(_geo_cache, key, result, _GEO_CACHE_MAX)
    return result


async def resolve_location(location: str) -> tuple[str, float, float] | None:
    """(nhãn, lat, lon) của địa điểm; rỗng/tên khác của TP.HCM -> TP.HCM.

    None = geocoding không tìm thấy. Raise WeatherError khi tra toạ độ lỗi."""
    name = _clean_location(location)
    if is_default_location(name):
        return DEFAULT_LABEL, DEFAULT_LAT, DEFAULT_LON
    try:
        return await _geocode(name)
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherError(f"tra toạ độ '{name}' lỗi: {exc!r}") from exc


def reverse_cached(lat: float, lon: float) -> bool:
    """Toạ độ này đã tra tên rồi (kể cả không có tên) - tra lại không gọi Nominatim."""
    return (round(lat, 2), round(lon, 2)) in _reverse_cache


def _place_label(address: dict) -> str | None:
    """ "Phường 1, Đà Lạt" từ ``address`` của Nominatim: nơi nhỏ (phường/xã/thị trấn) +
    thành phố hoặc tỉnh, bỏ tiền tố hành chính ở cấp lớn, không lặp tên."""

    def first(*keys: str) -> tuple[str, str]:
        return next(((k, str(address[k]).strip()) for k in keys if address.get(k)), ("", ""))

    small_key, small = first("suburb", "quarter", "village", "town", "city_district", "county")
    # Cấp lớn không lấy lại trường đã dùng cho cấp nhỏ ("town" có thể là cả hai).
    big_keys = [k for k in ("city", "town", "state", "province") if k != small_key]
    big = _ADMIN_PREFIX_RE.sub("", first(*big_keys)[1])
    parts: list[str] = []
    for part in (small, big):
        part = _vi_place(part) if part else part
        if part and fold(part) not in {fold(x) for x in parts}:
            parts.append(part)
    return ", ".join(parts)[:80] or None


async def reverse_geocode(lat: float, lon: float) -> str | None:
    """Tên nơi của toạ độ (làm tròn 2 số lẻ ~1 km) qua Nominatim; None = không có tên,
    đã tắt (NOMINATIM_ENABLED=0), đang nghỉ sau lỗi, hoặc nguồn lỗi. Không bao giờ raise
    lỗi mạng - tên chỉ là phần phụ, nơi gọi tự đặt hạn chót (lượt chờ xếp hàng 1 giây)."""
    key = (round(lat, 2), round(lon, 2))
    if key in _reverse_cache:
        _reverse_cache.move_to_end(key)
        return _reverse_cache[key]
    if not config.NOMINATIM_ENABLED or time.monotonic() < _reverse_state["down_until"]:
        return None
    async with _reverse_lock:
        if key in _reverse_cache:  # lượt khác vừa tra xong trong lúc chờ khoá
            return _reverse_cache[key]
        wait = _reverse_state["last"] + _NOMINATIM_INTERVAL_SEC - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        site = config.SITE_BASE_URL or "https://github.com/traique/lananh"
        try:
            resp = await http_client.get_client().get(
                _NOMINATIM_URL,
                params={
                    "format": "jsonv2",
                    "lat": f"{key[0]:.2f}",
                    "lon": f"{key[1]:.2f}",
                    "zoom": 14,
                    "addressdetails": 1,
                    "accept-language": "vi",
                },
                headers={"User-Agent": f"LCTA-thoi-tiet/1.0 (+{site})", "Referer": site},
                timeout=_NOMINATIM_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # 429/403 (bị chặn) hay mạng lỗi: nghỉ hẳn một lúc, không dồn thêm lượt.
            _reverse_state["down_until"] = time.monotonic() + _NOMINATIM_COOLDOWN_SEC
            logger.info("Nominatim lỗi (%r), nghỉ %ds", exc, _NOMINATIM_COOLDOWN_SEC)
            return None
        finally:
            _reverse_state["last"] = time.monotonic()
    address = data.get("address") if isinstance(data, dict) else None
    name = _place_label(address) if isinstance(address, dict) else None
    _cache_put(_reverse_cache, key, name, _REVERSE_CACHE_MAX)
    return name


def _not_found(location: str) -> dict:
    return {"error": f"Không tìm thấy địa điểm '{_clean_location(location)}'"}


async def get_weather(location: str = "", days: int = 1) -> dict:
    """Thời tiết hiện tại + dự báo ``days`` ngày (1-7) cho ``location``.

    ``location`` rỗng hoặc là tên khác của TP.HCM -> TP.HCM. Không tìm thấy
    địa điểm -> dict có khoá ``error``. Raise WeatherError khi mọi nguồn lỗi."""
    geo = await resolve_location(location)
    if not geo:
        return _not_found(location)
    label, lat, lon = geo
    return await get_weather_at(lat, lon, label, days)


async def get_weather_at(lat: float, lon: float, label: str, days: int = 1) -> dict:
    """Như get_weather nhưng theo TOẠ ĐỘ có sẵn (tin nhắn vị trí Telegram) - không
    geocoding; dùng chung cache dự báo có trần. Raise WeatherError khi mọi nguồn lỗi."""
    try:
        days = int(days or 1)
    except (TypeError, ValueError):
        days = 1
    days = max(1, min(days, _MAX_DAYS))
    cache_key = (round(lat, 3), round(lon, 3), days)
    cached = _forecast_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _FORECAST_TTL_SEC:
        return {**cached[1], "location": label}

    try:
        data = await _open_meteo(label, lat, lon, days)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.info("Open-Meteo lỗi (%r), dùng wttr.in", exc)
        try:
            data = await _wttr(label, lat, lon, days)
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc2:
            raise WeatherError(f"Open-Meteo và wttr.in đều lỗi: {exc2!r}") from exc2
        alerts.send(
            f"⚠️ Thời tiết: Open-Meteo lỗi ({type(exc).__name__}), đang dùng nguồn dự phòng "
            f"wttr.in - dự báo chỉ còn {WTTR_MAX_DAYS} ngày. Trang /thoi-tiet đã ghi rõ.",
            key=_FALLBACK_ALERT_KEY,
            cooldown=_FALLBACK_ALERT_COOLDOWN_SEC,
        )
    _cache_put(_forecast_cache, cache_key, (time.monotonic(), data), _FORECAST_CACHE_MAX)
    return data


async def get_hourly(location: str = "") -> dict:
    """Dự báo theo giờ 3 ngày (hôm nay -> ngày kia): {"location", "source", "hours": [HourPoint]}.

    Không tìm thấy địa điểm -> dict có ``error``. Raise WeatherError khi lỗi
    (không có nguồn dự phòng theo giờ: nơi gọi tự quay về dự báo theo ngày)."""
    geo = await resolve_location(location)
    if not geo:
        return _not_found(location)
    label, lat, lon = geo
    key = (round(lat, 3), round(lon, 3))
    cached = _hourly_cache.get(key)
    if cached and time.monotonic() - cached[0] < _FORECAST_TTL_SEC:
        return {"location": label, "source": "Open-Meteo", "hours": cached[1]}
    try:
        resp = await _get(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": lat,
                "longitude": lon,
                "timezone": _TIMEZONE,
                "forecast_days": _HOURLY_DAYS,
                "hourly": "precipitation_probability,precipitation,temperature_2m,weather_code",
            },
        )
        h = resp.json()["hourly"]
        hours = [
            HourPoint(
                time=dt.datetime.fromisoformat(t),
                rain_chance=h["precipitation_probability"][i],
                precip_mm=h["precipitation"][i],
                temperature_c=h["temperature_2m"][i],
                code=h["weather_code"][i],
            )
            for i, t in enumerate(h["time"][: 24 * _HOURLY_DAYS])
        ]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise WeatherError(f"dự báo theo giờ lỗi: {exc!r}") from exc
    if not hours:
        raise WeatherError("dự báo theo giờ rỗng")
    _cache_put(_hourly_cache, key, (time.monotonic(), hours), _HOURLY_CACHE_MAX)
    return {"location": label, "source": "Open-Meteo", "hours": hours}


async def get_air_quality(location: str = "") -> dict:
    """US AQI + PM2.5 hiện tại: {"location", "us_aqi", "pm2_5", "source"}.

    Không tìm thấy địa điểm -> dict có ``error``. Raise WeatherError khi lỗi."""
    geo = await resolve_location(location)
    if not geo:
        return _not_found(location)
    label, lat, lon = geo
    return await get_air_quality_at(lat, lon, label)


async def get_air_quality_at(lat: float, lon: float, label: str) -> dict:
    """AQI theo toạ độ có sẵn (không geocoding). Raise WeatherError khi lỗi."""
    key = (round(lat, 3), round(lon, 3))
    cached = _air_cache.get(key)
    if cached and time.monotonic() - cached[0] < _AIR_TTL_SEC:
        return {**cached[1], "location": label}
    try:
        resp = await _get(
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            {
                "latitude": lat,
                "longitude": lon,
                "timezone": _TIMEZONE,
                "current": "us_aqi,pm2_5",
            },
        )
        cur = resp.json()["current"]
        aqi = cur.get("us_aqi")
        data = {
            "location": label,
            "us_aqi": None if aqi is None else round(float(aqi)),
            "pm2_5": cur.get("pm2_5"),
            "source": AIR_SOURCE,
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise WeatherError(f"chất lượng không khí lỗi: {exc!r}") from exc
    if data["us_aqi"] is None:
        raise WeatherError("Open-Meteo không có US AQI cho điểm này")
    _cache_put(_air_cache, key, (time.monotonic(), data), _AIR_CACHE_MAX)
    return data


def aqi_level(aqi: int) -> str:
    """Mức US AQI (thang EPA) bằng tiếng Việt."""
    for limit, label in (
        (50, "tốt"),
        (100, "trung bình"),
        (150, "kém - không tốt cho nhóm nhạy cảm"),
        (200, "xấu"),
        (300, "rất xấu"),
    ):
        if aqi <= limit:
            return label
    return "nguy hại"


def describe_air(air: dict) -> str:
    """AQI đầy đủ (khi người dùng hỏi thẳng chất lượng không khí)."""
    if air.get("error"):
        return air["error"]
    aqi = int(air["us_aqi"])
    pm = air.get("pm2_5")
    pm_text = f", PM2.5 {_n(pm)} µg/m³" if pm is not None else ""
    return f"US AQI {aqi} ({aqi_level(aqi)}){pm_text}"


def air_warning(air: dict | None) -> str | None:
    """Câu cảnh báo bụi mịn - CHỈ khi US AQI vượt AQI_WARN_THRESHOLD, còn lại None."""
    if not air or air.get("error") or air.get("us_aqi") is None:
        return None
    if int(air["us_aqi"]) <= AQI_WARN_THRESHOLD:
        return None
    return (
        f"😷 Không khí {describe_air(air)} - nhóm nhạy cảm hạn chế ra ngoài, "
        f"nên đeo khẩu trang. ({AIR_SOURCE})"
    )


async def _open_meteo(label: str, lat: float, lon: float, days: int) -> dict:
    resp = await _get(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": lat,
            "longitude": lon,
            "timezone": _TIMEZONE,
            "forecast_days": days,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,wind_speed_10m,precipitation",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,uv_index_max",
        },
    )
    data = resp.json()
    cur = data["current"]
    out = {
        "location": label,
        "source": "Open-Meteo",
        "now": {
            "description": WMO.get(cur.get("weather_code"), "không rõ"),
            "temperature_c": cur.get("temperature_2m"),
            "feels_like_c": cur.get("apparent_temperature"),
            "humidity_percent": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "precipitation_mm": cur.get("precipitation"),
        },
        "daily": [],
    }
    d = data.get("daily", {})
    for i, day in enumerate(d.get("time", [])):
        out["daily"].append(
            {
                "date": day,
                "weekday": WEEKDAYS[dt.date.fromisoformat(day).weekday()],
                "description": WMO.get(d["weather_code"][i], "không rõ"),
                "min_c": d["temperature_2m_min"][i],
                "max_c": d["temperature_2m_max"][i],
                "rain_chance_percent": d["precipitation_probability_max"][i],
                "uv_max": d["uv_index_max"][i],
            }
        )
    return out


async def _wttr(label: str, lat: float, lon: float, days: int) -> dict:
    resp = await _get(f"https://wttr.in/{lat:.3f},{lon:.3f}", {"format": "j1", "lang": "vi"})
    d = resp.json()
    c = d["current_condition"][0]
    out = {
        "location": label,
        "source": f"wttr.in (dự phòng, tối đa {WTTR_MAX_DAYS} ngày)",
        "fallback": True,
        "now": {
            "description": _wttr_text(c),
            "temperature_c": float(c["temp_C"]),
            "feels_like_c": float(c["FeelsLikeC"]),
            "humidity_percent": int(c["humidity"]),
            "wind_kmh": float(c["windspeedKmph"]),
            "precipitation_mm": float(c["precipMM"]),
        },
        "daily": [],
    }
    for w in d.get("weather", [])[:days]:
        noon = w["hourly"][4]
        out["daily"].append(
            {
                "date": w["date"],
                "weekday": WEEKDAYS[dt.date.fromisoformat(w["date"]).weekday()],
                "description": _wttr_text(noon),
                "min_c": float(w["mintempC"]),
                "max_c": float(w["maxtempC"]),
                "rain_chance_percent": max(int(h.get("chanceofrain", 0)) for h in w["hourly"]),
                "uv_max": float(w.get("uvIndex", 0)),
            }
        )
    return out


def _wttr_text(entry: dict) -> str:
    """Mô tả tiếng Việt của 1 mục wttr.in: theo mã thời tiết, rồi lang_vi; không bao giờ
    lấy weatherDesc (tiếng Anh)."""
    try:
        text = _WWO.get(int(entry.get("weatherCode")))
    except (TypeError, ValueError):
        text = None
    if text:
        return text
    vi = (entry.get("lang_vi") or [{}])[0].get("value", "").strip()
    return vi or "không rõ"


def _n(x) -> str:
    return "?" if x is None else f"{round(float(x))}"


def describe_weather(w: dict) -> str:
    """Đoạn văn tiếng Việt từ ``get_weather`` để chèn làm grounding cho chat."""
    if w.get("error"):
        return w["error"]
    now = w.get("now") or {}
    lines = [
        f"{w.get('location')} (nguồn {w.get('source')}): hiện {now.get('description')}, "
        f"{_n(now.get('temperature_c'))}°C (cảm giác {_n(now.get('feels_like_c'))}°C), "
        f"độ ẩm {_n(now.get('humidity_percent'))}%, gió {_n(now.get('wind_kmh'))} km/h."
    ]
    for i, d in enumerate(w.get("daily") or []):
        name = "Hôm nay" if i == 0 else ("Ngày mai" if i == 1 else d["weekday"])
        date_vn = dt.date.fromisoformat(d["date"]).strftime("%d/%m")
        lines.append(
            f"{name} ({date_vn}): {d['description']}, {_n(d['min_c'])}-{_n(d['max_c'])}°C, "
            f"khả năng mưa {_n(d['rain_chance_percent'])}%, UV tối đa {_n(d['uv_max'])}."
        )
    return "\n".join(lines)
