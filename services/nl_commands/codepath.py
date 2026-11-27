"""Đường code (không AI) của "hỏi tự nhiên thay cho lệnh": ``maybe_handle`` - 1 cổng
trong chuỗi trả lời nhanh dùng chung mọi kênh (services/quick_reply.py)."""

from __future__ import annotations

import re
from typing import Any

from core.text_normalize import fold, nfc
from services import command_catalog as catalog
from services import tool_registry as reg
from services.nl_commands.builders_huyenhoc import _DATE_RE, _date_text
from services.nl_commands.help import short_help
from services.nl_commands.registry import _DETAIL_RE, COMMANDS, _clean, _run_command
from services.nl_commands.router import outcome_messages
from services.nl_commands.runner import CommandRun
from services.nl_commands.suggest import _suggest_text
from services.nl_commands.turn import (
    _forms,
    _Pending,
    _set_pending,
    clear_pending,
    enabled,
    pending,
    role_for,
    turn_scope,
)

_PREFIX_RE = re.compile(
    r"^(?:(?:em|lan anh|bot|ban) oi|oi (?:em|lan anh)|nay em|em|cho (?:anh|chi|em|minh|toi) hoi|"
    r"(?:anh|chi|minh|toi) muon|(?:giup|gium|ho) (?:anh|chi|minh|toi))[\s,]+"
)
_SUFFIX_RE = re.compile(
    r"(?:[\s,]+(?:di|nhe|nha|nhen|voi|em|a|ha|duoc khong|dc khong|giup (?:anh|chi|minh|toi)"
    r"|cho (?:anh|chi|minh|toi)|gium (?:anh|chi|minh|toi)|xem|thu|coi))+$"
)


def _core(text: str) -> str:
    low = fold(text).strip(" ?.!,")
    for _ in range(2):
        low = _PREFIX_RE.sub("", low).strip()
    return _SUFFIX_RE.sub("", low).strip(" ?.!,")


_CAPABILITY_RE = re.compile(
    r"\b(?:lam|giup) (?:duoc )?(?:nhung )?(?:gi|viec gi)\b|\bbiet lam (?:nhung )?gi\b|"
    r"\bco (?:nhung )?(?:tinh nang|chuc nang|lenh) (?:gi|nao)\b|"
    r"\b(?:huong dan|cach) (?:su dung|dung)(?: bot| em)?$|^(?:danh sach|xem) (?:cac )?lenh$"
)
_HOWTO_RE = (
    re.compile(r"^(?:lam (?:sao|the nao)|bang cach nao|cach nao) (?:de )?(?P<x>.+)$"),
    re.compile(r"^(?:co )?lenh (?:nao|gi) (?:de |dung de )?(?P<x>.+?)(?: khong| ko)?$"),
    re.compile(r"^muon (?P<x>.+?) thi (?:lam sao|lam the nao|go gi|go lenh gi|dung lenh gi)$"),
)


# "em làm gì đấy" (hỏi đang làm gì) không phải hỏi tính năng.
_FEATURE_WORD_RE = re.compile(r"\b(?:duoc|dc|giup|biet|tinh nang|chuc nang|lenh|su dung|dung)\b")


def _capability_question(core: str) -> bool:
    if len(core) > 60 or not _CAPABILITY_RE.search(core):
        return False
    return bool(_FEATURE_WORD_RE.search(core))


def _howto(core: str, role: str, channel: str, user_id: int) -> str | None:
    if len(core) > 90:
        return None
    for pattern in _HOWTO_RE:
        match = pattern.match(core)
        if not match:
            continue
        found = catalog.search(match.group("x"), role, channel)
        if not found:
            if "lenh" in core:
                you, me = _forms(user_id)
                return (
                    f"{me.capitalize()} chưa có lệnh riêng cho việc này, {you} cứ hỏi thẳng {me} "
                    "bằng lời nha. Gõ /help để xem các việc em làm được."
                )
            return None
        return how_to_text(found, user_id)
    return None


def how_to_text(entries: list[catalog.Entry], user_id: int) -> str:
    you, me = _forms(user_id)
    lines = [f"{you.capitalize()} cứ nhắn tự nhiên cho {me} là được:"]
    for entry in entries:
        ask = f"“{entry.ask}”" if entry.ask and not entry.suggest_only else ""
        if ask:
            lines.append(f"• {ask} — hoặc gõ {entry.usage}: {entry.summary}")
        else:
            lines.append(f"• Gõ {entry.usage}: {entry.summary}")
    return "\n".join(lines)


