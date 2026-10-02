"""Concurrency boundaries shared by Telegram and Zalo.

The application is intentionally single-user. Serializing assistant turns keeps
chat history, tool side effects, and memory updates in the same order across all
input channels without blocking unrelated scheduler delivery work.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

_assistant_turn_lock = asyncio.Lock()
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
async def assistant_turn() -> AsyncIterator[None]:
    """Allow only one interactive Telegram/Zalo turn at a time."""
    async with _assistant_turn_lock:
        yield
