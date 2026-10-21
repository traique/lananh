"""Thời tiết từ Open-Meteo (miễn phí, không cần API key), dự phòng wttr.in.

Chuyển từ gói vn-info (MIT License, Copyright (c) 2026), sửa cho hợp repo:
- Dùng HTTP client chung (services.http_client) thay vì tự mở client riêng.
- Cache toạ độ và cache dự báo đều có trần số mục (RAM 512 MB trên Render).
- Timeout ngắn, không retry dài: đây là grounding cho 1 lượt chat còn chịu hạn
  chót CHAT_TURN_TIMEOUT_SEC.

Không nói địa điểm (hoặc nói Sài Gòn/HCM...) -> mặc định TP. Hồ Chí Minh.

    data = await get_weather("Hà Nội", days=3)
    describe_weather(data)
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
import unicodedata
from collections import OrderedDict

import httpx

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

_GEO_CACHE_MAX = 128
_FORECAST_CACHE_MAX = 32
_FORECAST_TTL_SEC = 600
# tên đã chuẩn hoá -> (nhãn, lat, lon) hoặc None (không tìm thấy, tránh gọi lại)
_geo_cache: OrderedDict[str, tuple[str, float, float] | None] = OrderedDict()
_forecast_cache: OrderedDict[tuple[float, float, int], tuple[float, dict]] = OrderedDict()

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


def fold(text: str) -> str:
    """Chữ thường, bỏ dấu tiếng Việt, gộp khoảng trắng."""
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text.lower()).strip()


def _clean_location(location: str) -> str:
    return _PREFIX_RE.sub("", (location or "").strip(" .,?!")).strip()


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
                if part and fold(part).replace(" ", "") not in {
                    fold(p).replace(" ", "") for p in parts
                }:
                    parts.append(part)
            result = (", ".join(parts), float(g["latitude"]), float(g["longitude"]))
            break
    _cache_put(_geo_cache, key, result, _GEO_CACHE_MAX)
    return result


async def get_weather(location: str = "", days: int = 1) -> dict:
    """Thời tiết hiện tại + dự báo ``days`` ngày (1-7) cho ``location``.

    ``location`` rỗng hoặc là tên khác của TP.HCM -> TP.HCM. Không tìm thấy
    địa điểm -> dict có khoá ``error``. Raise WeatherError khi mọi nguồn lỗi."""
    try:
        days = int(days or 1)
    except (TypeError, ValueError):
        days = 1
    days = max(1, min(days, _MAX_DAYS))
    name = _clean_location(location)
    if is_default_location(name):
        label, lat, lon = DEFAULT_LABEL, DEFAULT_LAT, DEFAULT_LON
    else:
        try:
            geo = await _geocode(name)
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherError(f"tra toạ độ '{name}' lỗi: {exc!r}") from exc
        if not geo:
            return {"error": f"Không tìm thấy địa điểm '{name}'"}
        label, lat, lon = geo

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
    _cache_put(_forecast_cache, cache_key, (time.monotonic(), data), _FORECAST_CACHE_MAX)
    return data


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
    desc = (c.get("lang_vi") or c.get("weatherDesc") or [{"value": ""}])[0]["value"]
    out = {
        "location": label,
        "source": "wttr.in (dự phòng, tối đa 3 ngày)",
        "now": {
            "description": desc,
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
                "description": (noon.get("lang_vi") or noon["weatherDesc"])[0]["value"],
                "min_c": float(w["mintempC"]),
                "max_c": float(w["maxtempC"]),
                "rain_chance_percent": max(int(h.get("chanceofrain", 0)) for h in w["hourly"]),
                "uv_max": float(w.get("uvIndex", 0)),
            }
        )
    return out


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
