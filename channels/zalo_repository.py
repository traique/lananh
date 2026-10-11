"""Supabase persistence for dynamically managed Zalo groups and summaries."""

import os
from datetime import datetime

from core import database as db


def _retention_days() -> int:
    try:
        return max(1, min(365, int(os.getenv("ZALO_GROUP_RETENTION_DAYS", "30"))))
    except ValueError:
        return 30


# Outbox: gateway báo gửi lỗi -> hẹn thử lại sau BASE * 2^(lần lỗi - 1) giây
# (trần MAX), quá ZALO_OUTBOX_MAX_ATTEMPTS lần thì bỏ cuộc (failed_at) để 1 tin
# không gửi được (vd người nhận chặn bot) không chặn mọi tin phía sau.
OUTBOX_RETRY_BASE_SEC = 120
OUTBOX_RETRY_MAX_SEC = 1800
OUTBOX_ERROR_MAX_CHARS = 500


def outbox_max_attempts() -> int:
    try:
        return max(1, min(20, int(os.getenv("ZALO_OUTBOX_MAX_ATTEMPTS", "5"))))
    except ValueError:
        return 5


async def ensure_schema() -> None:
    await db.ensure_migrations()


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
        WHERE account_id = $1 AND recipient_id = $2
          AND sent_at IS NULL AND failed_at IS NULL
        ORDER BY id
        LIMIT $3
        """,
        account_id,
        recipient_id,
        limit,
    )


async def enqueue_reminder(
    reminder_id: int, account_id: str, recipient_id: str, content: str
) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """INSERT INTO zalo_outbox (account_id, recipient_id, content, reminder_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (reminder_id) WHERE reminder_id IS NOT NULL DO NOTHING""",
        account_id,
        recipient_id,
        content,
        reminder_id,
    )


@db.with_reconnect
async def get_account_outbox(account_id: str, limit: int = 20):
    """Tin chờ gửi của 1 tài khoản bot, cũ trước.

    Bỏ qua tin đã bỏ cuộc (failed_at) và tin đang chờ backoff. Giữ thứ tự theo
    TỪNG người nhận: tin sau của người có tin trước đang chờ thử lại cũng đợi
    (không chen lên trước phần còn lại của 1 tin dài), người nhận khác vẫn đi.
    """
    await ensure_schema()
    return await (await db.get_pool()).fetch(
        """
        SELECT o.id, o.recipient_id, o.content, o.chunks_sent
        FROM zalo_outbox o
        WHERE o.account_id = $1 AND o.sent_at IS NULL AND o.failed_at IS NULL
          AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= now())
          AND NOT EXISTS (
              SELECT 1 FROM zalo_outbox e
              WHERE e.account_id = o.account_id AND e.recipient_id = o.recipient_id
                AND e.id < o.id AND e.sent_at IS NULL AND e.failed_at IS NULL
                AND e.next_attempt_at > now()
          )
        ORDER BY o.id
        LIMIT $2
        """,
        account_id,
        limit,
    )


@db.with_reconnect
async def mark_outbox_sent(item_id: int) -> None:
    await ensure_schema()
    await (await db.get_pool()).execute(
        """WITH delivered AS (
            UPDATE zalo_outbox SET sent_at = now() WHERE id = $1 AND sent_at IS NULL
            RETURNING reminder_id
        )
        UPDATE reminders SET sent = true, claimed_at = NULL
        WHERE id IN (SELECT reminder_id FROM delivered WHERE reminder_id IS NOT NULL)""",
        item_id,
    )


@db.with_reconnect(idempotent=False)
async def mark_outbox_failed(item_id: int, error: str, chunks_sent: int = 0):
    """Ghi 1 lần gửi lỗi: tăng attempts, lưu lỗi + số đoạn đã gửi được, hẹn giờ
    thử lại; đủ ``outbox_max_attempts()`` lần thì đặt failed_at (bỏ cuộc).

    Tin nhắc việc bị bỏ cuộc được đánh dấu sent (đã xử lý xong): scheduler
    không claim lại mãi một reminder mà outbox (unique theo reminder_id) sẽ
    không bao giờ tạo lại tin. Trả record (id, recipient_id, attempts, failed,
    reminder_id) hoặc None nếu tin không còn chờ gửi (đã gửi/đã bỏ cuộc/không có).
    """
    await ensure_schema()
    return await (await db.get_pool()).fetchrow(
        """
        WITH bumped AS (
            UPDATE zalo_outbox
            SET attempts = attempts + 1,
                last_error = $2::text,
                chunks_sent = GREATEST(chunks_sent, $3::integer),
                failed_at = CASE WHEN attempts + 1 >= $4::integer THEN now() END,
                next_attempt_at = CASE
                    WHEN attempts + 1 >= $4::integer THEN NULL
                    ELSE now() + LEAST(
                        $6::double precision,
                        $5::double precision * power(2, attempts)
                    ) * INTERVAL '1 second'
                END
            WHERE id = $1 AND sent_at IS NULL AND failed_at IS NULL
            RETURNING id, recipient_id, attempts, failed_at, reminder_id
        ),
        abandoned AS (
            UPDATE reminders SET sent = true, claimed_at = NULL
            WHERE id IN (
                SELECT reminder_id FROM bumped
                WHERE failed_at IS NOT NULL AND reminder_id IS NOT NULL
            )
        )
        SELECT id, recipient_id, attempts, failed_at IS NOT NULL AS failed, reminder_id
        FROM bumped
        """,
        item_id,
        (error or "")[:OUTBOX_ERROR_MAX_CHARS],
        max(0, int(chunks_sent or 0)),
        outbox_max_attempts(),
        OUTBOX_RETRY_BASE_SEC,
        OUTBOX_RETRY_MAX_SEC,
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
