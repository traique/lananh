"""Concurrency boundaries shared by Telegram, Zalo and Zoom.

Mỗi người dùng có 1 lock riêng (``assistant_turn(key)``) để lịch sử chat, side
effect của tool và cập nhật trí nhớ CỦA NGƯỜI ĐÓ luôn theo đúng thứ tự. Người
dùng khác nhau chạy song song, nhưng tổng số lượt AI đang chạy bị chặn bởi
``MAX_CONCURRENT_TURNS`` (mặc định 2) để giữ RAM dưới 512 MB trên Render free.

Thứ tự lấy khoá: lock theo user TRƯỚC, semaphore toàn cục SAU - người đang
chờ lượt của chính mình không giữ chỗ trong semaphore.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

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
    try:
        async with lock:
            async with _turn_slots:
                yield
    finally:
        _, users = _turn_locks[key]
        if users == 1:
            del _turn_locks[key]
        else:
            _turn_locks[key] = (lock, users - 1)
