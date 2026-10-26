"""SQL nhắc việc v2 (services/reminder_service.py, migrations/019).

Mỗi hàm nhận ``pool`` (asyncpg pool hoặc pool PGlite của test); wrapper
@_with_reconnect nằm ở core/database.py cạnh các hàm reminders cũ. Mọi truy vấn
lọc theo telegram_user_id của người gọi - Zalo (id âm) không đụng dữ liệu
Telegram và ngược lại.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

# Tin nhắc đang chờ trong outbox Zalo: FK chỉ SET NULL khi xoá reminder, nên phải
# xoá tin chưa gửi TRƯỚC, nếu không nhắc đã huỷ vẫn được gửi đi.
_DELETE_UNSENT_OUTBOX = "DELETE FROM zalo_outbox WHERE sent_at IS NULL AND failed_at IS NULL AND reminder_id = ANY($1::int[])"


async def count_pending_once(pool: Any, user_id: int) -> int:
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM reminders WHERE telegram_user_id = $1 AND sent = false "
            "AND series_id IS NULL",
            user_id,
        )
        or 0
    )


async def count_series(pool: Any, user_id: int) -> int:
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM reminder_series WHERE telegram_user_id = $1", user_id
        )
        or 0
    )


async def create_series(
    pool: Any,
    user_id: int,
    message: str,
    *,
    kind: str,
    category: str,
    recurrence: dict,
    lead_days: int,
    channel: str,
    recipient_id: str,
    account_id: str,
    user_jid: str,
    event_key: str | None,
    first: list[tuple[datetime, str]],
) -> tuple[int, bool]:
    """Tạo chuỗi + các lần nhắc đầu trong 1 transaction. Trả (series_id, đã tạo
    mới?); event_key trùng (webhook gửi lại) -> trả chuỗi cũ, không tạo thêm."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            series_id = await conn.fetchval(
                """
                INSERT INTO reminder_series (
                    telegram_user_id, message, kind, category, recurrence, lead_days,
                    channel, recipient_id, account_id, user_jid, event_key
                ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (event_key) DO NOTHING
                RETURNING id
                """,
                user_id,
                message,
                kind,
                category,
                json.dumps(recurrence, ensure_ascii=False),
                lead_days,
                channel,
                recipient_id,
                account_id,
                user_jid,
                event_key,
            )
            if series_id is None:
                existing = await conn.fetchval(
                    "SELECT id FROM reminder_series WHERE event_key = $1", event_key
                )
                return int(existing), False
            for due_at, text in first:
                await conn.execute(
                    """
                    INSERT INTO reminders (
                        telegram_user_id, message, due_at, channel, recipient_id,
                        account_id, user_jid, series_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (series_id, due_at) WHERE series_id IS NOT NULL DO NOTHING
                    """,
                    user_id,
                    text,
                    due_at,
                    channel,
                    recipient_id,
                    account_id,
                    user_jid,
                    series_id,
                )
            return int(series_id), True


