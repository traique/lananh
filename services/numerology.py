"""Thần số học (Pythagoras) cho /tsh và /tsh_chitiet.

Mọi con số tính bằng code; ý nghĩa lấy từ ``assets/than_so_hoc.yaml`` (soạn
sẵn, không tốn AI). ``explain_with_ai`` (chỉ /tsh_chitiet) đưa các con số ĐÃ
TÍNH cho AI diễn giải dài hơn - AI không được tự tính lại.

    /tsh 15/3/1990
    /tsh 15/3/1990 Nguyễn Văn An
"""

from __future__ import annotations

import datetime as dt
import functools
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_TEXTS_PATH = Path(__file__).resolve().parent.parent / "assets" / "than_so_hoc.yaml"
_MASTER = (11, 22, 33)
_DATE_RE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
_VOWELS = set("AEIOUY")
# Lưới biểu đồ ngày sinh (hàng trên xuống): 3 6 9 / 2 5 8 / 1 4 7.
_GRID = ((3, 6, 9), (2, 5, 8), (1, 4, 7))
_ARROWS = ("1-2-3", "4-5-6", "7-8-9", "1-4-7", "2-5-8", "3-6-9", "1-5-9", "3-5-7")

USAGE = (
    "Cú pháp: /tsh <ngày/tháng/năm sinh dương lịch> [họ tên]\n"
    "Ví dụ: /tsh 15/3/1990 hoặc /tsh 15/3/1990 Nguyễn Văn An\n"
    "/tsh_chitiet <như trên> - AI diễn giải chi tiết (tốn 1 lượt AI)."
)
DISCLAIMER = "ℹ️ Thần số học mang tính tham khảo, giải trí."


@dataclass
class Reading:
    birth: dt.date
    name: str = ""
    life_path: int = 0
    day_number: int = 0
    attitude: int = 0
    personal_year: int = 0
    personal_month: int = 0
    for_year: int = 0
    counts: dict[int, int] = field(default_factory=dict)
    arrows: list[str] = field(default_factory=list)
    empty_arrows: list[str] = field(default_factory=list)
    soul: int | None = None
    personality: int | None = None
    expression: int | None = None


@functools.lru_cache(maxsize=1)
def _texts() -> dict:
    with _TEXTS_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _digits_sum(n: int) -> int:
    return sum(int(c) for c in str(n))


def reduce_number(n: int, keep_master: bool = True) -> int:
    """Rút gọn về 1-9 (giữ 11/22/33 nếu keep_master)."""
    while n > 9 and not (keep_master and n in _MASTER):
        n = _digits_sum(n)
    return n


def life_path(birth: dt.date) -> int:
    """Số chủ đạo theo cách phổ biến ở VN: cộng mọi chữ số ngày sinh; giữ 10,
    11, 22, 33 (không có số 1)."""
    total = sum(int(c) for c in birth.strftime("%d%m%Y"))
    while total > 11 and total not in (22, 33):
        total = _digits_sum(total)
    return total


def _letters(name: str) -> str:
    text = name.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Za-z]", "", text).upper()


def _letter_value(ch: str) -> int:
    return (ord(ch) - ord("A")) % 9 + 1


def compute(birth: dt.date, name: str = "", today: dt.date | None = None) -> Reading:
    today = today or dt.datetime.now(_VN_TZ).date()
    digits = [int(c) for c in birth.strftime("%d%m%Y") if c != "0"]
    counts = {d: digits.count(d) for d in range(1, 10)}
    arrows, empty = [], []
    for arrow in _ARROWS:
        members = [int(x) for x in arrow.split("-")]
        if all(counts[m] for m in members):
            arrows.append(arrow)
        elif not any(counts[m] for m in members):
            empty.append(arrow)
    personal_year = reduce_number(
        _digits_sum(birth.day) + _digits_sum(birth.month) + _digits_sum(today.year),
        keep_master=False,
    )
    reading = Reading(
        birth=birth,
        name=name.strip(),
        life_path=life_path(birth),
        day_number=reduce_number(birth.day),
        attitude=reduce_number(birth.day + birth.month, keep_master=False),
        personal_year=personal_year,
        personal_month=reduce_number(personal_year + today.month, keep_master=False),
        for_year=today.year,
        counts=counts,
        arrows=arrows,
        empty_arrows=empty,
    )
    letters = _letters(name)
    if letters:
        vowels = [c for c in letters if c in _VOWELS]
        consonants = [c for c in letters if c not in _VOWELS]
        reading.soul = reduce_number(sum(map(_letter_value, vowels))) if vowels else None
        reading.personality = (
            reduce_number(sum(map(_letter_value, consonants))) if consonants else None
        )
        reading.expression = reduce_number(sum(map(_letter_value, letters)))
    return reading


