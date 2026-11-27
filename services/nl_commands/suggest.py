"""Gợi ý lệnh (``suggest_command``: lệnh quản trị/nguy hiểm/xoá KHÔNG BAO GIỜ tự chạy,
chỉ ra đúng lệnh cần gõ) và hỏi lại (``clarify_command``) của services/nl_commands."""

from __future__ import annotations

from typing import Any

from services import command_catalog as catalog
from services import tool_registry as reg
from services.nl_commands.registry import COMMANDS, _clean, _schema
from services.nl_commands.turn import _ctx, _forms, role_for


async def _suggest_handler(user_id: int, command: str = "", args: str = "", **_ignored):
    ctx = _ctx()
    entry = catalog.get(command)
    role = role_for(user_id, ctx.is_admin)
    if entry is None or not entry.suggest_only or role not in entry.allowed_roles:
        # Không lộ lệnh người hỏi không được dùng: coi như không có tool.
        return reg.ToolResult("", status=reg.STATUS_NONE)
    return reg.ToolResult(*_suggest_text(user_id, entry, _clean(args, 120), ctx.channel))


def _suggest_text(user_id: int, entry: catalog.Entry, args: str, channel: str):
    you, me = _forms(user_id)
    line = f"/{entry.name} {args}".strip()
    where = " (trên Telegram)" if entry.where == "tg" and channel != "telegram" else ""
    text = (
        f"🔐 Việc này {me} không tự làm từ tin nhắn được - {you} tự gõ lệnh để xác nhận "
        f"nha{where}:\n{line}\n({entry.summary})"
    )
    return text, reg.STATUS_OK, "", "", {"messages": [text], "command": line, "suggest": True}


SUGGEST_SPEC = reg.register(
    reg.ToolSpec(
        name="suggest_command",
        description="Chỉ ra lệnh / cần gõ cho việc quản trị/xoá/nguy hiểm (không tự chạy).",
        parameters=_schema({"command": reg.string("tên lệnh"), "args": reg.string("tham số")}),
        handler=_suggest_handler,
        surfaces=frozenset({reg.COMMAND}),
        verbatim=True,
        group="suggest",
    )
)


async def _clarify_handler(user_id: int, tools: Any = None, **_ignored) -> reg.ToolResult:
    you, me = _forms(user_id)
    names = tools if isinstance(tools, list) else []
    labels = [COMMANDS[n].label for n in names if isinstance(n, str) and n in COMMANDS][:3]
    if len(labels) >= 2:
        options = ", ".join(labels[:-1]) + f" hay {labels[-1]}"
        text = f"{me.capitalize()} chưa chắc {you} muốn xem {options}? {you.capitalize()} nói rõ giúp {me} nha."
    else:
        text = (
            f"{me.capitalize()} chưa rõ {you} muốn dùng chức năng nào - {you} nói cụ thể hơn "
            f"giúp {me} nha (hoặc gõ /help để xem gợi ý)."
        )
    return reg.ToolResult(text, status=reg.STATUS_INVALID, extra={"messages": [text]})


CLARIFY_SPEC = reg.register(
    reg.ToolSpec(
        name="clarify_command",
        description="Hỏi lại 1 câu khi rõ là cần 1 chức năng ở trên nhưng không chắc cái nào.",
        parameters=_schema({"tools": {"type": "array", "items": {"type": "string"}}}),
        handler=_clarify_handler,
        surfaces=frozenset({reg.COMMAND}),
        router_hint='args {"tools": [2-3 tên tool ứng viên]}. Chỉ khi thật sự mơ hồ.',
        verbatim=True,
        group="clarify",
    )
)
