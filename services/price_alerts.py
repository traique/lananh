"""Cảnh báo giá TRONG PHIÊN: ngưỡng người dùng tự đặt + stop/target danh mục chủ bot.

- Ngưỡng ("báo anh khi HPG về 25", /canhbaogia) là mức NGƯỜI DÙNG tự nhập, không
  phải tín hiệu hay khuyến nghị - stock/policy.py vẫn là nguồn duy nhất của
  action/vùng giá/stop/target hệ thống.
- Quét mỗi PRICE_ALERT_POLL_SEC giây (mặc định 300) CHỈ trong giờ giao dịch
  (stock.calendar.is_market_hours), CHỈ dùng tick khớp lệnh của hôm nay
  (providers.fetch_realtime_tick - không bao giờ dùng giá phiên trước).
- Có trần: số ngưỡng mỗi người (PRICE_ALERT_MAX_PER_USER), tổng số mã mỗi vòng
  (PRICE_ALERT_MAX_SYMBOLS - ưu tiên mã đang giữ rồi tới ngưỡng cũ trước), số
  request song song. Request/ngày <= số mã x số vòng (~72 vòng/ngày).
- Trạng thái vượt ngưỡng lưu DB (stock_price_alerts.last_state): unknown/armed
  -> chạm thì báo 1 lần (crossed) -> giá quay lại quá ngưỡng _REARM_PCT mới
  armed lại; mỗi ngưỡng tối đa 1 tin/ngày. Restart/redeploy không báo lại.
- Stop/target danh mục chủ bot (vị thế đã quy đổi sự kiện quyền): báo ngay khi
  chạm trong phiên thay vì chờ 15h05. Ghi stock_alert_log để cảnh báo cuối phiên
  không báo lại cùng ngày; mã đã đóng cửa dưới stop từ phiên trước thì để job
  cuối phiên báo "ngày thứ N" (không báo lại mỗi sáng).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from core import database as db
from core import trace
from core.envutil import env_int, env_on
from services import portfolio_alerts, portfolio_push
from stock import calendar as trading_calendar
from stock import portfolio, providers

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
BELOW, ABOVE = "BELOW", "ABOVE"
# Giá phải quay lại quá ngưỡng 1% mới tính là lần chạm mới (chống báo dồn dập khi
# giá dao động sát ngưỡng).
_REARM_PCT = 0.01
_MAX_ALERT_ROWS = 500
_TICK_CONCURRENCY = 4
# Sự kiện quyền đã xác nhận của vị thế chủ bot: cache ngắn (mỗi lần tính tốn 1
# chuỗi DNSE 90 phiên + lịch quyền), có trần. Chỉ cache SỰ KIỆN theo (mã, mốc giá
# vốn) - quy đổi áp lên dòng vị thế mới đọc mỗi vòng, nên sửa stop/target hay bán
# bớt có hiệu lực ngay thay vì chờ hết TTL.
_VIEW_TTL_SEC = 30 * 60
_VIEW_CACHE_MAX = 64
_view_cache: dict[tuple[str, str], tuple[float, tuple]] = {}
_task: asyncio.Task | None = None


def enabled() -> bool:
    return env_on("PRICE_ALERTS_ENABLED")


def holdings_intraday_enabled() -> bool:
    return env_on("PRICE_ALERTS_HOLDINGS_INTRADAY")


def poll_seconds() -> int:
    return env_int("PRICE_ALERT_POLL_SEC", 300, 120, 1800)


def max_symbols() -> int:
    return env_int("PRICE_ALERT_MAX_SYMBOLS", 40, 1, 200)


def max_per_user() -> int:
    return env_int("PRICE_ALERT_MAX_PER_USER", 10, 1, 50)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}".replace(",", ".")


def direction_word(direction: str) -> str:
    return "về" if direction == BELOW else "lên"


def is_hit(direction: str, level: float, price: float) -> bool:
    return price <= level if direction == BELOW else price >= level


def evaluate(direction: str, level: float, price: float, last_state: str) -> tuple[bool, str]:
    """(có báo không, trạng thái mới) cho 1 ngưỡng - hàm thuần."""
    if is_hit(direction, level, price):
        return last_state != "crossed", "crossed"
    if last_state == "crossed":
        if direction == BELOW:
            rearmed = price >= level * (1 + _REARM_PCT)
        else:
            rearmed = price <= level * (1 - _REARM_PCT)
        return False, "armed" if rearmed else "crossed"
    return False, "armed"


def _notified_today(notified_at, today: str) -> bool:
    if notified_at is None:
        return False
    when = notified_at.astimezone(_VN_TZ) if getattr(notified_at, "tzinfo", None) else notified_at
    return when.date().isoformat() == today


def alert_notice(symbol: str, direction: str, level: float, price: float, now: datetime) -> str:
    return (
        f"🔔 {symbol} chạm {_fmt(level)}đ (ngưỡng anh đặt: {direction_word(direction)} "
        f"{_fmt(level)}đ) - giá khớp {_fmt(price)}đ lúc {now:%H:%M}. Đây là mức anh tự đặt, "
        "không phải khuyến nghị mua bán."
    )


def holding_notice(symbol: str, kind: str, level: float, price: float, now: datetime) -> str:
    what = "chạm/thủng stop" if kind == "stop" else "đạt/vượt target"
    icon = "🚨" if kind == "stop" else "🎯"
    return (
        f"{icon} {symbol} {what} {_fmt(level)}đ trong phiên (giá khớp {_fmt(price)}đ lúc "
        f"{now:%H:%M}) - mức anh đặt trong danh mục, không phải lệnh. Dùng /danhmuc để xem chi tiết."
    )


async def _fetch_ticks(symbols: list[str]) -> dict[str, float | None]:
    slots = asyncio.Semaphore(_TICK_CONCURRENCY)

    async def one(symbol: str) -> float | None:
        async with slots:
            try:
                return await providers.fetch_realtime_tick(symbol)
            except Exception:
                logger.warning("price_alerts: lỗi lấy tick %s.", symbol, exc_info=True)
                return None

    prices = await asyncio.gather(*(one(s) for s in symbols))
    return dict(zip(symbols, prices, strict=True))


async def _effective(holding: portfolio.Holding) -> portfolio.Holding:
    """Vị thế (dòng DB mới đọc) sau quy đổi sự kiện quyền đã xác nhận (cache)."""
    key = (holding.symbol, holding.cost_basis_as_of or "")
    now = time.monotonic()
    hit = _view_cache.get(key)
    if hit is not None and now - hit[0] < _VIEW_TTL_SEC:
        return portfolio.effective_holding(holding, hit[1])
    (view,) = await portfolio.corporate_views([holding])
    for stale in [k for k, (ts, _) in _view_cache.items() if now - ts >= _VIEW_TTL_SEC]:
        _view_cache.pop(stale, None)
    while len(_view_cache) >= _VIEW_CACHE_MAX:
        _view_cache.pop(next(iter(_view_cache)))
    _view_cache[key] = (now, tuple(view.applied))
    return portfolio.effective_holding(holding, view.applied)


async def _owner_holdings(owner_id: int) -> list[portfolio.Holding]:
    if not owner_id or not holdings_intraday_enabled():
        return []
    holdings = [
        h
        for h in await portfolio.list_holdings(owner_id)
        if h.stop_price is not None or h.target_price is not None
    ]
    return [await _effective(h) for h in holdings]


def _breached_at_last_close(state: dict, symbol: str, kind: str, level: float, today) -> bool:
    """Job cuối phiên đã ghi điều kiện còn đúng ở phiên trước -> đang là chuỗi
    "ngày thứ N", không phải lần chạm mới."""
    entry = (state.get(symbol) or {}).get(kind)
    if not isinstance(entry, dict) or entry.get("level") != level:
        return False
    return entry.get("last") == trading_calendar.previous_trading_day(today).isoformat()


async def _check_holdings(
    owner_id: int, holdings: list[portfolio.Holding], prices: dict, now: datetime
) -> int:
    if not holdings:
        return 0
    today = now.date()
    today_iso = today.isoformat()
    state = await portfolio_alerts.load_state(owner_id)
    sent = 0
    for holding in holdings:
        price = prices.get(holding.symbol)
        if price is None:
            continue
        for kind, level in (("stop", holding.stop_price), ("target", holding.target_price)):
            if level is None:
                continue
            hit = price <= level if kind == "stop" else price >= level
            if not hit or _breached_at_last_close(state, holding.symbol, kind, level, today):
                continue
            ref = portfolio_alerts.holding_alert_ref(holding.symbol, level)
            if await portfolio_push.already_sent(owner_id, kind, ref, on=today_iso):
                continue
            if await portfolio_push.deliver(
                owner_id,
                holding_notice(holding.symbol, kind, level, price, now),
                kind=kind,
                ref_key=ref,
                today=today_iso,
            ):
                sent += 1
    return sent


async def _check_user_alerts(alerts, prices: dict, now: datetime) -> int:
    today_iso = now.date().isoformat()
    sent = 0
    for alert in alerts:
        price = prices.get(alert["symbol"])
        if price is None:
            continue
        level = float(alert["level"])
        notify, state = evaluate(alert["direction"], level, price, alert["last_state"])
        if notify and _notified_today(alert["notified_at"], today_iso):
            notify = False  # tối đa 1 tin/ngày/ngưỡng; vẫn ghi trạng thái crossed
        if notify:
            delivered = await portfolio_push.deliver(
                int(alert["telegram_user_id"]),
                alert_notice(alert["symbol"], alert["direction"], level, price, now),
                kind="price",
                ref_key=str(alert["id"]),
                today=today_iso,
            )
            if not delivered:
                continue  # giữ trạng thái cũ để vòng sau thử lại
            sent += 1
            await db.portfolio_set_price_alert_state(alert["id"], state, notified=True)
        elif state != alert["last_state"]:
            await db.portfolio_set_price_alert_state(alert["id"], state, notified=False)
    return sent


async def run_once(owner_id: int, now: datetime | None = None) -> int:
    """1 vòng quét; trả số tin đã gửi. Ngoài giờ giao dịch không làm gì."""
    now = now or datetime.now(_VN_TZ)
    if not trading_calendar.is_market_hours(now):
        return 0
    alerts = await db.portfolio_active_price_alerts(_MAX_ALERT_ROWS)
    holdings = await _owner_holdings(owner_id)
    symbols: list[str] = []
    for symbol in [h.symbol for h in holdings] + [a["symbol"] for a in alerts]:
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        return 0
    if len(symbols) > max_symbols():
        logger.warning(
            "price_alerts: %d mã vượt trần %d, bỏ bớt mã mới nhất.", len(symbols), max_symbols()
        )
        symbols = symbols[: max_symbols()]
    prices = await _fetch_ticks(symbols)
    sent = await _check_holdings(owner_id, holdings, prices, now)
    sent += await _check_user_alerts(alerts, prices, now)
    return sent


async def _loop(owner_id: int) -> None:
    trace.detach()
    while True:
        try:
            with trace.job("price_alerts"):
                await run_once(owner_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("price_alerts: lỗi vòng quét.", exc_info=True)
        await asyncio.sleep(poll_seconds())


def start(owner_id: int) -> None:
    global _task
    if not enabled():
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(owner_id))


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    await asyncio.gather(_task, return_exceptions=True)
    _task = None
