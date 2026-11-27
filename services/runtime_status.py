"""Dòng trạng thái hệ thống cho /status (Telegram, Zalo, Zoom) - không gọi mạng/DB."""

from __future__ import annotations

from core import database as db


def db_pool_line() -> str:
    """Pool kết nối DB (core/db/pool.py ``pool_stats``): kết nối đang dùng/đang mở, số
    lần phải chờ mượn kết nối > 1 giây - dấu hiệu cần tăng DB_POOL_MAX_SIZE."""
    stats = db.pool_stats()
    if not stats["ready"] or stats["size"] is None:
        return f"🗄️ DB pool: chưa mở (tối đa {stats['max']} kết nối)"
    line = (
        f"🗄️ DB pool: {stats['in_use']}/{stats['size']} kết nối đang dùng "
        f"(tối đa {stats['max']}, cache câu lệnh {stats['statement_cache']})"
    )
    if stats["slow_waits"]:
        line += f"; {stats['slow_waits']} lần chờ > 1 giây, lâu nhất {stats['max_wait_ms']} ms"
    elif stats["exhausted"]:
        line += f"; {stats['exhausted']} lần hết kết nối rảnh"
    return line
