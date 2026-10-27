"""Quota ngày cho thành viên Zalo (role=user) - chặn trước khi giữ khoá lượt.

- Áp dụng: chỉ kênh Zalo, người KHÔNG phải admin. Chủ bot (Telegram/Zoom/web) và
  admin Zalo không bị giới hạn. Lệnh chỉ đọc (/help, /notes, /lich...) và /huy
  không bị đếm/chặn.
- Trần lượt/ngày (ZALO_MEMBER_DAILY_TURNS, mặc định 40): đếm bảng user_calls của
  người đó từ 0h giờ Việt Nam - mọi lượt có AI ghi qua telemetry (chat, /prompt,
  /gia, /dich, luận giải, ảnh, tin thoại, tài liệu) + /anh và phân tích cổ phiếu
  (ghi riêng, kind "image"/"stock"). Cổ phiếu tính như lượt thường, không có
  trần riêng.
- Trần lượt "nặng"/ngày (ZALO_MEMBER_DAILY_HEAVY, mặc định 5): /anh tạo ảnh,
  /tuvi_chitiet, /kymon_chitiet, /tsh_chitiet, lượt chat có tra web (user_calls
  kind "chat_search") và tin thoại trả lời /noi (kind "tts", ``heavy_available``/``record_heavy``). Hết lượt nặng: lệnh nặng bị chặn, chat vẫn trả lời nhưng
  không tra web (kèm ghi chú để model không bịa tin mới).
- 0 = không giới hạn. Chủ bot sửa 2 trần trên /admin (bảng settings, cache ghi-xuyên
  core.settings_cache - sửa xong áp dụng ngay).
- Câu báo hết lượt giọng Lan Anh kèm giờ mở lại, mỗi người tối đa 1 lần/giờ cho
  mỗi loại; những tin sau trong giờ đó im lặng (không gửi thêm tin Zalo nào).
- Số đếm cache 60 giây/người (trần MAX_CACHE người, bỏ người cũ nhất), cộng dồn
  ngay khi cho qua để không vượt trần trong 60 giây đó.
- FAIL-OPEN: DB lỗi khi đọc trần/số đếm -> cho qua (chỉ log).
"""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import messages
from core import database as db
from core import settings_cache
from services import divination_commands

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
CHANNEL = "zalo"
TURNS_KEY = "quota:zalo_member_daily_turns"
HEAVY_KEY = "quota:zalo_member_daily_heavy"
DEFAULT_TURNS = 40
DEFAULT_HEAVY = 5
MAX_CAP = 10_000
CACHE_TTL_SEC = 60.0
MAX_CACHE = 256
NOTICE_INTERVAL_SEC = 3600.0
MAX_NOTICES = 256

KIND_CHAT = "chat"
KIND_CHAT_SEARCH = "chat_search"
KIND_IMAGE = "image"
KIND_STOCK = "stock"
# Tin thoại trả lời (services/voice_reply.py, /noi): đọc chữ thành giọng tốn quota TTS.
KIND_TTS = "tts"
# Loại lượt tính vào trần "nặng" (user_calls.kind).
HEAVY_KINDS = ("image", "tuvi_chitiet", "kymon_chitiet", "tsh_chitiet", KIND_CHAT_SEARCH, KIND_TTS)
# Lệnh huyền học gọi AI, gồm alias (/ths_chitiet): cùng bảng định tuyến của
# divination_commands.handle nên không alias nào lọt trần nặng.
_HEAVY_COMMANDS = dict(divination_commands.AI_COMMAND_KINDS)
_ANH_SWITCHES = frozenset({"on", "off", "bật", "tắt", "bat", "tat"})


@dataclass(frozen=True)
class Caps:
    turns: int
    heavy: int


@dataclass
class _Usage:
    day: str
    turns: int
    heavy: int
    fetched: float


