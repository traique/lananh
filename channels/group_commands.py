"""Deterministic controller commands for the Zalo group allowlist and summaries.

Dùng chung cho MỌI kênh (Zalo bridge, Zoom, Telegram) - không có logic riêng
theo kênh gọi tới, chỉ cần (account_id, text). Nơi gọi tự chịu trách nhiệm suy
ra account_id phù hợp: Zalo bridge có sẵn account_id trong payload
(channels/router.py), các kênh khác dùng
channels.zalo_repository.resolve_default_account_id() (xem web.py, handlers/commands.py)."""
import asyncpg

from channels import zalo_repository
from channels.zalo_summary import resolve_window, summarize_group, today_discussion
from services.channel_result import ChannelResult


async def maybe_handle_group_command(account_id: str, text: str) -> ChannelResult | None:
    raw = text.strip()
    command = raw.split(maxsplit=1)[0].lower() if raw else ""

    if command == "/nhom":
        groups = await zalo_repository.list_groups(account_id)
        if not groups:
            return ChannelResult(["Chưa theo dõi nhóm nào. Dùng /nhomzalo để xem ID, sau đó /themnhom <group_id> <tên-gợi-nhớ>."])
        lines = ["📚 Các nhóm đang theo dõi:"]
        lines.extend(f"{index}. {alias} — {group_id}" for index, (group_id, alias) in enumerate(groups, 1))
        return ChannelResult(["\n".join(lines)])

    if command == "/themnhom":
        parts = raw.split(maxsplit=2)
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /themnhom <group_id> <tên-gợi-nhớ>"])
        group_id = parts[1].strip()
        alias = (parts[2].strip() if len(parts) == 3 else group_id).lower()
        if not group_id or not alias or len(alias) > 100:
            return ChannelResult(["Group ID hoặc tên gợi nhớ không hợp lệ."])
        try:
            await zalo_repository.add_group(account_id, group_id, alias)
        except asyncpg.UniqueViolationError:
            return ChannelResult([f"Tên gợi nhớ “{alias}” đang được dùng cho nhóm khác."])
        return ChannelResult([f"✅ Đã thêm nhóm {alias} ({group_id})."])

    if command == "/xoanhom":
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /xoanhom <group_id hoặc tên-gợi-nhớ>"])
        target = parts[1].strip()
        removed = await zalo_repository.remove_group(account_id, target)
        if not removed:
            return ChannelResult([f"Không tìm thấy nhóm “{target}”."])
        return ChannelResult([f"✅ Đã ngừng theo dõi và xóa dữ liệu đã lưu của nhóm {target}."])

    if command == "/tongket":
        parts = raw.split()
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /tongket <nhóm> [24h|7d|homnay|homqua]"])
        target = parts[1]
        spec = parts[2] if len(parts) >= 3 else "24h"
        start, end = resolve_window(spec)
        try:
            _, _, content = await summarize_group(account_id, target, start, end)
            return ChannelResult([content])
        except ValueError as exc:
            return ChannelResult([str(exc)])

    if command == "/dangnoi":
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /dangnoi <nhóm> — xem nguyên văn thảo luận trong ngày hôm nay"])
        target = parts[1].strip()
        try:
            _, _, contents = await today_discussion(account_id, target)
            return ChannelResult(contents)
        except ValueError as exc:
            return ChannelResult([str(exc)])

    return None
