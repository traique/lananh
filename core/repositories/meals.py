"""SQL nhật ký ăn uống (services/nutrition_service.py, migrations/028).

Tổng kcal cộng bằng SQL - không nhờ LLM cộng. Mọi truy vấn lọc theo
telegram_user_id của người gọi (Zalo tách Telegram). Ngày truyền dạng text
YYYY-MM-DD rồi ép ``::date`` trong SQL (chạy được cả asyncpg lẫn PGlite của test).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

_COLUMNS = (
    "id, eaten_on::text AS eaten_on, meal, food, portion::float8 AS portion, kcal, "
    "kcal_source, source, created_at"
)


async def insert(
    pool: Any,
    user_id: int,
    *,
    eaten_on: str,
    meal: str,
    food: str,
    portion: float,
    kcal: int | None,
    kcal_source: str,
    source: str,
    event_key: str | None,
) -> tuple[int, bool]:
    """(id, mới tạo?) - event_key trùng (tin gửi lại) trả dòng cũ."""
    meal_id = await pool.fetchval(
        """
        INSERT INTO meals (
            telegram_user_id, eaten_on, meal, food, portion, kcal, kcal_source, source,
            event_key
        ) VALUES ($1, ($2::text)::date, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (event_key) DO NOTHING
        RETURNING id
        """,
        user_id,
        eaten_on,
        meal,
        food,
        portion,
        kcal,
        kcal_source,
        source,
        event_key,
    )
    if meal_id is not None:
        return int(meal_id), True
    existing = await pool.fetchval("SELECT id FROM meals WHERE event_key = $1", event_key)
    return int(existing), False


async def count_since(pool: Any, user_id: int, within: timedelta) -> int:
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM meals WHERE telegram_user_id = $1 "
            "AND created_at >= now() - $2::interval",
            user_id,
            within,
        )
        or 0
    )


async def day_rows(pool: Any, user_id: int, day: str, limit: int) -> list[dict]:
    rows = await pool.fetch(
        f"""
        SELECT {_COLUMNS} FROM meals
        WHERE telegram_user_id = $1 AND eaten_on = ($2::text)::date
        ORDER BY created_at, id
        LIMIT $3
        """,
        user_id,
        day,
        limit,
    )
    return [dict(r) for r in rows]


async def day_totals(pool: Any, user_id: int, start: str, end: str) -> list[dict]:
    """Mỗi ngày trong [start, end): tổng kcal đã biết, số món, số món chưa có kcal."""
    rows = await pool.fetch(
        """
        SELECT eaten_on::text AS eaten_on,
               coalesce(sum(kcal), 0)::bigint AS kcal,
               count(*)::int AS items,
               count(*) FILTER (WHERE kcal IS NULL)::int AS unknown
        FROM meals
        WHERE telegram_user_id = $1 AND eaten_on >= ($2::text)::date
          AND eaten_on < ($3::text)::date
        GROUP BY eaten_on
        ORDER BY eaten_on
        """,
        user_id,
        start,
        end,
    )
    return [dict(r) for r in rows]


async def delete(pool: Any, user_id: int, meal_id: int) -> dict | None:
    row = await pool.fetchrow(
        """
        DELETE FROM meals WHERE id = $1 AND telegram_user_id = $2
        RETURNING id, food, kcal, meal, eaten_on::text AS eaten_on
        """,
        meal_id,
        user_id,
    )
    return dict(row) if row else None


async def recent(pool: Any, user_id: int, limit: int) -> list[dict]:
    rows = await pool.fetch(
        f"""
        SELECT {_COLUMNS} FROM meals
        WHERE telegram_user_id = $1
        ORDER BY eaten_on DESC, id DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def clear(pool: Any, user_id: int) -> int:
    result = await pool.execute("DELETE FROM meals WHERE telegram_user_id = $1", user_id)
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:
        return 0
