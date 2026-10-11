"""Application-level timeout helpers for official Google AI calls.

SDK/network defaults are not sufficient for the provider chain because a hung
request can hold the global orchestration lock and block Telegram and Zalo.
"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar

T = TypeVar("T")


def _env_timeout(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


OFFICIAL_CHAT_TIMEOUT_SEC = _env_timeout("GEMINI_API_CALL_TIMEOUT_SEC", 90.0)
OFFICIAL_VISION_TIMEOUT_SEC = _env_timeout("GEMINI_API_VISION_TIMEOUT_SEC", 120.0)
OFFICIAL_UTILITY_TIMEOUT_SEC = _env_timeout("GEMINI_API_UTILITY_TIMEOUT_SEC", 45.0)
OFFICIAL_STATUS_TIMEOUT_SEC = _env_timeout("GEMINI_API_STATUS_TIMEOUT_SEC", 20.0)


async def with_timeout(awaitable: Awaitable[T], timeout_sec: float, operation: str) -> T:
    """Await an SDK call with a bounded application-level deadline."""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_sec)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"{operation} timed out after {timeout_sec:g}s") from exc


# ─── Hạn chót cho cả 1 lượt trả lời (nhiều provider/bước cộng dồn) ───────────
# Đặt bằng ``turn_deadline()`` (lồng nhau thì giữ hạn SỚM hơn). Mọi lượt gọi
# provider đi qua ``bounded()`` để không vượt phần thời gian còn lại; hết hạn
# thì raise TurnTimeoutError (không thử provider kế tiếp nữa) để caller trả lỗi
# rõ ràng và nhả slot MAX_CONCURRENT_TURNS ngay.
_turn_deadline: ContextVar[float | None] = ContextVar("turn_deadline", default=None)
# Biên nhỏ coi như "đã hết hạn" khi so thời gian còn lại.
_DEADLINE_EPSILON_SEC = 0.05


class TurnTimeoutError(RuntimeError):
    """Hết ngân sách thời gian của cả lượt trả lời (CHAT_TURN_TIMEOUT_SEC)."""


def _default_turn_seconds() -> float:
    from core import config

    return float(config.CHAT_TURN_TIMEOUT_SEC)


@contextmanager
def turn_deadline(seconds: float | None = None) -> Iterator[None]:
    """Đặt hạn chót cho lượt hiện tại (mặc định CHAT_TURN_TIMEOUT_SEC).

    ContextVar: task con (asyncio.gather/create_task) tạo SAU khi vào khối này
    thừa hưởng cùng hạn chót."""
    seconds = _default_turn_seconds() if seconds is None else seconds
    new_deadline = time.monotonic() + max(0.0, seconds)
    current = _turn_deadline.get()
    token = _turn_deadline.set(new_deadline if current is None else min(current, new_deadline))
    try:
        yield
    finally:
        _turn_deadline.reset(token)


def remaining_turn_seconds() -> float | None:
    """Số giây còn lại của lượt hiện tại; None nếu không có hạn chót."""
    deadline = _turn_deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def _turn_timeout_error() -> TurnTimeoutError:
    import messages

    return TurnTimeoutError(
        messages.CHAT_TURN_TIMEOUT_ERROR.format(seconds=int(_default_turn_seconds()))
    )


async def bounded(call: Callable[[], Awaitable[T]], own_timeout: float | None = None) -> T:
    """Chạy ``call()`` trong min(own_timeout, thời gian còn lại của lượt).

    Hết own_timeout -> TimeoutError thường (provider chậm, caller tự chuyển
    provider kế tiếp); hết ngân sách lượt -> TurnTimeoutError."""
    remaining = remaining_turn_seconds()
    if remaining is not None and remaining <= _DEADLINE_EPSILON_SEC:
        raise _turn_timeout_error()
    if remaining is None or (own_timeout is not None and own_timeout <= remaining):
        if own_timeout is None:
            return await call()
        return await asyncio.wait_for(call(), timeout=own_timeout)
    try:
        return await asyncio.wait_for(call(), timeout=remaining)
    except TimeoutError as exc:
        left = remaining_turn_seconds()
        if left is not None and left <= _DEADLINE_EPSILON_SEC:
            raise _turn_timeout_error() from exc
        raise
