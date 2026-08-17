"""Concurrency boundaries shared by Telegram and Zalo.

The application is intentionally single-user. Serializing assistant turns keeps
chat history, tool side effects, and memory updates in the same order across all
input channels without blocking unrelated scheduler delivery work.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

_assistant_turn_lock = asyncio.Lock()


@asynccontextmanager
async def assistant_turn() -> AsyncIterator[None]:
    """Allow only one interactive Telegram/Zalo turn at a time."""
    async with _assistant_turn_lock:
        yield
