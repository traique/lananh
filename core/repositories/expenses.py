"""SQL chi tiêu (services/expense_service.py, migrations/020).

Tổng theo danh mục cộng bằng SQL - không bao giờ nhờ LLM cộng tiền. Mọi truy vấn
lọc theo telegram_user_id của người gọi (Zalo tách Telegram). Ngày truyền dạng text
YYYY-MM-DD rồi ép ``::date`` trong SQL (chạy được cả asyncpg lẫn PGlite của test).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

_COLUMNS = "id, amount_vnd, category, note, spent_on::text AS spent_on, source, draft"


async def insert(
    pool: Any,
    user_id: int,
    amount_vnd: int,
    *,
    category: str,
    note: str,
    spent_on: str,
    source: str,
    draft: bool,
    event_key: str | None,
) -> tuple[int, bool]:
    """(id, mới tạo?) - event_key trùng (webhook gửi lại) trả dòng cũ."""
    expense_id = await pool.fetchval(
        """
        INSERT INTO expenses (
            telegram_user_id, amount_vnd, category, note, spent_on, source, draft, event_key
        ) VALUES ($1, $2, $3, $4, ($5::text)::date, $6, $7, $8)
        ON CONFLICT (event_key) DO NOTHING
        RETURNING id
        """,
        user_id,
        amount_vnd,
        category,
        note,
        spent_on,
        source,
        draft,
        event_key,
    )
    if expense_id is not None:
        return int(expense_id), True
    existing = await pool.fetchval("SELECT id FROM expenses WHERE event_key = $1", event_key)
    return int(existing), False


async def count_since(pool: Any, user_id: int, within: timedelta) -> int:
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM expenses WHERE telegram_user_id = $1 "
            "AND created_at >= now() - $2::interval",
            user_id,
            within,
        )
        or 0
    )


async def delete(pool: Any, user_id: int, expense_id: int) -> dict | None:
    row = await pool.fetchrow(
        """
        DELETE FROM expenses WHERE id = $1 AND telegram_user_id = $2
        RETURNING id, amount_vnd, note, category, spent_on::text AS spent_on
        """,
        expense_id,
        user_id,
    )
    return dict(row) if row else None


async def last_created(pool: Any, user_id: int, within: timedelta) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT id, amount_vnd, note, category, spent_on::text AS spent_on FROM expenses
        WHERE telegram_user_id = $1 AND created_at >= now() - $2::interval
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        user_id,
        within,
    )
    return dict(row) if row else None


async def totals(pool: Any, user_id: int, start: str, end: str) -> list[dict]:
    """Tổng + số khoản theo danh mục trong [start, end), lớn trước."""
    rows = await pool.fetch(
        """
        SELECT category, sum(amount_vnd)::bigint AS total, count(*)::int AS items
        FROM expenses
        WHERE telegram_user_id = $1 AND spent_on >= ($2::text)::date
          AND spent_on < ($3::text)::date
        GROUP BY category
        ORDER BY total DESC, category
        """,
        user_id,
        start,
        end,
    )
    return [dict(r) for r in rows]


async def recent(pool: Any, user_id: int, limit: int) -> list[dict]:
    rows = await pool.fetch(
        f"""
        SELECT {_COLUMNS} FROM expenses
        WHERE telegram_user_id = $1
        ORDER BY spent_on DESC, id DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(r) for r in rows]
