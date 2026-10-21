"""An lá số Tử Vi Đẩu Số bằng code (/tuvi) + AI luận giải (/tuvi_chitiet).

Các bước an sao theo cách phổ biến ở Việt Nam (Thái Thứ Lang):
1. Đổi ngày sinh sang âm lịch; giờ sinh -> chi giờ (23h-1h Tý, tính cùng ngày).
2. Cung Mệnh: từ Dần là tháng Giêng đếm thuận tới tháng sinh, rồi đếm nghịch
   theo giờ; cung Thân đếm thuận theo giờ. 12 cung xếp thuận từ Mệnh.
3. Can các cung theo Ngũ hổ độn (can năm), Cục theo nạp âm can chi cung Mệnh.
4. Tử Vi theo ngày sinh và số Cục; 13 chính tinh còn lại theo Tử Vi/Thiên Phủ.
5. Phụ tinh theo năm/tháng/giờ/ngày; Tứ hóa theo can năm; vòng Tràng sinh,
   Bác sĩ, Thái tuế; Tuần, Triệt; đại hạn 10 năm.

Tháng nhuận an như tháng chính. Chưa an độ sáng (miếu/vượng/hãm) và một số
phụ tinh hiếm (mỗi trường phái mỗi khác) - ghi rõ ở cuối lá số.
"""

from __future__ import annotations

import datetime as dt
import functools
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from services import can_chi, lunar
from services.weather import fold

CHI = can_chi.CHI
CAN = can_chi.CAN
PALACES = ["Mệnh", "Phụ Mẫu", "Phúc Đức", "Điền Trạch", "Quan Lộc", "Nô Bộc",
           "Thiên Di", "Tật Ách", "Tài Bạch", "Tử Tức", "Phu Thê", "Huynh Đệ"]  # fmt: skip
_CUC = {"Thủy": 2, "Mộc": 3, "Kim": 4, "Thổ": 5, "Hỏa": 6}
_CUC_NAME = {
    2: "Thủy nhị cục",
    3: "Mộc tam cục",
    4: "Kim tứ cục",
    5: "Thổ ngũ cục",
    6: "Hỏa lục cục",
}
_CUC_ELEMENT = {v: k for k, v in _CUC.items()}
_DAN_CAN = [2, 4, 6, 8, 0]  # can của cung Dần theo can năm % 5 (Ngũ hổ độn)
_LOC_TON = [2, 3, 5, 6, 5, 6, 8, 9, 11, 0]  # theo can năm Giáp..Quý
_KHOI_VIET = {0: (1, 7), 4: (1, 7), 1: (0, 8), 5: (0, 8), 2: (11, 9), 3: (11, 9),
              6: (6, 2), 7: (6, 2), 8: (3, 5), 9: (3, 5)}  # fmt: skip
_TRIET = [(8, 9), (6, 7), (4, 5), (2, 3), (0, 1)]  # theo can năm % 5
_TU_HOA = {  # Lộc, Quyền, Khoa, Kỵ theo can năm
    0: ("Liêm Trinh", "Phá Quân", "Vũ Khúc", "Thái Dương"),
    1: ("Thiên Cơ", "Thiên Lương", "Tử Vi", "Thái Âm"),
    2: ("Thiên Đồng", "Thiên Cơ", "Văn Xương", "Liêm Trinh"),
    3: ("Thái Âm", "Thiên Đồng", "Thiên Cơ", "Cự Môn"),
    4: ("Tham Lang", "Thái Âm", "Hữu Bật", "Thiên Cơ"),
    5: ("Vũ Khúc", "Tham Lang", "Thiên Lương", "Văn Khúc"),
    6: ("Thái Dương", "Vũ Khúc", "Thái Âm", "Thiên Đồng"),
    7: ("Cự Môn", "Thái Dương", "Văn Khúc", "Văn Xương"),
    8: ("Thiên Lương", "Tử Vi", "Tả Phù", "Vũ Khúc"),
    9: ("Phá Quân", "Cự Môn", "Thái Âm", "Tham Lang"),
}
_HOA_NAMES = ("Hóa Lộc", "Hóa Quyền", "Hóa Khoa", "Hóa Kỵ")
_TRANG_SINH = ["Tràng Sinh", "Mộc Dục", "Quan Đới", "Lâm Quan", "Đế Vượng", "Suy",
               "Bệnh", "Tử", "Mộ", "Tuyệt", "Thai", "Dưỡng"]  # fmt: skip
