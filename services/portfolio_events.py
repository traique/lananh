"""Job nền hằng ngày cho danh mục (mọi người dùng, kể cả thành viên Zalo).

Chạy 1 lần/ngày giao dịch từ PORTFOLIO_EVENTS_HOUR_VN giờ VN (khung bù 3 giờ,
guard ngày trong settings):

1. Lưu hẳn quy đổi sự kiện quyền (thưởng/cổ tức cổ phiếu/chia tách) ĐÃ được giá
   xác nhận vào vị thế (stock.portfolio.apply_confirmed_corporate_actions) và báo
   1 dòng cho người sở hữu. Cần lưu vì chuỗi giá 90 phiên chỉ xác nhận được sự
   kiện trong ~4 tháng; quá hạn đó phần quy đổi tạm tính sẽ mất và "lỗ giả" quay lại.
2. Báo trước lịch sự kiện quyền của mã đang giữ (GDKHQ cổ tức tiền, phát hành/
   quyền mua, ĐHCĐ) trong NOTICE_SESSIONS phiên tới - mỗi sự kiện 1 lần (nhật ký
   stock_alert_log), lịch lấy riêng qua stock.vci_direct.fetch_upcoming_events
   (cache 12 giờ). Tắt riêng bằng PORTFOLIO_EVENT_NOTICES=0.
3. Dọn nhật ký thông báo cũ hơn _LOG_KEEP_DAYS ngày.

Có trần số vị thế, số mã tra lịch mỗi lượt và số lượt tra cứu song song
(RAM/requests VCI). Tắt cả job bằng PORTFOLIO_EVENTS_ENABLED=0.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core import database as db
from core.envutil import env_int, env_on
from services import portfolio_push
from stock import calendar as trading_calendar
from stock import corporate_actions, portfolio, providers, vci_direct

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_LAST_RUN_KEY = "portfolio_events:last_run_date"
CATCHUP_HOURS = 3
_POLL_SEC = 15 * 60
# Số mã tra cứu song song (mỗi mã: 1 chuỗi DNSE 90 phiên + lịch quyền VCI có cache 24h).
_CONCURRENCY = 3
# Báo trước sự kiện có ngày rơi trong chừng này phiên tới (0 = hôm nay).
NOTICE_SESSIONS = 3
_EVENTS_FETCH_TIMEOUT_SEC = 15
_LOG_KEEP_DAYS = 120
_task: asyncio.Task | None = None


def enabled() -> bool:
    return env_on("PORTFOLIO_EVENTS_ENABLED")


def run_hour() -> int:
    return env_int("PORTFOLIO_EVENTS_HOUR_VN", 8, 0, 20)


def max_holdings() -> int:
    return env_int("PORTFOLIO_EVENTS_MAX_HOLDINGS", 60, 1, 500)


def notices_enabled() -> bool:
    return env_on("PORTFOLIO_EVENT_NOTICES")


def max_event_symbols() -> int:
    return env_int("PORTFOLIO_EVENTS_MAX_SYMBOLS", 40, 1, 200)


def is_due(now: datetime, last_run_date: str | None) -> bool:
    if last_run_date == now.date().isoformat():
        return False
    if not trading_calendar.is_trading_day(now.date()):
        return False
    start = now.replace(hour=run_hour(), minute=0, second=0, microsecond=0)
    return start <= now < start + timedelta(hours=CATCHUP_HOURS)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}".replace(",", ".")


def corporate_notice(before: portfolio.Holding, after: portfolio.Holding, note: str) -> str:
    text = (
        f"🔁 Em đã quy đổi vị thế {after.symbol} theo {note}: {_fmt(before.quantity)} → "
        f"{_fmt(after.quantity)} cp, giá vốn {_fmt(before.average_price)} → "
        f"{_fmt(after.average_price)}đ"
    )
    if before.stop_price is not None or before.target_price is not None:
        text += (
            f", SL {_fmt(before.stop_price)} → {_fmt(after.stop_price)}, "
            f"TP {_fmt(before.target_price)} → {_fmt(after.target_price)}"
        )
    return (
        text + ". Số cp lẻ bị làm tròn xuống, tổng giá vốn giữ nguyên. Nếu số liệu CTCK khác, "
        "anh gõ /capnhatcp để ghi đè nhé."
    )


async def persist_corporate_actions(holdings: list[portfolio.Holding], today: str) -> int:
    """Lưu quy đổi quyền đã xác nhận cho các vị thế; trả số vị thế đã quy đổi."""
    slots = asyncio.Semaphore(_CONCURRENCY)

    async def one(holding: portfolio.Holding) -> int:
        if not holding.cost_basis_as_of or holding.cost_basis_as_of >= today:
            return 0
        async with slots:
            try:
                result = await portfolio.apply_confirmed_corporate_actions(holding)
            except Exception:
                logger.warning(
                    "portfolio_events: lỗi quy đổi quyền %s.", holding.symbol, exc_info=True
                )
                return 0
        if result is None:
            return 0
        before, after, rebase = result
        await portfolio_push.deliver(
            holding.user_id,
            corporate_notice(before, after, rebase.note),
            kind="corp",
            ref_key=f"{holding.symbol}:{rebase.as_of}",
            today=today,
        )
        return 1

    return sum(await asyncio.gather(*(one(h) for h in holdings)))


# ─── Lịch sự kiện quyền sắp tới ──────────────────────────────────────────────


def _vn_day(raw: str) -> str:
    try:
        return f"{date.fromisoformat(raw):%d/%m}"
    except ValueError:
        return raw


def due_events(events: list[vci_direct.UpcomingEvent], today: date) -> list:
    """Sự kiện có ngày quan trọng rơi trong [hôm nay, NOTICE_SESSIONS phiên tới]."""
    out = []
    for event in events:
        try:
            day = date.fromisoformat(event.key_date)
        except ValueError:
            continue
        if day < today:
            continue
        if trading_calendar.trading_days_between(today, day) <= NOTICE_SESSIONS:
            out.append(event)
    return out


def event_ref(event: vci_direct.UpcomingEvent) -> str:
    return f"{event.symbol}:{event.code or 'EVT'}:{event.key_date}"


def describe_event(event: vci_direct.UpcomingEvent, today: date) -> str:
    """1 dòng mô tả sự kiện (số liệu lấy thẳng từ lịch quyền, không qua AI)."""
    title = event.title.lower()
    adjusts_price = False
    if event.code == "DIV":
        dps = corporate_actions.normalize_dps(event.value)
        what = f"cổ tức tiền {_fmt(dps)}đ/cp" if dps else "cổ tức tiền"
        adjusts_price = True
    elif event.code == "ISS":
        if any(k in title for k in ("quyền mua", "rights")):
            what = "quyền mua cổ phiếu phát hành thêm (muốn thực hiện quyền thì đăng ký, nộp tiền)"
        else:
            what = "phát hành cổ phiếu (thưởng/cổ tức bằng cổ phiếu)"
        adjusts_price = True
    elif event.code in ("AGME", "AGMR"):
        what = "quyền dự họp ĐHCĐ thường niên"
    elif event.code == "EGME":
        what = "quyền dự họp ĐHCĐ bất thường"
    else:
        what = "sự kiện quyền"
    key = event.key_date
    sessions = trading_calendar.trading_days_between(today, date.fromisoformat(key))
    when = "HÔM NAY" if key == today.isoformat() else f"ngày {_vn_day(key)} ({sessions} phiên nữa)"
    if event.ex_date == key:
        label = "GDKHQ"
    elif event.record_date == key:
        label = "chốt danh sách"
    else:
        label = "ngày sự kiện"
    line = f"• {event.symbol}: {label} {what} {when}"
    if event.record_date and event.record_date != key:
        line += f", chốt danh sách {_vn_day(event.record_date)}"
    if adjusts_price and event.ex_date == key:
        line += " - giá tham chiếu được điều chỉnh, không phải lỗ thật"
    if event.title:
        line += f"\n  ({event.title[:120]})"
    return line


async def _fetch_events(symbol: str) -> list[vci_direct.UpcomingEvent]:
    try:
        async with providers.get_vnstock_semaphore():
            return await asyncio.wait_for(
                asyncio.to_thread(vci_direct.fetch_upcoming_events, symbol),
                timeout=_EVENTS_FETCH_TIMEOUT_SEC,
            )
    except Exception:
        logger.warning("portfolio_events: không lấy được lịch quyền %s.", symbol, exc_info=True)
        return []


async def send_event_notices(holdings: list[portfolio.Holding], today: date) -> int:
    """Gom sự kiện sắp tới của các mã mỗi người đang giữ thành 1 tin/người; trả
    số tin đã gửi."""
    symbols = list(dict.fromkeys(h.symbol for h in holdings))[: max_event_symbols()]
    slots = asyncio.Semaphore(_CONCURRENCY)

    async def fetch(symbol: str):
        async with slots:
            return symbol, due_events(await _fetch_events(symbol), today)

    events_by_symbol = dict(await asyncio.gather(*(fetch(s) for s in symbols)))
    by_user: dict[int, list[portfolio.Holding]] = {}
    for holding in holdings:
        by_user.setdefault(holding.user_id, []).append(holding)
    today_iso = today.isoformat()
    sent = 0
    for user_id, user_holdings in by_user.items():
        lines: list[str] = []
        refs: list[str] = []
        for holding in user_holdings:
            for event in events_by_symbol.get(holding.symbol, []):
                ref = event_ref(event)
                if ref in refs or await portfolio_push.already_sent(user_id, "event", ref):
                    continue
                lines.append(describe_event(event, today))
                refs.append(ref)
        if not lines:
            continue
        text = (
            "📅 *Lịch sự kiện quyền sắp tới (mã anh đang giữ)*\n"
            + "\n".join(lines)
            + "\n\nNguồn lịch quyền: VCI (không chính thức) - anh đối chiếu thông báo của "
            "doanh nghiệp/CTCK nhé."
        )
        if await portfolio_push.deliver(
            user_id, text, kind="event", ref_key=refs[0], today=today_iso
        ):
            sent += 1
            for ref in refs[1:]:
                await portfolio_push.mark_sent(user_id, "event", ref, today_iso)
    return sent


async def run_once(*, force: bool = False) -> bool:
    now = datetime.now(_VN_TZ)
    last = await db.get_setting(_LAST_RUN_KEY)
    if not force and not is_due(now, last):
        return False
    today = now.date().isoformat()
    holdings = await portfolio.list_all_holdings(max_holdings())
    rebased = await persist_corporate_actions(holdings, today)
    if rebased:
        logger.info("portfolio_events: đã quy đổi quyền %d vị thế.", rebased)
    if notices_enabled() and holdings:
        await send_event_notices(holdings, now.date())
    try:
        await db.portfolio_prune_alert_log(_LOG_KEEP_DAYS)
    except Exception:
        logger.warning("portfolio_events: không dọn được nhật ký thông báo.", exc_info=True)
    await db.set_setting(_LAST_RUN_KEY, today)
    return True


async def _loop() -> None:
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("portfolio_events: lỗi vòng chạy.", exc_info=True)
        await asyncio.sleep(_POLL_SEC)


def start() -> None:
    global _task
    if not enabled():
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    await asyncio.gather(_task, return_exceptions=True)
    _task = None
