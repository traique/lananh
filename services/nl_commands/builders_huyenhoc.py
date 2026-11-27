"""Lệnh huyền học và tra lịch chạy từ câu tự nhiên: tử vi, thần số học, bát tự, thước
Lỗ Ban, cung phi, kỳ môn, lịch (services/nl_commands)."""

from __future__ import annotations

import re
from typing import Any

from core.text_normalize import fold
from services import tool_registry as reg
from services.nl_commands.registry import Ask, _clean, _cmd, _truthy, _wants_detail
from services.nl_commands.turn import _turn_text

_DATE_RE = re.compile(r"\b(\d{1,2})\s*[/.-]\s*(\d{1,2})n?\s*[/.-]\s*(\d{4})\b")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DIGIT_RE = re.compile(r"\d")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _date_text(value: Any) -> str:
    """Ngày sinh "1990-03-15" | "15/3/1990" | câu có ngày -> "15/3/1990"; không có -> ""."""
    raw = _clean(value, 300)
    iso = _ISO_DATE_RE.fullmatch(raw)
    if iso:
        return f"{int(iso.group(3))}/{int(iso.group(2))}/{iso.group(1)}"
    m = _DATE_RE.search(raw)
    return f"{int(m.group(1))}/{int(m.group(2))}/{m.group(3)}" if m else ""


_TUVI_ASK = (
    "{You} cho {me} xin ngày sinh, giờ sinh và giới tính để {me} an lá số nha, "
    "vd: 15/3/1990 9h30 nam (ngày âm thì ghi thêm chữ “âm”)."
)


def _build_tuvi(args: dict[str, Any]) -> str | Ask:
    detail = _wants_detail(args)
    date, time_, gender = _date_text(args.get("date")), _clean(args.get("time"), 30), ""
    gender_raw = fold(_clean(args.get("gender"), 10))
    if gender_raw in {"nam", "male", "trai"}:
        gender = "nam"
    elif gender_raw in {"nu", "female", "gai"}:
        gender = "nữ"
    missing = [
        label
        for label, value in (("ngày sinh", date), ("giờ sinh", time_), ("giới tính", gender))
        if not value
    ]
    if missing:
        return Ask(_TUVI_ASK, detail=detail)
    lunar = "âm " if _truthy(args.get("lunar")) else ""
    command = "/tuvi_chitiet" if detail else "/tuvi"
    return f"{command} {lunar}{date} {time_} {gender}"


_TSH_ASK = "{You} cho {me} xin ngày sinh dương lịch (vd 15/3/1990), có thể kèm họ tên nha."


def _build_tsh(args: dict[str, Any]) -> str | Ask:
    detail = _wants_detail(args)
    date = _date_text(args.get("date"))
    if not date:
        return Ask(_TSH_ASK, detail=detail)
    name = _clean(args.get("name"), 60)
    command = "/tsh_chitiet" if detail else "/tsh"
    return f"{command} {date} {name}".strip()


_KYMON_ASK = (
    "{You} muốn {me} luận giải việc gì trên bàn Kỳ Môn? Vd: “9h sáng mai ký hợp đồng có "
    "thuận không”."
)


def _build_kymon(args: dict[str, Any]) -> str | Ask:
    when = _clean(args.get("when"), 40)
    question = _clean(args.get("question"), 300)
    if _truthy(args.get("detail")) or question:
        if not question:
            return Ask(_KYMON_ASK, detail=True)
        return f"/kymon_chitiet {when} {question}".replace("  ", " ").strip()
    return f"/kymon {when}".strip()


def _ai_detail(args: dict[str, Any]) -> bool:
    return _wants_detail(args)


def _gender_word(value: Any) -> str:
    raw = fold(_clean(value, 10))
    if raw in {"nam", "male", "trai"}:
        return "nam"
    return "nữ" if raw in {"nu", "female", "gai"} else ""


_BATTU_ASK = (
    "{You} cho {me} xin ngày sinh dương lịch, giờ sinh (nếu biết) và giới tính nha, vd "
    "15/3/1990 9h30 nam."
)


def _build_battu(args: dict[str, Any]) -> str | Ask:
    detail = _wants_detail(args)
    date, time_ = _date_text(args.get("date")), _clean(args.get("time"), 30)
    gender = _gender_word(args.get("gender"))
    if not date or not gender:
        return Ask(_BATTU_ASK, detail=detail)
    command = "/battu_chitiet" if detail else "/battu"
    return " ".join(part for part in (command, date, time_, gender) if part)


_LOBAN_ASK = "{You} cho {me} xin số đo (vd 80cm) và món cần đo (cửa, giường, bàn thờ...) nha."


def _build_loban(args: dict[str, Any]) -> str | Ask:
    size, item = _clean(args.get("size"), 20), _clean(args.get("item"), 30)
    if not _DIGIT_RE.search(size):
        return Ask(_LOBAN_ASK)
    return f"/loban {size} {item}".strip()


