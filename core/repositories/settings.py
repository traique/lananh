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
