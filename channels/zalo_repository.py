"""Supabase persistence for dynamically managed Zalo groups and summaries."""

import asyncio
import os
from datetime import datetime

from core import database as db

_schema_lock = asyncio.Lock()
_schema_ready = False


def _retention_days() -> int:
    try:
        return max(1, min(365, int(os.getenv("ZALO_GROUP_RETENTION_DAYS", "30"))))
    except ValueError:
        return 30


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
                CREATE TABLE IF NOT EXISTS zalo_groups (
                    account_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (account_id, group_id),
                    UNIQUE (account_id, alias)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS zalo_group_messages (
                    id BIGSERIAL PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    sent_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (account_id, group_id, message_id),
                    FOREIGN KEY (account_id, group_id)
                        REFERENCES zalo_groups(account_id, group_id) ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_zalo_group_messages_window
                ON zalo_group_messages (account_id, group_id, sent_at DESC)
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS zalo_group_summaries (
                    id BIGSERIAL PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    summary_type TEXT NOT NULL,
                    window_start TIMESTAMPTZ NOT NULL,
                    window_end TIMESTAMPTZ NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (
                        account_id, group_id, summary_type, window_start, window_end
                    ),
                    FOREIGN KEY (account_id, group_id)
                        REFERENCES zalo_groups(account_id, group_id) ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS zalo_outbox (
                    id BIGSERIAL PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    sent_at TIMESTAMPTZ,
                    summary_id BIGINT
                )
                """
            )
            await conn.execute("ALTER TABLE zalo_outbox ADD COLUMN IF NOT EXISTS summary_id BIGINT")
            await conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_zalo_outbox_summary
                ON zalo_outbox (summary_id)
                WHERE summary_id IS NOT NULL
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_zalo_outbox_pending
                ON zalo_outbox (account_id, id)
                WHERE sent_at IS NULL
                """
            )
        _schema_ready = True


async def list_groups(account_id: str) -> list[tuple[str, str]]:
    await ensure_schema()
    rows = await (await db.get_pool()).fetch(
        """
        SELECT group_id, alias
        FROM zalo_groups
        WHERE account_id = $1
        ORDER BY alias
        """,
        account_id,
    )
    return [(row["group_id"], row["alias"]) for row in rows]


async def resolve_group(account_id: str, target: str) -> tuple[str, str] | None:
    await ensure_schema()
    row = await (await db.get_pool()).fetchrow(
        """
        SELECT group_id, alias
        FROM zalo_groups
        WHERE account_id = $1 AND (group_id = $2 OR alias = $3)
        """,
        account_id,
        target,
        target.lower(),
    )
    return (row["group_id"], row["alias"]) if row else None


async def add_group(account_id: str, group_id: str, alias: str) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        INSERT INTO zalo_groups (account_id, group_id, alias)
        VALUES ($1, $2, $3)
        ON CONFLICT (account_id, group_id)
        DO UPDATE SET alias = EXCLUDED.alias, updated_at = now()
        """,
        account_id,
        group_id,
        alias.lower(),
    )


async def remove_group(account_id: str, target: str) -> bool:
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        """
        DELETE FROM zalo_groups
        WHERE account_id = $1 AND (group_id = $2 OR alias = $3)
        """,
        account_id,
        target,
        target.lower(),
    )
    return result != "DELETE 0"


async def save_group_message(**values) -> bool:
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        """
        INSERT INTO zalo_group_messages (
            account_id, group_id, message_id, sender_id, sender_name, content, sent_at
        )
        SELECT $1, $2, $3, $4, $5, $6, to_timestamp($7::double precision / 1000.0)
        WHERE EXISTS (
            SELECT 1 FROM zalo_groups WHERE account_id = $1 AND group_id = $2
        )
        ON CONFLICT (account_id, group_id, message_id) DO NOTHING
        """,
        values["account_id"],
        values["group_id"],
        values["message_id"],
        values["sender_id"],
        values["sender_name"][:500],
        values["text"],
        values["sent_at_ms"],
    )
    return result == "INSERT 0 1"


async def get_group_messages(
    account_id: str,
    group_id: str,
    start: datetime,
    end: datetime,
    limit: int,
):
    await ensure_schema()
    return await (await db.get_pool()).fetch(
        """
        SELECT sender_id, sender_name, content, sent_at
        FROM zalo_group_messages
        WHERE account_id = $1 AND group_id = $2
          AND sent_at >= $3 AND sent_at < $4
        ORDER BY sent_at ASC
        LIMIT $5
        """,
        account_id,
        group_id,
        start,
        end,
        limit,
    )


async def summary_exists(
    account_id: str,
    group_id: str,
    summary_type: str,
    start: datetime,
    end: datetime,
) -> bool:
    await ensure_schema()
    return bool(
        await (await db.get_pool()).fetchval(
            """
            SELECT 1
            FROM zalo_group_summaries
            WHERE account_id = $1 AND group_id = $2 AND summary_type = $3
              AND window_start = $4 AND window_end = $5
            """,
            account_id,
            group_id,
            summary_type,
            start,
            end,
        )
    )


async def save_summary(
    account_id: str,
    group_id: str,
    summary_type: str,
    start: datetime,
    end: datetime,
    content: str,
) -> bool:
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        """
        INSERT INTO zalo_group_summaries (
            account_id, group_id, summary_type, window_start, window_end, content
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT DO NOTHING
        """,
        account_id,
        group_id,
        summary_type,
        start,
        end,
        content,
    )
    return result == "INSERT 0 1"


async def save_summary_and_enqueue(
    account_id: str,
    group_id: str,
    summary_type: str,
    start: datetime,
    end: datetime,
    content: str,
    recipient_id: str,
) -> bool:
    """Create a summary and its outbox item in one idempotent transaction."""
    await ensure_schema()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            summary_id = await conn.fetchval(
                """
                INSERT INTO zalo_group_summaries (
                    account_id, group_id, summary_type,
                    window_start, window_end, content
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                account_id,
                group_id,
                summary_type,
                start,
                end,
                content,
            )
            if summary_id is None:
                return False
            await conn.execute(
                """
                INSERT INTO zalo_outbox (
                    account_id, recipient_id, content, summary_id
                )
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (summary_id) WHERE summary_id IS NOT NULL DO NOTHING
                """,
                account_id,
                recipient_id,
                content,
                summary_id,
            )
    return True


async def enqueue_outbox(account_id: str, recipient_id: str, content: str) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        INSERT INTO zalo_outbox (account_id, recipient_id, content)
        VALUES ($1, $2, $3)
        """,
        account_id,
        recipient_id,
        content,
    )


async def get_pending_outbox(
    account_id: str,
    recipient_id: str,
    limit: int = 20,
):
    await ensure_schema()
    return await (await db.get_pool()).fetch(
        """
        SELECT id, content
        FROM zalo_outbox
        WHERE account_id = $1 AND recipient_id = $2 AND sent_at IS NULL
        ORDER BY id
        LIMIT $3
        """,
        account_id,
        recipient_id,
        limit,
    )


async def mark_outbox_sent(item_id: int) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        "UPDATE zalo_outbox SET sent_at = now() WHERE id = $1 AND sent_at IS NULL",
        item_id,
    )


async def cleanup_old_messages(account_id: str) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        DELETE FROM zalo_group_messages
        WHERE account_id = $1
          AND sent_at < now() - ($2::integer * INTERVAL '1 day')
        """,
        account_id,
        _retention_days(),
    )


async def resolve_default_account_id() -> str | None:
    """Bot Zalo B trong repo này chỉ có ĐÚNG 1 tài khoản đăng nhập (thiết kế
    1-chủ) - nhưng account_id THẬT (numeric, do zalo-gateway gửi lên) chỉ được
    biết SAU khi gateway đăng nhập thành công (accountId = api.getOwnId()),
    không có nơi nào lưu tường minh "account_id hiện tại đang active". Suy ra
    bằng cách lấy account_id có NHIỀU NHÓM ĐANG THEO DÕI NHẤT trong
    zalo_groups - dùng cho các lệnh nhóm gọi từ kênh KHÔNG PHẢI Zalo (vd Zoom,
    Telegram), nơi không có account_id kèm sẵn theo request như
    channels/router.py (Zalo bridge, account_id lấy trực tiếp từ payload).
    Trả None nếu chưa có nhóm Zalo nào được thêm (/themnhom) - gọi nơi dùng tự
    quyết định thông báo phù hợp thay vì coi None là account_id rỗng hợp lệ."""
    await ensure_schema()
    row = await (await db.get_pool()).fetchrow(
        """
        SELECT account_id
        FROM zalo_groups
        GROUP BY account_id
        ORDER BY COUNT(*) DESC, MIN(created_at) ASC
        LIMIT 1
        """
    )
    return row["account_id"] if row else None