# Lệnh nguy hiểm nhận ra bằng code: chỉ gợi ý lệnh, không bao giờ tự chạy.
_DANGER_PATTERNS = (
    (re.compile(r"(?:xoa|quen) (?:het|sach|toan bo) (?:tri nho|nhung gi em nho(?: ve .*)?)"), "forget"),
    (re.compile(r"xoa (?:het |sach |toan bo )?du lieu(?: ca nhan)?(?: cua (?:anh|chi|minh|toi))?"), "xoadulieu"),
    (re.compile(r"(?:xoa ngu canh|lam moi (?:cuoc )?(?:tro chuyen|hoi thoai)|reset (?:chat|hoi thoai))"), "reset"),
)  # fmt: skip

_TUVI_TRIGGER = re.compile(r"\b(?:tu vi|la so)\b")
_TSH_TRIGGER = re.compile(r"\bthan so hoc\b")
_KYMON_TRIGGER = re.compile(r"\bky mon\b")
_KNOWLEDGE_RE = re.compile(
    r"\b(?:la gi|nghia la|co dung khong|co tin|tai sao|vi sao|khac gi|nhu the nao|bao nhieu sao)\b"
)
_LICH_TRIGGER = re.compile(
    r"\b(?:ngay tot|ngay xau|ngay dep|ngay hoang dao|sao han|han nam|tam tai|kim lau|hoang oc|"
    r"ngay le|xuat hanh)\b"
)
# "xem tử vi", "lập lá số tử vi cho anh", "luận giải tử vi chi tiết": hỏi ngày giờ sinh.
_ASK_TUVI_RE = re.compile(
    r"(?:xem|lap|an|coi|tra|doc|luan giai|giai)?\s*(?:la so )?(?:tu vi|la so)(?: tu vi)?"
    r"(?: (?:chi tiet|cho (?:anh|chi|em|minh|toi)|giup|gium|ho))*"
)
_ASK_TSH_RE = re.compile(
    r"(?:xem|tinh|tra|coi|luan giai)?\s*than so hoc(?: (?:chi tiet|cho (?:anh|chi|em|minh|toi)|"
    r"giup|gium|ho))*"
)
_TSH_NAME_RE = re.compile(r"\bt[eê]n(?: l[aà])?\s*:?\s*([^\d,.;]{2,60})", re.IGNORECASE)


async def _run_spec(name: str, user_id: int, args: dict[str, Any]) -> CommandRun | None:
    from services import tools

    spec = reg.get(name)
    if spec is None:
        return None
    outcome = await tools.execute(spec, user_id, args)
    messages = outcome_messages(outcome)
    if not messages and not outcome.extra.get("image_b64") and not outcome.extra.get("image_url"):
        return None
    return CommandRun(
        tuple(messages),
        None,
        outcome.extra.get("image_b64"),
        outcome.extra.get("image_url"),
    )


async def _tuvi(user_id: int, text: str, low: str, waiting: _Pending | None) -> CommandRun | None:
    from services import tuvi

    trigger = bool(_TUVI_TRIGGER.search(low))
    follow = not trigger and waiting is not None and waiting.spec == "cmd_tuvi"
    if not (trigger or follow) or (trigger and _KNOWLEDGE_RE.search(low)):
        return None
    has_date = bool(_DATE_RE.search(text))
    if follow and not has_date:
        clear_pending(user_id)  # người dùng nói chuyện khác: thôi chờ
        return None
    detail = bool(_DETAIL_RE.search(low)) or bool(follow and waiting and waiting.detail)
    if not has_date:
        if not _ASK_TUVI_RE.fullmatch(_core(text)):
            return None  # câu khác có chữ "tử vi" mà không có ngày sinh: router/chat lo
        return await _run_spec("cmd_tuvi", user_id, {"detail": detail})
    parsed = tuvi.parse(text)
    if isinstance(parsed, str):
        you, me = _forms(user_id)
        _set_pending(user_id, "cmd_tuvi", detail=detail)
        first = parsed.splitlines()[0]
        return CommandRun(
            (f"{first} {you.capitalize()} ghi thêm giúp {me} nha, vd: 15/3/1990 9h30 nam.",)
        )
    command = "/tuvi_chitiet" if detail else "/tuvi"
    result = await _run_command(user_id, f"{command} {_clean(text, 160)}", heavy=detail)
    return CommandRun(tuple(result.extra.get("messages") or [result.text]))


