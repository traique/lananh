"""Lệnh trí nhớ dùng chung Telegram (handlers/commands.py) và Zalo/Zoom
(services/channel_command_service.py): /memory, /forget, /quen, /nho, /ghim,
/boghim, /notes, /xoaghichu, /xuatdulieu, /xoadulieu và /hoithan (hỏi thăm chủ
động, services/proactive_service.py). Handler chỉ parse và gửi; logic nằm ở
services/memory_control.py."""

from __future__ import annotations

from dataclasses import dataclass

from core.text_utils import parse_on_off
from services import memory_control, memory_service

COMMANDS = frozenset(
    {
        "/memory",
        "/forget",
        "/quen",
        "/nho",
        "/ghim",
        "/boghim",
        "/notes",
        "/xoaghichu",
        "/xuatdulieu",
        "/xoadulieu",
        "/hoithan",
    }
)


@dataclass(frozen=True)
class CommandReply:
    messages: list[str]
    # (nội dung, tên file) - chỉ Telegram gửi được file (/xuatdulieu).
    file: tuple[bytes, str] | None = None


async def handle(
    command: str,
    argument: str,
    user_id: int,
    *,
    channel: str,
    about: str = "",
) -> CommandReply | None:
    """None nếu không phải lệnh trí nhớ. ``about``: cách gọi người dùng trong
    câu trả lời ("anh" với chủ bot, cách xưng hô riêng của thành viên Zalo, ""
    = trung tính)."""
    if command not in COMMANDS:
        return None
    argument = (argument or "").strip()
    if command == "/memory":
        toggle = parse_on_off(argument)
        if toggle is True:
            await memory_service.set_enabled(user_id, True)
            return CommandReply(["✅ Đã bật trí nhớ dài hạn."])
        if toggle is False:
            await memory_service.set_enabled(user_id, False)
            return CommandReply(
                [
                    "🔴 Đã tắt trí nhớ dài hạn - em không trích/nhớ thêm gì mới cho tới khi "
                    "bật lại bằng /memory on."
                ]
            )
        return CommandReply([await memory_control.listing_text(user_id, about=about)])
    if command == "/forget":
        try:
            await memory_service.clear_memory(user_id)
        except memory_service.MemoryBusy:
            return CommandReply([memory_service.MEMORY_BUSY_TEXT])
        return CommandReply(["🗑️ Đã xoá toàn bộ trí nhớ dài hạn."])
    if command == "/quen":
        return CommandReply([await memory_control.forget_item(user_id, argument)])
    if command == "/nho":
        return CommandReply([await memory_control.remember(user_id, argument)])
    if command in ("/ghim", "/boghim"):
        pinned = command == "/ghim"
        return CommandReply([await memory_control.pin_item(user_id, argument, pinned)])
    if command == "/notes":
        return CommandReply([await memory_control.notes_text(user_id, argument)])
    if command == "/xoaghichu":
        return CommandReply([await memory_control.delete_note(user_id, argument)])
    if command == "/xuatdulieu":
        if channel == "telegram":
            raw, name = await memory_control.export_json_bytes(user_id)
            return CommandReply(["📦 Dữ liệu em đang lưu (file .json):"], file=(raw, name))
        return CommandReply([await memory_control.export_text(user_id)])
    if command == "/hoithan":
        from services import proactive_service

        return CommandReply([await proactive_service.command(user_id, argument)])
    # /xoadulieu
    return CommandReply([await memory_control.delete_own_data(user_id, argument)])
