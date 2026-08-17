"""Generate one idempotent digest per tracked group after 09:00 Vietnam time."""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from channels import zalo_repository, zalo_session
from channels.zalo_summary import summarize_group

logger = logging.getLogger(__name__)
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_task: asyncio.Task | None = None


def _enabled() -> bool:
    enabled = os.getenv("ZALO_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return enabled and bool(os.getenv("ZALO_BRIDGE_SECRET", "").strip())


async def _controller_id() -> str:
    return os.getenv("ZALO_CONTROLLER_ID", "").strip() or await zalo_session.load_controller()


def _hour() -> int:
    try:
        return max(0, min(23, int(os.getenv("ZALO_DAILY_SUMMARY_HOUR", "9"))))
    except ValueError:
        return 9


async def _run_due_digest() -> None:
    now = datetime.now(VN_TZ)
    end = now.replace(hour=_hour(), minute=0, second=0, microsecond=0)
    if now < end:
        return
    start = end - timedelta(days=1)
    account_id = os.getenv("ZALO_BOT_ACCOUNT_ID", "zalo-bot").strip() or "zalo-bot"
    recipient_id = await _controller_id()
    if not recipient_id:
        logger.info("Chưa có Zalo controller; bỏ qua lượt tổng kết này.")
        await zalo_repository.cleanup_old_messages(account_id)
        return

    for group_id, alias in await zalo_repository.list_groups(account_id):
        if await zalo_repository.summary_exists(account_id, group_id, "daily", start, end):
            continue
        try:
            _, _, content = await summarize_group(account_id, group_id, start, end)
            await zalo_repository.save_summary_and_enqueue(
                account_id,
                group_id,
                "daily",
                start,
                end,
                content,
                recipient_id,
            )
        except Exception:
            logger.exception("Không tổng kết được nhóm Zalo %s (%s)", alias, group_id)
    await zalo_repository.cleanup_old_messages(account_id)


async def _loop() -> None:
    while True:
        try:
            await _run_due_digest()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Vòng lặp tổng kết Zalo lỗi")
        await asyncio.sleep(300)


def start() -> None:
    global _task
    if not _enabled():
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