_TRANG_SINH_START = {"Thủy": 8, "Thổ": 8, "Mộc": 11, "Kim": 5, "Hỏa": 2}
_BAC_SI = ["Bác Sĩ", "Lực Sĩ", "Thanh Long", "Tiểu Hao", "Tướng Quân", "Tấu Thư",
           "Phi Liêm", "Hỷ Thần", "Bệnh Phù", "Đại Hao", "Phục Binh", "Quan Phủ"]  # fmt: skip
_THAI_TUE = ["Thái Tuế", "Thiếu Dương", "Tang Môn", "Thiếu Âm", "Quan Phù", "Tử Phù",
             "Tuế Phá", "Long Đức", "Bạch Hổ", "Phúc Đức", "Điếu Khách", "Trực Phù"]  # fmt: skip
_MENH_CHU = ["Tham Lang", "Cự Môn", "Lộc Tồn", "Văn Khúc", "Liêm Trinh", "Vũ Khúc",
             "Phá Quân", "Vũ Khúc", "Liêm Trinh", "Văn Khúc", "Lộc Tồn", "Cự Môn"]  # fmt: skip
_THAN_CHU = ["Hỏa Tinh", "Thiên Tướng", "Thiên Lương", "Thiên Đồng", "Văn Xương", "Thiên Cơ",
             "Hỏa Tinh", "Thiên Tướng", "Thiên Lương", "Thiên Đồng", "Văn Xương", "Thiên Cơ"]  # fmt: skip
CHINH_TINH = ("Tử Vi", "Thiên Cơ", "Thái Dương", "Vũ Khúc", "Thiên Đồng", "Liêm Trinh",
              "Thiên Phủ", "Thái Âm", "Tham Lang", "Cự Môn", "Thiên Tướng", "Thiên Lương",
              "Thất Sát", "Phá Quân")  # fmt: skip
_TEXTS_PATH = Path(__file__).resolve().parent.parent / "assets" / "tu_vi.yaml"

USAGE = (
    "Cú pháp: /tuvi <ngày/tháng/năm sinh> <giờ sinh> <nam|nữ>\n"
    "Ví dụ: /tuvi 15/3/1990 9h30 nam\n"
    "Ngày âm: /tuvi âm 20/2/1990 giờ Tỵ nữ (thêm 'n' sau tháng nhuận: âm 20/6n/2025)\n"
    "/tuvi_chitiet <như trên> - AI luận giải lá số (tốn 1 lượt AI)."
)
DISCLAIMER = "ℹ️ Tử vi mang tính tham khảo, giải trí."


@dataclass
class Chart:
    solar: dt.date | None
    lunar_day: int
    lunar_month: int
    lunar_year: int
    leap: bool
    hour_chi: int
    male: bool
    menh: int = 0
    than: int = 0
    cuc: int = 0
    stars: dict[int, list[str]] = field(default_factory=dict)
    trang_sinh: dict[int, str] = field(default_factory=dict)
    dai_han: dict[int, tuple[int, int]] = field(default_factory=dict)
    tuan: tuple[int, int] = (0, 0)
    triet: tuple[int, int] = (0, 0)
    tu_hoa: dict[str, str] = field(default_factory=dict)

    @property
    def can_y(self) -> int:
        return can_chi.year_can(self.lunar_year)

    @property
    def chi_y(self) -> int:
        return can_chi.year_chi(self.lunar_year)

    @property
    def thuan(self) -> bool:
        """Dương nam / Âm nữ đi thuận."""
        return (self.can_y % 2 == 0) == self.male

    def palace_at(self, pos: int) -> str:
        return PALACES[(pos - self.menh) % 12]

    def palace_can(self, pos: int) -> int:
        return (_DAN_CAN[self.can_y % 5] + (pos - 2) % 12) % 10


