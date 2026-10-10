"""Persistence for the Zalo -> Facebook publishing flow.

This module is intentionally separate from zalo_repository so Facebook source
configuration and queued posts cannot affect /tongket tracked groups.
"""

import secrets
import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import timedelta

from core import database as db
from services import facebook_dedup

logger = logging.getLogger(__name__)


class DuplicatePostError(Exception):
    """Bài mới trùng một bài đã có trong FACEBOOK_DEDUP_DAYS ngày gần đây."""

    def __init__(self, match: "facebook_dedup.DuplicateMatch"):
        super().__init__(f"{match.reason} (bài #{match.post_id})")
        self.match = match


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def max_pending() -> int:
    """Số bài chờ duyệt tối đa; vượt ngưỡng thì tự xoá bài chờ CŨ NHẤT."""
    return _env_int("FACEBOOK_MAX_PENDING", 150)


def history_days() -> int:
    """Bài đã đăng/đã bỏ qua giữ lại bao nhiêu ngày (ảnh của chúng bị xoá ngay)."""
    return _env_int("FACEBOOK_HISTORY_DAYS", 30)


_DEDUP_SCAN_LIMIT = 500


async def ensure_schema() -> None:
    await db.ensure_migrations()


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
    fingerprint: "facebook_dedup.Fingerprint | None" = None,
) -> int | None:
    """Đưa bài vào hàng chờ. Trả về post_id (bài cũ nếu gateway gửi lại đúng sự
    kiện đã nhận), None nếu nhóm không còn là nguồn Facebook.

    Có ``fingerprint`` thì so với các bài trong FACEBOOK_DEDUP_DAYS ngày; trùng
    -> raise DuplicatePostError và KHÔNG ghi gì vào DB.
    """
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
            event_key = hashlib.sha256(json.dumps(
                [account_id, group_id, sorted(set(source_message_ids))],
                ensure_ascii=False, separators=(",", ":"),
            ).encode()).hexdigest()
            existing = await conn.fetchval(
                "SELECT id FROM facebook_post_queue WHERE source_event_key = $1", event_key,
            )
            if existing is not None:
                return int(existing)
            if fingerprint is not None:
                # Khoá theo tài khoản trong transaction: 2 bài giống nhau tới cùng lúc
                # không thể cùng lọt qua bước so trùng.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", account_id)
                recent = await conn.fetch(
                    """
                    SELECT post_id, text_hash, product_keys, image_hashes, folded_text
                    FROM facebook_post_fingerprints
                    WHERE account_id = $1 AND created_at > now() - make_interval(days => $2)
                    ORDER BY created_at DESC
                    LIMIT $3
                    """,
                    account_id,
                    facebook_dedup.dedup_days(),
                    _DEDUP_SCAN_LIMIT,
                )
                match = facebook_dedup.find_duplicate(fingerprint, recent)
                if match is not None:
                    raise DuplicatePostError(match)
            post_id = await conn.fetchval(
                """
                INSERT INTO facebook_post_queue (
                    account_id, group_id, sender_id, sender_name,
                    source_message_ids, original_content, processed_content, source_event_key
                )
                VALUES ($1, $2, $3, $4, $5, $6, $6, $7)
                ON CONFLICT (source_event_key) DO NOTHING
                RETURNING id
                """,
                account_id,
                group_id,
                sender_id,
                sender_name[:500],
                source_message_ids,
                content,
                event_key,
            )
            if post_id is None:
                return await conn.fetchval(
                    "SELECT id FROM facebook_post_queue WHERE source_event_key = $1", event_key,
                )
            if media:
                await conn.executemany(
                    """
                    INSERT INTO facebook_post_media (post_id, position, mime_type, content)
                    VALUES ($1, $2, $3, $4)
                    """,
                    [
                        (post_id, position, mime_type, body)
                        for position, (mime_type, body) in enumerate(media)
                    ],
                )
            if fingerprint is not None:
                await conn.execute(
                    """
                    INSERT INTO facebook_post_fingerprints (
                        post_id, account_id, text_hash, product_keys, image_hashes, folded_text
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (post_id) DO NOTHING
                    """,
                    post_id,
                    account_id,
                    fingerprint.text_hash,
                    fingerprint.product_keys,
                    fingerprint.image_hashes,
                    fingerprint.folded_text,
                )
            return int(post_id)


