"""Trạng thái và bộ đếm hoạt động của zalo-gateway cho trang /admin.

Đọc control server của gateway (chỉ bind 127.0.0.1:ZALO_CONTROL_PORT) qua
GET /status: trạng thái đăng nhập, bộ đếm từ lúc gateway khởi động (tin nhận,
lượt gộp, làn nhanh, đoạn gửi, rơi về chữ thuần, typing/đã xem/reaction, outbox
gửi/lỗi/hoãn vì hết ngân sách) và hàng đợi hiện tại. Bỏ mã QR - trang admin
không cần và không nên giữ nó.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

_TIMEOUT = httpx.Timeout(3.0, connect=1.0)
# Trường /status được đưa lên /admin (không có qr, cookie hay nội dung tin).
_FIELDS = ("state", "connected", "controllerPaired", "stats", "queue")


def _enabled() -> bool:
    """Như scripts/run-zalo-gateway.sh: gateway chỉ chạy khi ZALO_ENABLED bật rõ ràng."""
    return os.getenv("ZALO_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _control_url() -> str:
    raw = os.getenv("ZALO_CONTROL_PORT", "").strip() or "9901"
    port = int(raw) if raw.isdigit() else 9901
    return f"http://127.0.0.1:{port}/status"


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
