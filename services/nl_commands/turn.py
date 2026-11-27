"""Ngữ cảnh lượt (kênh, quyền admin) và trạng thái "đang chờ bổ sung tham số" của
services/nl_commands ("xem tử vi" -> em hỏi ngày giờ sinh -> người dùng trả lời)."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from core import config
from core.envutil import env_on
from services import tool_registry as reg

PENDING_TTL_SEC = 600
_PENDING_MAX = 500


def enabled() -> bool:
    """NL_COMMANDS_ENABLED=0: tắt hẳn (chỉ còn lệnh / và các đường tự nhiên cũ)."""
    return env_on("NL_COMMANDS_ENABLED")


def llm_enabled() -> bool:
    """NL_COMMANDS_LLM=0: chỉ đường code, không thêm lệnh vào router LLM."""
    return enabled() and env_on("NL_COMMANDS_LLM")


# ─── Ngữ cảnh lượt ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Turn:
    channel: str = "telegram"
    is_admin: bool = False


_turn: ContextVar[_Turn | None] = ContextVar("nl_command_turn", default=None)


@contextmanager
def turn_scope(channel: str, is_admin: bool) -> Iterator[None]:
    """Kênh + quyền admin Zalo của lượt cho handler ToolSpec "chạy lệnh"."""
    token = _turn.set(_Turn(channel, bool(is_admin)))
    try:
        yield
    finally:
        _turn.reset(token)


def _ctx() -> _Turn:
    return _turn.get() or _Turn()


def role_for(user_id: int, is_admin: bool) -> str:
    if config.is_owner_user_id(user_id):
        return reg.ROLE_OWNER
    return reg.ROLE_ADMIN if is_admin else reg.ROLE_MEMBER


def _forms(user_id: int) -> tuple[str, str]:
    if config.is_owner_user_id(user_id):
        return "anh", "em"
    from services import reply_audience

    return reply_audience.forms()


def _turn_text() -> str:
    from services import tools

    return tools._turn_text.get()


# ─── Đang chờ bổ sung tham số ───────────────────────────────────────────────


@dataclass
class _Pending:
    spec: str
    expires: float
    detail: bool = False


_pending: OrderedDict[int, _Pending] = OrderedDict()


def _set_pending(user_id: int, spec: str, *, detail: bool = False) -> None:
    _pending[user_id] = _Pending(spec, time.monotonic() + PENDING_TTL_SEC, detail)
    _pending.move_to_end(user_id)
    while len(_pending) > _PENDING_MAX:
        _pending.popitem(last=False)


def pending(user_id: int) -> _Pending | None:
    item = _pending.get(user_id)
    if item is None:
        return None
    if item.expires < time.monotonic():
        _pending.pop(user_id, None)
        return None
    return item


def clear_pending(user_id: int) -> None:
    _pending.pop(user_id, None)


def note_turn(user_id: int, outcome: reg.ToolOutcome) -> None:
    """Sau 1 lượt chat: không phải câu bổ sung cho lệnh đang chờ thì bỏ trạng thái chờ."""
    if outcome.status == reg.STATUS_INVALID and outcome.verbatim:
        return
    clear_pending(user_id)
