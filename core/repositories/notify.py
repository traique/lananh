"""SQL hàng đợi thông báo gom (migrations/026, services/notify_hub.py).

Hàm nhận ``pool`` (asyncpg pool hoặc connection) như các repository khác; wrapper
có retry kết nối nằm ở core/database.py. Xếp NORMAL tuần tự theo id; khi xả, tin
URGENT bị giữ trong giờ yên lặng đứng trước.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_ORDER = "(level = 'urgent') DESC, id"


async def enqueue(
    pool: Any,
    channel: str,
    user_id: int,
    level: str,
    source: str,
    content: str,
    dedup_key: str | None,
    cap: int,
) -> tuple[int | None, int]:
    """(id dòng mới hoặc None nếu dedup_key đã có, số tin NORMAL cũ bị bỏ vì vượt
    trần ``cap`` tin chờ của người nhận). INSERT + cắt trần trong 1 transaction."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            new_id = await conn.fetchval(
                """
                INSERT INTO notify_queue (channel, user_id, level, source, content, dedup_key)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (channel, user_id, dedup_key) WHERE dedup_key IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                channel,
                user_id,
                level,
                source,
                content,
                dedup_key,
            )
            if new_id is None:
                return None, 0
            dropped = await conn.fetch(
                """
                UPDATE notify_queue SET status = 'dropped'
                WHERE id IN (
                    SELECT id FROM notify_queue
                    WHERE channel = $1 AND user_id = $2 AND status = 'pending'
                    ORDER BY id DESC OFFSET $3
                ) AND level = 'normal'
                RETURNING id
                """,
                channel,
                user_id,
                cap,
            )
    return int(new_id), len(dropped)


async def pending_recipients(pool: Any, limit: int, priority_sources: list[str]) -> list[dict]:
    """Người nhận đang có tin chờ: tin cũ nhất, có tin ưu tiên (URGENT bị giữ trong
    giờ yên lặng, hoặc tin tổng hợp hẹn giờ thuộc ``priority_sources``) không."""
    rows = await pool.fetch(
        """
        SELECT channel, user_id, min(created_at) AS oldest,
               bool_or(level = 'urgent' OR source = ANY($2::text[])) AS has_priority,
               count(*) AS pending
        FROM notify_queue
        WHERE status = 'pending'
        GROUP BY channel, user_id
        ORDER BY min(created_at)
        LIMIT $1
        """,
        limit,
        priority_sources,
    )
    return [dict(r) for r in rows]


async def claim(pool: Any, channel: str, user_id: int, limit: int, lease_sec: int) -> list[dict]:
    """Nhận các tin chờ (chưa ai đang gửi) của 1 người nhận, giữ lease ``lease_sec``
    giây. Gửi xong gọi mark_sent; lỗi thì release - quá lease thì tự nhả."""
    rows = await pool.fetch(
        f"""
        UPDATE notify_queue SET claimed_until = now() + $4::int * interval '1 second'
        WHERE id IN (
            SELECT id FROM notify_queue
            WHERE channel = $1 AND user_id = $2 AND status = 'pending'
              AND (claimed_until IS NULL OR claimed_until < now())
            ORDER BY {_ORDER}
            LIMIT $3
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, level, source, content, created_at
        """,
        channel,
        user_id,
        limit,
        lease_sec,
    )
    return sorted((dict(r) for r in rows), key=lambda r: (r["level"] != "urgent", r["id"]))


async def mark_sent(pool: Any, ids: list[int]) -> None:
    await pool.execute(
        "UPDATE notify_queue SET status = 'sent', sent_at = now(), claimed_until = NULL "
        "WHERE id = ANY($1::bigint[]) AND status = 'pending'",
        ids,
    )


async def release(pool: Any, ids: list[int]) -> None:
    await pool.execute(
        "UPDATE notify_queue SET claimed_until = NULL "
        "WHERE id = ANY($1::bigint[]) AND status = 'pending'",
        ids,
    )


async def take_dropped(pool: Any, channel: str, user_id: int) -> int:
    """Số tin đã bị bỏ vì vượt trần mà chưa báo người nhận (đánh dấu đã báo)."""
    rows = await pool.fetch(
        """
        UPDATE notify_queue SET sent_at = now()
        WHERE channel = $1 AND user_id = $2 AND status = 'dropped' AND sent_at IS NULL
        RETURNING id
        """,
        channel,
        user_id,
    )
    return len(rows)


async def flush_zalo(
    pool: Any,
    user_id: int,
    limit: int,
    account_id: str,
    recipient_id: str,
    compose: Callable[[list[dict], int], str | None],
) -> int:
    """Xả tin chờ của 1 thành viên Zalo vào zalo_outbox TRONG 1 transaction: đánh
    dấu sent và INSERT outbox cùng commit (INSERT outbox không idempotent - không
    bao giờ gửi đôi, sập giữa chừng thì cả hai cùng huỷ). ``compose(rows, dropped)``
    dựng nội dung (None = không gửi gì). Trả số tin đã xả."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                f"""
                SELECT id, level, source, content, created_at FROM notify_queue
                WHERE channel = 'zalo' AND user_id = $1 AND status = 'pending'
                ORDER BY {_ORDER}
                LIMIT $2
                FOR UPDATE SKIP LOCKED
                """,
                user_id,
                limit,
            )
            items = [dict(r) for r in rows]
            if not items:
                return 0
            dropped = await take_dropped(conn, "zalo", user_id)
            text = compose(items, dropped)
            await conn.execute(
                "UPDATE notify_queue SET status = 'sent', sent_at = now(), claimed_until = NULL "
                "WHERE id = ANY($1::bigint[])",
                [r["id"] for r in items],
            )
            if text:
                await conn.execute(
                    "INSERT INTO zalo_outbox (account_id, recipient_id, content) VALUES ($1, $2, $3)",
                    account_id,
                    recipient_id,
                    text,
                )
    return len(items)


async def drop_pending(pool: Any, channel: str, user_id: int) -> int:
    """Người nhận không còn nhận được (gỡ pairing, chưa pair Zoom): bỏ tin chờ."""
    rows = await pool.fetch(
        """
        UPDATE notify_queue SET status = 'dropped', sent_at = now(), claimed_until = NULL
        WHERE channel = $1 AND user_id = $2 AND status = 'pending'
        RETURNING id
        """,
        channel,
        user_id,
    )
    return len(rows)


async def count_pending(pool: Any, user_id: int) -> int:
    """Số tin đang chờ gom của 1 người (mọi kênh của chính user_id đó)."""
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM notify_queue WHERE user_id = $1 AND status = 'pending'",
            user_id,
        )
        or 0
    )


async def clear_user(pool: Any, user_id: int) -> None:
    """/xoadulieu, /zaloxoa: xoá mọi dòng (cả đã gửi) của người đó."""
    await pool.execute("DELETE FROM notify_queue WHERE user_id = $1", user_id)
