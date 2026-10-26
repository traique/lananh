"""Cache ghi-xuyên (write-through) trong RAM cho vài khoá settings đọc ở MỌI
lượt gọi AI (model ưu tiên + Base URL của 9Router, công tắc Tavily).

Bot chạy 1 worker uvicorn nên mọi thay đổi đều đi qua ``set()`` ở đây (lệnh
/model, /tavily, trang admin) - cập nhật cache ngay khi ghi, không cần TTL.
Lỗi DB khi đọc KHÔNG bị cache (lần sau đọc lại). Trần số khoá để không phình RAM.
"""

from __future__ import annotations

from core import database as db

MAX_KEYS = 64
_cache: dict[str, str | None] = {}


async def get(key: str) -> str | None:
    if key in _cache:
        return _cache[key]
    value = await db.get_setting(key)
    if len(_cache) < MAX_KEYS:
        _cache[key] = value
    return value


async def set(key: str, value: str) -> None:
    await db.set_setting(key, value)
    _cache[key] = value


def clear() -> None:
    """Xoá toàn bộ cache (test dùng giữa các case; prod không cần)."""
    _cache.clear()
