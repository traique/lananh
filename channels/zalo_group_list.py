"""/nhomzalo: mọi nhóm Zalo tài khoản bot đang tham gia, kèm group ID - để thêm
nhóm mà không phải tự đi tìm group ID.

- Danh sách lấy thẳng từ gateway (GET /groups, channels/zalo_gateway_status.py),
  dùng được từ Telegram, Zoom và Zalo (admin). Trước đây gateway tự trả lời
  /nhomzalo và chỉ cho chủ bot gõ trên Zalo.
- Đánh dấu nhóm đang theo dõi /tongket (✅) và nhóm nguồn Facebook (📣); lọc theo
  tên không phân biệt dấu (/nhomzalo gia dinh).
- Danh sách vừa hiện được lưu (setting, ``_PICK_TTL_SEC``) để /themnhom và
  /fb_themnhom chọn nhóm theo SỐ THỨ TỰ thay vì chép group ID. Số 1-3 chữ số luôn
  là số thứ tự: group ID Zalo thật dài 16-19 chữ số.
"""

from __future__ import annotations

import json
import logging
import re
import time

from channels import facebook_repository, zalo_gateway_status, zalo_repository
from core import database as db
from core.text_normalize import fold
from services.channel_result import ChannelResult

logger = logging.getLogger(__name__)

_PICK_KEY = "zalo:group_pick:{}"
_PICK_TTL_SEC = 60 * 60
_INDEX_RE = re.compile(r"\d{1,3}")
_UNNAMED = "(không rõ tên)"


class PickError(ValueError):
    """Số thứ tự không dùng được; ``str(exc)`` là câu báo cho người dùng."""


def default_alias(name: str, group_id: str) -> str:
    """Tên gợi nhớ khi /themnhom <số> không kèm tên: tên nhóm bỏ dấu, nối bằng "-"
    (1 từ - /tongket <tên> chỉ đọc 1 từ); tên rỗng/toàn emoji thì dùng group ID."""
    slug = re.sub(r"[^a-z0-9]+", "-", fold(name)).strip("-")[:40].strip("-")
    if slug in {"on", "off"}:  # từ khoá của /tongket on|off
        slug = f"nhom-{slug}"
    return slug or group_id


async def _save_pick(account_id: str, groups: list[zalo_gateway_status.ZaloGroup]) -> None:
    payload = {"at": time.time(), "groups": [[g.group_id, g.name] for g in groups]}
    try:
        await db.set_setting(_PICK_KEY.format(account_id), json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.warning("Không lưu được danh sách /nhomzalo để chọn theo số.", exc_info=True)


async def _load_pick(account_id: str) -> list[tuple[str, str]] | None:
    """Danh sách /nhomzalo gần nhất còn hạn, None nếu chưa có/quá hạn/hỏng."""
    try:
        data = json.loads(await db.get_setting(_PICK_KEY.format(account_id)) or "null")
    except Exception:
        logger.warning("Không đọc được danh sách /nhomzalo đã lưu.", exc_info=True)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return None
    saved_at = data.get("at")
    if not isinstance(saved_at, int | float) or time.time() - saved_at > _PICK_TTL_SEC:
        return None
    return [
        (str(row[0]), str(row[1]))
        for row in data["groups"]
        if isinstance(row, list) and len(row) == 2 and row[0]
    ]


async def resolve_group_arg(account_id: str, token: str) -> tuple[str, str]:
    """(group_id, tên nhóm). ``token`` 1-3 chữ số là số thứ tự trong danh sách
    /nhomzalo gần nhất; còn lại là group ID (tên nhóm để trống). Raise PickError
    kèm câu báo khi chưa xem danh sách, danh sách quá hạn hoặc số ngoài khoảng."""
    if not _INDEX_RE.fullmatch(token):
        return token, ""
    picked = await _load_pick(account_id)
    if not picked:
        minutes = _PICK_TTL_SEC // 60
        raise PickError(
            f"Số {token} là số thứ tự trong danh sách /nhomzalo. Gõ /nhomzalo để xem "
            f"danh sách (dùng được {minutes} phút) rồi chọn số."
        )
    index = int(token)
    if not 1 <= index <= len(picked):
        raise PickError(
            f"Danh sách /nhomzalo gần nhất chỉ có {len(picked)} nhóm (số 1 đến {len(picked)})."
        )
    return picked[index - 1]


async def render(account_id: str, keyword: str = "") -> ChannelResult:
    try:
        groups, total = await zalo_gateway_status.list_groups()
    except zalo_gateway_status.GatewayGroupsError as exc:
        return ChannelResult([str(exc)])
    if not groups:
        return ChannelResult(["Tài khoản Zalo của bot chưa tham gia nhóm nào."])
    followed = {
        gid: alias for gid, alias, _ in await zalo_repository.list_groups_with_daily(account_id)
    }
    sources = dict(await facebook_repository.list_groups(account_id))
    groups = sorted(groups, key=lambda g: (not g.name, fold(g.name), g.group_id))
    needle = fold(keyword)
    shown = [g for g in groups if needle in fold(g.name)] if needle else groups
    if not shown:
        return ChannelResult(
            [f"Không có nhóm nào có tên chứa “{keyword.strip()}”. Gõ /nhomzalo để xem tất cả."]
        )
    await _save_pick(account_id, shown)

    if needle:
        header = f"👥 Nhóm Zalo có tên chứa “{keyword.strip()}” ({len(shown)}/{len(groups)}):"
    else:
        header = f"👥 Nhóm Zalo bot đang tham gia ({len(groups)}):"
    lines = [header]
    for number, group in enumerate(shown, 1):
        members = f" · {group.members} người" if group.members is not None else ""
        marks = "".join(
            f" {icon} {aliases[group.group_id]}"
            for icon, aliases in (("✅", followed), ("📣", sources))
            if group.group_id in aliases
        )
        lines.append(f"{number}. {group.name or _UNNAMED}{members} — {group.group_id}{marks}")
    if total > len(groups):
        lines.append(f"⚠️ Bot đang ở {total} nhóm, danh sách chỉ lấy {len(groups)} nhóm đầu.")
    if followed or sources:
        lines.append("✅ đang theo dõi /tongket · 📣 nguồn đăng Facebook")
    minutes = _PICK_TTL_SEC // 60
    lines.append(
        "➕ /themnhom <số> [tên-gợi-nhớ] — theo dõi để /tongket; bỏ trống tên thì em đặt "
        "theo tên nhóm\n"
        "📣 /fb_themnhom <số> [tên-gợi-nhớ] — làm nguồn đăng Facebook\n"
        f"Số thứ tự dùng được trong {minutes} phút. Lọc theo tên: /nhomzalo <từ khoá>"
    )
    return ChannelResult(["\n".join(lines)])
