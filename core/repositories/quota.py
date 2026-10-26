"""SQL quota ngày của thành viên Zalo (services/usage_quota.py) trên bảng user_calls
(migrations/001, cột kind từ migrations/025).

Đếm theo idx_user_calls_user (channel, telegram_user_id, created_at DESC): mỗi
truy vấn chỉ quét các dòng của 1 người trong ngày.
"""

from datetime import datetime
from typing import Any


async def count_since(
    pool: Any, channel: str, user_id: int, since: datetime, heavy_kinds: list[str]
) -> tuple[int, int]:
    """(số lượt, số lượt "nặng") của 1 người từ ``since``."""
    row = await pool.fetchrow(
        """
        SELECT count(*) AS turns,
               count(*) FILTER (WHERE kind = ANY($4::text[])) AS heavy
        FROM user_calls
        WHERE channel = $1 AND telegram_user_id = $2 AND created_at >= $3
        """,
        channel,
        user_id,
        since,
        heavy_kinds,
    )
    if row is None:
        return 0, 0
    return int(row["turns"] or 0), int(row["heavy"] or 0)


async def insert(pool: Any, channel: str, user_id: int, kind: str) -> None:
    """1 lượt không đi qua telemetry (tạo ảnh /anh, phân tích cổ phiếu Zalo/Zoom)."""
    await pool.execute(
        "INSERT INTO user_calls (channel, telegram_user_id, kind) VALUES ($1, $2, $3)",
        channel,
        user_id,
        kind,
    )


async def relabel_latest(
    pool: Any, channel: str, user_id: int, from_kind: str, to_kind: str, since: datetime
) -> bool:
    """Đổi loại của lượt ``from_kind`` MỚI NHẤT (lượt đang chạy - khoá lượt theo
    người bảo đảm tuần tự) sang ``to_kind``, vd chat -> chat_search khi lượt chat
    có tra web. KHÔNG idempotent: chạy lại sau khi đã commit sẽ đổi luôn lượt
    ``from_kind`` trước đó (core.database bọc bằng idempotent=False)."""
    status = await pool.execute(
        """
        UPDATE user_calls SET kind = $4
        WHERE id = (
            SELECT id FROM user_calls
            WHERE channel = $1 AND telegram_user_id = $2 AND created_at >= $5 AND kind = $3
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        )
        """,
        channel,
        user_id,
        from_kind,
        to_kind,
        since,
    )
    return str(status).endswith(" 1")


async def counts_by_user_since(
    pool: Any, channel: str, since: datetime, heavy_kinds: list[str], limit: int
) -> list[dict]:
    """Số lượt/lượt nặng từ ``since`` của từng người trên kênh (trang /admin)."""
    rows = await pool.fetch(
        """
        SELECT telegram_user_id, count(*) AS turns,
               count(*) FILTER (WHERE kind = ANY($3::text[])) AS heavy
        FROM user_calls
        WHERE channel = $1 AND created_at >= $2
        GROUP BY telegram_user_id
        ORDER BY turns DESC
        LIMIT $4
        """,
        channel,
        since,
        heavy_kinds,
        limit,
    )
    return [dict(row) for row in rows]
