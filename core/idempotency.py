"""Durable claims and response caches for at-least-once channel delivery."""

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, NamedTuple

from core import config
from core import database as db

logger = logging.getLogger(__name__)

RETENTION = timedelta(days=2)
_CLEANUP_INTERVAL_SEC = 60 * 60
_cleanup_task: asyncio.Task | None = None


async def ensure_schema() -> None:
    await db.ensure_migrations()


@db.with_reconnect
async def cleanup_expired(retention: timedelta = RETENTION) -> None:
    """Delete idempotency/cache rows older than the retention window."""
    await ensure_schema()
    pool = await db.get_pool()
    # response_json may contain large base64 images, so this table is especially
    # important to prune on schedule.
    await pool.execute(
        "DELETE FROM zalo_direct_responses WHERE created_at < now() - $1::interval",
        retention,
    )
    await pool.execute(
        "DELETE FROM webhook_inbox WHERE status = 'DONE' AND completed_at < now() - $1::interval",
        retention,
    )
    # Dòng DEAD giữ lâu hơn (tra cứu sự cố) rồi mới xoá - kèm payload.
    await pool.execute(
        "DELETE FROM webhook_inbox WHERE status = 'DEAD' AND completed_at < now() - $1::interval",
        timedelta(days=config.WEBHOOK_DEAD_RETENTION_DAYS),
    )


async def _cleanup_loop() -> None:
    while True:
        try:
            await cleanup_expired()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Không dọn được idempotency cache cũ.", exc_info=True)
        await asyncio.sleep(_CLEANUP_INTERVAL_SEC)


def start_cleanup_task() -> None:
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_loop(), name="idempotency-retention")


async def stop_cleanup_task() -> None:
    global _cleanup_task
    task, _cleanup_task = _cleanup_task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class DueReminder(NamedTuple):
    id: int
    user_id: int
    message: str
    channel: str = "telegram"
    recipient_id: str = ""
    account_id: str = ""
    user_jid: str = ""


@db.with_reconnect(idempotent=False)
async def claim_due_reminders(
    *,
    limit: int = 20,
    lease: timedelta = timedelta(minutes=5),
) -> list[DueReminder]:
    """Lease due reminders so concurrent schedulers cannot send the same row."""
    await ensure_schema()
    rows = await (await db.get_pool()).fetch(
        """
        WITH due AS (
            SELECT id
            FROM reminders
            WHERE due_at <= now()
              AND sent = false
              AND (claimed_at IS NULL OR claimed_at < now() - $2::interval)
            ORDER BY due_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT $1
        )
        UPDATE reminders AS reminder
        SET claimed_at = now()
        FROM due
        WHERE reminder.id = due.id
        RETURNING reminder.id, reminder.telegram_user_id, reminder.message,
                  reminder.channel, reminder.recipient_id, reminder.account_id, reminder.user_jid
        """,
        limit,
        lease,
    )
    return [
        DueReminder(
            row["id"],
            row["telegram_user_id"],
            row["message"],
            row["channel"],
            row["recipient_id"],
            row["account_id"],
            row["user_jid"],
        )
        for row in rows
    ]


@db.with_reconnect
async def release_reminder_claim(reminder_id: int) -> None:
    """Release a failed delivery for retry on the next scheduler pass."""
    await ensure_schema()
    await (await db.get_pool()).execute(
        "UPDATE reminders SET claimed_at = NULL WHERE id = $1 AND sent = false",
        reminder_id,
    )


@db.with_reconnect
async def get_zalo_response(
    account_id: str,
    message_id: str,
    message_kind: str,
) -> dict[str, Any] | None:
    await ensure_schema()
    value = await (await db.get_pool()).fetchval(
        """
        SELECT response_json
        FROM zalo_direct_responses
        WHERE account_id = $1 AND message_id = $2 AND message_kind = $3
        """,
        account_id,
        message_id,
        message_kind,
    )
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else dict(value)


@db.with_reconnect
async def save_zalo_response(
    account_id: str,
    message_id: str,
    message_kind: str,
    response: dict[str, Any],
) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        INSERT INTO zalo_direct_responses (
            account_id, message_id, message_kind, response_json
        )
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (account_id, message_id, message_kind) DO NOTHING
        """,
        account_id,
        message_id,
        message_kind,
        json.dumps(response, ensure_ascii=False),
    )
