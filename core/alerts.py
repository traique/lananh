"""Kênh cảnh báo chủ bot dùng chung cho tầng core/ai (không phụ thuộc Telegram).

Tầng khởi tạo bot đăng ký 1 callback async nhận text (bot_app gọi
``orchestrator.set_alert_callback`` -> ``ai.provider_state.set_alert_callback``
-> ``set_callback`` ở đây). Các module tầng thấp (webhook inbox, migrations)
chỉ cần gọi ``send(text)``: fire-and-forget, không bao giờ raise, không chặn
luồng chính. Số tác vụ gửi đang chờ có trần để không phình RAM khi Telegram
lỗi kéo dài.

``send(text, key=...)``: cảnh báo cùng ``key`` chỉ gửi 1 lần trong ``cooldown``
giây (chống spam khi 1 sự cố kéo dài, vd watchdog RAM/DB/lỗi lặp lại). Bảng
key -> lần gửi gần nhất nằm trong RAM, có trần ``MAX_KEYS`` (bỏ key cũ nhất);
restart thì reset - dedup cần sống qua restart (theo ngày) tự lưu ở settings.

Mức thông báo (services/notify_hub.py đăng ký ``set_router`` lúc khởi động):
cảnh báo mở đầu bằng 🚨 hoặc ``urgent=True`` gửi ngay; còn lại là cảnh báo nhẹ,
được hub gom vào 1 tin theo lượt xả. Chưa có router (NOTIFY_HUB_ENABLED=0, test)
thì mọi cảnh báo đi thẳng callback như cũ. ``urgent=False`` ép gom dù có 🚨.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Trần số cảnh báo đang gửi dở cùng lúc - vượt thì bỏ (đã log), tránh dồn
# hàng nghìn task khi Telegram sập mà sự kiện lỗi vẫn tới liên tục.
MAX_PENDING = 32
# Trần số key chống trùng nhớ trong RAM và thời gian chờ mặc định giữa 2 lần
# gửi cùng key.
MAX_KEYS = 128
DEFAULT_COOLDOWN_SEC = 3600.0

_callback: Callable[[str], Awaitable[None]] | None = None
# (text, urgent) -> gửi ngay hoặc xếp hàng gom; None = gửi thẳng _callback.
_router: Callable[[str, bool | None], Awaitable[None]] | None = None
_last_sent: OrderedDict[str, float] = OrderedDict()
# bot_app._post_shutdown drain đúng set này (qua ai.provider_state._background_tasks).
tasks: set[asyncio.Task] = set()


def set_callback(fn: Callable[[str], Awaitable[None]] | None) -> None:
    global _callback
    _callback = fn


def set_router(fn: Callable[[str, bool | None], Awaitable[None]] | None) -> None:
    global _router
    _router = fn


async def deliver_now(text: str) -> None:
    """Gửi thẳng qua callback (router dùng cho cảnh báo khẩn); chưa có callback thì bỏ."""
    if _callback is not None:
        await _callback(text)


def in_cooldown(key: str, cooldown: float = DEFAULT_COOLDOWN_SEC) -> bool:
    """True nếu cảnh báo ``key`` vừa được gửi trong ``cooldown`` giây gần đây."""
    last = _last_sent.get(key)
    return last is not None and time.monotonic() - last < cooldown


def _remember(key: str) -> None:
    _last_sent[key] = time.monotonic()
    _last_sent.move_to_end(key)
    while len(_last_sent) > MAX_KEYS:
        _last_sent.popitem(last=False)


def send(
    text: str,
    *,
    key: str | None = None,
    cooldown: float = DEFAULT_COOLDOWN_SEC,
    urgent: bool | None = None,
) -> bool:
    """Gửi cảnh báo cho chủ bot ở nền. Trả False nếu chưa có callback/quá tải,
    hoặc cùng ``key`` đã gửi trong ``cooldown`` giây gần đây. ``urgent``: None =
    theo tiền tố 🚨 (xem docstring module)."""
    if _callback is None:
        logger.info("Chưa đăng ký kênh cảnh báo chủ bot, bỏ qua: %.120s", text)
        return False
    if key is not None and in_cooldown(key, cooldown):
        logger.info("Cảnh báo %s vừa gửi gần đây, bỏ qua bản trùng.", key)
        return False
    if len(tasks) >= MAX_PENDING:
        logger.warning("Quá nhiều cảnh báo đang chờ gửi, bỏ qua: %.120s", text)
        return False
    try:
        coro = _router(text, urgent) if _router is not None else _callback(text)
        task = asyncio.create_task(coro)
    except Exception:
        logger.warning("Không gửi được cảnh báo chủ bot.", exc_info=True)
        return False
    tasks.add(task)
    task.add_done_callback(_done)
    if key is not None:
        _remember(key)
    return True


def _done(task: asyncio.Task) -> None:
    tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Gửi cảnh báo chủ bot lỗi: %r", exc)
