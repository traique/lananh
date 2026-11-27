"""Chạy 1 dòng lệnh / (vd "/tuvi 15/3/1990 9h30 nam") y như người dùng gõ, qua đúng
dispatcher của kênh chat (services/nl_commands)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core import config


@dataclass(frozen=True)
class CommandRun:
    messages: tuple[str, ...] = ()
    provider: str | None = None
    image_b64: str | None = None
    image_url: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(m for m in self.messages if m)


_PORTFOLIO_COMMANDS = frozenset({"/danhmuc", "/lichsucp", "/laicp", "/theodoi"})
_GROUP_COMMANDS = frozenset({"/nhomzalo", "/nhom", "/tongket", "/dangnoi"})


async def _zalo_account_id() -> str | None:
    from channels import zalo_repository, zalo_session

    return (
        await zalo_session.load_account_id() or await zalo_repository.resolve_default_account_id()
    )


async def run_line(user_id: int, line: str, *, channel: str, is_admin: bool) -> CommandRun:
    """Chạy ``line`` (vd "/tuvi 15/3/1990 9h30 nam") y như người dùng gõ lệnh trên
    Zalo/Zoom - cùng hàm service, cùng kiểm quyền; Telegram dùng cùng dispatcher
    (chủ bot = admin)."""
    is_admin = is_admin or config.is_owner_user_id(user_id)
    command = line.split(maxsplit=1)[0].lower()
    if command in _PORTFOLIO_COMMANDS:
        from services import portfolio_service

        text = await portfolio_service.handle_command(user_id, line)
        return CommandRun((text,) if text else ())
    if command in _GROUP_COMMANDS:
        if not is_admin:
            return CommandRun(("Lệnh này chỉ dành cho admin.",))
        from channels import group_commands

        account_id = await _zalo_account_id()
        if not account_id:
            return CommandRun(
                ("Chưa xác định được tài khoản Zalo đang đăng nhập nên chưa xem được nhóm.",)
            )
        result = await group_commands.maybe_handle_group_command(str(account_id), line)
        return CommandRun(tuple(result.messages) if result is not None else ())
    from services import channel_command_service as ccs

    result = await ccs.maybe_handle_command(user_id, line, is_admin, channel)
    if result is None:
        return CommandRun()
    messages, provider = result
    return CommandRun(
        tuple(messages), provider, ccs.take_pending_image(), ccs.take_pending_image_url()
    )


Runner = Callable[..., Awaitable[CommandRun]]
