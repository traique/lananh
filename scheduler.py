"""Background scheduler for reminders and the daily portfolio digest.

Nhắc việc là tin URGENT do người dùng tự đặt giờ: gửi thẳng (kể cả giờ yên lặng),
không qua services/notify_hub. Digest danh mục 8h là tin tổng hợp: qua hub.

Vòng nhắc việc không poll đều: mỗi lượt hỏi DB còn bao lâu tới khi có nhắc claim
được (``_seconds_until_next_due``, giờ tính bằng đồng hồ DB), ngủ đúng chừng đó -
tối đa REMINDER_CHECK_INTERVAL_SEC (lưới an toàn cho nhắc tạo từ nơi không đánh
thức, vd instance khác lúc deploy chồng) - và thức sớm khi services/reminder_service
đặt/hoãn nhắc (``wake()``). Nhắc vì thế gửi đúng giờ hẹn, còn nhịp kiểm tra tối đa
có thể đặt lớn (vd 300 s) để bớt lượt DB khi rảnh mà không làm nhắc trễ.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from core import config, idempotency, trace
from core import database as db

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_notify_callback: Optional[Callable[[int, str], Awaitable[None]]] = None
# Tin nhắc Telegram kèm nút hoãn [+10p][+1h][Sáng mai] (bot_app dựng bàn phím);
# chưa đặt thì gửi chữ qua _notify_callback như cũ.
_reminder_notify_callback: Optional[Callable[[int, str, list], Awaitable[None]]] = None
_reminder_task: Optional[asyncio.Task] = None
_digest_task: Optional[asyncio.Task] = None
# Đánh thức vòng nhắc (tạo trong start(), cùng event loop với task).
_wake_event: Optional[asyncio.Event] = None
# Khớp mặc định của idempotency.claim_due_reminders: claim đủ lô = còn tồn, làm tiếp
# ngay; nhắc đã claim chỉ claim lại được sau lease (Zalo chờ ack, giao lỗi giữ lease).
_CLAIM_LIMIT = 20
_CLAIM_LEASE = timedelta(minutes=5)
# Sàn nhịp kiểm tra: REMINDER_CHECK_INTERVAL_SEC=0 không biến vòng thành quay gấp.
_MIN_INTERVAL_SEC = 1.0


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


def _item_id(item) -> object:
    """Id của 1 dòng nhắc để ghi log, kể cả khi dòng lệch cột (không raise)."""
    try:
        return item[0]
    except Exception:
        return "?"


async def _process_due_reminders(due: Sequence[tuple]) -> int:
    """Giao các nhắc đã claim. Trả số nhắc giao lỗi đã NHẢ claim (claim được lại
    ngay - vòng nhắc chờ 1 nhịp rồi mới thử, không quay vòng gấp)."""
    from services import reminder_service

    released = 0
    for item in due:
        reminder_id = _item_id(item)
        try:
            # Dựng trong try: 1 dòng lệch cột chỉ bỏ qua dòng đó (claim giữ lease, thử
            # lại sau 5 phút), không giết cả vòng nhắc việc.
            reminder = idempotency.DueReminder(*item)
            reminder_id = reminder.id
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
                released += 1
                continue
            await db.mark_reminder_sent(reminder.id)
        except Exception:
            logger.warning(
                "scheduler: giao/ghi trạng thái reminder id=%s lỗi; lease giữ lần thử tiếp theo.",
                reminder_id,
                exc_info=True,
            )
    return released


def wake() -> None:
    """Đã ghi nhắc mới/đổi giờ nhắc (services/reminder_service, SAU khi ghi DB): vòng
    nhắc tính lại giờ ngủ ngay - nhắc "sau 1 phút" không phải chờ hết nhịp kiểm tra.
    Chưa start (test, lệnh CLI) thì không làm gì."""
    if _wake_event is not None:
        _wake_event.set()


def _interval() -> float:
    """Giờ ngủ tối đa giữa 2 lượt kiểm tra (REMINDER_CHECK_INTERVAL_SEC, có sàn)."""
    return max(_MIN_INTERVAL_SEC, float(config.REMINDER_CHECK_INTERVAL_SEC))


@db.with_reconnect
async def _seconds_until_next_due(lease: timedelta = _CLAIM_LEASE) -> float | None:
    """Số giây tới khi có nhắc claim được (<= 0: có ngay); None khi không còn nhắc
    nào chờ gửi. Tính bằng đồng hồ DB (không lệch giờ app/DB); nhắc đang bị claim
    (Zalo chờ gateway ack, giao lỗi giữ lease) chỉ tính từ lúc hết lease - đúng
    điều kiện của idempotency.claim_due_reminders. Chỉ quét index một phần
    ``idx_reminders_claimable`` (sent = false)."""
    value = await (await db.get_pool()).fetchval(
        """
        SELECT EXTRACT(EPOCH FROM (
            min(GREATEST(due_at, COALESCE(claimed_at + $1::interval, due_at))) - now()
        ))
        FROM reminders
        WHERE sent = false
        """,
        lease,
    )
    return None if value is None else float(value)


async def _reminder_pass() -> float:
    """1 lượt của vòng nhắc: nhắc tới hạn thì claim + giao. Trả số giây nên ngủ
    trước lượt sau (0 = kiểm tra lại ngay). Không raise (trừ khi bị huỷ)."""
    interval = _interval()
    # Bọc cả lượt như _daily_digest_loop: lỗi bất ngờ (đọc DB, claim, dựng dòng, giao)
    # chỉ bỏ lượt này, task nhắc việc không bao giờ chết âm thầm tới lần deploy sau.
    try:
        with trace.job("reminders"):
            wait = await _seconds_until_next_due()
            if wait is None:
                return interval
            if wait > 0:
                return min(wait, interval)
            due = await idempotency.claim_due_reminders(limit=_CLAIM_LIMIT, lease=_CLAIM_LEASE)
            released = await _process_due_reminders(due)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("scheduler: vòng nhắc việc lỗi; thử lại ở nhịp sau.", exc_info=True)
        return interval
    if released or not due:
        # Giao lỗi đã nhả claim (Telegram chập chờn), hoặc DB báo có nhắc mà claim
        # không lấy được (instance khác đang claim): thử lại sau 1 nhịp, không quay gấp.
        return interval
    # Vừa giao xong (lần kế của chuỗi lặp vừa được chèn, có thể còn tồn quá lô):
    # kiểm tra lại ngay - mỗi lượt claim đều tiến (dòng đã claim giữ lease).
    return 0.0


async def _wait_wake(seconds: float) -> None:
    """Ngủ ``seconds`` giây hoặc tới khi wake(). Xoá cờ SAU khi thức và TRƯỚC lượt
    kiểm tra kế: nhắc ghi trước wake() luôn được lượt kiểm tra sau đó thấy, wake()
    tới trong lúc đang kiểm tra cũng không bị mất (cờ còn đó, lần ngủ sau thức ngay)."""
    event = _wake_event
    if event is None:
        await asyncio.sleep(max(0.0, seconds))
        return
    if seconds > 0:
        try:
            await asyncio.wait_for(event.wait(), seconds)
        except TimeoutError:
            pass
    event.clear()


async def _reminder_loop() -> None:
    trace.detach()
    while True:
        await _wait_wake(await _reminder_pass())


def revive_reminder_loop() -> str | None:
    """Watchdog (services/watchdog.py): vòng nhắc việc đã start mà task đã chết vì
    lỗi không bắt được thì chạy lại. Trả tên lỗi đã giết task; None khi task còn
    sống, chưa start/đã stop, hoặc bị huỷ lúc tắt app (không chạy lại)."""
    global _reminder_task
    task = _reminder_task
    if task is None or not task.done() or task.cancelled():
        return None
    exc = task.exception()
    reason = type(exc).__name__ if exc is not None else "vòng lặp tự kết thúc"
    logger.warning("scheduler: vòng nhắc việc đã chết (%s) - chạy lại.", reason, exc_info=exc)
    _reminder_task = asyncio.create_task(_reminder_loop())
    return reason


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
    if await _offer_digest(user_id, digest, now, force=force) or await _notify(user_id, digest):
        await db.set_setting(_DIGEST_LAST_SENT_KEY, now.date().isoformat())
        return True
    return False


async def _offer_digest(user_id: int, digest: str, now: datetime, *, force: bool) -> bool:
    """Digest là tin tổng hợp (NORMAL): qua services/notify_hub - xả ngay kèm tin
    đang chờ gom, hoặc chờ hết giờ yên lặng. False = gửi thẳng như cũ."""
    if force:
        return False
    from services import notify_hub

    return await notify_hub.offer(
        "telegram",
        user_id,
        digest,
        source="portfolio_digest",
        dedup=f"digest:{now.date().isoformat()}",
        now=now,
    )


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
    global _reminder_task, _digest_task, _wake_event
    if _reminder_task is None or _reminder_task.done():
        _wake_event = asyncio.Event()
        _reminder_task = asyncio.create_task(_reminder_loop())
    if config.ENABLE_DAILY_DIGEST and allowed_user_id:
        if _digest_task is None or _digest_task.done():
            _digest_task = asyncio.create_task(_daily_digest_loop(allowed_user_id))


async def stop() -> None:
    """Cancel scheduler loops and wait for them during graceful shutdown."""
    global _reminder_task, _digest_task, _wake_event
    tasks = [task for task in (_reminder_task, _digest_task) if task is not None]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _reminder_task = None
    _digest_task = None
    _wake_event = None
