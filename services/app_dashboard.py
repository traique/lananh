"""Dữ liệu cho màn hình thông tin của app Android (GET /api/v1/app/dashboard).

App gọi 1 request mỗi ``REFRESH_SEC`` (5 phút) và nhận MỘT JSON gồm: lịch hôm nay
+ lịch tháng âm dương, thời tiết, giá vàng, giá xăng, tỷ giá, tin RSS và watchlist
cổ phiếu của đúng người sở hữu thiết bị.

Nhẹ cho Render 512 MB:
- KHÔNG gọi AI, không chiếm khoá lượt chat (services.concurrency).
- Mọi nguồn đi qua cache có sẵn (vàng/tỷ giá 10 phút, xăng 1 giờ, thời tiết 10 phút)
  hoặc cache có trần ở đây (tin RSS ``_NEWS_TTL_SEC``, giá cổ phiếu
  ``_QUOTE_TTL_OPEN_SEC`` trong phiên / ``_QUOTE_TTL_CLOSED_SEC`` ngoài phiên).
- Cả bản JSON giữ ``_RESULT_TTL_SEC`` giây cho mỗi (người, địa điểm), có khoá để
  nhiều request cùng lúc chỉ tính 1 lần. Trần ``_RESULT_CACHE_MAX`` bản.
- Mỗi phần có hạn chót riêng; phần lỗi trả ``{"error": "..."}``, phần khác vẫn có.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import logging
import time
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from core import database as db
from core import version
from core.envutil import env_int
from services import morning_news, price_lookup, site_data, weather, weather_home
from stock import calendar as trading_calendar
from stock import providers

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
REFRESH_SEC = 300
_RESULT_TTL_SEC = 60
_RESULT_CACHE_MAX = 8
_SECTION_TIMEOUT_SEC = 20
_NEWS_TTL_SEC = 15 * 60
_NEWS_ERROR_TTL_SEC = 120
_NEWS_LIMIT = 10
_QUOTE_TTL_OPEN_SEC = 120
_QUOTE_TTL_CLOSED_SEC = 30 * 60
_QUOTE_CACHE_MAX = 64
_QUOTE_CONCURRENCY = 3
_GOLD_ROWS = 6
_HOURS_AHEAD = 12
_WEATHER_DAYS = 4
_FX_CODES = ("USD", "EUR", "CNY", "JPY")
PLACE_MAX = 60

_results: collections.OrderedDict[tuple[int, str], tuple[float, dict]] = collections.OrderedDict()
_result_locks: dict[tuple[int, str], asyncio.Lock] = {}
_news: tuple[float, list[dict] | None] | None = None
_news_lock = asyncio.Lock()
_quotes: collections.OrderedDict[str, tuple[float, dict | None]] = collections.OrderedDict()
_calendar: tuple[dt.date, dict] | None = None


def watchlist_limit() -> int:
    return env_int("APP_WATCHLIST_MAX", 20, 1, 50)


def clear_cache() -> None:
    global _news, _calendar
    _results.clear()
    _result_locks.clear()
    _quotes.clear()
    _news = None
    _calendar = None


def _now() -> dt.datetime:
    return dt.datetime.now(_VN_TZ)


# ─── Lịch ────────────────────────────────────────────────────────────────────


def calendar_section(today: dt.date) -> dict:
    """Hôm nay + lưới tháng (tính thuần, giữ 1 bản theo ngày)."""
    global _calendar
    if _calendar is None or _calendar[0] != today:
        _calendar = (
            today,
            {
                "today": site_data.day_info(today),
                "month": site_data.month_grid(today.year, today.month),
            },
        )
    return _calendar[1]


# ─── Thời tiết ───────────────────────────────────────────────────────────────


def _hours(points: list, now: dt.datetime) -> list[dict]:
    start = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    out = []
    for p in points:
        when = p.time.replace(tzinfo=None) if p.time.tzinfo else p.time
        if when < start:
            continue
        out.append(
            {
                "time": when.strftime("%Y-%m-%dT%H:%M"),
                "temperatureC": p.temperature_c,
                "rainChance": p.rain_chance,
                "code": p.code,
            }
        )
        if len(out) >= _HOURS_AHEAD:
            break
    return out


async def weather_section(user_id: int, place: str, now: dt.datetime) -> dict:
    location = place or await weather_home.get(user_id)
    data = await weather.get_weather(location, days=_WEATHER_DAYS)
    if data.get("error"):
        return {"error": str(data["error"])}
    try:
        hourly = await weather.get_hourly(location)
        hours = _hours(hourly.get("hours") or [], now)
    except weather.WeatherError:
        hours = []  # thiếu theo giờ vẫn hiện thời tiết hiện tại + theo ngày
    cur = data.get("now") or {}
    return {
        "location": data.get("location", ""),
        "source": data.get("source", ""),
        "now": {
            "description": cur.get("description"),
            "temperatureC": cur.get("temperature_c"),
            "feelsLikeC": cur.get("feels_like_c"),
            "humidity": cur.get("humidity_percent"),
            "windKmh": cur.get("wind_kmh"),
            "code": hours[0]["code"] if hours else None,
        },
        "hours": hours,
        "daily": [
            {
                "date": d.get("date"),
                "weekday": d.get("weekday"),
                "description": d.get("description"),
                "minC": d.get("min_c"),
                "maxC": d.get("max_c"),
                "rainChance": d.get("rain_chance_percent"),
            }
            for d in data.get("daily") or []
        ],
    }


# ─── Vàng, xăng, tỷ giá ──────────────────────────────────────────────────────


async def gold_section() -> dict:
    g = await price_lookup.gold()
    world = g.get("world") or None
    fx = g.get("fx") or {}
    return {
        "source": g.get("source"),
        "updated": g.get("updated"),
        "unit": g.get("unit"),
        "domestic": [
            {
                "name": r.get("name"),
                "buy": r.get("buy"),
                "sell": r.get("sell"),
                "changeSell": r.get("changeSell"),
            }
            for r in (g.get("domestic") or [])[:_GOLD_ROWS]
        ],
        "world": (
            {
                "priceUsdPerOunce": world.get("priceUsdPerOunce"),
                "change": world.get("change"),
                "vndPerTael": (world.get("conversion") or {}).get("vndPerTael"),
            }
            if world
            else None
        ),
        "fx": [
            {"code": r.get("code"), "transferBuy": r.get("transferBuy"), "sell": r.get("sell")}
            for r in fx.get("rates") or []
            if r.get("code") in _FX_CODES
        ],
    }


async def fuel_section() -> dict:
    f = await price_lookup.fuel()
    return {
        "source": f.get("source"),
        "updated": f.get("updated"),
        "unit": f.get("unit"),
        "items": [
            {"name": r.get("name"), "price": r.get("zone1"), "change": r.get("change")}
            for r in f.get("petrolimex") or []
        ],
    }


# ─── Tin RSS ─────────────────────────────────────────────────────────────────


async def news_section() -> dict:
    global _news
    async with _news_lock:
        now = time.monotonic()
        if _news is not None:
            ttl = _NEWS_TTL_SEC if _news[1] is not None else _NEWS_ERROR_TTL_SEC
            if now - _news[0] < ttl:
                if _news[1] is None:
                    return {"error": "Nguồn tin đang lỗi, thử lại sau."}
                return {"items": _news[1]}
        try:
            items = await morning_news.rss_headlines(_NEWS_LIMIT)
        except Exception:
            logger.warning("app_dashboard: lỗi đọc RSS.", exc_info=True)
            items = []
        rows = [{"title": i.title, "source": i.source, "url": i.url} for i in items] or None
        _news = (now, rows)
        if rows is None:
            return {"error": "Nguồn tin đang lỗi, thử lại sau."}
        return {"items": rows}


# ─── Watchlist cổ phiếu ──────────────────────────────────────────────────────


def _quote_put(symbol: str, value: dict | None) -> None:
    _quotes[symbol] = (time.monotonic(), value)
    _quotes.move_to_end(symbol)
    while len(_quotes) > _QUOTE_CACHE_MAX:
        _quotes.popitem(last=False)


async def _quote(symbol: str, ttl: float, slots: asyncio.Semaphore) -> dict | None:
    hit = _quotes.get(symbol)
    if hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    async with slots:
        try:
            q = await providers.fetch_quote(symbol)
        except Exception:
            logger.warning("app_dashboard: lỗi lấy giá %s.", symbol, exc_info=True)
            q = None
    value = (
        None
        if q is None
        else {
            "symbol": q.symbol,
            "price": q.price,
            "change": q.change,
            "changePct": q.change_pct,
            "date": q.date,
            "realtime": q.is_realtime,
        }
    )
    if value is None and hit is not None:
        return hit[1]  # nguồn lỗi thoáng qua: giữ giá cũ thay vì ô trống (lần sau thử lại)
    _quote_put(symbol, value)
    return value


async def watchlist_section(user_id: int, now: dt.datetime) -> dict:
    symbols = (await db.portfolio_watchlist(user_id))[: watchlist_limit()]
    market_open = trading_calendar.is_market_hours(now)
    ttl = _QUOTE_TTL_OPEN_SEC if market_open else _QUOTE_TTL_CLOSED_SEC
    slots = asyncio.Semaphore(_QUOTE_CONCURRENCY)
    quotes = await asyncio.gather(*(_quote(s, ttl, slots) for s in symbols))
    return {
        "marketOpen": market_open,
        "items": [
            q if q is not None else {"symbol": s, "price": None}
            for s, q in zip(symbols, quotes, strict=True)
        ],
    }


# ─── Gộp ─────────────────────────────────────────────────────────────────────


async def _section(name: str, load: Callable[[], Awaitable[dict]]) -> dict:
    try:
        return await asyncio.wait_for(load(), _SECTION_TIMEOUT_SEC)
    except TimeoutError:
        logger.warning("app_dashboard: phần %s quá %ss.", name, _SECTION_TIMEOUT_SEC)
        return {"error": "Nguồn chậm, thử lại sau."}
    except Exception as exc:
        logger.warning("app_dashboard: phần %s lỗi: %r", name, exc)
        return {"error": "Nguồn đang lỗi, thử lại sau."}


async def _build(user_id: int, place: str, now: dt.datetime) -> dict:
    weather_data, gold, fuel, news, watchlist = await asyncio.gather(
        _section("weather", lambda: weather_section(user_id, place, now)),
        _section("gold", gold_section),
        _section("fuel", fuel_section),
        _section("news", news_section),
        _section("watchlist", lambda: watchlist_section(user_id, now)),
    )
    data: dict[str, Any] = {
        "generatedAt": now.isoformat(timespec="seconds"),
        "refreshSec": REFRESH_SEC,
        "serverVersion": version.label(),
        "calendar": calendar_section(now.date()),
        "weather": weather_data,
        "gold": gold,
        "fuel": fuel,
        "news": news,
        "watchlist": watchlist,
    }
    return data


async def dashboard(user_id: int, place: str = "", now: dt.datetime | None = None) -> dict:
    """JSON màn hình thông tin của ``user_id`` (cache ``_RESULT_TTL_SEC`` giây)."""
    place = " ".join(str(place or "").split())[:PLACE_MAX]
    key = (user_id, place.lower())
    if key not in _result_locks and len(_result_locks) >= _RESULT_CACHE_MAX * 2:
        for old in [k for k, lk in _result_locks.items() if k not in _results and not lk.locked()]:
            _result_locks.pop(old, None)
    lock = _result_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _results.get(key)
        if hit is not None and time.monotonic() - hit[0] < _RESULT_TTL_SEC:
            return hit[1]
        data = await _build(user_id, place, now or _now())
        _results[key] = (time.monotonic(), data)
        _results.move_to_end(key)
        while len(_results) > _RESULT_CACHE_MAX:
            old, _ = _results.popitem(last=False)
            _result_locks.pop(old, None)
        return data