async def enforce_pending_cap(account_id: str, limit: int | None = None) -> list[int]:
    """Giữ tối đa ``limit`` (mặc định FACEBOOK_MAX_PENDING=150) bài chờ duyệt.

    Xoá bài PENDING_APPROVAL cũ nhất vượt ngưỡng (ảnh/target xoá theo CASCADE).
    Không đụng bài ERROR/POSTING hay bài đã có page đăng thành công. Dấu vân tay
    vẫn giữ nên bài bị xoá không lọt lại vào hàng chờ khi được đăng lại.
    """
    limit = max_pending() if limit is None else limit
    await ensure_schema()
    rows = await (await db.get_pool()).fetch(
        """
        DELETE FROM facebook_post_queue
        WHERE id IN (
            SELECT q.id FROM facebook_post_queue q
            WHERE q.account_id = $1 AND q.status = 'PENDING_APPROVAL'
              AND NOT EXISTS (
                  SELECT 1 FROM facebook_post_targets t
                  WHERE t.post_id = q.id AND t.facebook_post_id IS NOT NULL
              )
            ORDER BY q.created_at DESC, q.id DESC
            OFFSET $2
        )
        RETURNING id
        """,
        account_id,
        limit,
    )
    return sorted(int(row["id"]) for row in rows)


async def count_pending(account_id: str) -> int:
    await ensure_schema()
    return int(await (await db.get_pool()).fetchval(
        "SELECT count(*) FROM facebook_post_queue WHERE account_id = $1 AND status = 'PENDING_APPROVAL'",
        account_id,
    ) or 0)


async def list_pending_for_refilter(account_id: str):
    """Bài đang chờ duyệt (chưa page nào đăng) kèm số ảnh, để chạy lại bộ lọc."""
    await ensure_schema()
    return await (await db.get_pool()).fetch(
        """
        SELECT q.id, q.original_content,
               (SELECT count(*) FROM facebook_post_media m WHERE m.post_id = q.id) AS media_count
        FROM facebook_post_queue q
        WHERE q.account_id = $1 AND q.status = 'PENDING_APPROVAL'
          AND NOT EXISTS (
              SELECT 1 FROM facebook_post_targets t
              WHERE t.post_id = q.id AND t.facebook_post_id IS NOT NULL
          )
        ORDER BY q.id
        """,
        account_id,
    )


async def delete_media(post_id: int) -> None:
    """Ảnh chỉ cần cho lúc đăng; bài đã đăng xong/bỏ qua thì xoá để nhẹ DB."""
    await (await db.get_pool()).execute(
        "DELETE FROM facebook_post_media WHERE post_id = $1", post_id,
    )


_PRUNE_INTERVAL_SEC = 3600
_last_prune_monotonic: float | None = None


async def prune_history(*, force: bool = False) -> dict[str, int]:
    """Dọn dữ liệu cũ cho Supabase free tier; chạy tối đa 1 lần/giờ.

    - Ảnh của bài POSTED/REJECTED (sót lại từ trước).
    - Bài POSTED/REJECTED cũ hơn FACEBOOK_HISTORY_DAYS ngày.
    - Dấu vân tay cũ hơn max(FACEBOOK_DEDUP_DAYS, FACEBOOK_HISTORY_DAYS) ngày.
    """
    global _last_prune_monotonic
    now = time.monotonic()
    if (
        not force
        and _last_prune_monotonic is not None
        and now - _last_prune_monotonic < _PRUNE_INTERVAL_SEC
    ):
        return {}
    _last_prune_monotonic = now
    await ensure_schema()
    pool = await db.get_pool()
    keep_days = history_days()
    media = await pool.execute(
        """
        DELETE FROM facebook_post_media m
        USING facebook_post_queue q
        WHERE m.post_id = q.id AND q.status IN ('POSTED', 'REJECTED')
        """
    )
    posts = await pool.execute(
        """
        DELETE FROM facebook_post_queue
        WHERE status IN ('POSTED', 'REJECTED')
          AND created_at < now() - make_interval(days => $1)
        """,
        keep_days,
    )
    fingerprints = await pool.execute(
        """
        DELETE FROM facebook_post_fingerprints
        WHERE created_at < now() - make_interval(days => $1)
        """,
        max(keep_days, facebook_dedup.dedup_days()),
    )

    def count(status: str) -> int:
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError, AttributeError):
            return 0

    result = {"media": count(media), "posts": count(posts), "fingerprints": count(fingerprints)}
    if any(result.values()):
        logger.info("Dọn dữ liệu Facebook: %s", result)
    return result


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
    if result == "UPDATE 0":
        return False
    await delete_media(post_id)
    return True


async def ensure_targets(post_id: int, page_keys: list[str]) -> None:
    """Make sure every currently configured page has a target row for this
    post. Existing rows (any status) are left untouched — this is what lets a
    re-run of /fb_ok add a newly-configured page to an old post without
    disturbing pages that already posted or already failed."""
    if not page_keys:
        return
    await ensure_schema()
    await (await db.get_pool()).executemany(
        """
        INSERT INTO facebook_post_targets (post_id, page_key)
        VALUES ($1, $2)
        ON CONFLICT (post_id, page_key) DO NOTHING
        """,
        [(post_id, page_key) for page_key in page_keys],
    )


