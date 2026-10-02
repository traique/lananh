"""PostgreSQL inbox with a fixed number of recoverable delivery workers."""

import asyncio
import json
import logging
import secrets
from datetime import timedelta
from collections.abc import Awaitable, Callable

from core import database as db

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()
_wake = asyncio.Event()
_LEASE = timedelta(minutes=5)


async def enqueue(channel: str, event_id: str, payload: dict) -> bool:
    await db.ensure_migrations()
    result = await (await db.get_pool()).execute(
        """INSERT INTO webhook_inbox (channel, event_id, payload)
        VALUES ($1, $2, $3::jsonb) ON CONFLICT (channel, event_id) DO NOTHING""",
        channel,
        event_id,
        json.dumps(payload, ensure_ascii=False),
    )
    _wake.set()
    return result == "INSERT 0 1"


async def claim():
    token = secrets.token_hex(16)
    return await (await db.get_pool()).fetchrow(
        """WITH ready AS (
            SELECT channel, event_id FROM webhook_inbox
            WHERE status <> 'DONE' AND available_at <= now()
              AND (lease_until IS NULL OR lease_until <= now())
            ORDER BY created_at, event_id FOR UPDATE SKIP LOCKED LIMIT 1
        )
        UPDATE webhook_inbox AS inbox
        SET status = 'PROCESSING', lease_token = $1, lease_until = now() + $2::interval,
            attempts = attempts + 1
        FROM ready WHERE inbox.channel = ready.channel AND inbox.event_id = ready.event_id
        RETURNING inbox.*""",
        token,
        _LEASE,
    )


async def finish(row) -> None:
    await (await db.get_pool()).execute(
        """UPDATE webhook_inbox SET status = 'DONE', payload = NULL, completed_at = now(),
        lease_token = NULL, lease_until = NULL, last_error = NULL
        WHERE channel = $1 AND event_id = $2 AND lease_token = $3""",
        row["channel"],
        row["event_id"],
        row["lease_token"],
    )


async def retry(row, error: BaseException) -> None:
    delay = timedelta(seconds=min(3600, 5 * 2 ** min(10, row["attempts"] - 1)))
    await (await db.get_pool()).execute(
        """UPDATE webhook_inbox SET status = 'PENDING', available_at = now() + $4::interval,
        lease_token = NULL, lease_until = NULL, last_error = $5
        WHERE channel = $1 AND event_id = $2 AND lease_token = $3""",
        row["channel"],
        row["event_id"],
        row["lease_token"],
        delay,
        type(error).__name__,
    )


async def _renew(row, owner: asyncio.Task) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            result = await (await db.get_pool()).execute(
                """UPDATE webhook_inbox SET lease_until = now() + $4::interval
                WHERE channel = $1 AND event_id = $2 AND lease_token = $3""",
                row["channel"],
                row["event_id"],
                row["lease_token"],
                _LEASE,
            )
            if result != "UPDATE 1":
                owner.cancel()
                return
        except Exception:
            # Continuing without ownership can duplicate tools or deliveries.
            owner.cancel()
            return


async def deliver(row, processor: Callable[[str, dict], Awaitable[None]]) -> None:
    async def process():
        payload = row["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        await processor(row["channel"], payload)

    delivery = asyncio.create_task(process())
    heartbeat = asyncio.create_task(_renew(row, delivery))
    try:
        await delivery
        await finish(row)
    except asyncio.CancelledError:
        # Keep the lease/payload for recovery after shutdown or lost ownership.
        if asyncio.current_task().cancelling():
            raise
    except Exception as exc:
        logger.warning(
            "Webhook %s/%s lỗi (%s); giữ payload để thử lại.",
            row["channel"],
            row["event_id"],
            type(exc).__name__,
        )
        await retry(row, exc)
    finally:
        heartbeat.cancel()
        delivery.cancel()
        await asyncio.gather(heartbeat, delivery, return_exceptions=True)


async def _worker(processor) -> None:
    while True:
        try:
            row = await claim()
            if row is not None:
                await deliver(row, processor)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Webhook inbox worker lỗi; sẽ thử lại.", exc_info=True)
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=5)
        except TimeoutError:
            pass


def start(processor, workers: int = 2) -> None:
    if _tasks:
        return
    for index in range(max(1, min(4, workers))):
        task = asyncio.create_task(_worker(processor), name=f"webhook-inbox-{index}")
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def stop() -> None:
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