async def get_series(pool: Any, series_id: int) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT id, telegram_user_id, message, kind, category, recurrence, lead_days,
               channel, recipient_id, account_id, user_jid
        FROM reminder_series WHERE id = $1
        """,
        series_id,
    )
    return _series_dict(row) if row else None


def _series_dict(row) -> dict:
    data = dict(row)
    raw = data.get("recurrence")
    data["recurrence"] = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    return data


async def add_occurrence(pool: Any, series: dict, due_at: datetime, message: str) -> bool:
    """Chèn 1 lần nhắc của chuỗi; đã có (cùng series_id, due_at) thì bỏ qua."""
    inserted = await pool.fetchval(
        """
        INSERT INTO reminders (
            telegram_user_id, message, due_at, channel, recipient_id, account_id, user_jid,
            series_id
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (series_id, due_at) WHERE series_id IS NOT NULL DO NOTHING
        RETURNING id
        """,
        series["telegram_user_id"],
        message,
        due_at,
        series["channel"],
        series["recipient_id"],
        series["account_id"],
        series["user_jid"],
        series["id"],
    )
    return inserted is not None


async def list_pending_once(pool: Any, user_id: int, limit: int) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id, due_at, message FROM reminders
        WHERE telegram_user_id = $1 AND sent = false AND series_id IS NULL
        ORDER BY due_at, id
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def list_series(pool: Any, user_id: int, kind: str, limit: int) -> list[dict]:
    """Chuỗi của 1 người kèm lần chờ gần nhất (next_id, next_due_at)."""
    rows = await pool.fetch(
        """
        SELECT s.id, s.telegram_user_id, s.message, s.kind, s.category, s.recurrence,
               s.lead_days, s.channel, s.recipient_id, s.account_id, s.user_jid,
               n.id AS next_id, n.due_at AS next_due_at
        FROM reminder_series s
        LEFT JOIN LATERAL (
            SELECT id, due_at FROM reminders r
            WHERE r.series_id = s.id AND r.sent = false
            ORDER BY r.due_at LIMIT 1
        ) n ON true
        WHERE s.telegram_user_id = $1 AND s.kind = $2
        ORDER BY s.id
        LIMIT $3
        """,
        user_id,
        kind,
        limit,
    )
    return [_series_dict(r) for r in rows]


async def delete_reminder(pool: Any, user_id: int, reminder_id: int) -> bool:
    """Huỷ 1 nhắc 1 lần CHƯA gửi của đúng người đó (kèm tin outbox chưa gửi)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            owned = await conn.fetchval(
                "SELECT id FROM reminders WHERE id = $1 AND telegram_user_id = $2 AND sent = false",
                reminder_id,
                user_id,
            )
            if owned is None:
                return False
            await conn.execute(_DELETE_UNSENT_OUTBOX, [reminder_id])
            await conn.execute("DELETE FROM reminders WHERE id = $1", reminder_id)
            return True


async def delete_series(pool: Any, user_id: int, series_id: int) -> bool:
    """Xoá cả chuỗi (các lần của nó xoá theo ON DELETE CASCADE)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            owned = await conn.fetchval(
                "SELECT id FROM reminder_series WHERE id = $1 AND telegram_user_id = $2",
                series_id,
                user_id,
            )
            if owned is None:
                return False
            ids = [
                r["id"]
                for r in await conn.fetch(
                    "SELECT id FROM reminders WHERE series_id = $1 AND sent = false", series_id
                )
            ]
            if ids:
                await conn.execute(_DELETE_UNSENT_OUTBOX, ids)
            await conn.execute("DELETE FROM reminder_series WHERE id = $1", series_id)
            return True


async def reschedule(pool: Any, user_id: int, reminder_id: int, due_at: datetime) -> bool:
    """Dời 1 lần nhắc CHƯA gửi sang giờ mới (hoãn). Tin Zalo đã vào outbox mà
    chưa gửi bị xoá để lần claim sau đưa lại đúng giờ mới."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            owned = await conn.fetchval(
                "SELECT id FROM reminders WHERE id = $1 AND telegram_user_id = $2 AND sent = false",
                reminder_id,
                user_id,
            )
            if owned is None:
                return False
            clash = await conn.fetchval(
                """
                SELECT 1 FROM reminders a JOIN reminders b ON b.series_id = a.series_id
                WHERE a.id = $1 AND a.series_id IS NOT NULL AND b.id <> a.id AND b.due_at = $2
                """,
                reminder_id,
                due_at,
            )
            if clash:
                return False
            await conn.execute(_DELETE_UNSENT_OUTBOX, [reminder_id])
            await conn.execute(
                "UPDATE reminders SET due_at = $2, claimed_at = NULL WHERE id = $1",
                reminder_id,
                due_at,
            )
            return True


async def last_fired(pool: Any, user_id: int, within: timedelta) -> dict | None:
    """Lần nhắc vừa gửi gần nhất (trong ``within``) mà người dùng chưa trả lời."""
    row = await pool.fetchrow(
        """
        SELECT id, message, series_id, fired_at, due_at FROM reminders
        WHERE telegram_user_id = $1 AND fired_at IS NOT NULL AND done_at IS NULL
          AND fired_at >= now() - $2::interval
        ORDER BY fired_at DESC, id DESC
        LIMIT 1
        """,
        user_id,
        within,
    )
    return dict(row) if row else None


async def get_owned(pool: Any, user_id: int, reminder_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, message, series_id, fired_at, done_at, sent FROM reminders "
        "WHERE id = $1 AND telegram_user_id = $2",
        reminder_id,
        user_id,
    )
    return dict(row) if row else None


async def mark_done(pool: Any, reminder_id: int) -> None:
    await pool.execute(
        "UPDATE reminders SET done_at = COALESCE(done_at, now()) WHERE id = $1", reminder_id
    )


async def mark_fired(pool: Any, reminder_id: int) -> None:
    await pool.execute(
        "UPDATE reminders SET fired_at = COALESCE(fired_at, now()) WHERE id = $1", reminder_id
    )
