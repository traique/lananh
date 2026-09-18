"""Persistence for the Zalo -> Facebook publishing flow.

This module is intentionally separate from zalo_repository so Facebook source
configuration and queued posts cannot affect /tongket tracked groups.
"""

import asyncio
import secrets
from datetime import datetime

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
                CREATE TABLE IF NOT EXISTS zalo_facebook_groups (
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
                CREATE TABLE IF NOT EXISTS facebook_post_queue (
                    id BIGSERIAL PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL DEFAULT '',
                    source_message_ids TEXT[] NOT NULL DEFAULT '{}',
                    original_content TEXT NOT NULL DEFAULT '',
                    processed_content TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
                    facebook_post_id TEXT,
                    error_message TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    approved_at TIMESTAMPTZ,
                    posted_at TIMESTAMPTZ,
                    CONSTRAINT facebook_post_queue_status CHECK (
                        status IN ('PENDING_APPROVAL', 'POSTING', 'POSTED', 'REJECTED', 'ERROR')
                    )
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS facebook_post_media (
                    id BIGSERIAL PRIMARY KEY,
                    post_id BIGINT NOT NULL REFERENCES facebook_post_queue(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    mime_type TEXT NOT NULL,
                    content BYTEA NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (post_id, position)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shopee_affiliate_links (
                    account_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    affiliate_url TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (account_id, source_url)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS affiliate_short_links (
                    token TEXT PRIMARY KEY,
                    target_url TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_facebook_post_queue_status
                ON facebook_post_queue (account_id, status, created_at DESC)
                """
            )
        _schema_ready = True


async def list_groups(account_id: str) -> list[tuple[str, str]]:
    await ensure_schema()
    rows = await (await db.get_pool()).fetch(
        """
        SELECT group_id, alias FROM zalo_facebook_groups
        WHERE account_id = $1 ORDER BY alias
        """,
        account_id,
    )
    return [(row["group_id"], row["alias"]) for row in rows]


async def add_group(account_id: str, group_id: str, alias: str) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        INSERT INTO zalo_facebook_groups (account_id, group_id, alias)
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
        DELETE FROM zalo_facebook_groups
        WHERE account_id = $1 AND (group_id = $2 OR alias = $3)
        """,
        account_id,
        target,
        target.lower(),
    )
    return result != "DELETE 0"


async def create_post(
    *,
    account_id: str,
    group_id: str,
    sender_id: str,
    sender_name: str,
    source_message_ids: list[str],
    content: str,
    media: list[tuple[str, bytes]],
) -> int | None:
    await ensure_schema()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            allowed = await conn.fetchval(
                "SELECT 1 FROM zalo_facebook_groups WHERE account_id = $1 AND group_id = $2",
                account_id,
                group_id,
            )
            if not allowed:
                return None
            post_id = await conn.fetchval(
                """
                INSERT INTO facebook_post_queue (
                    account_id, group_id, sender_id, sender_name,
                    source_message_ids, original_content, processed_content
                )
                VALUES ($1, $2, $3, $4, $5, $6, $6)
                RETURNING id
                """,
                account_id,
                group_id,
                sender_id,
                sender_name[:500],
                source_message_ids,
                content,
            )
            for position, (mime_type, body) in enumerate(media):
                await conn.execute(
                    """
                    INSERT INTO facebook_post_media (post_id, position, mime_type, content)
                    VALUES ($1, $2, $3, $4)
                    """,
                    post_id,
                    position,
                    mime_type,
                    body,
                )
            return int(post_id)


async def get_post(account_id: str, post_id: int):
    await ensure_schema()
    return await (await db.get_pool()).fetchrow(
        "SELECT * FROM facebook_post_queue WHERE account_id = $1 AND id = $2",
        account_id,
        post_id,
    )


async def get_media(post_id: int):
    await ensure_schema()
    return await (await db.get_pool()).fetch(
        """
        SELECT mime_type, content FROM facebook_post_media
        WHERE post_id = $1 ORDER BY position
        """,
        post_id,
    )


async def update_content(account_id: str, post_id: int, content: str) -> bool:
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        """
        UPDATE facebook_post_queue SET processed_content = $3, error_message = NULL
        WHERE account_id = $1 AND id = $2 AND status IN ('PENDING_APPROVAL', 'ERROR')
        """,
        account_id,
        post_id,
        content,
    )
    return result != "UPDATE 0"


async def reject_post(account_id: str, post_id: int) -> bool:
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        """
        UPDATE facebook_post_queue SET status = 'REJECTED', error_message = NULL
        WHERE account_id = $1 AND id = $2 AND status IN ('PENDING_APPROVAL', 'ERROR')
        """,
        account_id,
        post_id,
    )
    return result != "UPDATE 0"


async def claim_post(account_id: str, post_id: int):
    await ensure_schema()
    return await (await db.get_pool()).fetchrow(
        """
        UPDATE facebook_post_queue
        SET status = 'POSTING', approved_at = COALESCE(approved_at, now()), error_message = NULL
        WHERE account_id = $1 AND id = $2 AND status IN ('PENDING_APPROVAL', 'ERROR')
        RETURNING *
        """,
        account_id,
        post_id,
    )


async def mark_posted(account_id: str, post_id: int, facebook_post_id: str) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        UPDATE facebook_post_queue
        SET status = 'POSTED', facebook_post_id = $3, posted_at = now(), error_message = NULL
        WHERE account_id = $1 AND id = $2
        """,
        account_id,
        post_id,
        facebook_post_id,
    )


async def mark_error(account_id: str, post_id: int, message: str) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        UPDATE facebook_post_queue
        SET status = 'ERROR', error_message = $3
        WHERE account_id = $1 AND id = $2 AND status = 'POSTING'
        """,
        account_id,
        post_id,
        message[:1000],
    )


async def set_affiliate_link(account_id: str, source_url: str, affiliate_url: str) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        INSERT INTO shopee_affiliate_links (account_id, source_url, affiliate_url)
        VALUES ($1, $2, $3)
        ON CONFLICT (account_id, source_url)
        DO UPDATE SET affiliate_url = EXCLUDED.affiliate_url, updated_at = now()
        """,
        account_id,
        source_url,
        affiliate_url,
    )


async def get_affiliate_links(account_id: str, source_urls: list[str]) -> dict[str, str]:
    if not source_urls:
        return {}
    await ensure_schema()
    rows = await (await db.get_pool()).fetch(
        """
        SELECT source_url, affiliate_url FROM shopee_affiliate_links
        WHERE account_id = $1 AND source_url = ANY($2::text[])
        """,
        account_id,
        source_urls,
    )
    return {row["source_url"]: row["affiliate_url"] for row in rows}


async def create_short_link(target_url: str) -> str:
    await ensure_schema()
    pool = await db.get_pool()
    for _ in range(5):
        token = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        result = await pool.execute(
            """
            INSERT INTO affiliate_short_links (token, target_url)
            VALUES ($1, $2) ON CONFLICT DO NOTHING
            """,
            token,
            target_url,
        )
        if result == "INSERT 0 1":
            return token
    raise RuntimeError("Không tạo được short-link token")


async def resolve_short_link(token: str) -> str | None:
    await ensure_schema()
    return await (await db.get_pool()).fetchval(
        "SELECT target_url FROM affiliate_short_links WHERE token = $1",
        token,
    )
