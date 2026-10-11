"""Kênh cảnh báo chủ bot dùng chung cho tầng core/ai (không phụ thuộc Telegram).

Tầng khởi tạo bot đăng ký 1 callback async nhận text (bot_app gọi
``orchestrator.set_alert_callback`` -> ``ai.provider_state.set_alert_callback``
-> ``set_callback`` ở đây). Các module tầng thấp (webhook inbox, migrations)
chỉ cần gọi ``send(text)``: fire-and-forget, không bao giờ raise, không chặn
luồng chính. Số tác vụ gửi đang chờ có trần để không phình RAM khi Telegram
lỗi kéo dài.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Trần số cảnh báo đang gửi dở cùng lúc - vượt thì bỏ (đã log), tránh dồn
# hàng nghìn task khi Telegram sập mà sự kiện lỗi vẫn tới liên tục.
MAX_PENDING = 32

_callback: Callable[[str], Awaitable[None]] | None = None
# bot_app._post_shutdown drain đúng set này (qua ai.provider_state._background_tasks).
tasks: set[asyncio.Task] = set()


def set_callback(fn: Callable[[str], Awaitable[None]] | None) -> None:
    global _callback
    _callback = fn


def send(text: str) -> bool:
    """Gửi cảnh báo cho chủ bot ở nền. Trả False nếu chưa có callback/quá tải."""
    if _callback is None:
        logger.info("Chưa đăng ký kênh cảnh báo chủ bot, bỏ qua: %.120s", text)
        return False
    if len(tasks) >= MAX_PENDING:
        logger.warning("Quá nhiều cảnh báo đang chờ gửi, bỏ qua: %.120s", text)
        return False
    try:
        task = asyncio.create_task(_callback(text))
    except Exception:
        logger.warning("Không gửi được cảnh báo chủ bot.", exc_info=True)
        return False
    tasks.add(task)
    task.add_done_callback(_done)
    return True


def _done(task: asyncio.Task) -> None:
    tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Gửi cảnh báo chủ bot lỗi: %r", exc)