_CUNGPHI_ASK = "{You} cho {me} xin năm sinh (hoặc ngày sinh) và giới tính nha, vd 1990 nữ."


def _build_cungphi(args: dict[str, Any]) -> str | Ask:
    birth = _date_text(args.get("birth")) or _clean(args.get("birth"), 12)
    gender = _gender_word(args.get("gender"))
    if not _YEAR_RE.search(birth) or not gender:
        return Ask(_CUNGPHI_ASK)
    return f"/cungphi {birth} {gender}"


_cmd(
    "cmd_tuvi",
    "tuvi",
    "tử vi",
    "huyen_hoc",
    "An lá số tử vi (code tính).",
    '{"date": ngày sinh, "time": giờ sinh, "gender": nam|nữ, "lunar": ngày âm?, "detail": '
    "muốn AI luận giải}. Thiếu thì để rỗng (hệ thống hỏi lại), KHÔNG đoán.",
    {
        "date": reg.string("ngày sinh dd/mm/yyyy"),
        "time": reg.string("giờ sinh"),
        "gender": reg.string("nam|nữ"),
        "lunar": reg.boolean("ngày âm"),
        "detail": reg.boolean("AI luận giải"),
    },
    _build_tuvi,
    heavy=_ai_detail,
)
_cmd(
    "cmd_tsh",
    "tsh",
    "thần số học",
    "huyen_hoc",
    "Thần số học theo ngày sinh.",
    '{"date": ngày sinh dương, "name": họ tên nếu có, "detail": muốn AI diễn giải}. Thiếu '
    "ngày sinh để rỗng.",
    {
        "date": reg.string("dd/mm/yyyy"),
        "name": reg.string("họ tên"),
        "detail": reg.boolean("AI diễn giải"),
    },
    _build_tsh,
    heavy=_ai_detail,
)
_cmd(
    "cmd_battu",
    "battu",
    "bát tự",
    "huyen_hoc",
    "Lập lá số Bát Tự (tứ trụ, đại vận; code tính).",
    '{"date": ngày sinh dương, "time": giờ sinh nếu biết, "gender": nam|nữ, "detail": muốn '
    "AI luận giải}. Thiếu thì để rỗng (hệ thống hỏi lại), KHÔNG đoán.",
    {
        "date": reg.string("ngày sinh dd/mm/yyyy"),
        "time": reg.string("giờ sinh"),
        "gender": reg.string("nam|nữ"),
        "detail": reg.boolean("AI luận giải"),
    },
    _build_battu,
    heavy=_ai_detail,
)
_cmd(
    "cmd_loban",
    "loban",
    "thước Lỗ Ban",
    "phong_thuy",
    "Tra thước Lỗ Ban cho 1 số đo (cửa, giường, tủ, bàn thờ...).",
    '{"size": số đo kèm đơn vị, vd "80cm", "1m2", "item": món đồ nếu có, vd "cửa"}.',
    {"size": reg.string("số đo"), "item": reg.string("món đồ")},
    _build_loban,
)
_cmd(
    "cmd_cungphi",
    "cungphi",
    "cung phi",
    "phong_thuy",
    "Cung phi Bát Trạch, hướng nhà tốt xấu theo năm sinh và giới tính.",
    '{"birth": năm sinh hoặc ngày sinh, "gender": nam|nữ}. Thiếu thì để rỗng.',
    {"birth": reg.string("năm/ngày sinh"), "gender": reg.string("nam|nữ")},
    _build_cungphi,
)
_cmd(
    "cmd_kymon",
    "kymon",
    "kỳ môn",
    "huyen_hoc",
    "Lập bàn Kỳ Môn Độn Giáp.",
    '{"when": thời điểm nguyên văn ("" = lúc này), "question": việc cần xem nếu muốn luận '
    'giải}. Vd "kỳ môn 9h mai ký hợp đồng có thuận không".',
    {
        "when": reg.string("thời điểm"),
        "question": reg.string("việc cần xem"),
        "detail": reg.boolean("AI luận giải"),
    },
    _build_kymon,
    heavy=lambda args: bool(_clean(args.get("question"))),
)


def _build_lich(args: dict[str, Any]) -> str | Ask:
    query = _clean(args.get("query"), 120) or _clean(_turn_text(), 120)
    return f"/lich {query}".strip()


_cmd(
    "cmd_lich",
    "lich",
    "tra lịch",
    "lich",
    "Tra lịch: ngày tốt/xấu, ngày lễ, tuổi, sao hạn, lịch tháng.",
    '{"query": câu hỏi gọn, vd "ngày tốt tháng 11", "sao hạn 1990 nam năm 2027"}.',
    {"query": reg.string("câu hỏi lịch")},
    _build_lich,
)
