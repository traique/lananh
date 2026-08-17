"""Durable claims and response caches for at-least-once channel delivery."""

import asyncio
import json
from datetime import timedelta
from typing import Any

from core import database as db

_schema_lock = asyncio.Lock()
_schema_ready = False


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    async with _schema_lock:
        if _schema_ready:
            return
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_processed_updates (
                    update_id BIGINT PRIMARY KEY,
                    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                """
                ALTER TABLE reminders
                ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reminders_claimable
                ON reminders (due_at, claimed_at)
                WHERE sent = false
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS zalo_direct_responses (
                    account_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    message_kind TEXT NOT NULL,
                    response_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (account_id, message_id, message_kind)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS zoom_processed_events (
                    event_id TEXT PRIMARY KEY,
                    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        _schema_ready = True


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
