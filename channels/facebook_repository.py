"""Persistence for the Zalo -> Facebook publishing flow.

This module is intentionally separate from zalo_repository so Facebook source
configuration and queued posts cannot affect /tongket tracked groups.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
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


def max_media_mb() -> int:
    """Trần TỔNG dung lượng ảnh đang lưu trong facebook_post_media cho 1 tài khoản
    (MB). Supabase free 500 MB: 150 bài x vài ảnh 0,5 MB có thể vượt nếu chỉ chốt
    theo SỐ bài; vượt trần thì xoá bài chờ cũ nhất trước, nếu vẫn không vừa thì
    lưu bài mới KHÔNG kèm ảnh (text vẫn vào hàng chờ)."""
    return _env_int("FACEBOOK_MAX_MEDIA_MB", 150)


async def _fit_media_budget(conn, account_id: str, new_bytes: int) -> bool:
    """Giải phóng chỗ cho ``new_bytes`` ảnh mới bằng cách xoá bài chờ cũ nhất.
    Trả False nếu sau khi dọn vẫn không đủ chỗ (ảnh bài mới bị bỏ)."""
    budget = max_media_mb() * 1024 * 1024
    if new_bytes <= 0:
        return True
    if new_bytes > budget:
        return False
    for _ in range(50):
        used = int(
            await conn.fetchval(
                """
                SELECT COALESCE(SUM(octet_length(m.content)), 0)
                FROM facebook_post_media m JOIN facebook_post_queue q ON q.id = m.post_id
                WHERE q.account_id = $1
                """,
                account_id,
            )
            or 0
        )
        if used + new_bytes <= budget:
            return True
        victim = await conn.fetchval(
            """
            SELECT q.id FROM facebook_post_queue q
            WHERE q.account_id = $1 AND q.status = 'PENDING_APPROVAL'
              AND EXISTS (SELECT 1 FROM facebook_post_media m WHERE m.post_id = q.id)
              AND NOT EXISTS (
                  SELECT 1 FROM facebook_post_targets t
                  WHERE t.post_id = q.id AND t.facebook_post_id IS NOT NULL
              )
            ORDER BY q.created_at ASC, q.id ASC
            LIMIT 1
            """,
            account_id,
        )
        if victim is None:
            return False
        await conn.execute("DELETE FROM facebook_post_queue WHERE id = $1", victim)
        logger.warning(
            "Facebook media vượt trần %s MB: đã xoá bài chờ cũ nhất #%s để lấy chỗ.",
            max_media_mb(),
            victim,
        )
    return False


def history_days() -> int:
    """Bài đã đăng/đã bỏ qua giữ lại bao nhiêu ngày (ảnh của chúng bị xoá ngay)."""
    return _env_int("FACEBOOK_HISTORY_DAYS", 30)


# Trùng chính xác (cùng nội dung/sản phẩm) tra bằng index trên toàn cửa sổ chống trùng:
# chừng này dòng là đủ để báo bài gốc.
_DEDUP_EXACT_LIMIT = 20
# Tối đa số nhóm ghi lại cho 1 bài bị đăng lại (dup_groups).
_MAX_DUP_GROUPS = 20
_FINGERPRINT_COLUMNS = (
    "post_id, text_hash, product_keys, image_hashes, folded_text, group_id, sender_id"
)


@dataclass(frozen=True)
class SourceGroup:
    """Một nhóm nguồn Facebook: group ID, tên gợi nhớ, tên thật của nhóm Zalo ('' = chưa biết)."""

    group_id: str
    alias: str
    name: str = ""


async def ensure_schema() -> None:
    await db.ensure_migrations()


@db.with_reconnect
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


@db.with_reconnect
async def list_group_details(account_id: str) -> list[SourceGroup]:
    """Nhóm nguồn kèm tên thật của nhóm Zalo (để hiện tên thay cho group ID)."""
    await ensure_schema()
    rows = await (await db.get_pool()).fetch(
        """
        SELECT group_id, alias, group_name FROM zalo_facebook_groups
        WHERE account_id = $1 ORDER BY alias
        """,
        account_id,
    )
    return [SourceGroup(row["group_id"], row["alias"], row["group_name"] or "") for row in rows]


@db.with_reconnect
async def add_group(account_id: str, group_id: str, alias: str, group_name: str = "") -> None:
    """Thêm/cập nhật nhóm nguồn; ``group_name`` rỗng thì giữ tên thật đã biết."""
    await ensure_schema()
    await (await db.get_pool()).execute(
        """
        INSERT INTO zalo_facebook_groups (account_id, group_id, alias, group_name)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (account_id, group_id)
        DO UPDATE SET alias = EXCLUDED.alias,
            group_name = CASE WHEN EXCLUDED.group_name <> '' THEN EXCLUDED.group_name
                              ELSE zalo_facebook_groups.group_name END,
            updated_at = now()
        """,
        account_id,
        group_id,
        alias.lower(),
        group_name.strip()[:200],
    )


@db.with_reconnect
async def update_group_names(account_id: str, names: Mapping[str, str]) -> int:
    """Ghi tên thật mới của các nhóm nguồn (từ gateway); bỏ qua tên rỗng/không đổi.
    Trả số nhóm được đổi tên."""
    pairs = [(gid, name.strip()[:200]) for gid, name in names.items() if gid and name.strip()]
    if not pairs:
        return 0
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        """
        UPDATE zalo_facebook_groups AS g SET group_name = v.name, updated_at = now()
        FROM unnest($2::text[], $3::text[]) AS v(group_id, name)
        WHERE g.account_id = $1 AND g.group_id = v.group_id AND g.group_name <> v.name
        """,
        account_id,
        [gid for gid, _ in pairs],
        [name for _, name in pairs],
    )
    try:
        return int(str(result).split()[-1])
    except (ValueError, IndexError):
        return 0


@db.with_reconnect(idempotent=False)
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


async def _dedup_rows(conn, account_id: str, fingerprint: "facebook_dedup.Fingerprint") -> list:
    """Dòng để so trùng, mới nhất trước: (1) trùng chính xác nội dung/sản phẩm trên TOÀN
    cửa sổ FACEBOOK_DEDUP_DAYS (index, mọi nhóm nguồn), (2) các bài gần nhất cho luật
    gần giống (FACEBOOK_DEDUP_SCAN_LIMIT)."""
    days = facebook_dedup.dedup_days()
    exact = []
    if fingerprint.text_hash or fingerprint.product_keys:
        exact = await conn.fetch(
            f"""
            SELECT {_FINGERPRINT_COLUMNS} FROM facebook_post_fingerprints
            WHERE account_id = $1 AND created_at > now() - make_interval(days => $2)
              AND (text_hash = $3 OR product_keys && $4::text[])
            ORDER BY created_at DESC
            LIMIT $5
            """,
            account_id,
            days,
            fingerprint.text_hash,
            list(fingerprint.product_keys),
            _DEDUP_EXACT_LIMIT,
        )
    recent = await conn.fetch(
        f"""
        SELECT {_FINGERPRINT_COLUMNS} FROM facebook_post_fingerprints
        WHERE account_id = $1 AND created_at > now() - make_interval(days => $2)
        ORDER BY created_at DESC
        LIMIT $3
        """,
        account_id,
        days,
        facebook_dedup.scan_limit(),
    )
    seen = {int(row["post_id"]) for row in exact}
    return [*exact, *(row for row in recent if int(row["post_id"]) not in seen)]


@db.with_reconnect(idempotent=False)
async def record_duplicate(post_id: int, group_id: str) -> None:
    """Bài #post_id vừa bị đăng lại (đã chặn) ở nhóm ``group_id``: tăng đếm, nhớ nhóm
    (khác nhóm gốc, tối đa _MAX_DUP_GROUPS nhóm) để /fb_xem, /fb_boloc hiện ra."""
    await (await db.get_pool()).execute(
        """
        UPDATE facebook_post_fingerprints
        SET dup_count = dup_count + 1,
            last_dup_at = now(),
            dup_groups = CASE
                WHEN $2 = '' OR $2 = ANY(dup_groups) OR group_id IS NOT DISTINCT FROM $2
                     OR cardinality(dup_groups) >= $3 THEN dup_groups
                ELSE array_append(dup_groups, $2)
            END
        WHERE post_id = $1
        """,
        post_id,
        group_id or "",
        _MAX_DUP_GROUPS,
    )


@db.with_reconnect
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

    Có ``fingerprint`` thì so với các bài trong FACEBOOK_DEDUP_DAYS ngày của MỌI nhóm
    nguồn; trùng -> ghi nhận lần đăng lại vào bài gốc, raise DuplicatePostError và
    KHÔNG ghi bài mới. Idempotent theo source_event_key nên retry an toàn.
    """
    await ensure_schema()
    pool = await db.get_pool()
    match = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            allowed = await conn.fetchval(
                "SELECT 1 FROM zalo_facebook_groups WHERE account_id = $1 AND group_id = $2",
                account_id,
                group_id,
            )
            if not allowed:
                return None
            event_key = hashlib.sha256(
                json.dumps(
                    [account_id, group_id, sorted(set(source_message_ids))],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            existing = await conn.fetchval(
                "SELECT id FROM facebook_post_queue WHERE source_event_key = $1",
                event_key,
            )
            if existing is not None:
                return int(existing)
            if fingerprint is not None:
                # Khoá theo tài khoản trong transaction: 2 bài giống nhau tới cùng lúc
                # (kể cả từ 2 nhóm khác nhau) không thể cùng lọt qua bước so trùng.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", account_id)
                rows = await _dedup_rows(conn, account_id, fingerprint)
                # So gần giống là CPU thuần Python: chạy trong thread, không chặn event loop.
                match = await asyncio.to_thread(facebook_dedup.find_duplicate, fingerprint, rows)
            if match is None:
                return await _insert_post(
                    conn,
                    account_id=account_id,
                    group_id=group_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    source_message_ids=source_message_ids,
                    content=content,
                    media=media,
                    fingerprint=fingerprint,
                    event_key=event_key,
                )
    try:
        await record_duplicate(match.post_id, group_id)
    except Exception:
        logger.warning("Không ghi được lần đăng lại của bài #%s.", match.post_id, exc_info=True)
    raise DuplicatePostError(match)


async def _insert_post(
    conn,
    *,
    account_id: str,
    group_id: str,
    sender_id: str,
    sender_name: str,
    source_message_ids: list[str],
    content: str,
    media: list[tuple[str, bytes]],
    fingerprint: "facebook_dedup.Fingerprint | None",
    event_key: str,
) -> int:
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
        return int(
            await conn.fetchval(
                "SELECT id FROM facebook_post_queue WHERE source_event_key = $1",
                event_key,
            )
        )
    if media and not await _fit_media_budget(conn, account_id, sum(len(body) for _, body in media)):
        logger.warning(
            "Bài Facebook #%s lưu KHÔNG kèm %d ảnh: tổng ảnh đang lưu vượt trần %s MB.",
            post_id,
            len(media),
            max_media_mb(),
        )
        media = []
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
                post_id, account_id, text_hash, product_keys, image_hashes, folded_text,
                group_id, sender_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (post_id) DO NOTHING
            """,
            post_id,
            account_id,
            fingerprint.text_hash,
            fingerprint.product_keys,
            fingerprint.image_hashes,
            fingerprint.folded_text,
            group_id,
            sender_id,
        )
    return int(post_id)


@db.with_reconnect
async def recent_fingerprints(account_id: str) -> list:
    """Dấu vân tay trong cửa sổ chống trùng (mới nhất trước) để /fb_loclai so lại."""
    await ensure_schema()
    return await (await db.get_pool()).fetch(
        f"""
        SELECT {_FINGERPRINT_COLUMNS} FROM facebook_post_fingerprints
        WHERE account_id = $1 AND created_at > now() - make_interval(days => $2)
        ORDER BY created_at DESC
        LIMIT $3
        """,
        account_id,
        facebook_dedup.dedup_days(),
        facebook_dedup.scan_limit(),
    )


@db.with_reconnect(idempotent=False)
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


@db.with_reconnect
async def count_pending(account_id: str) -> int:
    await ensure_schema()
    return int(
        await (await db.get_pool()).fetchval(
            "SELECT count(*) FROM facebook_post_queue WHERE account_id = $1 AND status = 'PENDING_APPROVAL'",
            account_id,
        )
        or 0
    )


@db.with_reconnect
async def list_pending_for_refilter(account_id: str):
    """Bài đang chờ duyệt (chưa page nào đăng) kèm số ảnh, nhóm, người gửi và dấu vân
    tay, để chạy lại bộ lọc và lọc trùng giữa các nhóm."""
    await ensure_schema()
    return await (await db.get_pool()).fetch(
        """
        SELECT q.id, q.group_id, q.sender_id, q.original_content,
               (SELECT count(*) FROM facebook_post_media m WHERE m.post_id = q.id) AS media_count,
               f.text_hash, f.product_keys, f.image_hashes, f.folded_text
        FROM facebook_post_queue q
        LEFT JOIN facebook_post_fingerprints f ON f.post_id = q.id
        WHERE q.account_id = $1 AND q.status = 'PENDING_APPROVAL'
          AND NOT EXISTS (
              SELECT 1 FROM facebook_post_targets t
              WHERE t.post_id = q.id AND t.facebook_post_id IS NOT NULL
          )
        ORDER BY q.id
        """,
        account_id,
    )


@db.with_reconnect
async def delete_media(post_id: int) -> None:
    """Ảnh chỉ cần cho lúc đăng; bài đã đăng xong/bỏ qua thì xoá để nhẹ DB."""
    await (await db.get_pool()).execute(
        "DELETE FROM facebook_post_media WHERE post_id = $1",
        post_id,
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


@db.with_reconnect
async def get_post(account_id: str, post_id: int):
    """Bài trong hàng chờ kèm số lần bị đăng lại và các nhóm đã đăng lại nó."""
    await ensure_schema()
    return await (await db.get_pool()).fetchrow(
        """
        SELECT q.*, COALESCE(f.dup_count, 0) AS dup_count,
               COALESCE(f.dup_groups, '{}'::text[]) AS dup_groups
        FROM facebook_post_queue q
        LEFT JOIN facebook_post_fingerprints f ON f.post_id = q.id
        WHERE q.account_id = $1 AND q.id = $2
        """,
        account_id,
        post_id,
    )


@db.with_reconnect
async def get_media(post_id: int):
    await ensure_schema()
    return await (await db.get_pool()).fetch(
        """
        SELECT mime_type, content FROM facebook_post_media
        WHERE post_id = $1 ORDER BY position
        """,
        post_id,
    )


@db.with_reconnect
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


@db.with_reconnect(idempotent=False)
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


@db.with_reconnect
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


@db.with_reconnect
async def list_targets(post_id: int):
    await ensure_schema()
    return await (await db.get_pool()).fetch(
        "SELECT * FROM facebook_post_targets WHERE post_id = $1 ORDER BY page_key",
        post_id,
    )


@db.with_reconnect
async def record_target_posted(
    post_id: int,
    page_key: str,
    facebook_post_id: str,
    permalink_url: str | None,
    *,
    claim_token: str | None = None,
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


@db.with_reconnect
async def record_target_comment(
    post_id: int,
    page_key: str,
    comment_id: str,
    claim_token: str,
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


@db.with_reconnect
async def record_target_error(
    post_id: int,
    page_key: str,
    message: str,
    *,
    claim_token: str | None = None,
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


@db.with_reconnect(idempotent=False)
async def mark_target_creating(post_id: int, page_key: str, claim_token: str) -> None:
    result = await (await db.get_pool()).execute(
        """UPDATE facebook_post_targets SET status = 'POSTING', publish_started_at = now(),
        error_message = NULL, updated_at = now()
        WHERE post_id = $1 AND page_key = $2 AND status IN ('PENDING', 'ERROR')
          AND facebook_post_id IS NULL AND EXISTS (
              SELECT 1 FROM facebook_post_queue WHERE id = $1 AND claim_token = $3
          )""",
        post_id,
        page_key,
        claim_token,
    )
    if result != "UPDATE 1":
        raise RuntimeError("Facebook publish claim no longer belongs to this worker")


@db.with_reconnect
async def record_target_unknown(
    post_id: int, page_key: str, message: str, claim_token: str
) -> None:
    await (await db.get_pool()).execute(
        """UPDATE facebook_post_targets SET status = 'UNKNOWN', error_message = $3, updated_at = now()
        WHERE post_id = $1 AND page_key = $2 AND facebook_post_id IS NULL
          AND EXISTS (SELECT 1 FROM facebook_post_queue WHERE id = $1 AND claim_token = $4)""",
        post_id,
        page_key,
        message[:1000],
        claim_token,
    )


def _error_summary(posted: int, total: int, errored: int, uncommented: int) -> str:
    text = f"{posted}/{total} page đã đăng, {errored} page lỗi."
    return f"{text[:-1]}, {uncommented} page chưa bình luận link." if uncommented else text


@db.with_reconnect(idempotent=False)
async def finalize_post_status(
    account_id: str,
    post_id: int,
    *,
    claim_token: str | None = None,
    needs_comment: bool = False,
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
        if total
        else "Chưa có Facebook Page nào được cấu hình.",
        claim_token,
    )
    if result != "UPDATE 1":
        raise RuntimeError("Facebook publish claim lost before finalizing")
    return "ERROR"


@db.with_reconnect(idempotent=False)
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
                account_id,
                post_id,
                secrets.token_hex(16),
                timedelta(minutes=5),
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


@db.with_reconnect
async def release_post_claim(account_id: str, post_id: int, claim_token: str, message: str) -> None:
    await (await db.get_pool()).execute(
        """UPDATE facebook_post_queue SET status = 'ERROR', error_message = $4,
        claim_token = NULL, lease_until = NULL
        WHERE account_id = $1 AND id = $2 AND claim_token = $3""",
        account_id,
        post_id,
        claim_token,
        message[:1000],
    )


async def _renew_post(post_id: int, token: str, owner: asyncio.Task) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            result = await (await db.get_pool()).execute(
                """UPDATE facebook_post_queue SET lease_until = now() + interval '5 minutes'
                WHERE id = $1 AND claim_token = $2""",
                post_id,
                token,
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


@db.with_reconnect(idempotent=False)
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
                "DELETE FROM facebook_post_fingerprints WHERE account_id = $1",
                account_id,
            )
            remaining = await conn.fetchval("SELECT COUNT(*) FROM facebook_post_queue")
            sequence_reset = int(remaining or 0) == 0
            if sequence_reset:
                # ID bài sẽ đánh lại từ #1: dấu vân tay mang post_id cũ phải đi theo.
                await conn.execute("DELETE FROM facebook_post_fingerprints")
                await conn.execute("ALTER SEQUENCE facebook_post_queue_id_seq RESTART WITH 1")
            return len(rows), sequence_reset


@db.with_reconnect
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


@db.with_reconnect
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
