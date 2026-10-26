"""Truy vấn đọc phục vụ giám sát vận hành (watchdog) và sao lưu.

Toàn câu SELECT rẻ, chạy tối đa vài lần mỗi MONITOR_INTERVAL_SEC.
"""

from datetime import timedelta
from typing import Any


async def database_bytes(pool: Any) -> int:
    """Dung lượng DB hiện tại (con số Supabase tính vào quota 500 MB)."""
    return int(await pool.fetchval("SELECT pg_database_size(current_database())"))


async def webhook_backlog(pool: Any, stale_after: timedelta) -> tuple[int, float | None]:
    """(số sự kiện PENDING đã tới hạn xử lý quá ``stale_after`` mà worker chưa
    nhận, tuổi của sự kiện cũ nhất tính bằng giây). Sự kiện đang chờ backoff
    (available_at trong tương lai) không tính."""
    row = await pool.fetchrow(
        """
        SELECT count(*) AS stale,
               EXTRACT(EPOCH FROM now() - min(available_at)) AS oldest_sec
        FROM webhook_inbox
        WHERE status = 'PENDING' AND available_at < now() - $1::interval
        """,
        stale_after,
    )
    if row is None:
        return 0, None
    oldest = row["oldest_sec"]
    return int(row["stale"] or 0), (float(oldest) if oldest is not None else None)
