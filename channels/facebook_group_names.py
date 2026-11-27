"""Tên hiển thị của nhóm nguồn Zalo -> Facebook, thay cho group ID dài 16-19 chữ số.

Thứ tự ưu tiên: tên thật của nhóm Zalo (cột ``zalo_facebook_groups.group_name``) ->
tên gợi nhớ (alias) -> "nhóm chưa rõ tên". Tên thật được ghi khi:

- ``/fb_themnhom <số>`` (tên lấy từ danh sách /nhomzalo vừa xem);
- ``/nhomzalo`` (cập nhật tên các nhóm nguồn nếu nhóm vừa đổi tên);
- gateway gửi kèm tên nhóm trong bài Facebook;
- lúc cần hiển thị mà còn nhóm chưa có tên: hỏi gateway (GET /groups), tối đa 1 lần
  mỗi ``_REFRESH_INTERVAL_SEC`` cho mỗi tài khoản - nhóm thêm từ trước khi có cột tên.

Danh sách nhóm nguồn được nhớ ``_CACHE_TTL_SEC`` giây (vài chục dòng) để mỗi bài vào
hàng chờ không phải đọc lại DB; thêm/xoá/đổi tên nhóm thì xoá cache.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping

from channels import facebook_repository, zalo_gateway_status
from channels.facebook_repository import SourceGroup
from core.text_normalize import fold

logger = logging.getLogger(__name__)

UNKNOWN = "nhóm chưa rõ tên"
_REFRESH_INTERVAL_SEC = 6 * 60 * 60
_CACHE_TTL_SEC = 60.0
# Số tài khoản Zalo thực tế chỉ 1-2; trần để cache không lớn dần nếu có lỗi lạ.
_MAX_ACCOUNTS = 16

_last_refresh: dict[str, float] = {}
_cache: dict[str, tuple[float, list[SourceGroup]]] = {}


def label_of(group: SourceGroup) -> str:
    return group.name or group.alias or UNKNOWN


def invalidate(account_id: str | None = None) -> None:
    """Xoá cache danh sách nhóm (sau /fb_themnhom, /fb_xoanhom, đổi tên)."""
    if account_id is None:
        _cache.clear()
    else:
        _cache.pop(account_id, None)


def _remember_cache(account_id: str, groups: list[SourceGroup]) -> None:
    if account_id not in _cache and len(_cache) >= _MAX_ACCOUNTS:
        _cache.clear()
    _cache[account_id] = (time.monotonic(), groups)


async def sources(account_id: str, *, fill_missing: bool = True) -> list[SourceGroup]:
    """Nhóm nguồn Facebook kèm tên. ``fill_missing``: còn nhóm chưa có tên thật thì hỏi
    gateway (có giới hạn tần suất) rồi đọc lại. Lỗi DB được ném ra cho nơi gọi."""
    cached = _cache.get(account_id)
    if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SEC:
        groups = cached[1]
    else:
        groups = await facebook_repository.list_group_details(account_id)
        _remember_cache(account_id, groups)
    if fill_missing and any(not group.name for group in groups):
        if await refresh_from_gateway(account_id):
            groups = await facebook_repository.list_group_details(account_id)
            _remember_cache(account_id, groups)
    return groups


async def labels(account_id: str) -> dict[str, str]:
    """group_id -> tên hiển thị của mọi nhóm nguồn; lỗi DB/gateway -> {} (nơi gọi dùng
    ``label``, không bao giờ hỏng vì thiếu tên)."""
    try:
        return {group.group_id: label_of(group) for group in await sources(account_id)}
    except Exception:
        logger.warning("Không đọc được tên nhóm nguồn Facebook.", exc_info=True)
        return {}


def label(names: Mapping[str, str], group_id: str | None) -> str:
    """Tên hiển thị của ``group_id`` (nhóm đã gỡ khỏi nguồn hoặc chưa rõ -> UNKNOWN)."""
    return names.get(group_id or "") or UNKNOWN


def labels_text(names: Mapping[str, str], group_ids: Iterable[str]) -> str:
    """ "A, B, C" - mỗi nhóm 1 lần, giữ thứ tự."""
    shown = dict.fromkeys(label(names, gid) for gid in group_ids if gid)
    return ", ".join(shown)


async def remember(account_id: str, names: Mapping[str, str]) -> int:
    """Ghi tên thật mới (group_id -> tên) cho các nhóm nguồn; trả số nhóm đổi tên.
    Không bao giờ raise: thiếu tên chỉ làm hiển thị kém đi."""
    if not names:
        return 0
    try:
        changed = await facebook_repository.update_group_names(account_id, names)
    except Exception:
        logger.warning("Không lưu được tên nhóm nguồn Facebook.", exc_info=True)
        return 0
    if changed:
        invalidate(account_id)
    return changed


async def refresh_from_gateway(account_id: str, *, force: bool = False) -> bool:
    """Lấy tên nhóm từ gateway Zalo rồi ghi cho các nhóm nguồn. Tối đa 1 lần mỗi
    _REFRESH_INTERVAL_SEC (trừ ``force``). True khi có nhóm được cập nhật tên."""
    now = time.monotonic()
    last = _last_refresh.get(account_id)
    if not force and last is not None and now - last < _REFRESH_INTERVAL_SEC:
        return False
    if account_id not in _last_refresh and len(_last_refresh) >= _MAX_ACCOUNTS:
        _last_refresh.clear()
    _last_refresh[account_id] = now
    try:
        groups, _total = await zalo_gateway_status.list_groups()
    except zalo_gateway_status.GatewayGroupsError as exc:
        logger.info("Chưa lấy được tên nhóm từ gateway Zalo: %s", exc)
        return False
    except Exception:
        logger.warning("Lỗi lấy tên nhóm từ gateway Zalo.", exc_info=True)
        return False
    return await remember(account_id, {g.group_id: g.name for g in groups if g.name}) > 0


async def name_of(account_id: str, group_id: str) -> str:
    """Tên thật của một nhóm (vừa thêm bằng group ID): hỏi gateway ngay, '' nếu không rõ."""
    await refresh_from_gateway(account_id, force=True)
    try:
        for group in await sources(account_id, fill_missing=False):
            if group.group_id == group_id:
                return group.name
    except Exception:
        logger.warning("Không đọc được tên nhóm nguồn Facebook.", exc_info=True)
    return ""


def find(groups: Iterable[SourceGroup], target: str) -> SourceGroup | None:
    """Nhóm khớp ``target``: group ID, tên gợi nhớ hoặc tên nhóm (không phân biệt dấu,
    hoa thường)."""
    raw = target.strip()
    folded = fold(raw)
    candidates = list(groups)
    for group in candidates:
        if raw == group.group_id or raw.lower() == group.alias:
            return group
    named = [g for g in candidates if g.name and fold(g.name) == folded]
    return named[0] if len(named) == 1 else None