async def _tsh(user_id: int, text: str, low: str, waiting: _Pending | None) -> CommandRun | None:
    trigger = bool(_TSH_TRIGGER.search(low))
    follow = not trigger and waiting is not None and waiting.spec == "cmd_tsh"
    if not (trigger or follow) or (trigger and _KNOWLEDGE_RE.search(low)):
        return None
    date = _date_text(text)
    if follow and not date:
        clear_pending(user_id)
        return None
    if not date and not _ASK_TSH_RE.fullmatch(_core(text)):
        return None
    name = _TSH_NAME_RE.search(nfc(text))
    args = {
        "date": date,
        "name": name.group(1).strip() if name else "",
        "detail": bool(_DETAIL_RE.search(low)) or bool(follow and waiting and waiting.detail),
    }
    return await _run_spec("cmd_tsh", user_id, args)


_BATTU_TRIGGER = re.compile(r"\b(?:bat tu|tu tru)\b")
_LOBAN_TRIGGER = re.compile(r"\blo ban\b")
_CUNGPHI_TRIGGER = re.compile(r"\b(?:cung phi|bat trach|huong nha)\b")
_BIRTH_YEAR_RE = re.compile(r"\b(?:19\d{2}|20\d{2}|2100)\b")
# Giới tính trên chữ CÓ dấu: bỏ dấu thì "năm 1990" cũng thành "nam".
_GENDER_WORD_RE = re.compile(r"(?<!\w)(?:nam|nữ|nu)(?!\w)")
_DIGIT_RE = re.compile(r"\d")


async def _battu_phong_thuy(user_id: int, text: str, low: str) -> CommandRun | None:
    """ "bát tự 15/3/1990 9h30 nam", "cửa 80cm có hợp lỗ ban không", "cung phi 1990 nữ":
    đủ tham số thì chạy thẳng lệnh (không tốn lượt router); thiếu thì để router hỏi lại."""
    if _KNOWLEDGE_RE.search(low):
        return None
    heavy = False
    if _LOBAN_TRIGGER.search(low) and _DIGIT_RE.search(low):
        line = f"/loban {_clean(text, 120)}"
    elif (
        _CUNGPHI_TRIGGER.search(low)
        and _BIRTH_YEAR_RE.search(low)
        and _GENDER_WORD_RE.search(nfc(text).lower())
    ):
        line = f"/cungphi {_clean(text, 120)}"
    elif _BATTU_TRIGGER.search(low) and _DATE_RE.search(text):
        heavy = bool(_DETAIL_RE.search(low))
        line = f"{'/battu_chitiet' if heavy else '/battu'} {_clean(text, 160)}"
    else:
        return None
    result = await _run_command(user_id, line, heavy=heavy)
    return CommandRun(tuple(result.extra.get("messages") or [result.text]))


# "lập bàn kỳ môn (lúc này)": không có thời điểm/việc cần xem -> chạy thẳng.
_KYMON_NOW_RE = re.compile(
    r"(?:lap |xem )?(?:ban )?ky mon(?: (?:luc nay|bay gio|gio nay|hom nay))?"
)


async def _kymon_or_lich(user_id: int, text: str, low: str) -> CommandRun | None:
    from services import calendar_query

    if _KNOWLEDGE_RE.search(low):
        return None
    if _KYMON_TRIGGER.search(low):
        core = _core(text)
        if _KYMON_NOW_RE.fullmatch(core):
            return await _run_spec("cmd_kymon", user_id, {})
        return None  # có thời điểm/việc cần xem: router tách tham số
    if _LICH_TRIGGER.search(low) and len(low) <= 100:
        answer = calendar_query.answer(_core(text) or text)
        if not answer.startswith("Em chưa hiểu"):
            return CommandRun((answer,))
    return None


