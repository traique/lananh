"""Callback gửi cảnh báo chủ bot (đăng ký vào core.alerts qua bot_app).

Trước đây cảnh báo chỉ đi Telegram DM: Telegram sập hoặc token bị thu hồi là
MỌI cảnh báo im lặng. Giờ Telegram vẫn là kênh chính, Zoom (nếu bật + đã pair)
làm kênh phụ theo ``ALERT_ZOOM_MIRROR``:

- ``critical`` (mặc định): gửi Zoom khi Telegram lỗi, và luôn nhân bản cảnh báo
  nghiêm trọng (mở đầu bằng 🚨) sang Zoom.
- ``fallback``: chỉ gửi Zoom khi Telegram lỗi.
- ``all``: nhân bản mọi cảnh báo sang Zoom.
- ``off``: không bao giờ dùng Zoom.

Logger riêng của module này được services.error_digest bỏ qua: lỗi khi gửi cảnh
báo không được sinh ra cảnh báo mới (vòng lặp khi Telegram sập).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from channels import zoom
from core import config
from core import database as db

logger = logging.getLogger(__name__)

CRITICAL_PREFIX = "🚨"
_MODES = ("off", "fallback", "critical", "all")


def mirror_mode() -> str:
    mode = os.getenv("ALERT_ZOOM_MIRROR", "critical").strip().lower()
    return mode if mode in _MODES else "critical"


def _wants_zoom(text: str, telegram_ok: bool, mode: str) -> bool:
    if mode == "off":
        return False
    if not telegram_ok or mode == "all":
        return True
    return mode == "critical" and text.lstrip().startswith(CRITICAL_PREFIX)


async def _send_zoom(text: str) -> bool:
    if not config.ZOOM_ENABLED:
        return False
    pairing = await db.zoom_get_pairing()
    if pairing is None:
        return False
    await zoom.send_message(to_jid=pairing[0], text=text)
    return True


def make_alert_callback(
    send_telegram: Callable[[str], Awaitable[None]],
) -> Callable[[str], Awaitable[None]]:
    """Bọc hàm gửi Telegram thành callback cảnh báo có Zoom dự phòng/nhân bản.
    Callback không bao giờ raise (core.alerts chạy nó fire-and-forget)."""

    async def alert(text: str) -> None:
        telegram_ok = True
        try:
            await send_telegram(text)
        except Exception:
            telegram_ok = False
            logger.warning("Không gửi được cảnh báo qua Telegram.", exc_info=True)
        if not _wants_zoom(text, telegram_ok, mirror_mode()):
            return
        try:
            sent = await _send_zoom(text)
        except Exception:
            logger.warning("Không gửi được cảnh báo qua Zoom.", exc_info=True)
            return
        if not sent and not telegram_ok:
            logger.warning("Cảnh báo chủ bot không tới được kênh nào (Zoom chưa bật/pair).")

    return alert


async def mirror_to_zoom(text: str) -> None:
    """Cảnh báo đã gửi Telegram theo đường riêng (vd tin kèm nút bấm): nhân bản
    sang Zoom theo đúng luật ALERT_ZOOM_MIRROR như ``alert``. Không raise."""
    if not _wants_zoom(text, True, mirror_mode()):
        return
    try:
        await _send_zoom(text)
    except Exception:
        logger.warning("Không gửi được cảnh báo qua Zoom.", exc_info=True)