@dataclass(frozen=True)
class Decision:
    allowed: bool
    notice: str | None = None  # câu báo hết lượt (None = im lặng)
    search_allowed: bool = True
    reason: str = ""  # "" | "turns" | "heavy"
    # True = lượt của thành viên đã qua kiểm quota (scope() chỉ áp cho lượt này).
    applied: bool = False


ALLOW = Decision(True)
_cache: OrderedDict[int, _Usage] = OrderedDict()
_notices: OrderedDict[tuple[int, str], float] = OrderedDict()


@dataclass(frozen=True)
class _Scope:
    user_id: int
    search_allowed: bool


_scope: ContextVar[_Scope | None] = ContextVar("member_quota_scope", default=None)


def applies(channel: str, is_admin: bool) -> bool:
    return channel == CHANNEL and not is_admin


def _env_cap(name: str, default: int) -> int:
    try:
        return max(0, min(MAX_CAP, int(os.getenv(name, str(default)).strip())))
    except ValueError:
        return default


def _parse_cap(raw: str | None, default: int) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0, min(MAX_CAP, int(str(raw).strip())))
    except ValueError:
        return default


async def caps() -> Caps:
    """Trần hiện hành: giá trị chủ bot đặt trên /admin, chưa đặt thì theo env.
    DB lỗi -> theo env (không chặn ai chỉ vì không đọc được settings)."""
    turns_default = _env_cap("ZALO_MEMBER_DAILY_TURNS", DEFAULT_TURNS)
    heavy_default = _env_cap("ZALO_MEMBER_DAILY_HEAVY", DEFAULT_HEAVY)
    try:
        turns = _parse_cap(await settings_cache.get(TURNS_KEY), turns_default)
        heavy = _parse_cap(await settings_cache.get(HEAVY_KEY), heavy_default)
    except Exception:
        logger.warning("Không đọc được trần quota thành viên, dùng env.", exc_info=True)
        return Caps(turns_default, heavy_default)
    return Caps(turns, heavy)


async def set_caps(turns: int | None = None, heavy: int | None = None) -> Caps:
    """Trang /admin: đặt trần (0 = không giới hạn). Ghi xuyên cache -> áp dụng ngay."""
    if turns is not None:
        await settings_cache.set(TURNS_KEY, str(max(0, min(MAX_CAP, int(turns)))))
    if heavy is not None:
        await settings_cache.set(HEAVY_KEY, str(max(0, min(MAX_CAP, int(heavy)))))
    return await caps()


