"""Application-level timeout helpers for official Google AI calls.

SDK/network defaults are not sufficient for the provider chain because a hung
request can hold the global orchestration lock and block Telegram and Zalo.
"""
import asyncio
import os
from collections.abc import Awaitable
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
OFFICIAL_EMBED_TIMEOUT_SEC = _env_timeout("GEMINI_API_EMBED_TIMEOUT_SEC", 30.0)
OFFICIAL_STATUS_TIMEOUT_SEC = _env_timeout("GEMINI_API_STATUS_TIMEOUT_SEC", 20.0)


async def with_timeout(awaitable: Awaitable[T], timeout_sec: float, operation: str) -> T:
    """Await an SDK call with a bounded application-level deadline."""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_sec)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"{operation} timed out after {timeout_sec:g}s") from exc
