"""/nhomzalo: mọi nhóm Zalo tài khoản bot đang tham gia, kèm group ID - để thêm
nhóm mà không phải tự đi tìm group ID.

- Danh sách lấy thẳng từ gateway (GET /groups, channels/zalo_gateway_status.py),
  dùng được từ Telegram, Zoom và Zalo (admin). Trước đây gateway tự trả lời
  /nhomzalo và chỉ cho chủ bot gõ trên Zalo.
- Đánh dấu nhóm đang theo dõi /tongket (✅) và nhóm nguồn Facebook (📣); lọc theo
  tên không phân biệt dấu (/nhomzalo gia dinh).
- /themnhom và /fb_themnhom chọn nhóm theo SỐ THỨ TỰ thay vì chép group ID (số 1-3
  chữ số luôn là số thứ tự: group ID Zalo thật dài 16-19 chữ số). Số đánh theo TOÀN BỘ
  danh sách sắp theo tên, lọc theo tên vẫn giữ số gốc - nên các admin xem lúc khác
  nhau, có lọc hay không, đều thấy cùng 1 số cho cùng 1 nhóm (trừ khi bot vừa vào/rời/
  đổi tên nhóm; câu xác nhận luôn ghi tên nhóm). Danh sách đầy đủ lưu ở setting
  ``_PICK_TTL_SEC`` giây.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping

from channels import facebook_group_names, facebook_repository, zalo_gateway_status, zalo_repository
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


async def resolve_add(
    account_id: str,
    args: list[str],
    current: Callable[[], Awaitable[Mapping[str, str]]],
    already: str,
) -> tuple[str, str, str]:
    """(group_id, tên nhóm, tên gợi nhớ) cho /themnhom và /fb_themnhom; ``args`` =
    [<số|group_id>, (tên-gợi-nhớ)]. Không kèm tên: nhóm đã có trong ``current()``
    (group_id -> tên gợi nhớ) thì raise PickError(``already`` điền {alias}) thay vì ghi
    đè tên cũ; chưa có thì đặt theo tên nhóm. Raise PickError khi số không dùng được."""
    group_id, name = await resolve_group_arg(account_id, args[0].strip())
    if len(args) > 1 and args[1].strip():
        return group_id, name, args[1].strip().lower()
    existing = (await current()).get(group_id)
    if existing is not None:
        raise PickError(already.format(alias=existing))
    return group_id, name, default_alias(name, group_id)


def describe(group_id: str, name: str) -> str:
    """ "Tên nhóm (group_id)" cho câu xác nhận; nhập thẳng group ID thì chỉ có ID."""
    return f"{name} ({group_id})" if name else group_id


async def render(account_id: str, keyword: str = "") -> ChannelResult:
    try:
        groups, total = await zalo_gateway_status.list_groups()
    except zalo_gateway_status.GatewayGroupsError as exc:
        return ChannelResult([str(exc)])
    if not groups:
        return ChannelResult(["Tài khoản Zalo của bot chưa tham gia nhóm nào."])
    followed_rows, source_rows = await asyncio.gather(
        zalo_repository.list_groups_with_daily(account_id),
        facebook_repository.list_groups(account_id),
    )
    followed = {gid: alias for gid, alias, _ in followed_rows}
    sources = dict(source_rows)
    # Nhóm nguồn Facebook hiện TÊN thay cho group ID (channels/facebook_group_names.py):
    # danh sách vừa lấy từ gateway là nguồn tên mới nhất (nhóm có thể vừa đổi tên).
    await facebook_group_names.remember(
        account_id, {g.group_id: g.name for g in groups if g.group_id in sources and g.name}
    )
    groups = sorted(groups, key=lambda g: (not g.name, fold(g.name), g.group_id))
    numbered = list(enumerate(groups, 1))
    needle = fold(keyword)
    shown = [(n, g) for n, g in numbered if needle in fold(g.name)] if needle else numbered
    if not shown:
        return ChannelResult(
            [f"Không có nhóm nào có tên chứa “{keyword.strip()}”. Gõ /nhomzalo để xem tất cả."]
        )
    # Lưu TOÀN BỘ danh sách (số gốc), kể cả khi đang lọc: số không đổi theo bộ lọc.
    await _save_pick(account_id, groups)

    if needle:
        header = f"👥 Nhóm Zalo có tên chứa “{keyword.strip()}” ({len(shown)}/{len(groups)}):"
    else:
        header = f"👥 Nhóm Zalo bot đang tham gia ({len(groups)}):"
    lines = [header]
    for number, group in shown:
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