async def list_targets(post_id: int):
    await ensure_schema()
    return await (await db.get_pool()).fetch(
        "SELECT * FROM facebook_post_targets WHERE post_id = $1 ORDER BY page_key",
        post_id,
    )


async def record_target_posted(
    post_id: int, page_key: str, facebook_post_id: str, permalink_url: str | None,
    *, claim_token: str | None = None,
) -> None:
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        """
        UPDATE facebook_post_targets
        SET status = 'POSTED', facebook_post_id = $3, permalink_url = $4,
            error_message = NULL, posted_at = now(), updated_at = now()
        WHERE post_id = $1 AND page_key = $2
          AND ($5::text IS NULL OR EXISTS (
              SELECT 1 FROM facebook_post_queue WHERE id = $1 AND claim_token = $5
          ))
        """,
        post_id,
        page_key,
        facebook_post_id,
        permalink_url,
        claim_token,
    )
    if result != "UPDATE 1":
        raise RuntimeError("Facebook publish claim no longer belongs to this worker")


async def record_target_comment(
    post_id: int, page_key: str, comment_id: str, claim_token: str,
) -> None:
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        """
        UPDATE facebook_post_targets SET comment_id = $3, updated_at = now()
        WHERE post_id = $1 AND page_key = $2 AND facebook_post_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM facebook_post_queue WHERE id = $1 AND claim_token = $4)
        """,
        post_id,
        page_key,
        comment_id,
        claim_token,
    )
    if result != "UPDATE 1":
        raise RuntimeError("Facebook publish claim no longer belongs to this worker")


async def record_target_error(
    post_id: int, page_key: str, message: str, *, claim_token: str | None = None,
) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        UPDATE facebook_post_targets
        SET status = 'ERROR', error_message = $3, updated_at = now()
        WHERE post_id = $1 AND page_key = $2
          AND facebook_post_id IS NULL AND status <> 'POSTED'
          AND ($4::text IS NULL OR EXISTS (
              SELECT 1 FROM facebook_post_queue WHERE id = $1 AND claim_token = $4
          ))
        """,
        post_id,
        page_key,
        message[:1000],
        claim_token,
    )


async def mark_target_creating(post_id: int, page_key: str, claim_token: str) -> None:
    result = await (await db.get_pool()).execute(
        """UPDATE facebook_post_targets SET status = 'POSTING', publish_started_at = now(),
        error_message = NULL, updated_at = now()
        WHERE post_id = $1 AND page_key = $2 AND status IN ('PENDING', 'ERROR')
          AND facebook_post_id IS NULL AND EXISTS (
              SELECT 1 FROM facebook_post_queue WHERE id = $1 AND claim_token = $3
          )""",
        post_id, page_key, claim_token,
    )
    if result != "UPDATE 1":
        raise RuntimeError("Facebook publish claim no longer belongs to this worker")


async def record_target_unknown(post_id: int, page_key: str, message: str, claim_token: str) -> None:
    await (await db.get_pool()).execute(
        """UPDATE facebook_post_targets SET status = 'UNKNOWN', error_message = $3, updated_at = now()
        WHERE post_id = $1 AND page_key = $2 AND facebook_post_id IS NULL
          AND EXISTS (SELECT 1 FROM facebook_post_queue WHERE id = $1 AND claim_token = $4)""",
        post_id, page_key, message[:1000], claim_token,
    )


def _error_summary(posted: int, total: int, errored: int, uncommented: int) -> str:
    text = f"{posted}/{total} page đã đăng, {errored} page lỗi."
    return f"{text[:-1]}, {uncommented} page chưa bình luận link." if uncommented else text


async def finalize_post_status(
    account_id: str, post_id: int, *, claim_token: str | None = None, needs_comment: bool = False,
) -> str:
    """Recompute facebook_post_queue.status from facebook_post_targets: POSTED
    only once every target page succeeded (and, when ``needs_comment``, got its
    first comment), ERROR otherwise. Returns the resulting overall status."""
    await ensure_schema()
    pool = await db.get_pool()
    summary = await pool.fetchrow(
        """
        SELECT
            count(*) AS total,
            count(*) FILTER (
                WHERE status = 'POSTED' AND ($2::boolean IS FALSE OR comment_id IS NOT NULL)
            ) AS posted,
            count(*) FILTER (WHERE status = 'ERROR') AS errored,
            count(*) FILTER (WHERE status = 'POSTED' AND comment_id IS NULL) AS uncommented
        FROM facebook_post_targets WHERE post_id = $1
        """,
        post_id,
        needs_comment,
    )
    total = summary["total"] if summary else 0
    posted = summary["posted"] if summary else 0
    errored = summary["errored"] if summary else 0
    if total > 0 and posted == total:
        result = await pool.execute(
            """
            UPDATE facebook_post_queue
            SET status = 'POSTED', posted_at = COALESCE(posted_at, now()), error_message = NULL,
                claim_token = NULL, lease_until = NULL
            WHERE account_id = $1 AND id = $2
              AND ($3::text IS NULL OR claim_token = $3)
            """,
            account_id,
            post_id,
            claim_token,
        )
        if result != "UPDATE 1":
            raise RuntimeError("Facebook publish claim lost before finalizing")
        await delete_media(post_id)
        return "POSTED"
    result = await pool.execute(
        """
        UPDATE facebook_post_queue
        SET status = 'ERROR', error_message = $3, claim_token = NULL, lease_until = NULL
        WHERE account_id = $1 AND id = $2
          AND ($4::text IS NULL OR claim_token = $4)
        """,
        account_id,
        post_id,
        _error_summary(posted, total, errored, summary["uncommented"] if needs_comment else 0)
        if total else "Chưa có Facebook Page nào được cấu hình.",
        claim_token,
    )
    if result != "UPDATE 1":
        raise RuntimeError("Facebook publish claim lost before finalizing")
    return "ERROR"


async def claim_post(account_id: str, post_id: int):
    await ensure_schema()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE facebook_post_queue
                SET status = 'POSTING', approved_at = COALESCE(approved_at, now()),
                    error_message = NULL, claim_token = $3, lease_until = now() + $4::interval
                WHERE account_id = $1 AND id = $2 AND (
                    status IN ('PENDING_APPROVAL', 'ERROR') OR
                    (status = 'POSTING' AND (lease_until IS NULL OR lease_until <= now()))
                ) RETURNING *""",
                account_id, post_id, secrets.token_hex(16), timedelta(minutes=5),
            )
            if row is not None:
                await conn.execute(
                    """UPDATE facebook_post_targets SET status = CASE
                        WHEN facebook_post_id IS NOT NULL THEN 'POSTED'
                        WHEN publish_started_at IS NULL THEN 'ERROR' ELSE 'UNKNOWN' END,
                        updated_at = now()
                    WHERE post_id = $1 AND status = 'POSTING'""",
                    post_id,
                )
            return row


