"""Bật/tắt cài đặt từ câu tự nhiên (ToolSpec ``set_*``): câu phải nói rõ bật/tắt - code
kiểm lại trên chữ người dùng, không tin router; câu trả lời nhắc lại lệnh đã chạy
(services/nl_commands)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from core.text_normalize import fold, nfc
from services import tool_registry as reg
from services.nl_commands.registry import Command, _clean, _register, _run_command, _schema
from services.nl_commands.turn import _forms, _turn_text

# Kiểm trên chữ CÒN DẤU: bỏ dấu thì "dừng"/"dùng"/"đúng" cùng là "dung", "bỏ"/"bò"/
# "bố" cùng là "bo" - "anh muốn dùng giọng nói" không được thành TẮT giọng nói.
_ON_RE = re.compile(r"\b(?:bật|mở|on|kích hoạt)\b")
_OFF_RE = re.compile(r"\b(?:tắt|ngừng|dừng|off|thôi|khỏi|bỏ)\b")
_ON_PLAIN_RE = re.compile(r"\b(?:bat|on)\b")
_OFF_PLAIN_RE = re.compile(r"\b(?:tat|ngung|off)\b")


def _explicit_mode(text: str) -> str:
    """Trả "on"/"off" theo chữ người dùng (không tin router); không rõ/cả hai -> ""."""
    low = nfc(text or "").lower()
    plain = fold(low) == low
    on_re, off_re = (_ON_PLAIN_RE, _OFF_PLAIN_RE) if plain else (_ON_RE, _OFF_RE)
    on, off = bool(on_re.search(low)), bool(off_re.search(low))
    return "on" if on and not off else "off" if off and not on else ""


_SETTING_ECHO = "↪️ {Me} làm theo lệnh {line}"


def _setting(
    name: str,
    entry: str,
    label: str,
    description: str,
    hint: str,
    props: dict,
    build: Callable[[dict[str, Any], str], str | None],
    *,
    roles: frozenset[str] = reg.ALL_ROLES,
    patterns: tuple[str, ...] = (),
) -> Command:
    async def handler(user_id: int, **args: Any) -> reg.ToolResult:
        you, me = _forms(user_id)
        line = build(args, _explicit_mode(_turn_text()))
        if not line:
            text = f"{you.capitalize()} muốn {me} BẬT hay TẮT {label} ạ?"
            return reg.ToolResult(text, status=reg.STATUS_INVALID, extra={"messages": [text]})
        echo = _SETTING_ECHO.format(Me=me.capitalize(), line=line)
        result = await _run_command(user_id, line, echo=echo)
        return replace(result, confirmation=line)

    handler.__name__ = f"_nl_{name}"
    spec = reg.ToolSpec(
        name=name,
        description=description,
        parameters=_schema(props),
        handler=handler,
        side_effect=True,
        roles=roles,
        surfaces=frozenset({reg.COMMAND}),
        router_hint=hint,
        effect_label=f"cài đặt {label}",
        verbatim=True,
        group="cai_dat",
    )
    return _register(
        Command(spec, entry, label, tuple(re.compile(p) for p in patterns), {"_setting": True})
    )


def _on_off(command: str, *, auto: bool = False):
    def build(args: dict[str, Any], mode: str) -> str | None:
        if auto and fold(_clean(args.get("mode"), 10)) == "auto" and "auto" in fold(_turn_text()):
            return f"{command} auto"
        return f"{command} {mode}" if mode else None

    return build


_MODE = {"mode": reg.string("on|off")}
_setting(
    "set_noi",
    "noi",
    "trả lời kèm giọng nói",
    "Bật/tắt trả lời kèm giọng nói.",
    '{"mode": on|off|auto}. Chỉ khi nói RÕ bật/tắt.',
    {"mode": reg.string("on|off|auto")},
    _on_off("/noi", auto=True),
    patterns=(r"(?:bat|tat|mo|ngung) (?:che do )?(?:tra loi )?(?:bang |kem )?giong noi",),
)


_CHANGE_RE = re.compile(r"\b(?:đổi|chuyển|đặt|sửa|cài)\b")
_CHANGE_PLAIN_RE = re.compile(r"\b(?:doi|chuyen|dat|sua|cai)\b")


def _wants_change(text: str) -> bool:
    """Câu nói rõ đổi/đặt giờ ("đổi bản tin sang 7h") - kiểm trên chữ còn dấu."""
    low = nfc(text or "").lower()
    pattern = _CHANGE_PLAIN_RE if fold(low) == low else _CHANGE_RE
    return bool(pattern.search(low))


# "22h tới 7h", "22-7": khung giờ yên lặng; "7h": giờ bản tin (trên chữ đã bỏ dấu).
_HOUR_SPAN_RE = re.compile(r"\b(\d{1,2})(?:h|g|gio)?\s*(?:-|den|toi)\s*(\d{1,2})(?:h|g|gio)?\b")
_HOUR_RE = re.compile(r"\b(\d{1,2})\s*(?:h|g|gio)\b")


def _build_yenlang(args: dict[str, Any], mode: str) -> str | None:
    text = fold(_turn_text())
    span = _HOUR_SPAN_RE.search(text)
    if span and (mode == "on" or (not mode and _wants_change(_turn_text()))):
        return f"/yenlang {int(span.group(1))}-{int(span.group(2))}"
    return f"/yenlang {mode}" if mode else None


_setting(
    "set_yenlang",
    "yenlang",
    "giờ yên lặng",
    "Bật/tắt/đổi giờ yên lặng (gom thông báo ban đêm).",
    '{"mode": on|off|"22-7"}. Chỉ khi nói RÕ.',
    {"mode": reg.string("on|off|22-7")},
    _build_yenlang,
    patterns=(r"(?:bat|tat|mo|ngung) (?:che do )?gio yen lang(?: .*)?",),
)


def _build_bantin(args: dict[str, Any], mode: str) -> str | None:
    text = fold(_turn_text())
    hour = _HOUR_RE.search(text)
    if hour and (mode == "on" or (not mode and _wants_change(_turn_text()))):
        return f"/bantin {int(hour.group(1))}"
    return f"/bantin {mode}" if mode else None


_setting(
    "set_bantin",
    "bantin",
    "bản tin sáng",
    "Bật/tắt/đổi giờ bản tin sáng cá nhân.",
    '{"mode": on|off|giờ}. Chỉ khi nói RÕ.',
    _MODE,
    _build_bantin,
)
_setting(
    "set_hoithan",
    "hoithan",
    "hỏi thăm chủ động",
    "Bật/tắt em chủ động hỏi thăm việc người dùng kể.",
    '{"mode": on|off}. Chỉ khi nói RÕ.',
    _MODE,
    _on_off("/hoithan"),
    patterns=(r"(?:bat|tat|mo|ngung) (?:che do )?hoi (?:than|tham)(?: chu dong)?",),
)
_setting(
    "set_memory",
    "memory",
    "trí nhớ dài hạn",
    "Bật/tắt trí nhớ dài hạn.",
    '{"mode": on|off}. Chỉ khi nói RÕ bật/tắt TRÍ NHỚ.',
    _MODE,
    _on_off("/memory"),
    patterns=(r"(?:bat|tat|mo|ngung) tri nho(?: dai han)?",),
)
_setting(
    "set_tavily",
    "tavily",
    "tra web Tavily",
    "Bật/tắt tra web Tavily.",
    '{"mode": on|off}. Chỉ khi nói RÕ.',
    _MODE,
    _on_off("/tavily"),
    roles=reg.STAFF_ROLES,
)
