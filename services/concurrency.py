"""Concurrency boundaries shared by Telegram, Zalo and Zoom.

Mỗi người dùng có 1 lock riêng (``assistant_turn(key)``) để lịch sử chat, side
effect của tool và cập nhật trí nhớ CỦA NGƯỜI ĐÓ luôn theo đúng thứ tự. Người
dùng khác nhau chạy song song, nhưng tổng số lượt AI đang chạy bị chặn bởi
``MAX_CONCURRENT_TURNS`` (mặc định 2) để giữ RAM dưới 512 MB trên Render free.

Thứ tự lấy khoá: lock theo user TRƯỚC, semaphore toàn cục SAU - người đang
chờ lượt của chính mình không giữ chỗ trong semaphore.

/huy: phần việc AI của 1 lượt chạy trong 1 task CON đăng ký theo khoá người
dùng (``run_cancellable``); ``cancel_turn(key)`` chỉ huỷ task con đó. Không
bao giờ huỷ task giao sự kiện của webhook inbox (bị coi là mất lease -> chạy
lại lượt sau 5 phút) hay task request FastAPI của bridge Zalo (thành lỗi 500,
gateway gửi câu báo lỗi): handler bắt ``TurnCancelled``, trả lời "em dừng rồi"
và kết thúc bình thường để inbox finish()/cache idempotency vẫn được ghi.
"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import AsyncIterator, TypeVar

from core import trace

T = TypeVar("T")

OWNER_TURN_KEY = "owner"


def _max_concurrent_turns() -> int:
    try:
        return max(1, int(os.getenv("MAX_CONCURRENT_TURNS", "2").strip()))
    except ValueError:
        return 2


_turn_slots = asyncio.Semaphore(_max_concurrent_turns())
_turn_locks: dict[str, tuple[asyncio.Lock, int]] = {}
_message_locks: dict[tuple[str, str, str], tuple[asyncio.Lock, int]] = {}


@asynccontextmanager
async def channel_message_turn(account_id: str, message_id: str, kind: str):
    """Keep execution and response persistence in one boundary per event."""
    key = (account_id, message_id, kind)
    lock, users = _message_locks.get(key, (asyncio.Lock(), 0))
    _message_locks[key] = (lock, users + 1)
    try:
        async with lock:
            yield
    finally:
        _, users = _message_locks[key]
        if users == 1:
            del _message_locks[key]
        else:
            _message_locks[key] = (lock, users - 1)


@asynccontextmanager
async def assistant_turn(key: str = OWNER_TURN_KEY) -> AsyncIterator[None]:
    """Serialize turns of ONE user; different users run in parallel (bounded)."""
    lock, users = _turn_locks.get(key, (asyncio.Lock(), 0))
    _turn_locks[key] = (lock, users + 1)
    started = time.monotonic()
    try:
        async with lock:
            locked_at = time.monotonic()
            async with _turn_slots:
                # Thời gian chờ khoá của chính người này / chờ slot chung
                # (turn_metrics của lượt - core/trace.py).
                trace.mark_locked(_ms(locked_at - started), _ms(time.monotonic() - locked_at))
                yield
    finally:
        _, users = _turn_locks[key]
        if users == 1:
            del _turn_locks[key]
        else:
            _turn_locks[key] = (lock, users - 1)


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def is_busy(key: str = OWNER_TURN_KEY) -> bool:
    """Có lượt nào của người này đang chạy/chờ không (không chặn, không lấy khoá) -
    tin chủ động (services/proactive_service) bỏ qua lượt đó thay vì chen vào."""
    return key in _turn_locks


# ─── /huy: huỷ phần việc AI đang chạy của 1 người ───────────────────────────
# Chỉ giữ khoá CÒN chạy (xoá ngay khi task con xong) -> tối đa vài phần tử.
_running: dict[str, asyncio.Task] = {}
# Task con bị huỷ bởi cancel_turn (phân biệt với huỷ do shutdown/mất lease).
_cancel_requested: set[asyncio.Task] = set()


class TurnCancelled(Exception):
    """Người dùng gõ /huy trong lúc lượt đang chạy - handler trả lời "em dừng rồi"."""


async def run_cancellable(key: str, factory: Callable[[], Awaitable[T]]) -> T:
    """Chạy ``factory()`` trong 1 task con đăng ký dưới ``key`` để /huy huỷ được.

    Task con thừa hưởng ContextVar của lượt (hạn chót, người nhận, kênh nhắc
    việc...). /huy -> raise TurnCancelled. Task CHA bị huỷ (shutdown, mất lease
    inbox) thì huỷ cả task con và CancelledError đi tiếp như thường."""

    async def _run() -> T:
        return await factory()

    task = asyncio.create_task(_run())
    previous = _running.get(key)
    _running[key] = task
    try:
        return await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if task in _cancel_requested and not (current is not None and current.cancelling()):
            trace.set_outcome("cancelled")
            raise TurnCancelled() from None
        raise
    finally:
        _cancel_requested.discard(task)
        if _running.get(key) is task:
            if previous is not None and not previous.done():
                _running[key] = previous
            else:
                del _running[key]


def cancel_turn(key: str) -> bool:
    """Huỷ phần việc AI đang chạy dưới ``key``; True nếu có việc để huỷ.
    Không lấy khoá lượt (gọi được trong lúc lượt đó đang giữ khoá)."""
    task = _running.get(key)
    if task is None or task.done():
        return False
    _cancel_requested.add(task)
    task.cancel()
    return True


def has_running(key: str) -> bool:
    task = _running.get(key)
    return task is not None and not task.done()