async def release_post_claim(account_id: str, post_id: int, claim_token: str, message: str) -> None:
    await (await db.get_pool()).execute(
        """UPDATE facebook_post_queue SET status = 'ERROR', error_message = $4,
        claim_token = NULL, lease_until = NULL
        WHERE account_id = $1 AND id = $2 AND claim_token = $3""",
        account_id, post_id, claim_token, message[:1000],
    )


async def _renew_post(post_id: int, token: str, owner: asyncio.Task) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            result = await (await db.get_pool()).execute(
                """UPDATE facebook_post_queue SET lease_until = now() + interval '5 minutes'
                WHERE id = $1 AND claim_token = $2""", post_id, token,
            )
            if result != "UPDATE 1":
                owner.cancel()
                return
        except Exception:
            owner.cancel()
            return


@asynccontextmanager
async def keep_post_claim(post_id: int, token: str):
    heartbeat = asyncio.create_task(_renew_post(post_id, token, asyncio.current_task()))
    try:
        yield
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def reset_posts(account_id: str) -> tuple[int, bool]:
    """Delete every saved Facebook post for one account.

    Returns ``(deleted_count, sequence_reset)``. The global BIGSERIAL can only
    be restarted safely when no other account still has queued/history rows.
    """
    await ensure_schema()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "DELETE FROM facebook_post_queue WHERE account_id = $1 RETURNING id",
                account_id,
            )
            await conn.execute(
                "DELETE FROM facebook_post_fingerprints WHERE account_id = $1", account_id,
            )
            remaining = await conn.fetchval("SELECT COUNT(*) FROM facebook_post_queue")
            sequence_reset = int(remaining or 0) == 0
            if sequence_reset:
                # ID bài sẽ đánh lại từ #1: dấu vân tay mang post_id cũ phải đi theo.
                await conn.execute("DELETE FROM facebook_post_fingerprints")
                await conn.execute("ALTER SEQUENCE facebook_post_queue_id_seq RESTART WITH 1")
            return len(rows), sequence_reset


async def set_affiliate_link(account_id: str, source_url: str, affiliate_url: str) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        INSERT INTO shopee_affiliate_links (account_id, source_url, affiliate_url, verification_version)
        VALUES ($1, $2, $3, 1)
        ON CONFLICT (account_id, source_url)
        DO UPDATE SET
            affiliate_url = EXCLUDED.affiliate_url,
            verification_version = 1,
            updated_at = now()
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
        WHERE account_id = $1 AND source_url = ANY($2::text[]) AND verification_version = 1
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
