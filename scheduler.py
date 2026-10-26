"""Background scheduler for reminders and the daily portfolio digest."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from core import config
from core import database as db
from core import idempotency, trace

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_notify_callback: Optional[Callable[[int, str], Awaitable[None]]] = None
# Tin nhắc Telegram kèm nút hoãn [+10p][+1h][Sáng mai] (bot_app dựng bàn phím);
# chưa đặt thì gửi chữ qua _notify_callback như cũ.
_reminder_notify_callback: Optional[Callable[[int, str, list], Awaitable[None]]] = None
_reminder_task: Optional[asyncio.Task] = None
_digest_task: Optional[asyncio.Task] = None


def set_notify_callback(fn: Callable[[int, str], Awaitable[None]]) -> None:
    """Đặt callback gửi chữ; xoá luôn bản kèm nút (đặt lại bằng
    set_reminder_notify_callback SAU lệnh này nếu muốn có nút hoãn)."""
    global _notify_callback, _reminder_notify_callback
    _notify_callback = fn
    _reminder_notify_callback = None


def set_reminder_notify_callback(fn: Optional[Callable[[int, str, list], Awaitable[None]]]) -> None:
    global _reminder_notify_callback
    _reminder_notify_callback = fn


async def _notify_reminder(user_id: int, text: str, reminder_id: int) -> bool:
    if _reminder_notify_callback is None:
        return await _notify(user_id, text)
    from services import action_buttons

    try:
        await _reminder_notify_callback(
            user_id, text, action_buttons.reminder_snooze_rows(reminder_id)
        )
        return True
    except Exception:
        logger.warning("scheduler: gửi tin nhắc kèm nút lỗi.", exc_info=True)
        return False


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


async def _schedule_next(reminder: idempotency.DueReminder, now: datetime) -> None:
    """Lần của chuỗi lặp/ngày nhớ: chèn lần kế tiếp NGAY lúc claim (giao lỗi vẫn
    không làm đứt chuỗi). Lỗi chỉ log - lần claim sau (lease 5 phút) thử lại."""
    from services import reminder_service

    try:
        await reminder_service.schedule_next(int(reminder.series_id or 0), reminder.due_at, now)
    except Exception:
        logger.warning(
            "scheduler: không chèn được lần kế của chuỗi nhắc %s.",
            reminder.series_id,
            exc_info=True,
        )


async def _process_due_reminders(due: list[tuple]) -> None:
    from services import reminder_service

    for item in due:
        reminder = idempotency.DueReminder(*item)
        try:
            now = datetime.now(_VN_TZ)
            if reminder.series_id:
                await _schedule_next(reminder, now)
            # Nhãn "(nhắc trễ, lịch gốc HH:MM)" khi trễ quá 10 phút + gợi ý "hoãn/xong".
            text = reminder_service.delivery_text(
                reminder.message, reminder.kind, reminder.due_at, now
            )
            if reminder.channel == "zalo":
                from channels import zalo_repository, zalo_session

                account_id = reminder.account_id
                if not account_id:
                    account_id = await zalo_session.load_account_id()
                if not account_id:
                    raise ValueError("Legacy Zalo reminder needs a connected bot account")
                await zalo_repository.enqueue_reminder(
                    reminder.id, account_id, reminder.recipient_id, text
                )
                # Ack của gateway mới đánh dấu sent; fired_at (lối tắt "hoãn/xong") tính từ
                # lúc đưa vào outbox - gateway gửi ngay sau đó.
                await db.reminder_mark_fired(reminder.id)
                continue
            if reminder.channel == "zoom":
                from channels import zoom

                await zoom.send_message(
                    reminder.recipient_id,
                    text,
                    user_jid=reminder.user_jid or None,
                    account_id=reminder.account_id or None,
                )
            elif (
                reminder.channel != "telegram"
                or int(reminder.recipient_id or reminder.user_id) <= 0
            ):
                raise ValueError("Reminder has no valid channel recipient")
            elif not await _notify_reminder(
                int(reminder.recipient_id or reminder.user_id), text, reminder.id
            ):
                await idempotency.release_reminder_claim(reminder.id)
                continue
            await db.mark_reminder_sent(reminder.id)
        except Exception:
            logger.warning(
                "scheduler: giao/ghi trạng thái reminder id=%s lỗi; lease giữ lần thử tiếp theo.",
                reminder.id,
                exc_info=True,
            )


async def _reminder_loop() -> None:
    trace.detach()
    while True:
        await asyncio.sleep(config.REMINDER_CHECK_INTERVAL_SEC)
        with trace.job("reminders"):
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


_DIGEST_LAST_SENT_KEY = "digest:last_sent_date"
# Lỡ giờ (Render free ngủ, redeploy đúng giờ) thì gửi bù trong khung này.
DIGEST_CATCHUP_HOURS = 3
_DIGEST_POLL_SEC = 15 * 60


async def _brief_replaces_digest(user_id: int) -> bool:
    from services import morning_brief

    try:
        return await morning_brief.replaces_telegram_digest(user_id)
    except Exception:
        logger.warning("scheduler: không đọc được cấu hình bản tin sáng.", exc_info=True)
        return False


def digest_due(now: datetime, last_sent_date: str | None) -> bool:
    """Tới giờ (hoặc trong khung gửi bù) và hôm nay chưa gửi. Ngày không có phiên
    (cuối tuần, lễ) bỏ qua: giá vẫn là phiên trước nên digest chỉ lặp lại bản hôm
    trước - bật lại bằng DIGEST_ON_NON_TRADING_DAYS=true."""
    if last_sent_date == now.date().isoformat():
        return False
    if not config.DIGEST_ON_NON_TRADING_DAYS:
        from stock import calendar as trading_calendar

        if not trading_calendar.is_trading_day(now.date()):
            return False
    start = now.replace(hour=config.DAILY_DIGEST_HOUR_VN, minute=0, second=0, microsecond=0)
    return start <= now < start + timedelta(hours=DIGEST_CATCHUP_HOURS)


async def run_daily_digest_once(user_id: int, *, force: bool = False) -> bool:
    """Gửi digest nếu tới hạn và chưa gửi hôm nay (guard lưu DB nên sống qua
    restart). Trả True nếu đã gửi."""
    now = datetime.now(_VN_TZ)
    last_sent = await db.get_setting(_DIGEST_LAST_SENT_KEY)
    if not force and not digest_due(now, last_sent):
        return False
    if not force and await _brief_replaces_digest(user_id):
        # Bản tin sáng cá nhân (/bantin trên Telegram) đã gồm danh mục, chỉ gửi
        # vào ngày giao dịch - không gửi thêm digest riêng.
        await db.set_setting(_DIGEST_LAST_SENT_KEY, now.date().isoformat())
        return False
    digest = await _build_portfolio_digest(user_id)
    if not digest:
        # Không có danh mục: vẫn đánh dấu để không thử lại mỗi 15 phút cả buổi sáng.
        await db.set_setting(_DIGEST_LAST_SENT_KEY, now.date().isoformat())
        return False
    if await _notify(user_id, digest):
        await db.set_setting(_DIGEST_LAST_SENT_KEY, now.date().isoformat())
        return True
    return False


async def _daily_digest_loop(user_id: int) -> None:
    trace.detach()
    while True:
        try:
            with trace.job("digest"):
                await run_daily_digest_once(user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("scheduler: lỗi khi tạo/gửi daily digest.", exc_info=True)
        await asyncio.sleep(_DIGEST_POLL_SEC)


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
