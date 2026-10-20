"""Deterministic controller commands for the Zalo group allowlist and summaries.

Dùng chung cho MỌI kênh (Zalo bridge, Zoom, Telegram) - không có logic riêng
theo kênh gọi tới, chỉ cần (account_id, text). Nơi gọi tự chịu trách nhiệm suy
ra account_id phù hợp: Zalo bridge có sẵn account_id trong payload
(channels/router.py), các kênh khác dùng
channels.zalo_repository.resolve_default_account_id() (xem web.py,
handlers/zalo_bridge_commands.py)."""

import asyncpg

from channels import zalo_repository
from channels.zalo_summary import resolve_window, summarize_group, today_discussion
from services.channel_result import ChannelResult


# /tongket on|off bật/tắt bản tổng kết tự động hằng ngày; vì vậy "on"/"off"
# không được dùng làm tên gợi nhớ nhóm.
_TOGGLE_WORDS = ("on", "off")


async def _toggle_daily(account_id: str, enabled: bool, args: list[str]) -> ChannelResult:
    target = " ".join(args).strip() or None
    if target is not None and target.lower() == "all":
        target = None
    count = await zalo_repository.set_daily_summary(account_id, target, enabled)
    state = "BẬT" if enabled else "TẮT"
    if target is None:
        if count == 0:
            return ChannelResult(["Chưa theo dõi nhóm nào để bật/tắt tổng kết."])
        return ChannelResult([f"✅ Đã {state} tổng kết hằng ngày cho {count} nhóm."])
    if count == 0:
        return ChannelResult([f"Không tìm thấy nhóm “{target}”."])
    note = "" if enabled else " Tin nhóm vẫn được lưu, /tongket thủ công vẫn dùng được."
    return ChannelResult([f"✅ Đã {state} tổng kết hằng ngày cho nhóm {target}.{note}"])


async def maybe_handle_group_command(account_id: str, text: str) -> ChannelResult | None:
    raw = text.strip()
    command = raw.split(maxsplit=1)[0].lower() if raw else ""

    if command == "/nhom":
        groups = await zalo_repository.list_groups_with_daily(account_id)
        if not groups:
            return ChannelResult(
                [
                    "Chưa theo dõi nhóm nào. Dùng /nhomzalo để xem ID, sau đó /themnhom <group_id> <tên-gợi-nhớ>."
                ]
            )
        lines = ["📚 Các nhóm đang theo dõi (🔔 có / 🔕 tắt tổng kết hằng ngày):"]
        lines.extend(
            f"{index}. {'🔔' if daily else '🔕'} {alias} — {group_id}"
            for index, (group_id, alias, daily) in enumerate(groups, 1)
        )
        lines.append("Bật/tắt: /tongket on|off [nhóm]")
        return ChannelResult(["\n".join(lines)])

    if command == "/themnhom":
        parts = raw.split(maxsplit=2)
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /themnhom <group_id> <tên-gợi-nhớ>"])
        group_id = parts[1].strip()
        alias = (parts[2].strip() if len(parts) == 3 else group_id).lower()
        if not group_id or not alias or len(alias) > 100 or alias in _TOGGLE_WORDS:
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
            return ChannelResult(
                [
                    "Cú pháp: /tongket <nhóm> [24h|7d|homnay|homqua]\n"
                    "Bật/tắt tổng kết tự động hằng ngày: /tongket on|off [nhóm] (bỏ trống = mọi nhóm)"
                ]
            )
        if parts[1].lower() in _TOGGLE_WORDS:
            return await _toggle_daily(account_id, parts[1].lower() == "on", parts[2:])
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
            return ChannelResult(
                ["Cú pháp: /dangnoi <nhóm> — xem nguyên văn thảo luận trong ngày hôm nay"]
            )
        target = parts[1].strip()
        try:
            _, _, contents = await today_discussion(account_id, target)
            return ChannelResult(contents)
        except ValueError as exc:
            return ChannelResult([str(exc)])

    return None