def tu_vi_position(day: int, cuc: int) -> int:
    q = math.ceil(day / cuc)
    r = q * cuc - day
    base = 2 + q - 1
    if r == 0:
        return base % 12
    return (base - r) % 12 if r % 2 else (base + r) % 12


def build_chart(
    lunar_day: int,
    lunar_month: int,
    lunar_year: int,
    hour_chi: int,
    male: bool,
    leap: bool = False,
    solar: dt.date | None = None,
) -> Chart:
    c = Chart(solar, lunar_day, lunar_month, lunar_year, leap, hour_chi, male)
    h, m, d = hour_chi, lunar_month, lunar_day
    c.menh = (2 + (m - 1) - h) % 12
    c.than = (2 + (m - 1) + h) % 12
    element = can_chi.nap_am(c.palace_can(c.menh), c.menh)[1]
    c.cuc = _CUC[element]
    stars: dict[int, list[str]] = {i: [] for i in range(12)}

    def put(pos: int, name: str) -> None:
        stars[pos % 12].append(name)

    tv = tu_vi_position(d, c.cuc)
    for offset, name in ((0, "Tử Vi"), (-1, "Thiên Cơ"), (-3, "Thái Dương"), (-4, "Vũ Khúc"),
                         (-5, "Thiên Đồng"), (-8, "Liêm Trinh")):  # fmt: skip
        put(tv + offset, name)
    tp = (4 - tv) % 12
    for offset, name in ((0, "Thiên Phủ"), (1, "Thái Âm"), (2, "Tham Lang"), (3, "Cự Môn"),
                         (4, "Thiên Tướng"), (5, "Thiên Lương"), (6, "Thất Sát"),
                         (10, "Phá Quân")):  # fmt: skip
        put(tp + offset, name)

    cy, zy = c.can_y, c.chi_y
    sign = 1 if c.thuan else -1
    # Theo giờ
    put(10 - h, "Văn Xương")
    put(4 + h, "Văn Khúc")
    put(11 - h, "Địa Không")
    put(11 + h, "Địa Kiếp")
    put(6 + h, "Thai Phụ")
    put(2 + h, "Phong Cáo")
    # Theo tháng
    put(4 + (m - 1), "Tả Phù")
    put(10 - (m - 1), "Hữu Bật")
    put(9 + (m - 1), "Thiên Hình")
    put(1 + (m - 1), "Thiên Riêu")
    put(1 + (m - 1), "Thiên Y")
    put(8 + (m - 1), "Thiên Giải")
    put(7 + (m - 1), "Địa Giải")
    # Theo ngày (từ vị trí sao tháng/giờ)
    put(4 + (m - 1) + (d - 1), "Tam Thai")
    put(10 - (m - 1) - (d - 1), "Bát Tọa")
    put(10 - h + (d - 1) - 1, "Ân Quang")
    put(4 + h - (d - 1) + 1, "Thiên Quý")
    # Theo can năm
    loc = _LOC_TON[cy]
    put(loc, "Lộc Tồn")
    put(loc + 1, "Kình Dương")
    put(loc - 1, "Đà La")
    khoi, viet = _KHOI_VIET[cy]
    put(khoi, "Thiên Khôi")
    put(viet, "Thiên Việt")
    # Theo chi năm
    group = next(g for g in ((2, 6, 10), (8, 0, 4), (5, 9, 1), (11, 3, 7)) if zy in g)
    put({(2, 6, 10): 8, (8, 0, 4): 2, (5, 9, 1): 11, (11, 3, 7): 5}[group], "Thiên Mã")
    put({(2, 6, 10): 3, (8, 0, 4): 9, (5, 9, 1): 6, (11, 3, 7): 0}[group], "Đào Hoa")
    put({(2, 6, 10): 10, (8, 0, 4): 4, (5, 9, 1): 1, (11, 3, 7): 7}[group], "Hoa Cái")
    put({(2, 6, 10): 11, (8, 0, 4): 5, (5, 9, 1): 2, (11, 3, 7): 8}[group], "Kiếp Sát")
    hoa_start = {(2, 6, 10): 1, (8, 0, 4): 2, (5, 9, 1): 3, (11, 3, 7): 9}[group]
    linh_start = 3 if group == (2, 6, 10) else 10
    put(hoa_start + sign * h, "Hỏa Tinh")
    put(linh_start - sign * h, "Linh Tinh")
    put(3 - zy, "Hồng Loan")
    put(9 - zy, "Thiên Hỷ")
    put(6 - zy, "Thiên Khốc")
    put(6 + zy, "Thiên Hư")
    put(4 + zy, "Long Trì")
    put(10 - zy, "Phượng Các")
    put(9 + zy, "Thiên Đức")
    put(5 + zy, "Nguyệt Đức")
    co_qua = {0: (2, 10), 1: (2, 10), 11: (2, 10), 2: (5, 1), 3: (5, 1), 4: (5, 1),
              5: (8, 4), 6: (8, 4), 7: (8, 4), 8: (11, 7), 9: (11, 7), 10: (11, 7)}  # fmt: skip
    put(co_qua[zy][0], "Cô Thần")
    put(co_qua[zy][1], "Quả Tú")
    put({0: 5, 6: 5, 3: 5, 9: 5, 2: 9, 8: 9, 5: 9, 11: 9}.get(zy, 1), "Phá Toái")
    put(c.menh + zy, "Thiên Tài")
    put(c.than + zy, "Thiên Thọ")
    # Cố định
    put(4, "Thiên La")
    put(10, "Địa Võng")
    put(c.menh + 5, "Thiên Thương")  # cung Nô Bộc
    put(c.menh + 7, "Thiên Sứ")  # cung Tật Ách

    # Tứ hóa
    for name, hoa in zip(_TU_HOA[cy], _HOA_NAMES, strict=True):
        c.tu_hoa[hoa] = name
        for names in stars.values():
            if name in names:
                names[names.index(name)] = f"{name} ({hoa})"
                break

    # Vòng Bác sĩ (từ Lộc Tồn), Thái tuế (từ chi năm), Tràng sinh (theo Cục)
    for i, name in enumerate(_BAC_SI):
        put(loc + sign * i, name)
    for i, name in enumerate(_THAI_TUE):
        put(zy + i, name)
    start = _TRANG_SINH_START[element]
    c.trang_sinh = {(start + sign * i) % 12: name for i, name in enumerate(_TRANG_SINH)}

    # Đại hạn 10 năm, bắt đầu từ Mệnh ở tuổi = số Cục.
    for k in range(12):
        pos = (c.menh + sign * k) % 12
        c.dai_han[pos] = (c.cuc + 10 * k, c.cuc + 10 * k + 9)

    idx = can_chi.jiazi_index(cy, zy)
    decade_start = idx - idx % 10
    c.tuan = ((decade_start + 10) % 12, (decade_start + 11) % 12)
    c.triet = _TRIET[cy % 5]
    c.stars = stars
    return c


