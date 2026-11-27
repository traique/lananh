"""Ghi DB "bắn rồi quên" có trần cho tầng ai/: dòng provider_calls (ai/call_log.py)
và trạng thái provider-chain (ai/provider_state.py). Lượt trả lời không còn chờ 1
vòng mạng Supabase (INSERT/UPDATE) trước khi trả câu trả lời cho người dùng.

Task bắt đầu chạy NGAY (eager_start): phần đồng bộ (vd lấy kết nối sẵn có của pool)
chạy luôn trong lúc gọi, chỉ phần chờ mạng nằm nền - DB giả trong RAM (test) ghi
xong ngay khi gọi. Số lượt ghi đang chờ có trần ``MAX_PENDING`` (DB chậm/sập thì bỏ
bớt, có log); lỗi của từng lượt chỉ log cảnh báo, không bao giờ văng lên lượt trả
lời. ``drain()`` chờ các lượt còn dở - lúc tắt app (trước khi đóng pool DB) và trong
test cần đọc lại dữ liệu vừa ghi.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Lượt chat bình thường chỉ có vài lượt ghi đang chờ; trần chặn phình RAM khi DB treo.
MAX_PENDING = 64
_pending: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], label: str) -> bool:
    """Chạy ``coro`` ở nền; False nếu bỏ (không có event loop hoặc quá MAX_PENDING)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        logger.warning("Bỏ ghi %s: không có event loop đang chạy.", label)
        return False
    if len(_pending) >= MAX_PENDING:
        coro.close()
        logger.warning("Bỏ ghi %s: đã có %d lượt ghi DB nền đang chờ.", label, MAX_PENDING)
        return False
    task = asyncio.Task(_guard(coro, label), loop=loop, eager_start=True)
    if not task.done():
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    return True


async def _guard(coro: Coroutine[Any, Any, Any], label: str) -> None:
    try:
        await coro
    except Exception:
        logger.warning("Ghi nền %s lỗi (bỏ qua).", label, exc_info=True)


def pending() -> int:
    return sum(1 for task in _pending if not task.done())


async def drain(limit_sec: float = 3.0) -> None:
    """Chờ các lượt ghi nền còn dở, tối đa ``limit_sec`` giây."""
    tasks = [task for task in _pending if not task.done()]
    if not tasks:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), limit_sec)
    except Exception:
        logger.info("Hết giờ chờ %d lượt ghi DB nền.", len(tasks))
