"""Điều khiển lượt đang chạy: /huy và nhóm lệnh "chỉ đọc, không AI" được chạy
NGOÀI khoá lượt của người dùng.

- /huy: huỷ phần việc AI đang chạy của CHÍNH người gõ (services.concurrency.
  cancel_turn) mà không chờ khoá lượt. Telegram/Zoom xử lý ở webhook trước khi
  vào inbox (handlers/turn_feedback.py, web.py); Zalo xử lý ở bridge trước khi
  vào dispatch (channels/router.py) - gateway gửi /huy NGOÀI hàng đợi theo người
  gửi.
- Lệnh chỉ đọc (/help, /notes, /lich...): không gọi AI, không ghi dữ liệu nên
  chạy song song với lượt đang dở được - không bắt người dùng đợi lượt phân tích
  cổ phiếu 2-3 phút chỉ để xem /help.
"""

from __future__ import annotations

from core import address
from services import reply_audience

CANCEL_COMMANDS = frozenset({"/huy"})
# Lệnh chỉ đọc khi KHÔNG có tham số (có tham số thì ghi dữ liệu: /memory on|off,
# /nhac 8h mai..., /danhgia không có biến thể ghi; /an xem hôm nay, /noi xem chế độ).
_READONLY_BARE = frozenset({"/memory", "/nhac", "/yenlang", "/an", "/noi"})
# Lệnh chỉ đọc với mọi tham số (tính bằng code / đọc DB, không gọi AI).
_READONLY_ANY = frozenset(
    {
        "/help",
        "/start",
        "/notes",
        "/history",
        "/lich",
        "/tsh",
        "/tuvi",
        "/kymon",
        "/danhgia",
        "/chuy",
    }
)


def parse_command(text: str | None) -> tuple[str, str]:
    """("/lenh" viết thường, bỏ @tên_bot; phần tham số) - ("", "") nếu không phải lệnh."""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return "", ""
    head, _, rest = raw.partition(" ")
    return head.lower().split("@", 1)[0], rest.strip()


def is_cancel(text: str | None) -> bool:
    return parse_command(text)[0] in CANCEL_COMMANDS


def is_readonly(text: str | None) -> bool:
    """Lệnh không gọi AI và không ghi gì - chạy được ngoài khoá lượt."""
    command, argument = parse_command(text)
    if command in _READONLY_ANY:
        return True
    return command in _READONLY_BARE and not argument


def _forms(owner: bool) -> tuple[str, str]:
    """(cách gọi người nhận, Lan Anh tự xưng); thành viên chưa đặt cách gọi -> ("", "em")."""
    if owner:
        return "anh", "em"
    form = address.normalize(reply_audience.current().address_form)
    return (form, address.self_word(form)) if form else ("", "em")


def cancelled_text(*, owner: bool) -> str:
    """Câu xác nhận đã dừng lượt đang chạy (giọng Lan Anh)."""
    you, me = _forms(owner)
    tail = f" {you}" if you else ""
    return f"🫡 {me.capitalize()} dừng rồi nha{tail}. Cần gì cứ nhắn lại {me} nhé."


def nothing_to_cancel_text(*, owner: bool) -> str:
    you, me = _forms(owner)
    tail = f" {you}" if you else ""
    return f"{me.capitalize()} đang không làm dở việc gì để dừng cả nha{tail}."


async def cancelled_text_for(user_id: int) -> str:
    return cancelled_text(owner=await reply_audience.is_owner(user_id))


async def nothing_to_cancel_for(user_id: int) -> str:
    return nothing_to_cancel_text(owner=await reply_audience.is_owner(user_id))