@functools.lru_cache(maxsize=1)
def _texts() -> dict:
    with _TEXTS_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _split_stars(names: list[str]) -> tuple[list[str], list[str]]:
    main = [n for n in names if n.split(" (")[0] in CHINH_TINH]
    other = [n for n in names if n not in main]
    return main, other


def format_chart(c: Chart) -> str:
    born = can_chi.year_profile(c.lunar_year)
    element = _CUC_ELEMENT[c.cuc]
    gender = ("Dương" if c.can_y % 2 == 0 else "Âm") + (" Nam" if c.male else " Nữ")
    solar = f"{c.solar:%d/%m/%Y} dương, " if c.solar else ""
    lines = [
        f"🔮 Lá số Tử Vi - {solar}{lunar.lunar_text(c.lunar_day, c.lunar_month, c.lunar_year, c.leap)} "
        f"âm, giờ {CHI[c.hour_chi]}, {gender}",
        f"Bản mệnh: {born['nap_am']} ({born['menh']}) · Cục: {_CUC_NAME[c.cuc]} · "
        f"{can_chi.ngu_hanh_relation(element, born['menh'])}",
        f"Mệnh chủ: {_MENH_CHU[c.menh]} · Thân chủ: {_THAN_CHU[c.chi_y]} · "
        f"Thân cư {c.palace_at(c.than)}",
        "",
    ]
    for i in range(12):
        pos = (c.menh + i) % 12
        main, other = _split_stars(c.stars[pos])
        flags = []
        if pos == c.than:
            flags.append("Thân")
        if pos in c.tuan:
            flags.append("Tuần")
        if pos in c.triet:
            flags.append("Triệt")
        a, b = c.dai_han[pos]
        head = (
            f"▸ {PALACES[i].upper()} ({CAN[c.palace_can(pos)]} {CHI[pos]})"
            + (f" [{', '.join(flags)}]" if flags else "")
            + f" · ĐH {a}-{b} · {c.trang_sinh[pos]}"
        )
        lines.append(head)
        lines.append("  ★ " + (", ".join(main) if main else "Vô chính diệu"))
        if other:
            lines.append("  · " + ", ".join(other))
    texts = _texts().get("chinh_tinh", {})
    main_menh, _ = _split_stars(c.stars[c.menh])
    lines.append("")
    if main_menh:
        for name in main_menh:
            base = name.split(" (")[0]
            lines.append(f"📌 {base} thủ Mệnh: {texts.get(base, '')}")
    else:
        lines.append("📌 Mệnh vô chính diệu: " + str(_texts().get("vo_chinh_dieu", "")))
    lines.append(
        f"\n{DISCLAIMER} Chưa an độ sáng (miếu/vượng/hãm) và vài phụ tinh hiếm. "
        "Gõ /tuvi_chitiet để AI luận giải."
    )
    return "\n".join(lines)


