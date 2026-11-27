"""ToolSpec "chạy lệnh" của services/nl_commands: khai báo lệnh (``Command``, ``_cmd``),
dựng dòng lệnh từ tham số router (hỏi lại khi thiếu), chạy qua ``run_line``."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.text_normalize import fold, nfc
from services import tool_registry as reg
from services.nl_commands.turn import _ctx, _forms, _set_pending, _turn_text, clear_pending

# Cùng tên logger như khi còn là 1 file services/nl_commands.py.
logger = logging.getLogger(__package__)

_ARG_MAX = 200


@dataclass(frozen=True)
class Ask:
    """Thiếu tham số: câu hỏi lại ({you}/{me} thay theo người nhận)."""

    text: str
    detail: bool = False


Builder = Callable[[dict[str, Any]], "str | Ask"]


@dataclass(frozen=True)
class Command:
    spec: reg.ToolSpec
    entry: str  # tên lệnh trong command_catalog (nhãn cho câu hỏi lại, trợ giúp)
    label: str  # tên việc ngắn cho câu hỏi lại ("tử vi", "thần số học")
    # Mẫu câu RÕ RÀNG (đã bỏ dấu, đã bỏ "em ơi"/"nhé"...) chạy thẳng không cần router.
    patterns: tuple[re.Pattern[str], ...] = ()
    pattern_args: dict[str, Any] = field(default_factory=dict)


COMMANDS: dict[str, Command] = {}


def _register(command: Command) -> Command:
    COMMANDS[command.spec.name] = command
    reg.register(command.spec)
    return command


def _schema(props: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return reg.object_schema(props or {}, required)


def _clean(value: Any, limit: int = _ARG_MAX) -> str:
    """Tham số router -> 1 dòng an toàn (bỏ xuống dòng, bỏ "/" đầu, cắt độ dài)."""
    text = " ".join(nfc(str(value or "")).split()).lstrip("/")
    return text[:limit].strip()


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "co", "có"}


_DETAIL_RE = re.compile(r"\b(?:luan giai|chi tiet|giai thich|y nghia|phan tich|giai nghia)\b")


def _wants_detail(args: dict[str, Any]) -> bool:
    return _truthy(args.get("detail")) or bool(_DETAIL_RE.search(fold(_turn_text())))


def _make_handler(name: str, build: Builder, *, heavy: Callable[[dict], bool] | None = None):
    async def handler(user_id: int, **args: Any) -> reg.ToolResult:
        built = build(args)
        you, me = _forms(user_id)
        if isinstance(built, Ask):
            _set_pending(user_id, name, detail=built.detail)
            text = built.text.format(you=you, me=me, You=you.capitalize(), Me=me.capitalize())
            return reg.ToolResult(text, status=reg.STATUS_INVALID, extra={"messages": [text]})
        return await _run_command(user_id, built, heavy=bool(heavy and heavy(args)))

    handler.__name__ = f"_nl_{name}"
    return handler


async def _run_command(
    user_id: int, line: str, *, heavy: bool = False, echo: str = ""
) -> reg.ToolResult:
    ctx = _ctx()
    clear_pending(user_id)
    if heavy:
        from services import usage_quota

        if not await usage_quota.heavy_available(
            user_id, channel=ctx.channel, is_admin=ctx.is_admin
        ):
            import messages as bot_messages
            from services import reply_audience

            caps = await usage_quota.caps()
            text = bot_messages.member_quota_notice(
                "heavy", reply_audience.current().address_form, caps.heavy
            )
            return reg.ToolResult(text, extra={"messages": [text], "command": line})
    # Tra run_line qua GÓI lúc gọi: thay ``services.nl_commands.run_line`` (test) vẫn
    # có hiệu lực với mọi lệnh chạy từ câu tự nhiên.
    from services import nl_commands

    run = await nl_commands.run_line(user_id, line, channel=ctx.channel, is_admin=ctx.is_admin)
    outputs = [m for m in run.messages if m]
    if echo:
        outputs = [f"{echo}\n{outputs[0]}" if outputs else echo, *outputs[1:]]
    if not outputs and not (run.image_b64 or run.image_url):
        you, me = _forms(user_id)
        outputs = [
            f"{me.capitalize()} chưa chạy được {line} lúc này, {you} gõ thử lệnh giúp {me} nha."
        ]
    logger.info("Lệnh từ câu tự nhiên: %s (user_id=%s)", line.split()[0], user_id)
    return reg.ToolResult(
        "\n\n".join(outputs) or line,
        extra={
            "messages": outputs,
            "image_b64": run.image_b64,
            "image_url": run.image_url,
            "command": line,
        },
    )


def _cmd(
    name: str,
    entry: str,
    label: str,
    group: str,
    description: str,
    hint: str,
    props: dict | None,
    build: Builder,
    *,
    roles: frozenset[str] = reg.ALL_ROLES,
    required: tuple[str, ...] = (),
    heavy: Callable[[dict], bool] | None = None,
    patterns: tuple[str, ...] = (),
    pattern_args: dict | None = None,
) -> Command:
    spec = reg.ToolSpec(
        name=name,
        description=description,
        parameters=_schema(props, required),
        handler=_make_handler(name, build, heavy=heavy),
        roles=roles,
        surfaces=frozenset({reg.COMMAND}),
        router_hint=hint,
        verbatim=True,
        group=group,
    )
    return _register(
        Command(
            spec,
            entry,
            label,
            tuple(re.compile(p) for p in patterns),
            dict(pattern_args or {}),
        )
    )


# ── Hàm dựng dùng chung ──


def _is_switch(word: str) -> bool:
    """Từ "on"/"off"/"bật"/"tắt": tham số này biến lệnh tra cứu thành lệnh BẬT/TẮT."""
    from core.text_utils import parse_on_off

    return bool(word) and parse_on_off(word) is not None


def _need(field_name: str, ask: str, command: str, limit: int = _ARG_MAX) -> Builder:
    def build(args: dict[str, Any]) -> str | Ask:
        value = _clean(args.get(field_name), limit)
        if _is_switch(value):
            # "/anh on" là bật/tắt tạo ảnh toàn bot - không bao giờ chạy từ câu tự nhiên.
            return Ask(f"Bật/tắt thì {{you}} gõ {command} on|off giúp {{me}} nha.")
        return f"{command} {value}" if value else Ask(ask)

    return build


def _fixed(line: str) -> Builder:
    return lambda args: line


# Các module khác import để biết có mấy ToolSpec "chạy lệnh" (test, docs).
def command_names() -> list[str]:
    return sorted(COMMANDS)
