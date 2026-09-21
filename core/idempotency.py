"""Durable claims and response caches for at-least-once channel delivery."""

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

from core import database as db

logger = logging.getLogger(__name__)

RETENTION = timedelta(days=2)
_CLEANUP_INTERVAL_SEC = 60 * 60
_cleanup_task: asyncio.Task | None = None


async def ensure_schema() -> None:
    await db.ensure_migrations()


async def cleanup_expired(retention: timedelta = RETENTION) -> None:
    """Delete idempotency/cache rows older than the retention window."""
    await ensure_schema()
    pool = await db.get_pool()
    await pool.execute(
        "DELETE FROM telegram_processed_updates WHERE claimed_at < now() - $1::interval",
        retention,
    )
    await pool.execute(
        "DELETE FROM zoom_processed_events WHERE claimed_at < now() - $1::interval",
        retention,
    )
    # response_json may contain large base64 images, so this table is especially
    # important to prune on schedule.
    await pool.execute(
        "DELETE FROM zalo_direct_responses WHERE created_at < now() - $1::interval",
        retention,
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
async def claim_telegram_update(update_id: int) -> bool:
    """Atomically claim a Telegram update across restarts and instances."""
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        """
        INSERT INTO telegram_processed_updates (update_id)
        VALUES ($1)
        ON CONFLICT (update_id) DO NOTHING
        """,
        update_id,
    )
    return result == "INSERT 0 1"


async def claim_due_reminders(
    *,
    limit: int = 20,
    lease: timedelta = timedelta(minutes=5),
) -> list[tuple[int, int, str]]:
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
        RETURNING reminder.id, reminder.telegram_user_id, reminder.message
        """,
        limit,
        lease,
    )
    return [(row["id"], row["telegram_user_id"], row["message"]) for row in rows]


async def release_reminder_claim(reminder_id: int) -> None:
    """Release a failed delivery for retry on the next scheduler pass."""
    await ensure_schema()
    await (await db.get_pool()).execute(
        "UPDATE reminders SET claimed_at = NULL WHERE id = $1 AND sent = false",
        reminder_id,
    )


async def claim_zoom_event(event_id: str) -> bool:
    """Atomically claim a Zoom webhook event (Zoom retries on non-200/slow response)."""
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        """
        INSERT INTO zoom_processed_events (event_id)
        VALUES ($1)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id,
    )
    return result == "INSERT 0 1"


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
