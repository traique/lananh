"""Cảnh báo danh mục theo sự kiện: quét holdings sau giờ đóng cửa, báo ngay khi
giá chạm stop/target hoặc có breakout Donchian - thay vì chờ digest sáng hôm sau.

Chạy 1 lần/ngày giao dịch (sau 15h05 giờ VN, khung bù 3 giờ), guard ngày lưu
trong settings nên sống qua restart/spin-down. Chỉ gửi khi CÓ sự kiện; mỗi
(mã, loại sự kiện) chỉ báo 1 lần/ngày.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core import alerts, config, database as db
from stock import calendar as trading_calendar
from stock import features as feat
from stock import portfolio, providers

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_LAST_RUN_KEY = "portfolio_alerts:last_run_date"
# Sau giờ đóng cửa + 5 phút để nến ngày đã chốt trên DNSE.
ALERT_HOUR_VN, ALERT_MINUTE_VN = 15, 5
CATCHUP_HOURS = 3
_POLL_SEC = 10 * 60
_task: asyncio.Task | None = None
_notify = None


def set_notify_callback(fn) -> None:
    global _notify
    _notify = fn


def is_due(now: datetime, last_run_date: str | None) -> bool:
    if last_run_date == now.date().isoformat():
        return False
    if not trading_calendar.is_trading_day(now.date()):
        return False
    start = now.replace(hour=ALERT_HOUR_VN, minute=ALERT_MINUTE_VN, second=0, microsecond=0)
    return start <= now < start + timedelta(hours=CATCHUP_HOURS)


def detect_events(holding: portfolio.Holding, quote, series) -> list[str]:
    """Danh sách dòng cảnh báo cho 1 mã (rỗng = không có gì đáng báo).
    ``quote``: providers.Quote | None; ``series``: OhlcvSeries | None (90 bar)."""
    events: list[str] = []
    price = (
        quote.price
        if quote is not None
        else (series.closes[-1] if series and series.closes else None)
    )
    if price is None or price <= 0:
        return events
    if holding.stop_price is not None and price <= holding.stop_price:
        events.append(
            f"🚨 chạm/thủng stop {holding.stop_price:,.0f} (giá {price:,.0f})".replace(",", ".")
        )
    if holding.target_price is not None and price >= holding.target_price:
        events.append(
            f"🎯 đạt/vượt target {holding.target_price:,.0f} (giá {price:,.0f})".replace(",", ".")
        )
    if series is not None and len(series.closes) >= 21:
        donchian = feat.calc_donchian_breakout(series.highs, series.lows, series.closes)
        if donchian.state == "breakout_up":
            events.append(
                f"📈 breakout lên trên kênh Donchian 20 phiên ({donchian.upper:,.0f})".replace(
                    ",", "."
                )
            )
        elif donchian.state == "breakout_down":
            events.append(
                f"📉 breakdown xuống dưới kênh Donchian 20 phiên ({donchian.lower:,.0f})".replace(
                    ",", "."
                )
            )
    pnl_pct = (price / holding.average_price - 1) * 100 if holding.average_price > 0 else 0.0
    if pnl_pct <= -8:
        events.append(f"⚠️ lỗ {pnl_pct:.1f}% so với giá vốn".replace(".", ","))
    return events


async def build_alert_text(user_id: int) -> str | None:
    holdings = await portfolio.list_holdings(user_id)
    if not holdings:
        return None
    symbols = [h.symbol for h in holdings]
    quotes = await asyncio.gather(
        *(providers.fetch_quote(s) for s in symbols), return_exceptions=True
    )
    series_list = await asyncio.gather(
        *(providers.fetch_ohlcv(s, days=90) for s in symbols), return_exceptions=True
    )
    lines = []
    for holding, quote, series in zip(holdings, quotes, series_list, strict=False):
        if isinstance(quote, BaseException):
            quote = None
        if isinstance(series, BaseException) or not getattr(series, "closes", None):
            series = None
        events = detect_events(holding, quote, series)
        if events:
            lines.append(f"• *{holding.symbol}*: " + "; ".join(events))
    if not lines:
        return None
    return (
        "🔔 *Cảnh báo danh mục cuối phiên*\n"
        + "\n".join(lines)
        + (
            "\n\nSố liệu tham khảo từ dữ liệu đóng cửa, không phải lệnh. Dùng /danhmuc để xem chi tiết."
        )
    )


async def run_once(user_id: int, *, force: bool = False) -> bool:
    """Trả True nếu đã gửi cảnh báo. Đánh dấu ngày kể cả khi không có sự kiện
    (không quét lại mỗi 10 phút cả buổi chiều)."""
    now = datetime.now(_VN_TZ)
    last = await db.get_setting(_LAST_RUN_KEY)
    if not force and not is_due(now, last):
        return False
    text = await build_alert_text(user_id)
    await db.set_setting(_LAST_RUN_KEY, now.date().isoformat())
    if not text or _notify is None:
        return False
    try:
        await _notify(user_id, text)
    except Exception:
        logger.warning("portfolio_alerts: không gửi được cảnh báo.", exc_info=True)
        return False
    return True


# Nhắc chủ bot cập nhật bảng ngày lễ năm sau (stock/calendar.py) từ 1/11, tối
# đa 1 lần/tuần cho tới khi bảng có năm đó.
_CALENDAR_REMINDER_KEY = "trading_calendar:reminded_on"
_CALENDAR_REMINDER_EVERY_DAYS = 7


async def maybe_remind_calendar(now: datetime) -> bool:
    year = trading_calendar.missing_next_year(now.date())
    if year is None:
        return False
    last = await db.get_setting(_CALENDAR_REMINDER_KEY)
    try:
        last_day = date.fromisoformat(last) if last else None
    except ValueError:
        last_day = None
    if last_day is not None and (now.date() - last_day).days < _CALENDAR_REMINDER_EVERY_DAYS:
        return False
    sent = alerts.send(
        f"📅 Lịch giao dịch chưa có ngày nghỉ Tết/Giỗ Tổ năm {year}. Em đang tạm dùng bản dự phòng "
        "tính từ âm lịch (chỉ giao thừa - mùng 3 và Giỗ Tổ); khi Sở công bố lịch nghỉ, anh cập "
        "nhật _LUNAR_HOLIDAYS trong stock/calendar.py (hoặc env STOCK_EXTRA_HOLIDAYS) giúp em nhé."
    )
    if sent:
        await db.set_setting(_CALENDAR_REMINDER_KEY, now.date().isoformat())
    return sent


async def _loop(user_id: int) -> None:
    while True:
        try:
            await maybe_remind_calendar(datetime.now(_VN_TZ))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("portfolio_alerts: lỗi nhắc cập nhật lịch giao dịch.", exc_info=True)
        try:
            await run_once(user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("portfolio_alerts: lỗi vòng quét.", exc_info=True)
        await asyncio.sleep(_POLL_SEC)


def start(user_id: int) -> None:
    global _task
    if not config.ENABLE_PORTFOLIO_ALERTS or not user_id:
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(user_id))


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    await asyncio.gather(_task, return_exceptions=True)
    _task = None
