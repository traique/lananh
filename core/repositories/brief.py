"""Truy vấn phục vụ bản tin sáng cá nhân (services/morning_brief.py)."""

from datetime import datetime
from typing import Any


async def list_settings_with_prefix(pool: Any, prefix: str, limit: int) -> list[tuple[str, str]]:
    """(key, value) các dòng settings có key bắt đầu bằng ``prefix`` (so khớp
    chính xác, không dùng LIKE nên ký tự % _ trong prefix không thành wildcard)."""
    rows = await pool.fetch(
        """
        SELECT key, value FROM settings
        WHERE left(key, length($1)) = $1
        ORDER BY key
        LIMIT $2
        """,
        prefix,
        limit,
    )
    return [(r["key"], r["value"]) for r in rows]


async def list_reminders_between(
    pool: Any, user_id: int, start: datetime, end: datetime, limit: int
) -> list[tuple[datetime, str]]:
    """Nhắc việc CHƯA gửi của 1 người, đến hạn trong [start, end)."""
    rows = await pool.fetch(
        """
        SELECT due_at, message FROM reminders
        WHERE telegram_user_id = $1 AND sent = false
          AND due_at >= $2 AND due_at < $3
        ORDER BY due_at
        LIMIT $4
        """,
        user_id,
        start,
        end,
        limit,
    )
    return [(r["due_at"], r["message"]) for r in rows]
