"""Persistence operations for the application settings key/value store.

Mọi lần ghi (``set``, ``compare_and_set``) báo cho các bộ đệm đã đăng ký qua
``on_write`` (core/settings_cache.py) - khoá đọc qua cache không bao giờ cũ hơn những
gì process này đã ghi, kể cả ai gọi thẳng repository này. Ghi lỗi (không rõ đã commit
chưa) thì báo ``value=None`` = bỏ khoá khỏi cache.
"""

from collections.abc import Callable
from typing import Any

_listeners: list[Callable[[str, str | None], None]] = []


def on_write(listener: Callable[[str, str | None], None]) -> None:
    if listener not in _listeners:
        _listeners.append(listener)


def _written(key: str, value: str | None) -> None:
    for listener in tuple(_listeners):
        listener(key, value)


async def get(pool: Any, key: str) -> str | None:
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", key)
    return row["value"] if row else None


async def set(pool: Any, key: str, value: str) -> None:
    try:
        await pool.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            key,
            value,
        )
    except BaseException:
        _written(key, None)
        raise
    _written(key, value)


async def list_encrypted(pool: Any, limit: int = 500) -> list[tuple[str, str]]:
    """(key, value) của các dòng đã mã hoá (prefix "enc:" của core.crypto) - vài
    dòng (API key override, session Zalo); có trần để không kéo cả bảng vào RAM.
    Dùng khi xoay khoá (services/key_rotation.py)."""
    rows = await pool.fetch(
        "SELECT key, value FROM settings WHERE value LIKE 'enc:%' ORDER BY key LIMIT $1",
        limit,
    )
    return [(row["key"], row["value"]) for row in rows]


async def compare_and_set(pool: Any, key: str, expected: str, value: str) -> bool:
    """Ghi ``value`` chỉ khi dòng vẫn đang là ``expected`` (compare-and-swap):
    gateway Zalo có thể lưu lại session đúng lúc đó - giá trị mới hơn thắng."""
    try:
        result = await pool.execute(
            "UPDATE settings SET value = $3, updated_at = now() WHERE key = $1 AND value = $2",
            key,
            expected,
            value,
        )
    except BaseException:
        _written(key, None)
        raise
    swapped = result == "UPDATE 1"
    if swapped:
        _written(key, value)
    return swapped
