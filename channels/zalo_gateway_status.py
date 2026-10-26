"""Trạng thái và bộ đếm hoạt động của zalo-gateway cho trang /admin.

Đọc control server của gateway (chỉ bind 127.0.0.1:ZALO_CONTROL_PORT) qua
GET /status: trạng thái đăng nhập, bộ đếm từ lúc gateway khởi động (tin nhận,
lượt gộp, làn nhanh, đoạn gửi, rơi về chữ thuần, typing/đã xem/reaction, outbox
gửi/lỗi/hoãn vì hết ngân sách) và hàng đợi hiện tại. Bỏ mã QR - trang admin
không cần và không nên giữ nó.

forget_sender(): báo gateway quên 1 người gửi khi pairing bị gỡ (/zaloxoa,
channels/zalo_users.remove) qua POST /known/forget.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(3.0, connect=1.0)
# Trường /status được đưa lên /admin (không có qr, cookie hay nội dung tin).
_FIELDS = ("state", "connected", "controllerPaired", "stats", "queue")
# Route control server của gateway (zalo-gateway/src/control-server.ts).
FORGET_PATH = "/known/forget"


def _enabled() -> bool:
    """Như scripts/run-zalo-gateway.sh: gateway chỉ chạy khi ZALO_ENABLED bật rõ ràng."""
    return os.getenv("ZALO_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _control_base() -> str:
    raw = os.getenv("ZALO_CONTROL_PORT", "").strip() or "9901"
    port = int(raw) if raw.isdigit() else 9901
    return f"http://127.0.0.1:{port}"


def _control_url() -> str:
    return f"{_control_base()}/status"


async def _fetch_status() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(_control_url())
        response.raise_for_status()
        return response.json()


async def snapshot() -> dict[str, Any]:
    """{"enabled": False} khi tắt gateway; {"reachable": False} khi gateway chưa
    chạy/không trả lời; ngược lại các trường trong _FIELDS."""
    if not _enabled():
        return {"enabled": False}
    try:
        status = await _fetch_status()
    except (httpx.HTTPError, ValueError):
        return {"enabled": True, "reachable": False}
    data = {key: status.get(key) for key in _FIELDS if key in status}
    return {"enabled": True, "reachable": True, **data}


async def forget_sender(external_id: str) -> bool:
    """Báo gateway quên người gửi vừa bị gỡ pairing: từ tin kế tiếp, người này
    không còn nhận "đã xem", typing, reaction hay câu báo bận/sự cố của gateway
    (gateway gửi những thứ đó TRƯỚC khi hỏi Python). Best-effort: gateway tắt hoặc
    không trả lời thì chỉ log và trả False - gateway vẫn tự quên khi Python trả
    paired=False cho tin đầu tiên của người này."""
    if not _enabled() or not external_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{_control_base()}{FORGET_PATH}", json={"id": external_id}
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Không báo được gateway Zalo quên người gửi đã gỡ pairing: %s", exc)
        return False
    return True