# "xem nhắc việc", "tháng này anh tiêu bao nhiêu": chạy thẳng tool đọc của chat (đúng văn
# bản /nhac, /chitieu) thay vì router AI chọn tool rồi chat AI viết lại (2 lượt AI).
_WHO = r"(?:(?:anh|chi|em|minh|toi|tui) )?"
_REMINDER_LIST_RE = re.compile(
    r"(?:xem |liet ke |kiem tra )?(?:(?:cac|nhung) )?(?:nhac viec|viec can nhac|loi nhac)"
    r"(?: (?:dang cho|sap toi))?(?: cua (?:anh|chi|minh|toi))?|"
    rf"{_WHO}(?:da )?dat (?:nhung |may )?(?:nhac|loi nhac) (?:gi|nao)(?: roi)?|"
    rf"{_WHO}co (?:nhung )?(?:nhac viec|loi nhac|viec can nhac) (?:gi|nao)(?: (?:khong|ko|chua))?"
)
_PERIOD = (
    r"(?:hom nay|hom qua|tuan nay|tuan truoc|tuan roi|thang nay|thang truoc|thang roi|"
    r"thang \d{1,2}(?:/\d{4})?|nam nay)"
)
_EXPENSE_SUMMARY_RE = re.compile(
    rf"{_PERIOD} {_WHO}(?:da )?(?:tieu|chi|xai|chi tieu|tieu xai)(?: het| mat)? "
    r"(?:bao nhieu|het bao nhieu)(?: tien)?(?: (?:roi|vay|the))?|"
    rf"(?:xem |tong )?(?:chi tieu|tong chi)(?: cua (?:anh|chi|minh|toi))? {_PERIOD}"
    r"(?: (?:bao nhieu|het bao nhieu|the nao|ra sao))?(?: (?:roi|vay))?"
)


async def _read_list(user_id: int, core: str, role: str) -> CommandRun | None:
    from services import tools

    if _REMINDER_LIST_RE.fullmatch(core):
        name, args = "list_reminders", {}
    elif _EXPENSE_SUMMARY_RE.fullmatch(core):
        name, args = "expense_summary", {"period": core}
    else:
        return None
    spec = reg.get(name)
    if spec is None or role not in spec.roles:
        return None
    outcome = await tools.execute(spec, user_id, args)
    if outcome.status != reg.STATUS_OK or not outcome.text:
        return None  # lỗi tra: để đường router/chat cũ lo
    return CommandRun((outcome.text,))


async def _structured(user_id: int, text: str, low: str) -> CommandRun | None:
    """Tử vi / thần số học / kỳ môn / tra lịch đọc được bằng code."""
    waiting = pending(user_id)
    for step in (
        lambda: _tuvi(user_id, text, low, waiting),
        lambda: _tsh(user_id, text, low, waiting),
        lambda: _battu_phong_thuy(user_id, text, low),
        lambda: _kymon_or_lich(user_id, text, low),
    ):
        result = await step()
        if result is not None:
            return result
    return None


async def maybe_handle(
    user_id: int,
    text: str,
    *,
    channel: str,
    is_admin: bool = False,
    allow_side_effects: bool = True,
) -> CommandRun | None:
    """Đường code của "hỏi tự nhiên thay cho lệnh"; None = đi tiếp đường cũ."""
    text = nfc(text or "").strip()
    if not enabled() or not text or text.startswith("/") or len(text) > 300:
        return None
    role = role_for(user_id, is_admin)
    core = _core(text)
    low = fold(text)
    howto = _howto(core, role, channel, user_id)
    if howto is not None:
        return CommandRun((howto,))
    if _capability_question(core):
        you, _ = _forms(user_id)
        return CommandRun((short_help(role, channel, you),))
    for pattern, name in _DANGER_PATTERNS:
        if pattern.fullmatch(core):
            entry = catalog.get(name)
            if entry is not None and catalog.visible(entry, role, channel):
                return CommandRun((_suggest_text(user_id, entry, "", channel)[0],))
    from services import tools

    with turn_scope(channel, is_admin), tools.turn_text_scope(text):
        structured = await _structured(user_id, text, low) or await _read_list(user_id, core, role)
        if structured is not None:
            return structured
        for command in COMMANDS.values():
            if not command.patterns or role not in command.spec.roles:
                continue
            if command.spec.side_effect and not allow_side_effects:
                continue
            if any(p.fullmatch(core) for p in command.patterns):
                args = dict(command.pattern_args)
                args.pop("_setting", None)
                return await _run_spec(command.spec.name, user_id, args)
    return None
