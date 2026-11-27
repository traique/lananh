"""Cache ghi-xuyên (write-through) trong RAM cho các khoá settings đọc thường xuyên
mà hiếm khi đổi: model ưu tiên + Base URL của 9Router, công tắc Tavily, trần quota
thành viên Zalo, pairing Zoom (mỗi webhook Zoom), session/controller Zalo (mỗi tin
Zalo, mỗi lượt quét nền), con trỏ quét tín hiệu Zalo, mốc chạy của job dọn DB.

Bot chạy 1 worker uvicorn nên mọi thay đổi đi qua process này: ``set()`` ở đây, hoặc
``db.set_setting``/``db.compare_and_set_setting`` (core/database.py báo cho cache qua
``on_setting_write``, kể cả xoay khoá mã hoá) - cập nhật cache ngay khi ghi, không cần
TTL. Khoá đọc qua cache KHÔNG được ghi bằng đường khác (SQL trực tiếp, repository
settings) - nếu không cache sẽ cũ. Lỗi DB khi đọc KHÔNG bị cache (lần sau đọc lại).
Trần số khoá để không phình RAM.
"""

from __future__ import annotations

from core import database as db

MAX_KEYS = 64
_cache: dict[str, str | None] = {}
# Tăng mỗi lần có ghi: lượt đọc DB bắt đầu TRƯỚC 1 lần ghi không được nhét giá trị cũ
# (đọc được trước khi lần ghi commit) vào cache.
_writes = 0


async def get(key: str) -> str | None:
    if key in _cache:
        return _cache[key]
    writes = _writes
    value = await db.get_setting(key)
    if writes == _writes and len(_cache) < MAX_KEYS:
        _cache[key] = value
    return value


async def set(key: str, value: str) -> None:
    await db.set_setting(key, value)
    # Tự cập nhật (không chỉ dựa vào hook): test thay db.set_setting bằng bản giả.
    _remember(key, value)


def _remember(key: str, value: str) -> None:
    global _writes
    _writes += 1
    if key in _cache or len(_cache) < MAX_KEYS:
        _cache[key] = value


def _on_write(key: str, value: str | None) -> None:
    """Hook của db.set_setting: chỉ cập nhật khoá ĐANG được cache (khoá chỉ ghi, không
    đọc qua cache, không chiếm chỗ); value=None = ghi lỗi, không rõ DB đang giữ gì."""
    global _writes
    _writes += 1
    if value is None:
        _cache.pop(key, None)
    elif key in _cache:
        _cache[key] = value


db.on_setting_write(_on_write)


def clear() -> None:
    """Xoá toàn bộ cache (test dùng giữa các case; prod không cần)."""
    _cache.clear()
