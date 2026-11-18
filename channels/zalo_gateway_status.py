"""Trạng thái và bộ đếm hoạt động của zalo-gateway cho trang /admin.

Đọc control server của gateway (chỉ bind 127.0.0.1:ZALO_CONTROL_PORT) qua
GET /status: trạng thái đăng nhập, bộ đếm từ lúc gateway khởi động (tin nhận,
lượt gộp, làn nhanh, đoạn gửi, rơi về chữ thuần, typing/đã xem/reaction, outbox
gửi/lỗi/hoãn vì hết ngân sách) và hàng đợi hiện tại. Bỏ mã QR - trang admin
không cần và không nên giữ nó.

forget_sender(): báo gateway quên 1 người gửi khi pairing bị gỡ (/zaloxoa,
channels/zalo_users.remove) qua POST /known/forget.

list_groups(): mọi nhóm tài khoản bot đang tham gia (GET /groups) cho lệnh
/nhomzalo (channels/zalo_group_list.py).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(3.0, connect=1.0)
# Trường /status được đưa lên /admin (không có qr, cookie hay nội dung tin).
_FIELDS = ("state", "connected", "controllerPaired", "stats", "queue")
# Route control server của gateway (zalo-gateway/src/control-server.ts).
FORGET_PATH = "/known/forget"
GROUPS_PATH = "/groups"
# Gateway gọi Zalo getAllGroups + getGroupInfo theo lô 20 nhóm (tối đa 200 nhóm, thường
# vài giây) và tự bỏ chờ sau 25s. Trần ngắn vì lệnh chạy trong khoá lượt, và Telegram xử
# lý tin tuần tự: chờ lâu chặn mọi lệnh khác của chủ bot.
_GROUPS_TIMEOUT = httpx.Timeout(30.0, connect=1.0)


@dataclass(frozen=True)
class ZaloGroup:
    group_id: str
    name: str
    members: int | None = None


class GatewayGroupsError(RuntimeError):
    """Không lấy được danh sách nhóm; ``str(exc)`` là câu báo cho người dùng."""


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


def _parse_groups(data: Any) -> tuple[list[ZaloGroup], int]:
    rows = data.get("groups") if isinstance(data, dict) else None
    groups: list[ZaloGroup] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        group_id = str(row.get("id") or "").strip()
        if not group_id:
            continue
        members = row.get("members")
        groups.append(
            ZaloGroup(
                group_id=group_id,
                name=str(row.get("name") or "").strip()[:120],
                members=members if type(members) is int and members >= 0 else None,
            )
        )
    total = data.get("total") if isinstance(data, dict) else None
    return groups, total if type(total) is int and total >= len(groups) else len(groups)


async def list_groups() -> tuple[list[ZaloGroup], int]:
    """(nhóm, tổng số nhóm thật) - gateway trả tối đa 200 nhóm, tổng có thể lớn hơn.
    Raise GatewayGroupsError kèm câu báo khi gateway tắt, không trả lời hoặc Zalo
    chưa đăng nhập."""
    if not _enabled():
        raise GatewayGroupsError("Gateway Zalo đang tắt (ZALO_ENABLED) nên chưa xem được nhóm.")
    try:
        async with httpx.AsyncClient(timeout=_GROUPS_TIMEOUT) as client:
            response = await client.get(f"{_control_base()}{GROUPS_PATH}")
    except httpx.HTTPError as exc:
        logger.warning("Không lấy được danh sách nhóm từ gateway Zalo: %r", exc)
        raise GatewayGroupsError("Không kết nối được gateway Zalo, thử lại sau ít phút.") from exc
    if response.status_code == 409:
        raise GatewayGroupsError(
            "Zalo bot chưa đăng nhập hoặc đang mất kết nối nên chưa xem được nhóm."
        )
    if response.status_code != 200:
        logger.warning("Gateway Zalo trả HTTP %s cho %s", response.status_code, GROUPS_PATH)
        raise GatewayGroupsError(
            f"Zalo trả lỗi khi lấy danh sách nhóm (HTTP {response.status_code}), thử lại sau."
        )
    try:
        return _parse_groups(response.json())
    except ValueError as exc:
        raise GatewayGroupsError("Gateway Zalo trả dữ liệu nhóm không hợp lệ.") from exc
