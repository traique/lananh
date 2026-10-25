"""Cache NGẮN trong RAM kết quả đã sinh của 1 sự kiện webhook.

Webhook inbox thử lại sự kiện lỗi (tối đa WEBHOOK_MAX_ATTEMPTS lần trong ~11
phút). Nếu lỗi xảy ra ở bước GỬI sau khi câu trả lời đã sinh xong (/rag,
/agent, /dich), lần thử lại trước đây gọi LLM lại từ đầu: tốn quota và có thể
gửi 1 câu trả lời khác hẳn, rất muộn. Cache theo khoá sự kiện
(services.reminder_delivery.current_event_key) để lần thử lại chỉ GỬI LẠI.

Giới hạn bộ nhớ: tối đa MAX_ENTRIES mục, mỗi mục sống TTL_SEC giây, giá trị quá
MAX_VALUE_CHARS ký tự thì không cache. Đủ vì các lần thử lại xong trong ~11
phút và chạy trên cùng process (1 worker).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from services.reminder_delivery import current_event_key

T = TypeVar("T")

MAX_ENTRIES = 32
TTL_SEC = 15 * 60
MAX_VALUE_CHARS = 32_000

_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()


def _size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (tuple, list)):
        return sum(_size(item) for item in value)
    return len(str(value))


def _purge(now: float) -> None:
    for key in [key for key, (saved, _) in _cache.items() if now - saved > TTL_SEC]:
        _cache.pop(key, None)
    while len(_cache) > MAX_ENTRIES:
        _cache.popitem(last=False)


def get(key: str) -> Any | None:
    now = time.monotonic()
    _purge(now)
    entry = _cache.get(key)
    return None if entry is None else entry[1]


def put(key: str, value: Any) -> None:
    if _size(value) > MAX_VALUE_CHARS:
        return
    now = time.monotonic()
    _cache[key] = (now, value)
    _cache.move_to_end(key)
    _purge(now)


def clear() -> None:
    _cache.clear()


async def generate_once(scope: str, generate: Callable[[], Awaitable[T]]) -> T:
    """Chạy ``generate()`` 1 lần cho mỗi sự kiện webhook; lần thử lại của CÙNG
    sự kiện nhận lại kết quả cũ. Ngoài inbox (không có khoá sự kiện) luôn chạy."""
    event_key = current_event_key()
    if not event_key:
        return await generate()
    key = f"{scope}:{event_key}"
    cached = get(key)
    if cached is not None:
        return cached
    value = await generate()
    if value:
        put(key, value)
    return value
