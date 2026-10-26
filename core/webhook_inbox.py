"""PostgreSQL inbox with a fixed number of recoverable delivery workers.

Mỗi sự kiện được thử tối đa ``config.WEBHOOK_MAX_ATTEMPTS`` lần (backoff lũy
thừa). Quá số lần đó sự kiện chuyển DEAD: ngừng thử, báo chủ bot ĐÚNG 1 lần
(lệnh UPDATE chuyển trạng thái chỉ khớp 1 lần), giữ lại
``config.WEBHOOK_DEAD_RETENTION_DAYS`` ngày để tra cứu rồi bị dọn (xem
core/idempotency.py::cleanup_expired). Handler đọc ``current_attempt()`` để
chỉ trả lời lỗi cho người dùng ở lần thử đầu, không spam mỗi lần retry.
"""

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import timedelta

import messages
from core import alerts, config
from core import database as db

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()
_wake = asyncio.Event()
_LEASE = timedelta(minutes=5)
_RENEW_INTERVAL_SEC = 30.0
# Gia hạn lỗi tạm (DB chập chờn) thì thử lại sau khoảng ngắn này, miễn lease
# còn hiệu lực; chỉ huỷ lượt xử lý khi chắc chắn mất quyền sở hữu hoặc lease
# sắp hết (biên an toàn _LEASE_SAFETY_SEC).
_RENEW_RETRY_SEC = 5.0
_LEASE_SAFETY_SEC = 20.0
# Quét sự kiện hết lượt thử (reap_exhausted) tối đa 1 lần/phút lúc rảnh.
_REAP_INTERVAL_SEC = 60.0
_last_reap = 0.0

# Lần thử hiện tại (1 = lần đầu) của sự kiện đang được xử lý trong task này;
# None khi không chạy qua inbox (vd long polling local).
_current_attempt: ContextVar[int | None] = ContextVar("webhook_attempt", default=None)


def current_attempt() -> int | None:
    return _current_attempt.get()


def is_first_attempt() -> bool:
    """True nếu đây là lần xử lý đầu tiên (hoặc không chạy qua inbox)."""
    attempt = _current_attempt.get()
    return attempt is None or attempt <= 1


def max_attempts() -> int:
    return config.WEBHOOK_MAX_ATTEMPTS


@db.with_reconnect
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


@db.with_reconnect(idempotent=False)
async def claim():
    token = secrets.token_hex(16)
    return await (await db.get_pool()).fetchrow(
        """WITH ready AS (
            SELECT channel, event_id FROM webhook_inbox
            WHERE status IN ('PENDING', 'PROCESSING') AND available_at <= now()
              AND (lease_until IS NULL OR lease_until <= now())
              AND attempts < $3
            ORDER BY created_at, event_id FOR UPDATE SKIP LOCKED LIMIT 1
        )
        UPDATE webhook_inbox AS inbox
        SET status = 'PROCESSING', lease_token = $1, lease_until = now() + $2::interval,
            attempts = attempts + 1
        FROM ready WHERE inbox.channel = ready.channel AND inbox.event_id = ready.event_id
        RETURNING inbox.*""",
        token,
        _LEASE,
        max_attempts(),
    )


@db.with_reconnect
async def finish(row) -> None:
    await (await db.get_pool()).execute(
        """UPDATE webhook_inbox SET status = 'DONE', payload = NULL, completed_at = now(),
        lease_token = NULL, lease_until = NULL, last_error = NULL
        WHERE channel = $1 AND event_id = $2 AND lease_token = $3""",
        row["channel"],
        row["event_id"],
        row["lease_token"],
    )


def _notify_dead(channel: str, event_id: str, attempts: int, last_error: str | None) -> None:
    logger.error(
        "Webhook %s/%s lỗi %s lần (%s) - chuyển DEAD, ngừng thử lại.",
        channel,
        event_id,
        attempts,
        last_error or "?",
        extra={"alerted": True},  # đã báo chủ bot ngay dưới: error digest không báo lần 2
    )
    alerts.send(
        messages.WEBHOOK_DEAD_ALERT.format(
            channel=channel, event_id=event_id, attempts=attempts, error=last_error or "?"
        )
    )


@db.with_reconnect
async def _mark_dead(row, error_name: str) -> bool:
    result = await (await db.get_pool()).execute(
        """UPDATE webhook_inbox SET status = 'DEAD', completed_at = now(),
        lease_token = NULL, lease_until = NULL, last_error = $4
        WHERE channel = $1 AND event_id = $2 AND lease_token = $3""",
        row["channel"],
        row["event_id"],
        row["lease_token"],
        error_name,
    )
    return result == "UPDATE 1"


@db.with_reconnect
async def retry(row, error: BaseException) -> None:
    """Hẹn thử lại với backoff; hết lượt thử thì chuyển DEAD và báo chủ bot."""
    error_name = type(error).__name__
    if row["attempts"] >= max_attempts():
        if await _mark_dead(row, error_name):
            _notify_dead(row["channel"], row["event_id"], row["attempts"], error_name)
        return
    delay = timedelta(seconds=min(3600, 5 * 2 ** min(10, row["attempts"] - 1)))
    await (await db.get_pool()).execute(
        """UPDATE webhook_inbox SET status = 'PENDING', available_at = now() + $4::interval,
        lease_token = NULL, lease_until = NULL, last_error = $5
        WHERE channel = $1 AND event_id = $2 AND lease_token = $3""",
        row["channel"],
        row["event_id"],
        row["lease_token"],
        delay,
        error_name,
    )