def _today(now: datetime | None = None) -> datetime:
    local = (now or datetime.now(VN_TZ)).astimezone(VN_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _remember(user_id: int, usage: _Usage) -> None:
    _cache[user_id] = usage
    _cache.move_to_end(user_id)
    while len(_cache) > MAX_CACHE:
        _cache.popitem(last=False)


async def _usage(user_id: int) -> _Usage:
    start = _today()
    day = start.date().isoformat()
    cached = _cache.get(user_id)
    if (
        cached is not None
        and cached.day == day
        and time.monotonic() - cached.fetched < CACHE_TTL_SEC
    ):
        return cached
    turns, heavy = await db.user_calls_since(CHANNEL, user_id, start, list(HEAVY_KINDS))
    usage = _Usage(day, turns, heavy, time.monotonic())
    _remember(user_id, usage)
    return usage


def heavy_kind(text: str) -> str | None:
    """Loại lượt nặng nếu tin là lệnh nặng (/anh <mô tả>, /*_chitiet), không thì None."""
    parts = (text or "").strip().split(maxsplit=1)
    if not parts or not parts[0].startswith("/"):
        return None
    command = parts[0].lower().split("@", 1)[0]
    if command == "/anh":
        argument = parts[1].strip().lower() if len(parts) > 1 else ""
        return KIND_IMAGE if argument and argument not in _ANH_SWITCHES else None
    return _HEAVY_COMMANDS.get(command)


def _notice(user_id: int, reason: str, address_form: str | None, cap: int) -> str | None:
    """Câu báo hết lượt, tối đa 1 lần/giờ cho mỗi (người, loại)."""
    key = (user_id, reason)
    now = time.monotonic()
    last = _notices.get(key)
    if last is not None and now - last < NOTICE_INTERVAL_SEC:
        return None
    _notices[key] = now
    _notices.move_to_end(key)
    while len(_notices) > MAX_NOTICES:
        _notices.popitem(last=False)
    return messages.member_quota_notice(reason, address_form, cap)


async def check(
    user_id: int,
    text: str = "",
    *,
    channel: str,
    is_admin: bool,
    address_form: str | None = None,
) -> Decision:
    """Cho qua hay chặn 1 lượt (gọi TRƯỚC khoá lượt, SAU kiểm cache idempotency)."""
    if not applies(channel, is_admin):
        return ALLOW
    try:
        limits = await caps()
        if not limits.turns and not limits.heavy:
            return ALLOW
        usage = await _usage(user_id)
    except Exception:
        logger.warning("Quota thành viên: DB lỗi, cho qua (fail-open).", exc_info=True)
        return ALLOW
    if address_form is None:
        from services import reply_audience

        address_form = reply_audience.current().address_form
    if limits.turns and usage.turns >= limits.turns:
        logger.info("Quota: user_id=%s hết %d lượt hôm nay.", user_id, limits.turns)
        return Decision(
            False, _notice(user_id, "turns", address_form, limits.turns), False, "turns"
        )
    kind = heavy_kind(text)
    heavy_out = bool(limits.heavy) and usage.heavy >= limits.heavy
    if kind and heavy_out:
        logger.info("Quota: user_id=%s hết %d lượt nặng hôm nay.", user_id, limits.heavy)
        return Decision(
            False, _notice(user_id, "heavy", address_form, limits.heavy), False, "heavy"
        )
    # Cộng dồn ngay (cache 60s) để các tin liền nhau không vượt trần.
    usage.turns += 1
    if kind:
        usage.heavy += 1
    return Decision(True, search_allowed=not heavy_out, applied=True)


async def turns_exhausted(user_id: int, *, channel: str, is_admin: bool) -> bool:
    """Chỉ XEM (không cộng dồn, không dựng câu báo): thành viên đã hết trần lượt
    hôm nay chưa. Dùng cho câu báo trước lượt của gateway (POST /internal/zalo/notice
    kind "image") để không hứa "đợi em xem xíu" rồi lượt bị chặn. DB lỗi -> False."""
    if not applies(channel, is_admin):
        return False
    try:
        limits = await caps()
        if not limits.turns:
            return False
        usage = await _usage(user_id)
    except Exception:
        logger.warning("Quota thành viên: DB lỗi khi xem số lượt, coi như còn.", exc_info=True)
        return False
    return usage.turns >= limits.turns


async def heavy_available(user_id: int, *, channel: str, is_admin: bool) -> bool:
    """Chỉ XEM: còn lượt nặng hôm nay cho việc phụ trong lượt đã được cho qua (vd tin
    thoại trả lời /noi) không. Ngoài phạm vi quota (chủ bot, admin) hoặc không đặt
    trần nặng -> True. DB lỗi -> True (fail-open như check())."""
    if not applies(channel, is_admin):
        return True
    try:
        limits = await caps()
        if not limits.heavy:
            return True
        usage = await _usage(user_id)
    except Exception:
        logger.warning("Quota thành viên: DB lỗi khi xem lượt nặng, cho qua.", exc_info=True)
        return True
    return usage.heavy < limits.heavy


async def record_heavy(user_id: int, kind: str, *, channel: str, is_admin: bool) -> None:
    """Tính 1 lượt nặng ``kind`` (đã làm xong việc phụ, vd tin thoại trả lời): cộng
    dồn cache ngay và ghi user_calls. Ngoài phạm vi quota thì bỏ qua. Không raise."""
    if not applies(channel, is_admin):
        return
    cached = _cache.get(user_id)
    if cached is not None:
        cached.heavy += 1
        cached.turns += 1
    await record(user_id, channel, kind)


@contextmanager
def scope(decision: Decision, user_id: int) -> Iterator[None]:
    """Đặt quyết định quota cho lượt đang chạy (task con thừa hưởng) để
    run_chat_turn biết có được tra web không và ghi lượt tra web. Lượt không bị
    tính quota (chủ bot, admin, DB lỗi -> ALLOW) thì không đặt gì."""
    if not decision.applied:
        yield
        return
    token = _scope.set(_Scope(user_id, decision.search_allowed))
    try:
        yield
    finally:
        _scope.reset(token)


def search_blocked() -> bool:
    current = _scope.get()
    return current is not None and not current.search_allowed


async def _exhausted_plan():
    from services import web_search

    return web_search.SearchPlan(grounding=messages.MEMBER_SEARCH_QUOTA_NOTE, mode="off")


async def _no_plan():
    from services import web_search

    return web_search.SearchPlan(mode="off")


def exhausted_search_plan(text: str) -> Awaitable:
    """Kế hoạch tra web khi đã hết lượt nặng: không tra; câu cần tin mới thì kèm
    ghi chú cấm bịa (messages.MEMBER_SEARCH_QUOTA_NOTE)."""
    from services import web_search

    return _exhausted_plan() if web_search.looks_like_search_question(text) else _no_plan()


async def note_web_search(user_id: int) -> None:
    """Lượt chat của thành viên vừa tra web: đánh dấu lượt chat của hôm nay thành
    "chat_search" (tính vào trần nặng). Ngoài phạm vi quota thì bỏ qua. Không raise."""
    current = _scope.get()
    if current is None or current.user_id != user_id:
        return
    cached = _cache.get(user_id)
    if cached is not None:
        cached.heavy += 1
    try:
        await db.relabel_last_user_call(CHANNEL, user_id, KIND_CHAT, KIND_CHAT_SEARCH, _today())
    except Exception:
        logger.warning("Quota: không đánh dấu được lượt tra web user_id=%s.", user_id)


async def record(user_id: int, channel: str, kind: str) -> None:
    """Ghi 1 lượt không đi qua telemetry (/anh, phân tích cổ phiếu) vào user_calls -
    vừa để /thongke đủ số, vừa để quota thành viên đếm được. Không raise."""
    try:
        await db.record_user_call(channel, user_id, kind)
    except Exception:
        logger.warning("Không ghi được lượt %s của user_id=%s vào user_calls.", kind, user_id)


async def today_overview() -> dict:
    """Trang /admin: trần hiện hành + số lượt hôm nay của từng thành viên Zalo."""
    from channels import zalo_users

    limits = await caps()
    counts = {
        int(row["telegram_user_id"]): row
        for row in await db.user_calls_by_user_since(CHANNEL, _today(), list(HEAVY_KINDS))
    }
    members = []
    for user in await zalo_users.list_users():
        if user.is_admin:
            continue
        row = counts.get(user.internal_user_id) or {}
        members.append(
            {
                "user_id": user.internal_user_id,
                "label": user.display_name or user.external_id,
                "turns": int(row.get("turns") or 0),
                "heavy": int(row.get("heavy") or 0),
            }
        )
    members.sort(key=lambda item: -item["turns"])
    return {
        "turns": limits.turns,
        "heavy": limits.heavy,
        "default_turns": _env_cap("ZALO_MEMBER_DAILY_TURNS", DEFAULT_TURNS),
        "default_heavy": _env_cap("ZALO_MEMBER_DAILY_HEAVY", DEFAULT_HEAVY),
        "members": members[:100],
    }


def reset() -> None:
    """Xoá cache số đếm + nhật ký câu báo (test)."""
    _cache.clear()
    _notices.clear()
