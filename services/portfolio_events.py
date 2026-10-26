"""Job nền hằng ngày cho danh mục (mọi người dùng, kể cả thành viên Zalo).

Chạy 1 lần/ngày giao dịch từ PORTFOLIO_EVENTS_HOUR_VN giờ VN (khung bù 3 giờ,
guard ngày trong settings):

1. Lưu hẳn quy đổi sự kiện quyền (thưởng/cổ tức cổ phiếu/chia tách) ĐÃ được giá
   xác nhận vào vị thế (stock.portfolio.apply_confirmed_corporate_actions) và báo
   1 dòng cho người sở hữu. Cần lưu vì chuỗi giá 90 phiên chỉ xác nhận được sự
   kiện trong ~4 tháng; quá hạn đó phần quy đổi tạm tính sẽ mất và "lỗ giả" quay lại.

Có trần số vị thế mỗi lượt và số lượt tra cứu song song (RAM/requests VCI). Tắt
cả job bằng PORTFOLIO_EVENTS_ENABLED=0.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core import database as db
from core.envutil import env_int, env_on
from services import portfolio_push
from stock import calendar as trading_calendar
from stock import portfolio

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_LAST_RUN_KEY = "portfolio_events:last_run_date"
CATCHUP_HOURS = 3
_POLL_SEC = 15 * 60
# Số mã tra cứu song song (mỗi mã: 1 chuỗi DNSE 90 phiên + lịch quyền VCI có cache 24h).
_CONCURRENCY = 3
_task: asyncio.Task | None = None


def enabled() -> bool:
    return env_on("PORTFOLIO_EVENTS_ENABLED")


def run_hour() -> int:
    return env_int("PORTFOLIO_EVENTS_HOUR_VN", 8, 0, 20)


def max_holdings() -> int:
    return env_int("PORTFOLIO_EVENTS_MAX_HOLDINGS", 60, 1, 500)


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
