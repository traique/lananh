"""Background scheduler for reminders and the daily portfolio digest."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from core import config
from core import database as db
from core import idempotency

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_notify_callback: Optional[Callable[[int, str], Awaitable[None]]] = None
_reminder_task: Optional[asyncio.Task] = None
_digest_task: Optional[asyncio.Task] = None


def set_notify_callback(fn: Callable[[int, str], Awaitable[None]]) -> None:
    global _notify_callback
    _notify_callback = fn


async def _notify(user_id: int, text: str) -> bool:
    if _notify_callback is None:
        logger.warning("scheduler: chưa đăng ký notify_callback, bỏ qua gửi tin.")
        return False
    try:
        await _notify_callback(user_id, text)
        return True
    except Exception:
        logger.warning("scheduler: gửi tin chủ động lỗi.", exc_info=True)
        return False


async def _process_due_reminders(due: list[tuple[int, int, str]]) -> None:
    for reminder_id, user_id, message in due:
        if not await _notify(user_id, f"⏰ Nhắc việc: {message}"):
            await idempotency.release_reminder_claim(reminder_id)
            continue
        try:
            await db.mark_reminder_sent(reminder_id)
        except Exception:
            logger.warning(
                "scheduler: gửi reminder id=%s thành công nhưng mark sent lỗi; "
                "lease sẽ ngăn gửi trùng tức thời.",
                reminder_id,
                exc_info=True,
            )


async def _reminder_loop() -> None:
    while True:
        await asyncio.sleep(config.REMINDER_CHECK_INTERVAL_SEC)
        try:
            due = await idempotency.claim_due_reminders()
        except Exception:
            logger.warning("scheduler: lỗi khi claim reminders đến hạn.", exc_info=True)
            continue
        await _process_due_reminders(due)


def _seconds_until_next_hour(hour: int) -> float:
    now = datetime.now(_VN_TZ)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _build_portfolio_digest(user_id: int) -> Optional[str]:
    from stock import analysis as stock_analysis
    from stock import portfolio

    holdings = await portfolio.list_holdings(user_id)
    if holdings:
        return await portfolio.build_report(user_id, digest=True)

    facts = await db.get_facts(user_id)
    portfolio_text = " ".join(
        value for key, value in facts if stock_analysis.is_portfolio_fact(key)
    )
    if not portfolio_text.strip():
        return None
    symbols = await stock_analysis.find_valid_symbols(portfolio_text)
    if not symbols:
        return None

    lines = ["📊 *Digest danh mục cũ trong trí nhớ:*"]
    for symbol in symbols:
        try:
            lines.append(await stock_analysis.quick_quote(symbol))
        except Exception:
            logger.warning("scheduler: lỗi lấy giá %s cho digest.", symbol, exc_info=True)
            lines.append(f"{symbol}: ❌ lỗi lấy giá lúc này")
    lines.append("\nDùng /themcp để chuyển sang danh mục có số lượng và giá vốn.")
    return "\n".join(lines)


async def _daily_digest_loop(user_id: int) -> None:
    while True:
        await asyncio.sleep(_seconds_until_next_hour(config.DAILY_DIGEST_HOUR_VN))
        try:
            digest = await _build_portfolio_digest(user_id)
            if digest:
                await _notify(user_id, digest)
        except Exception:
            logger.warning("scheduler: lỗi khi tạo/gửi daily digest.", exc_info=True)


def start(allowed_user_id: int) -> None:
    global _reminder_task, _digest_task
    if _reminder_task is None or _reminder_task.done():
        _reminder_task = asyncio.create_task(_reminder_loop())
    if config.ENABLE_DAILY_DIGEST and allowed_user_id:
        if _digest_task is None or _digest_task.done():
            _digest_task = asyncio.create_task(_daily_digest_loop(allowed_user_id))


async def stop() -> None:
    """Cancel scheduler loops and wait for them during graceful shutdown."""
    global _reminder_task, _digest_task
    tasks = [task for task in (_reminder_task, _digest_task) if task is not None]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _reminder_task = None
    _digest_task = None