def parse(argument: str) -> tuple[dt.date, str] | str:
    """(ngày sinh, họ tên) hoặc câu báo lỗi."""
    m = _DATE_RE.search(argument or "")
    if not m:
        return USAGE
    try:
        birth = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return "Ngày sinh không hợp lệ.\n" + USAGE
    if not 1900 <= birth.year <= 2100:
        return "Năm sinh cần trong khoảng 1900-2100."
    name = (argument[: m.start()] + " " + argument[m.end() :]).strip(" ,-")
    return birth, re.sub(r"\s+", " ", name)


def _meaning(n: int | None) -> str:
    if n is None:
        return ""
    return str(_texts().get("so", {}).get(n, ""))


def _grid_text(counts: dict[int, int]) -> str:
    rows = []
    for row in _GRID:
        cells = [(str(d) * counts[d]) if counts[d] else "·" for d in row]
        rows.append(" | ".join(f"{c:<4}" for c in cells).rstrip())
    return "\n".join(rows)


def format_reading(r: Reading) -> str:
    texts = _texts()
    lines = [f"🔢 Thần số học - sinh {r.birth:%d/%m/%Y}" + (f" - {r.name}" if r.name else "")]
    lines.append(f"\n⭐ Con số chủ đạo: {r.life_path}")
    lines.append(str(texts.get("chu_dao", {}).get(r.life_path, "")))
    lines.append(f"\n• Số ngày sinh: {r.day_number} - {_meaning(r.day_number)}")
    lines.append(f"• Số thái độ: {r.attitude} - {_meaning(r.attitude)}")
    year_text = texts.get("nam_ca_nhan", {}).get(r.personal_year, "")
    lines.append(f"• Năm cá nhân {r.for_year}: {r.personal_year} - {year_text}")
    lines.append(f"• Tháng cá nhân hiện tại: {r.personal_month}")
    if r.expression is not None:
        lines.append(f"• Số sứ mệnh (cả tên): {r.expression} - {_meaning(r.expression)}")
    if r.soul is not None:
        lines.append(f"• Số linh hồn (nguyên âm): {r.soul} - {_meaning(r.soul)}")
    if r.personality is not None:
        lines.append(f"• Số nhân cách (phụ âm): {r.personality} - {_meaning(r.personality)}")
    lines.append("\n📊 Biểu đồ ngày sinh:\n" + _grid_text(r.counts))
    missing = [str(d) for d in range(1, 10) if not r.counts[d]]
    if missing:
        lines.append(f"Số thiếu: {', '.join(missing)}")
    for arrow in r.arrows:
        lines.append("➚ " + str(texts.get("mui_ten", {}).get(arrow, arrow)))
    for arrow in r.empty_arrows:
        lines.append("○ " + str(texts.get("mui_ten_trong", {}).get(arrow, arrow)))
    lines.append(f"\n{DISCLAIMER} Gõ /tsh_chitiet để AI diễn giải chi tiết hơn.")
    return "\n".join(lines)


def build_ai_prompt(r: Reading) -> str:
    return (
        "Bạn là chuyên gia thần số học Pythagoras, viết tiếng Việt, giọng ấm áp, "
        "thực tế, không mê tín, không hù doạ. Dưới đây là các con số ĐÃ ĐƯỢC TÍNH SẴN "
        "bằng code - TUYỆT ĐỐI KHÔNG tự tính lại hay đổi bất kỳ con số nào, chỉ diễn "
        "giải. Viết khoảng 350-500 từ, chia mục ngắn: Tổng quan con số chủ đạo; Điểm "
        "mạnh; Thách thức và lời khuyên; Năm cá nhân hiện tại nên tập trung gì; Ý nghĩa "
        "biểu đồ ngày sinh (mũi tên, số thiếu). Không dùng bảng markdown. Cuối bài nhắc "
        "nhẹ đây là tham khảo.\n\n"
        f"Dữ liệu (đã tính):\n{format_reading(r)}"
    )


async def explain_with_ai(r: Reading) -> str:
    """Diễn giải bằng AI (provider-chain, có hạn chót lượt). Raise khi AI lỗi."""
    from ai import orchestrator
    from ai.timeouts import turn_deadline

    with turn_deadline():
        response = await orchestrator.ask(build_ai_prompt(r))
    return (getattr(response, "text", "") or "").strip()