_HOUR_RE = re.compile(r"\b(\d{1,2})(?:[h:](\d{1,2})?|\s*gio\b)")
_GIO_CHI_RE = re.compile(
    r"\bgi(?:ờ|o)\s+(tý|tí|sửu|dần|mão|mẹo|thìn|tỵ|tị|ngọ|mùi|thân|dậu|tuất|hợi|"
    r"ty|ti|suu|dan|mao|meo|thin|ngo|mui|than|dau|tuat|hoi)\b"
)
_CHI_WORDS = {
    "tý": 0, "tí": 0, "sửu": 1, "suu": 1, "dần": 2, "dan": 2, "mão": 3, "mẹo": 3, "mao": 3,
    "meo": 3, "thìn": 4, "thin": 4, "tỵ": 5, "tị": 5, "ngọ": 6, "ngo": 6, "mùi": 7, "mui": 7,
    "thân": 8, "than": 8, "dậu": 9, "dau": 9, "tuất": 10, "tuat": 10, "hợi": 11, "hoi": 11,
}  # fmt: skip
_DATE_RE = re.compile(r"(\d{1,2})[/.-](\d{1,2})(n)?[/.-](\d{4})")


def parse(argument: str) -> Chart | str:
    """Lá số từ "15/3/1990 9h30 nam" / "âm 20/2/1990 giờ Tỵ nữ", hoặc câu báo lỗi."""
    low = (argument or "").lower().strip()
    m = _DATE_RE.search(low)
    if not m:
        return USAGE
    is_lunar = bool(re.match(r"^(âm|am|al)\b", low)) or bool(m.group(3))
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(4))
    if not 1900 <= y <= 2100:
        return "Năm sinh cần trong khoảng 1900-2100."
    rest = (low[: m.start()] + " " + low[m.end() :]).strip()
    gio = _GIO_CHI_RE.search(rest)
    if gio:
        word = gio.group(1)
        if word in ("ty", "ti"):
            return "Giờ Tý hay giờ Tỵ? Gõ có dấu hoặc ghi giờ cụ thể (vd 23h, 10h)."
        hour_chi = _CHI_WORDS[word]
    else:
        hm = _HOUR_RE.search(fold(rest))
        if not hm or int(hm.group(1)) > 23:
            return "Thiếu giờ sinh.\n" + USAGE
        hour_chi = ((int(hm.group(1)) + 1) // 2) % 12
    if re.search(r"\b(nữ|nu|gái|gai)\b", rest):
        male = False
    elif re.search(r"\b(nam|trai)\b", rest):
        male = True
    else:
        return "Thiếu giới tính (nam/nữ).\n" + USAGE
    if is_lunar:
        leap = bool(m.group(3))
        if lunar.lunar_to_solar(d, mo, y, leap) is None or not (1 <= d <= 30 and 1 <= mo <= 12):
            return "Ngày âm lịch không hợp lệ."
        sd, sm, sy = lunar.lunar_to_solar(d, mo, y, leap)  # type: ignore[misc]
        solar = dt.date(sy, sm, sd)
        if lunar.solar_to_lunar(sd, sm, sy) != (d, mo, y, leap):
            return f"Tháng {mo} âm năm {y} không có ngày {d}."
        return build_chart(d, mo, y, hour_chi, male, leap, solar)
    try:
        solar = dt.date(y, mo, d)
    except ValueError:
        return "Ngày sinh không hợp lệ.\n" + USAGE
    ld, lm, ly, leap = lunar.solar_to_lunar(d, mo, y)
    return build_chart(ld, lm, ly, hour_chi, male, leap, solar)


def build_ai_prompt(c: Chart) -> str:
    return (
        "Bạn là thầy tử vi Việt Nam (trường phái Tử Vi Đẩu Số), viết tiếng Việt, giọng "
        "điềm đạm, thực tế, không mê tín, không hù doạ, không phán chuyện sinh tử. Lá số "
        "dưới đây ĐÃ ĐƯỢC AN SẴN bằng code - TUYỆT ĐỐI KHÔNG tự an lại hay đổi vị trí sao, "
        "chỉ luận giải dựa trên đúng dữ liệu này. Viết khoảng 500-700 từ, chia mục ngắn: "
        "Tổng quan (Mệnh, Thân, Cục và bản mệnh); Tính cách; Công danh - Tài lộc (Quan Lộc, "
        "Tài Bạch); Gia đạo - Tình duyên (Phu Thê, Tử Tức, Phụ Mẫu); Sức khoẻ (Tật Ách); "
        "Đại hạn hiện tại và lời khuyên. Không dùng bảng markdown. Cuối bài nhắc đây là "
        "tham khảo.\n\n"
        f"Năm hiện tại: {dt.date.today().year}.\nLá số:\n{format_chart(c)}"
    )


async def explain_with_ai(c: Chart) -> str:
    """Luận giải bằng AI (provider-chain, có hạn chót lượt). Raise khi AI lỗi."""
    from ai import orchestrator
    from ai.timeouts import turn_deadline

    with turn_deadline():
        response = await orchestrator.ask(build_ai_prompt(c))
    return (getattr(response, "text", "") or "").strip()
