"""Persistence operations for the application settings key/value store."""

from typing import Any


async def get(pool: Any, key: str) -> str | None:
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", key)
    return row["value"] if row else None


async def set(pool: Any, key: str, value: str) -> None:
    await pool.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        key,
        value,
    )


async def list_encrypted(pool: Any, limit: int) -> list[tuple[str, str]]:
    """(key, value) của các dòng đã mã hoá (prefix "enc:" của core.crypto) - vài
    dòng (API key override, session Zalo); có trần để không kéo cả bảng vào RAM."""
    rows = await pool.fetch(
        "SELECT key, value FROM settings WHERE value LIKE 'enc:%' ORDER BY key LIMIT $1",
        limit,
    )
    return [(row["key"], row["value"]) for row in rows]


async def compare_and_set(pool: Any, key: str, expected: str, value: str) -> bool:
    """Ghi ``value`` chỉ khi dòng vẫn đang là ``expected`` (compare-and-swap):
    gateway Zalo có thể lưu lại session đúng lúc đó - giá trị mới hơn thắng."""
    result = await pool.execute(
        "UPDATE settings SET value = $3, updated_at = now() WHERE key = $1 AND value = $2",
        key,
        expected,
        value,
    )
    return result == "UPDATE 1"
