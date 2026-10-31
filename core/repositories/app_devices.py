"""SQL thiết bị app màn hình thông tin (migrations/030, services/app_devices.py).

Chỉ nhận/ghi HASH của mã ghép và token (services/app_devices.py băm trước khi gọi).
"""

from __future__ import annotations

from typing import Any

_COLUMNS = "id, telegram_user_id, name, created_at, paired_at, last_seen_at, pair_expires_at"


async def create_pending(
    pool: Any, user_id: int, name: str, code_hash: str, ttl_sec: int, max_devices: int
) -> int | None:
    """Thêm 1 thiết bị chờ ghép. None khi người này đã đủ ``max_devices`` thiết bị
    (đã ghép + đang chờ còn hạn). Dòng chờ đã hết hạn bị xoá luôn ở đây."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM app_devices WHERE telegram_user_id = $1 AND token_hash IS NULL "
                "AND (revoked_at IS NOT NULL OR pair_expires_at <= now())",
                user_id,
            )
            active = await conn.fetchval(
                "SELECT count(*) FROM app_devices "
                "WHERE telegram_user_id = $1 AND revoked_at IS NULL",
                user_id,
            )
            if active >= max_devices:
                return None
            return await conn.fetchval(
                "INSERT INTO app_devices (telegram_user_id, name, pair_code_hash, pair_expires_at) "
                "VALUES ($1, $2, $3, now() + make_interval(secs => $4)) RETURNING id",
                user_id,
                name,
                code_hash,
                float(ttl_sec),
            )


async def pair(pool: Any, code_hash: str, token_hash: str) -> dict | None:
    """Đổi mã ghép còn hạn lấy token. Mã chỉ dùng được 1 lần (xoá ngay khi ghép)."""
    row = await pool.fetchrow(
        "UPDATE app_devices SET token_hash = $2, pair_code_hash = NULL, pair_expires_at = NULL, "
        "paired_at = now(), last_seen_at = now() "
        "WHERE pair_code_hash = $1 AND token_hash IS NULL AND revoked_at IS NULL "
        "AND pair_expires_at > now() "
        "RETURNING id, telegram_user_id, name",
        code_hash,
        token_hash,
    )
    return dict(row) if row else None


async def by_token(pool: Any, token_hash: str) -> dict | None:
    """Thiết bị còn hiệu lực của token này; ghi lại lúc thấy gần nhất."""
    row = await pool.fetchrow(
        "UPDATE app_devices SET last_seen_at = now() "
        "WHERE token_hash = $1 AND revoked_at IS NULL "
        "RETURNING id, telegram_user_id, name",
        token_hash,
    )
    return dict(row) if row else None


async def list_for_user(pool: Any, user_id: int) -> list[dict]:
    """Thiết bị đã ghép + đang chờ ghép còn hạn, cũ trước."""
    rows = await pool.fetch(
        f"SELECT {_COLUMNS} FROM app_devices WHERE telegram_user_id = $1 "
        "AND revoked_at IS NULL AND (token_hash IS NOT NULL OR pair_expires_at > now()) "
        "ORDER BY created_at, id",
        user_id,
    )
    return [dict(r) for r in rows]


async def revoke(pool: Any, user_id: int, device_id: int) -> bool:
    """Thu hồi thiết bị (cả đang chờ ghép). Token và mã ghép bị xoá hash luôn."""
    row = await pool.fetchrow(
        "UPDATE app_devices SET revoked_at = now(), token_hash = NULL, pair_code_hash = NULL "
        "WHERE id = $1 AND telegram_user_id = $2 AND revoked_at IS NULL RETURNING id",
        device_id,
        user_id,
    )
    return row is not None