@db.with_reconnect
async def reap_exhausted() -> int:
    """Sự kiện đã dùng hết lượt nhưng không kịp gọi retry() (process chết giữa
    chừng, hết RAM...) -> chuyển DEAD khi lease hết hạn, báo chủ bot 1 lần."""
    rows = await (await db.get_pool()).fetch(
        """UPDATE webhook_inbox SET status = 'DEAD', completed_at = now(),
        lease_token = NULL, lease_until = NULL,
        last_error = COALESCE(last_error, 'LeaseExpired')
        WHERE status IN ('PENDING', 'PROCESSING') AND attempts >= $1
          AND (lease_until IS NULL OR lease_until <= now())
        RETURNING channel, event_id, attempts, last_error""",
        max_attempts(),
    )
    for row in rows:
        _notify_dead(row["channel"], row["event_id"], row["attempts"], row["last_error"])
    return len(rows)


@db.with_reconnect
async def _extend_lease(row) -> bool:
    result = await (await db.get_pool()).execute(
        """UPDATE webhook_inbox SET lease_until = now() + $4::interval
        WHERE channel = $1 AND event_id = $2 AND lease_token = $3""",
        row["channel"],
        row["event_id"],
        row["lease_token"],
        _LEASE,
    )
    return result == "UPDATE 1"


async def _renew(row, owner: asyncio.Task) -> None:
    lease_deadline = time.monotonic() + _LEASE.total_seconds()
    delay = _RENEW_INTERVAL_SEC
    while True:
        await asyncio.sleep(delay)
        try:
            owned = await _extend_lease(row)
        except Exception:
            # Lỗi tạm: còn lease thì thử lại sớm; chỉ huỷ khi lease sắp hết, vì
            # xử lý tiếp mà không còn quyền sở hữu có thể chạy trùng tool/tin.
            if time.monotonic() + _RENEW_RETRY_SEC >= lease_deadline - _LEASE_SAFETY_SEC:
                logger.warning(
                    "Không gia hạn được lease webhook %s/%s trước khi hết hạn; huỷ lượt xử lý.",
                    row["channel"],
                    row["event_id"],
                    exc_info=True,
                )
                owner.cancel()
                return
            logger.info("Gia hạn lease webhook lỗi tạm, thử lại.", exc_info=True)
            delay = _RENEW_RETRY_SEC
            continue
        if not owned:
            owner.cancel()
            return
        lease_deadline = time.monotonic() + _LEASE.total_seconds()
        delay = _RENEW_INTERVAL_SEC


async def release(row) -> None:
    """Trả sự kiện về PENDING ngay (available_at=now) khi lượt xử lý bị huỷ do
    shutdown: trước đây lease 5 phút được giữ nguyên nên sau redeploy sự kiện
    chờ hết lease mới chạy lại. Chỉ khớp khi lease_token còn là của ta."""
    try:
        await asyncio.wait_for(
            (await db.get_pool()).execute(
                """UPDATE webhook_inbox SET status = 'PENDING', available_at = now(),
                lease_token = NULL, lease_until = NULL
                WHERE channel = $1 AND event_id = $2 AND lease_token = $3""",
                row["channel"],
                row["event_id"],
                row["lease_token"],
            ),
            timeout=5.0,
        )
    except Exception:
        logger.info(
            "Không trả được lease webhook %s/%s khi shutdown (sẽ tự hết lease).",
            row["channel"],
            row["event_id"],
            exc_info=True,
        )


async def deliver(row, processor: Callable[[str, dict], Awaitable[None]]) -> None:
    async def process():
        _current_attempt.set(int(row["attempts"] or 1))
        payload = row["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        await processor(row["channel"], payload)

    delivery = asyncio.create_task(process())
    heartbeat = asyncio.create_task(_renew(row, delivery))
    try:
        await delivery
        await finish(row)
    except asyncio.CancelledError:
        # Giữ payload để chạy lại; nếu là shutdown (task này bị cancel) thì trả
        # lease ngay cho instance mới. Mất quyền sở hữu (heartbeat cancel) thì
        # không đụng lease - nó đã thuộc về người khác.
        if asyncio.current_task().cancelling():
            await asyncio.shield(release(row))
            raise
    except Exception as exc:
        logger.warning(
            "Webhook %s/%s lỗi (%s) ở lần thử %s/%s; giữ payload để thử lại.",
            row["channel"],
            row["event_id"],
            type(exc).__name__,
            row["attempts"],
            max_attempts(),
        )
        await retry(row, exc)
    finally:
        heartbeat.cancel()
        delivery.cancel()
        await asyncio.gather(heartbeat, delivery, return_exceptions=True)


async def _maybe_reap() -> None:
    global _last_reap
    now = time.monotonic()
    if now - _last_reap < _REAP_INTERVAL_SEC:
        return
    _last_reap = now
    await reap_exhausted()


async def _worker(processor) -> None:
    while True:
        try:
            row = await claim()
            if row is not None:
                await deliver(row, processor)
                continue
            await _maybe_reap()
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
