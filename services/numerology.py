"""Thần số học Pythagoras cho /tsh, /tsh_chitiet và trang /than-so-hoc.

Theo hệ phổ biến ở Việt Nam (sách "Thần số học" - Lê Đỗ Quỳnh Hương, gốc
David A. Phillips), có đối chiếu với cách tính Pythagoras phương Tây. Quy ước:

- Con số chủ đạo (ruling number): cộng MỌI chữ số của ngày sinh dương lịch,
  rút gọn tới khi còn 2..11 hoặc 22, 33. Hệ này có số 10, KHÔNG có số 1
  (28 -> 10 chứ không rút tiếp về 1; 29 -> 11; 22, 33 giữ nguyên).
  ``single`` là bản rút gọn 1..9 (10 -> 1, 11 -> 2, 22 -> 4, 33 -> 6) dùng
  để tính tuổi các đỉnh cao và số trưởng thành.
- Số ngày sinh: rút gọn ngày sinh về 1..9 (29 -> 11 -> 2, như các trang VN;
  Decoz giữ 11/22). Số thái độ: ngày + tháng, rút gọn về 1..9.
- Số sứ mệnh (expression) = cả tên; số linh hồn (soul urge) = nguyên âm;
  số nhân cách (personality) = phụ âm. Bảng Pythagoras A=1 ... I=9, J=1 ...
  Bỏ dấu tiếng Việt, Đ -> D. Rút gọn TỪNG TỪ (giữ 11/22/33) rồi cộng các từ
  và rút gọn lần nữa (cách Decoz và tracuuthansohoc; cộng liền một chuỗi có
  thể tạo/che số bậc thầy). Y là nguyên âm khi không đứng cạnh nguyên âm khác
  trong từ (Ly, My, Kỳ: nguyên âm; Nguyên, Huy, Thúy, Yến: phụ âm).
- Số trưởng thành (maturity) = chủ đạo + sứ mệnh, giữ 11/22/33 (chủ đạo 10
  tính là 1).
- 4 đỉnh cao (pinnacles): tháng+ngày; ngày+năm; đỉnh 1 + đỉnh 2; tháng+năm
  (mỗi thành phần đã rút gọn về 1..9; đỉnh 1, 2 rút gọn về 1..9, đỉnh 3, 4
  giữ 11/22 - "kim tự tháp" của các trang VN). Tuổi đỉnh 1 = 36 - số chủ đạo
  (10 -> 26, 11 -> 25, 22 -> 32, 33 -> 30), mỗi đỉnh sau cách 9 năm; giai
  đoạn 1 từ lúc sinh tới tuổi đỉnh 1, các giai đoạn sau 9 năm, giai đoạn 4
  tới hết đời.
- 4 thử thách (challenges): hiệu tuyệt đối |tháng-ngày|, |ngày-năm|,
  |thử thách 1 - thử thách 2|, |tháng-năm| (thành phần rút gọn 1..9, kết quả
  0..8), cùng mốc tuổi với đỉnh cao.
- Năm cá nhân = ngày + tháng sinh + năm dương lịch hiện tại (rút gọn 1..9,
  tính theo năm dương lịch); tháng cá nhân = năm cá nhân + tháng hiện tại.
- Biểu đồ ngày sinh: đếm chữ số 1-9 của ngày/tháng/năm sinh (bỏ 0); 8 mũi tên
  đủ và mũi tên trống theo Phillips. Biểu đồ tên: đếm giá trị 1-9 của các chữ
  trong họ tên; biểu đồ tổng hợp = ngày sinh + tên.

Mọi con số tính bằng code; ý nghĩa lấy từ ``assets/than_so_hoc.yaml``.
``explain_with_ai`` (chỉ /tsh_chitiet) đưa các con số ĐÃ TÍNH cho AI diễn giải
dài hơn - AI không được tự tính lại.

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
_VOWELS = set("AEIOU")
# Lưới biểu đồ ngày sinh (hàng trên xuống): 3 6 9 / 2 5 8 / 1 4 7.
_GRID = ((3, 6, 9), (2, 5, 8), (1, 4, 7))
_ARROWS = ("1-2-3", "4-5-6", "7-8-9", "1-4-7", "2-5-8", "3-6-9", "1-5-9", "3-5-7")
# Tuổi đỉnh 1 = 36 - số chủ đạo (10/11 giữ nguyên, 22/33 rút gọn); các đỉnh sau cách 9 năm.
_STAGE_BASE = 36
_STAGE_LEN = 9

USAGE = (
    "Cú pháp: /tsh <ngày/tháng/năm sinh dương lịch> [họ tên]\n"
    "Ví dụ: /tsh 15/3/1990 hoặc /tsh 15/3/1990 Nguyễn Văn An\n"
    "/tsh_chitiet <như trên> - AI diễn giải chi tiết (tốn 1 lượt AI)."
)
DISCLAIMER = "ℹ️ Thần số học mang tính tham khảo, giải trí."
NAME_ERROR = "Họ tên cần viết bằng chữ Latin (A-Z, có dấu được)."


@dataclass
class Stage:
    """Một giai đoạn đời: đỉnh cao hoặc thử thách, từ tuổi from_age tới to_age
    (to_age None = tới hết đời); peak_age là "tuổi đỉnh" theo kim tự tháp VN."""

    value: int
    from_age: int
    to_age: int | None
    peak_age: int = 0

    def contains(self, age: int) -> bool:
        return age >= self.from_age and (self.to_age is None or age <= self.to_age)


@dataclass
class Reading:
    birth: dt.date
    name: str = ""
    life_path: int = 0
    life_path_single: int = 0
    day_number: int = 0
    attitude: int = 0
    personal_year: int = 0
    personal_month: int = 0
    for_year: int = 0
    as_of: dt.date = dt.date(1900, 1, 1)  # ngày tính năm/tháng cá nhân, tuổi
    age: int = 0
    counts: dict[int, int] = field(default_factory=dict)
    arrows: list[str] = field(default_factory=list)
    empty_arrows: list[str] = field(default_factory=list)
    pinnacles: list[Stage] = field(default_factory=list)
    challenges: list[Stage] = field(default_factory=list)
    soul: int | None = None
    personality: int | None = None
    expression: int | None = None
    maturity: int | None = None
    name_counts: dict[int, int] | None = None

    @property
    def combined_counts(self) -> dict[int, int] | None:
        """Biểu đồ tổng hợp = biểu đồ ngày sinh + biểu đồ tên (có tên mới có)."""
        if self.name_counts is None:
            return None
        return {d: self.counts[d] + self.name_counts[d] for d in range(1, 10)}


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
    """Con số chủ đạo theo hệ Phillips/VN: cộng mọi chữ số ngày sinh; giữ 10,
    11, 22, 33 (không có số 1)."""
    total = sum(int(c) for c in birth.strftime("%d%m%Y"))
    while total > 11 and total not in (22, 33):
        total = _digits_sum(total)
    return total


def _ascii(name: str) -> str:
    text = name.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    return "".join(c for c in text if unicodedata.category(c) != "Mn").upper()


def _words(name: str) -> list[str]:
    """Các từ (chỉ chữ cái Latin, bỏ dấu, viết hoa) của họ tên."""
    return [w for w in re.split(r"[^A-Z]+", _ascii(name)) if w]


def _letters(name: str) -> str:
    return "".join(_words(name))


def has_letters(name: str) -> bool:
    """Tên có ít nhất 1 chữ cái Latin (sau khi bỏ dấu) để tính các số theo tên."""
    return bool(_letters(name))


def _letter_value(ch: str) -> int:
    return (ord(ch) - ord("A")) % 9 + 1


def _is_vowel(word: str, i: int) -> bool:
    """A E I O U luôn là nguyên âm; Y là nguyên âm khi không kề nguyên âm khác."""
    ch = word[i]
    if ch in _VOWELS:
        return True
    if ch != "Y":
        return False
    around = word[max(i - 1, 0) : i] + word[i + 1 : i + 2]
    return not any(c in _VOWELS for c in around)


def _name_number(name: str, keep) -> int | None:
    """Rút gọn từng từ (giữ số bậc thầy) rồi cộng và rút gọn lần nữa; ``keep``
    chọn chữ cái (toàn bộ / nguyên âm / phụ âm). None nếu không có chữ nào."""
    totals = []
    for word in _words(name):
        values = [_letter_value(c) for i, c in enumerate(word) if keep(word, i)]
        if values:
            totals.append(reduce_number(sum(values)))
    return reduce_number(sum(totals)) if totals else None


def _arrows(counts: dict[int, int]) -> tuple[list[str], list[str]]:
    """(mũi tên đủ, mũi tên trống) của một biểu đồ 3x3."""
    arrows, empty = [], []
    for arrow in _ARROWS:
        members = [int(x) for x in arrow.split("-")]
        if all(counts[m] for m in members):
            arrows.append(arrow)
        elif not any(counts[m] for m in members):
            empty.append(arrow)
    return arrows, empty


def first_peak_age(lp: int) -> int:
    """Tuổi đỉnh cao 1 = 36 - số chủ đạo (10, 11 giữ nguyên; 22 -> 4, 33 -> 6)."""
    return _STAGE_BASE - (lp if lp <= 11 else reduce_number(lp, keep_master=False))


def _stages(values: list[int], lp: int) -> list[Stage]:
    peak = first_peak_age(lp)
    stages = []
    start = 0
    for i, value in enumerate(values):
        last = i == len(values) - 1
        stages.append(Stage(value, start, None if last else peak, peak))
        start, peak = peak + 1, peak + _STAGE_LEN
    return stages


def pinnacles_and_challenges(birth: dt.date, lp: int) -> tuple[list[Stage], list[Stage]]:
    """4 đỉnh cao và 4 thử thách (thành phần ngày/tháng/năm rút gọn về 1..9;
    đỉnh 1, 2 rút gọn hẳn, đỉnh 3, 4 giữ 11/22)."""
    d, m, y = (reduce_number(x, keep_master=False) for x in (birth.day, birth.month, birth.year))
    p1, p2 = reduce_number(m + d, keep_master=False), reduce_number(d + y, keep_master=False)
    p3, p4 = reduce_number(p1 + p2), reduce_number(m + y)
    c1, c2 = abs(m - d), abs(d - y)
    c3, c4 = abs(c1 - c2), abs(m - y)
    return _stages([p1, p2, p3, p4], lp), _stages([c1, c2, c3, c4], lp)


def compute(birth: dt.date, name: str = "", today: dt.date | None = None) -> Reading:
    today = today or dt.datetime.now(_VN_TZ).date()
    digits = [int(c) for c in birth.strftime("%d%m%Y") if c != "0"]
    counts = {d: digits.count(d) for d in range(1, 10)}
    arrows, empty = _arrows(counts)
    personal_year = reduce_number(
        _digits_sum(birth.day) + _digits_sum(birth.month) + _digits_sum(today.year),
        keep_master=False,
    )
    lp = life_path(birth)
    single = reduce_number(lp, keep_master=False)
    pinnacles, challenges = pinnacles_and_challenges(birth, lp)
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    reading = Reading(
        birth=birth,
        name=name.strip(),
        life_path=lp,
        life_path_single=single,
        day_number=reduce_number(birth.day, keep_master=False),
        attitude=reduce_number(birth.day + birth.month, keep_master=False),
        personal_year=personal_year,
        personal_month=reduce_number(personal_year + today.month, keep_master=False),
        for_year=today.year,
        as_of=today,
        age=max(age, 0),
        counts=counts,
        arrows=arrows,
        empty_arrows=empty,
        pinnacles=pinnacles,
        challenges=challenges,
    )
    letters = _letters(name)
    if letters:
        reading.soul = _name_number(name, _is_vowel)
        reading.personality = _name_number(name, lambda w, i: not _is_vowel(w, i))
        reading.expression = _name_number(name, lambda w, i: True)
        base = lp if lp in _MASTER else single
        reading.maturity = reduce_number(base + reading.expression)
        values = [_letter_value(c) for c in letters]
        reading.name_counts = {d: values.count(d) for d in range(1, 10)}
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


def _meaning(n: int | None, key: str = "so") -> str:
    if n is None:
        return ""
    return str(_texts().get(key, {}).get(n, ""))


def _grid_text(counts: dict[int, int]) -> str:
    width = max(4, *counts.values())
    rows = []
    for row in _GRID:
        cells = [(str(d) * counts[d]) if counts[d] else "·" for d in row]
        rows.append(" | ".join(f"{c:<{width}}" for c in cells).rstrip())
    return "\n".join(rows)


def stage_label(s: Stage) -> str:
    span = f"{s.from_age}-{s.to_age} tuổi" if s.to_age is not None else f"từ {s.from_age} tuổi"
    return f"{span}, đỉnh ở tuổi {s.peak_age}"


def format_reading(r: Reading) -> str:
    texts = _texts()
    lines = [f"🔢 Thần số học - sinh {r.birth:%d/%m/%Y}" + (f" - {r.name}" if r.name else "")]
    lines.append(f"\n⭐ Con số chủ đạo: {r.life_path}")
    lines.append(str(texts.get("chu_dao", {}).get(r.life_path, "")))
    lines.append(f"\n• Số ngày sinh: {r.day_number} - {_meaning(r.day_number)}")
    lines.append(f"• Số thái độ: {r.attitude} - {_meaning(r.attitude)}")
    year_text = texts.get("nam_ca_nhan", {}).get(r.personal_year, "")
    lines.append(f"• Năm cá nhân {r.for_year}: {r.personal_year} - {year_text}")
    lines.append(
        f"• Tháng cá nhân (tháng {r.as_of.month}): {r.personal_month} - "
        f"{_meaning(r.personal_month, 'thang_ca_nhan')}"
    )
    if r.expression is not None:
        lines.append(f"• Số sứ mệnh (cả tên): {r.expression} - {_meaning(r.expression)}")
    if r.soul is not None:
        lines.append(f"• Số linh hồn (nguyên âm): {r.soul} - {_meaning(r.soul)}")
    if r.personality is not None:
        lines.append(f"• Số nhân cách (phụ âm): {r.personality} - {_meaning(r.personality)}")
    if r.maturity is not None:
        lines.append(f"• Số trưởng thành (sau 35-40 tuổi): {r.maturity} - {_meaning(r.maturity)}")
    lines.append(f"\n🏔 4 đỉnh cao (hiện {r.age} tuổi):")
    for i, s in enumerate(r.pinnacles, 1):
        now = " ◀ hiện tại" if s.contains(r.age) else ""
        lines.append(f"  {i}. {stage_label(s)}: {s.value} - {_meaning(s.value, 'dinh_cao')}{now}")
    lines.append("⛰ 4 thử thách:")
    for i, s in enumerate(r.challenges, 1):
        now = " ◀ hiện tại" if s.contains(r.age) else ""
        lines.append(f"  {i}. {stage_label(s)}: {s.value} - {_meaning(s.value, 'thu_thach')}{now}")
    lines.append("\n📊 Biểu đồ ngày sinh:\n" + _grid_text(r.counts))
    missing = [str(d) for d in range(1, 10) if not r.counts[d]]
    if missing:
        lines.append(f"Số thiếu: {', '.join(missing)}")
    for arrow in r.arrows:
        lines.append("➚ " + str(texts.get("mui_ten", {}).get(arrow, arrow)))
    for arrow in r.empty_arrows:
        lines.append("○ " + str(texts.get("mui_ten_trong", {}).get(arrow, arrow)))
    if r.combined_counts is not None:
        lines.append("\n📊 Biểu đồ tổng hợp (ngày sinh + tên):\n" + _grid_text(r.combined_counts))
    lines.append(f"\n{DISCLAIMER} Gõ /tsh_chitiet để AI diễn giải chi tiết hơn.")
    return "\n".join(lines)


def reading_data(r: Reading) -> dict:
    """Kết quả dạng dict khoá camelCase cho trang web và JSON API (hợp đồng
    công khai: chỉ THÊM trường, không đổi/bỏ trường cũ)."""
    texts = _texts()
    meaning = texts.get("so", {})

    def number(value: int | None) -> dict | None:
        return None if value is None else {"value": value, "meaning": meaning.get(value, "")}

    def stages(items: list[Stage], key: str) -> list[dict]:
        return [
            {
                "value": s.value,
                "fromAge": s.from_age,
                "toAge": s.to_age,
                "peakAge": s.peak_age,
                "label": stage_label(s),
                "meaning": texts.get(key, {}).get(s.value, ""),
                "isCurrent": s.contains(r.age),
            }
            for s in items
        ]

    def grid(counts: dict[int, int] | None) -> dict | None:
        return None if counts is None else {str(d): counts[d] for d in range(1, 10)}

    return {
        "birthDate": r.birth.isoformat(),
        "name": r.name,
        # Các trường theo thời điểm (age, personalYear/Month, isCurrent) tính tại asOf (giờ VN).
        "asOf": r.as_of.isoformat(),
        "age": r.age,
        "lifePath": {
            "value": r.life_path,
            "single": r.life_path_single,
            "meaning": texts.get("chu_dao", {}).get(r.life_path, ""),
        },
        "dayNumber": number(r.day_number),
        "attitude": number(r.attitude),
        "personalYear": {
            "year": r.for_year,
            "value": r.personal_year,
            "meaning": texts.get("nam_ca_nhan", {}).get(r.personal_year, ""),
        },
        "personalMonth": r.personal_month,
        "personalMonthDetail": {
            "month": r.as_of.month,
            "value": r.personal_month,
            "meaning": texts.get("thang_ca_nhan", {}).get(r.personal_month, ""),
        },
        "expression": number(r.expression),
        "soul": number(r.soul),
        "personality": number(r.personality),
        "maturity": number(r.maturity),
        "pinnacles": stages(r.pinnacles, "dinh_cao"),
        "challenges": stages(r.challenges, "thu_thach"),
        "chart": grid(r.counts),
        "nameChart": grid(r.name_counts),
        "combinedChart": grid(r.combined_counts),
        "missingDigits": [d for d in range(1, 10) if not r.counts[d]],
        "arrows": [{"digits": a, "meaning": texts.get("mui_ten", {}).get(a, "")} for a in r.arrows],
        "emptyArrows": [
            {"digits": a, "meaning": texts.get("mui_ten_trong", {}).get(a, "")}
            for a in r.empty_arrows
        ],
        "disclaimer": DISCLAIMER,
    }


def build_ai_prompt(r: Reading) -> str:
    return (
        "Bạn là chuyên gia thần số học Pythagoras, viết tiếng Việt, giọng ấm áp, "
        "thực tế, không mê tín, không hù doạ. Dưới đây là các con số ĐÃ ĐƯỢC TÍNH SẴN "
        "bằng code - TUYỆT ĐỐI KHÔNG tự tính lại hay đổi bất kỳ con số nào, chỉ diễn "
        "giải. Viết khoảng 400-550 từ, chia mục ngắn: Tổng quan con số chủ đạo; Điểm "
        "mạnh; Thách thức và lời khuyên; Đỉnh cao và thử thách của giai đoạn hiện tại; "
        "Năm cá nhân hiện tại nên tập trung gì; Ý nghĩa biểu đồ ngày sinh (mũi tên, số "
        "thiếu). Không dùng bảng markdown. Cuối bài nhắc nhẹ đây là tham khảo.\n\n"
        f"Dữ liệu (đã tính):\n{format_reading(r)}"
    )


async def explain_with_ai(r: Reading) -> str:
    """Diễn giải bằng AI (provider-chain, có hạn chót lượt). Raise khi AI lỗi."""
    from ai import orchestrator
    from ai.timeouts import turn_deadline

    with turn_deadline():
        response = await orchestrator.ask(build_ai_prompt(r))
    return (getattr(response, "text", "") or "").strip()
