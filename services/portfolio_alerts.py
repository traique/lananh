"""Cảnh báo danh mục theo sự kiện: quét holdings sau giờ đóng cửa, báo ngay khi
giá chạm stop/target, lỗ sâu hoặc có breakout Donchian - thay vì chờ digest sáng
hôm sau.

Chạy 1 lần/ngày giao dịch (sau 15h05 giờ VN, khung bù 3 giờ). Guard ngày lưu
trong settings (sống qua restart/spin-down) và CHỈ được ghi sau khi gửi thành
công, hoặc khi đã có dữ liệu mới mà không có gì để báo - giống digest và bản tin
sáng: Telegram lỗi mạng thì vòng 10 phút sau thử lại, không mất cảnh báo. Ghi
guard lỗi sau khi đã gửi thì cảnh báo có thể lặp lại (ít nhất 1 lần, chấp nhận).

Độ tin cậy dữ liệu (không báo trên dữ liệu cũ mà không nói):
- Không có phiên hôm nay (VNINDEX mới nhất là phiên trước và không mã nào có tick
  hôm nay - ngày lễ chưa có trong bảng, ngày hoán đổi...): không báo; tới vòng cuối
  khung thì đánh dấu ngày, im lặng. Không phụ thuộc bảng ngày lễ.
- 1 mã "thiếu dữ liệu" khi không có quote, hoặc không có tick hôm nay mà nến mới
  nhất không phải hôm nay (DNSE chưa chốt nến). Mã thiếu không được xét sự kiện.
  Từ một nửa số mã trở lên thiếu -> chưa đánh dấu, vòng sau thử lại; tới vòng cuối
  khung thì gửi phần đã có + 1 dòng báo mất dữ liệu rồi mới đánh dấu.

Chống lặp (trạng thái JSON 1 khoá settings/user, cỡ theo số mã đang giữ):
- stop/target vẫn báo MỖI ngày điều kiện còn đúng (không tắt tiếng cảnh báo rủi
  ro), nhưng từ ngày thứ 2 rút gọn thành "ngày thứ N dưới stop".
- dòng lỗ >= 8% (vị thế anh cố ý giữ) chỉ lặp khi lỗ sâu thêm _LOSS_STEP_PCT điểm %
  hoặc sau _LOSS_REPEAT_DAYS ngày.
- breakout Donchian đo trên 20 phiên trước nên mỗi lần là thông tin mới - giữ.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core import alerts, config, database as db
from stock import calendar as trading_calendar
from stock import corporate_actions
from stock import features as feat
from stock import portfolio, providers

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_LAST_RUN_KEY = "portfolio_alerts:last_run_date"
_STATE_KEY = "portfolio_alerts:state:{user_id}"
# Sau giờ đóng cửa + 5 phút để nến ngày đã chốt trên DNSE.
ALERT_HOUR_VN, ALERT_MINUTE_VN = 15, 5
CATCHUP_HOURS = 3
_POLL_SEC = 10 * 60
LOSS_ALERT_PCT = -8.0
_LOSS_STEP_PCT = 3.0
_LOSS_REPEAT_DAYS = 7
# Job nền: chờ lịch quyền lâu hơn lệnh tương tác (cache VCI nguội lúc 15h05).
_CORPORATE_TIMEOUT_SEC = 45.0
_task: asyncio.Task | None = None
_notify = None


def set_notify_callback(fn) -> None:
    global _notify
    _notify = fn


def _window_start(now: datetime) -> datetime:
    return now.replace(hour=ALERT_HOUR_VN, minute=ALERT_MINUTE_VN, second=0, microsecond=0)


def is_due(now: datetime, last_run_date: str | None) -> bool:
    if last_run_date == now.date().isoformat():
        return False
    if not trading_calendar.is_trading_day(now.date()):
        return False
    start = _window_start(now)
    return start <= now < start + timedelta(hours=CATCHUP_HOURS)


def is_final_poll(now: datetime) -> bool:
    """Vòng quét cuối cùng trong khung bù (vòng kế tiếp đã quá khung)."""
    end = _window_start(now) + timedelta(hours=CATCHUP_HOURS)
    return now >= end - timedelta(seconds=_POLL_SEC)


def _fmt(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")


@dataclass(frozen=True)
class Signal:
    """1 sự kiện của 1 mã. kind: stop | target | breakout_up | breakout_down | loss."""

    kind: str
    text: str
    level: float | None = None
    price: float | None = None
    pnl_pct: float | None = None


def detect_signals(holding: portfolio.Holding, quote, series) -> list[Signal]:
    """Sự kiện đáng báo của 1 mã (rỗng = không có gì).
    ``quote``: providers.Quote | None; ``series``: OhlcvSeries | None (90 bar)."""
    signals: list[Signal] = []
    price = (
        quote.price
        if quote is not None
        else (series.closes[-1] if series and series.closes else None)
    )
    if price is None or price <= 0:
        return signals
    if holding.stop_price is not None and price <= holding.stop_price:
        signals.append(
            Signal(
                "stop",
                f"🚨 chạm/thủng stop {_fmt(holding.stop_price)} (giá {_fmt(price)})",
                holding.stop_price,
                price,
            )
        )
    if holding.target_price is not None and price >= holding.target_price:
        signals.append(
            Signal(
                "target",
                f"🎯 đạt/vượt target {_fmt(holding.target_price)} (giá {_fmt(price)})",
                holding.target_price,
                price,
            )
        )
    if series is not None and len(series.closes) >= 21:
        donchian = feat.calc_donchian_breakout(series.highs, series.lows, series.closes)
        if donchian.state == "breakout_up":
            signals.append(
                Signal(
                    "breakout_up",
                    f"📈 breakout lên trên kênh Donchian 20 phiên ({_fmt(donchian.upper)})",
                )
            )
        elif donchian.state == "breakout_down":
            signals.append(
                Signal(
                    "breakout_down",
                    f"📉 breakdown xuống dưới kênh Donchian 20 phiên ({_fmt(donchian.lower)})",
                )
            )
    pnl_pct = (price / holding.average_price - 1) * 100 if holding.average_price > 0 else 0.0
    if pnl_pct <= LOSS_ALERT_PCT:
        signals.append(
            Signal(
                "loss",
                f"⚠️ lỗ {pnl_pct:.1f}% so với giá vốn".replace(".", ","),
                price=price,
                pnl_pct=pnl_pct,
            )
        )
    return signals


def detect_events(holding: portfolio.Holding, quote, series) -> list[str]:
    """Danh sách dòng cảnh báo cho 1 mã (rỗng = không có gì đáng báo)."""
    return [signal.text for signal in detect_signals(holding, quote, series)]


def _parse_day(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def _streak(prev: dict | None, level: float, today: date) -> int:
    """Số phiên liên tiếp điều kiện stop/target còn đúng (tính cả hôm nay)."""
    if not isinstance(prev, dict) or prev.get("level") != level:
        return 1
    last = _parse_day(prev.get("last"))
    days = int(prev.get("days") or 0)
    if last is None or days <= 0:
        return 1
    if last >= today:
        return days  # quét lại trong cùng ngày
    return days + max(1, trading_calendar.trading_days_between(last, today))


def holding_alert_ref(symbol: str, level: float) -> str:
    """Khoá nhật ký stock_alert_log cho lần chạm stop/target của 1 mã (dùng chung
    với vòng quét trong phiên services/price_alerts.py)."""
    return f"{symbol}:{level:.0f}"


def apply_repeat_rules(
    signals: list[Signal],
    prev_state: dict | None,
    today: date,
    *,
    reported_intraday: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[str], dict]:
    """(dòng cần báo, trạng thái mới) cho 1 mã. Hàm thuần - xem docstring module.
    ``reported_intraday``: loại (stop/target) đã báo trong phiên hôm nay - ngày
    đầu chạm thì không báo lại lúc cuối phiên."""
    prev_state = prev_state if isinstance(prev_state, dict) else {}
    lines: list[str] = []
    state: dict = {}
    for signal in signals:
        if signal.kind in ("stop", "target") and signal.level is not None:
            days = _streak(prev_state.get(signal.kind), signal.level, today)
            state[signal.kind] = {"level": signal.level, "days": days, "last": today.isoformat()}
            if days <= 1 and signal.kind in reported_intraday:
                continue
            if days <= 1:
                lines.append(signal.text)
            elif signal.kind == "stop":
                lines.append(
                    f"🚨 ngày thứ {days} dưới stop {_fmt(signal.level)} (giá {_fmt(signal.price or 0)})"
                )
            else:
                lines.append(
                    f"🎯 ngày thứ {days} trên target {_fmt(signal.level)} "
                    f"(giá {_fmt(signal.price or 0)})"
                )
        elif signal.kind == "loss" and signal.pnl_pct is not None:
            prev = prev_state.get("loss")
            prev_pct = prev.get("pct") if isinstance(prev, dict) else None
            prev_day = _parse_day(prev.get("date")) if isinstance(prev, dict) else None
            show = (
                prev_pct is None
                or prev_day is None
                or signal.pnl_pct <= float(prev_pct) - _LOSS_STEP_PCT
                or (today - prev_day).days >= _LOSS_REPEAT_DAYS
            )
            if show:
                lines.append(signal.text)
                state["loss"] = {"pct": round(signal.pnl_pct, 2), "date": today.isoformat()}
            else:
                state["loss"] = prev
        else:
            lines.append(signal.text)
    return lines, state


@dataclass
class HoldingScan:
    holding: portfolio.Holding  # đã quy đổi theo sự kiện quyền đã được giá xác nhận
    quote: object | None = None
    series: object | None = None  # chuỗi đã điều chỉnh quyền (cho Donchian)
    fresh: bool = False
    view: portfolio.CorporateView | None = None


@dataclass
class PortfolioScan:
    items: list[HoldingScan] = field(default_factory=list)
    # True: có phiên hôm nay; False: chắc chắn không có phiên (nghỉ lễ); None:
    # không rõ (nguồn lỗi) - xử lý như thiếu dữ liệu.
    session_today: bool | None = None

    @property
    def missing(self) -> list[str]:
        return [item.holding.symbol for item in self.items if not item.fresh]


def _is_fresh(quote, today_iso: str) -> bool:
    if quote is None:
        return False
    return bool(getattr(quote, "is_realtime", False)) or getattr(quote, "date", "") == today_iso


async def _index_last_date() -> str | None:
    try:
        series = await providers.fetch_ohlcv("VNINDEX", days=5)
    except Exception:
        logger.warning("portfolio_alerts: không lấy được VNINDEX.", exc_info=True)
        return None
    dates = getattr(series, "dates", None) or []
    return str(dates[-1])[:10] if dates else None


async def _adjusted_series(symbol: str, series):
    """Chuỗi 90 phiên đã điều chỉnh sự kiện quyền được gap xác nhận - breakout
    Donchian không bị gap GDKHQ làm giả. Lỗi/thiếu dữ liệu -> chuỗi thô."""
    if series is None or len(series.closes) < corporate_actions.MIN_BARS_FOR_EVENT_LOOKUP:
        return series
    try:
        outcome = await corporate_actions.adjust_with_corporate_actions(
            symbol, series.closes, series.highs, series.lows, series.dates, series.volumes
        )
    except Exception:
        logger.warning("portfolio_alerts: lỗi điều chỉnh quyền %s.", symbol, exc_info=True)
        return series
    if not outcome.events_applied:
        return series
    return providers.OhlcvSeries(
        symbol=series.symbol,
        closes=outcome.closes,
        highs=outcome.highs,
        lows=outcome.lows,
        volumes=outcome.volumes or series.volumes,
        dates=series.dates,
        source=series.source,
        is_adjusted=True,
    )


async def scan_portfolio(user_id: int, today: date) -> PortfolioScan:
    holdings = await portfolio.list_holdings(user_id)
    if not holdings:
        return PortfolioScan()
    symbols = [h.symbol for h in holdings]
    quotes, index_date = await asyncio.gather(
        asyncio.gather(*(providers.fetch_quote(s) for s in symbols), return_exceptions=True),
        _index_last_date(),
    )
    quotes = [None if isinstance(q, BaseException) else q for q in quotes]
    today_iso = today.isoformat()
    if any(getattr(q, "is_realtime", False) for q in quotes) or index_date == today_iso:
        session: bool | None = True
    elif index_date is not None:
        session = False
    else:
        session = None
    if session is False:
        # Không có phiên hôm nay: không tốn thêm request chuỗi giá/lịch quyền.
        return PortfolioScan(
            [HoldingScan(h, q) for h, q in zip(holdings, quotes, strict=True)], False
        )
    series_list = await asyncio.gather(
        *(providers.fetch_ohlcv(s, days=90) for s in symbols), return_exceptions=True
    )
    clean_series = [
        None if isinstance(series, BaseException) or not getattr(series, "closes", None) else series
        for series in series_list
    ]
    # Sự kiện quyền: so giá với vị thế ĐÃ quy đổi (không báo "thủng stop" giả ngày
    # GDKHQ cổ tức cổ phiếu); sự kiện chưa được giá xác nhận chỉ được ghi chú.
    views, adjusted = await asyncio.gather(
        portfolio.corporate_views(holdings, clean_series, limit_sec=_CORPORATE_TIMEOUT_SEC),
        asyncio.gather(
            *(_adjusted_series(s, ser) for s, ser in zip(symbols, clean_series, strict=True))
        ),
    )
    items = [
        HoldingScan(view.effective, quote, series, _is_fresh(quote, today_iso), view)
        for view, quote, series in zip(views, quotes, adjusted, strict=True)
    ]
    return PortfolioScan(items, session)


def compose_alert(
    scan: PortfolioScan,
    state: dict,
    today: date,
    *,
    outage: bool,
    reported_intraday: dict[str, set[str]] | None = None,
) -> tuple[str | None, dict]:
    """(nội dung cảnh báo hoặc None, trạng thái chống lặp mới).
    ``reported_intraday``: {mã: {"stop", "target"}} đã báo trong phiên hôm nay."""
    reported_intraday = reported_intraday or {}
    lines: list[str] = []
    new_state: dict = {}
    for item in scan.items:
        symbol = item.holding.symbol
        if not item.fresh:
            # Không xét mã thiếu dữ liệu; giữ nguyên trạng thái cũ của mã đó.
            if isinstance(state.get(symbol), dict):
                new_state[symbol] = state[symbol]
            continue
        signals = detect_signals(item.holding, item.quote, item.series)
        shown, symbol_state = apply_repeat_rules(
            signals,
            state.get(symbol),
            today,
            reported_intraday=reported_intraday.get(symbol, set()),
        )
        if symbol_state:
            new_state[symbol] = symbol_state
        if shown:
            lines.append(f"• *{symbol}*: " + "; ".join(shown))
            # Ghi chú sự kiện quyền (đã quy đổi / chưa xác nhận / cổ tức tiền)
            # ngay dưới mã - không bao giờ tắt dòng stop vì sự kiện chưa xác nhận.
            if item.view is not None:
                lines.extend(f"  {note}" for note in item.view.notes())
    missing = scan.missing
    notes: list[str] = []
    if outage:
        notes.append(
            f"⚠️ Hôm nay em không lấy được dữ liệu giá cuối phiên của {', '.join(missing)} "
            f"({len(missing)}/{len(scan.items)} mã), mai em kiểm tra lại nha anh."
        )
    elif missing and lines:
        notes.append(f"ℹ️ Chưa có dữ liệu phiên hôm nay của {', '.join(missing)} nên em chưa xét.")
    if not lines and not notes:
        return None, new_state
    text = "\n".join(["🔔 *Cảnh báo danh mục cuối phiên*", *lines, *notes])
    if lines:
        text += (
            "\n\nSố liệu tham khảo từ dữ liệu đóng cửa, không phải lệnh. "
            "Dùng /danhmuc để xem chi tiết."
        )
    return text, new_state


async def _reported_intraday(user_id: int, scan: PortfolioScan, today: date) -> dict[str, set[str]]:
    """Mã/loại (stop, target) đã được vòng quét trong phiên báo hôm nay."""
    reported: dict[str, set[str]] = {}
    for item in scan.items:
        for kind, level in (
            ("stop", item.holding.stop_price),
            ("target", item.holding.target_price),
        ):
            if level is None:
                continue
            try:
                logged = await db.portfolio_alert_logged(
                    user_id, kind, holding_alert_ref(item.holding.symbol, level), today.isoformat()
                )
            except Exception:
                logger.warning(
                    "portfolio_alerts: không đọc được nhật ký trong phiên.", exc_info=True
                )
                return reported
            if logged:
                reported.setdefault(item.holding.symbol, set()).add(kind)
    return reported


async def load_state(user_id: int) -> dict:
    try:
        raw = await db.get_setting(_STATE_KEY.format(user_id=user_id))
        data = json.loads(raw) if raw else {}
    except Exception:
        logger.warning("portfolio_alerts: trạng thái chống lặp hỏng, bỏ qua.", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


async def _save_state(user_id: int, state: dict) -> None:
    await db.set_setting(
        _STATE_KEY.format(user_id=user_id), json.dumps(state, ensure_ascii=False, sort_keys=True)
    )


async def run_once(user_id: int, *, force: bool = False) -> bool:
    """Trả True nếu đã gửi cảnh báo. Đánh dấu ngày khi đã gửi, khi có dữ liệu mới
    mà không có gì để báo, hoặc ở vòng cuối khung (xem docstring module)."""
    now = datetime.now(_VN_TZ)
    today = now.date()
    last = await db.get_setting(_LAST_RUN_KEY)
    if not force and not is_due(now, last):
        return False
    final_poll = force or is_final_poll(now)
    scan = await scan_portfolio(user_id, today)
    if not scan.items:
        await db.set_setting(_LAST_RUN_KEY, today.isoformat())
        return False
    if scan.session_today is False:
        # VNINDEX mới nhất là phiên trước, không mã nào có tick hôm nay: nghỉ lễ
        # (kể cả ngày chưa có trong bảng) hoặc DNSE chậm - thử lại tới hết khung.
        if final_poll:
            logger.info("portfolio_alerts: không có phiên hôm nay %s, bỏ qua.", today)
            await db.set_setting(_LAST_RUN_KEY, today.isoformat())
        return False
    outage = len(scan.missing) * 2 >= len(scan.items)
    if outage and not final_poll:
        logger.info(
            "portfolio_alerts: thiếu dữ liệu %d/%d mã, thử lại vòng sau.",
            len(scan.missing),
            len(scan.items),
        )
        return False
    state = await load_state(user_id)
    reported = await _reported_intraday(user_id, scan, today)
    text, new_state = compose_alert(scan, state, today, outage=outage, reported_intraday=reported)
    if not text:
        await _save_state(user_id, new_state)
        await db.set_setting(_LAST_RUN_KEY, today.isoformat())
        return False
    if _notify is None:
        logger.warning("portfolio_alerts: chưa đăng ký notify, chưa gửi được cảnh báo.")
        return False
    try:
        await _notify(user_id, text)
    except Exception:
        logger.warning(
            "portfolio_alerts: không gửi được cảnh báo, vòng sau thử lại.", exc_info=True
        )
        return False
    # Chỉ ghi sau khi gửi thành công (lỗi gửi -> vòng sau thử lại cả nội dung).
    await _save_state(user_id, new_state)
    await db.set_setting(_LAST_RUN_KEY, today.isoformat())
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
