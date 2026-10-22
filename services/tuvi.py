"""An lá số Tử Vi Đẩu Số bằng code (/tuvi, trang /tu-vi) + AI luận giải (/tuvi_chitiet).

Các bước an sao theo cách phổ biến ở Việt Nam (Tử Vi Đẩu Số Tân Biên - Vân Đằng
Thái Thứ Lang, cũng là cách của các chương trình an sao lyso.vn, tuvilyso):

1. Đổi ngày sinh sang âm lịch; giờ sinh -> chi giờ. Sinh 23h-24h là giờ Tý
   ĐẦU: an theo ngày hôm sau (cách phổ biến ở VN; ``early_ty``). Tháng nhuận:
   sinh ngày 1-15 an như tháng trước (tháng chính), từ 16 trở đi an như tháng
   sau (quy tắc Thái Thứ Lang; ``Chart.an_month``).
2. Cung Mệnh: từ Dần là tháng Giêng đếm thuận tới tháng sinh, rồi coi cung đó
   là giờ Tý đếm nghịch tới giờ sinh; cung Thân đếm thuận. 12 cung xếp thuận
   từ Mệnh: Mệnh, Phụ Mẫu, Phúc Đức, Điền Trạch, Quan Lộc, Nô Bộc, Thiên Di,
   Tật Ách, Tài Bạch, Tử Tức, Phu Thê, Huynh Đệ.
3. Can các cung theo Ngũ hổ độn (can năm); Cục = nạp âm can chi cung Mệnh.
4. Tử Vi theo ngày sinh và số Cục; 13 chính tinh còn lại theo Tử Vi / Thiên Phủ.
5. Phụ tinh theo giờ (Xương Khúc, Không Kiếp, Thai Phụ, Phong Cáo), tháng
   (Tả Hữu, Hình Riêu Y, Thiên Giải Địa Giải), ngày (Tam Thai Bát Tọa, Ân
   Quang Thiên Quý), can năm (Lộc Tồn Kình Đà, Khôi Việt, Thiên Quan Thiên
   Phúc, Lưu Hà Thiên Trù, Quốc Ấn Đường Phù, Lưu Niên Văn Tinh), chi năm (Mã, Đào, Cái, Kiếp
   Sát, Hỏa Linh, Hồng Hỷ, Khốc Hư, Long Phượng Giải Thần, Thiên Đức Nguyệt
   Đức, Cô Quả, Phá Toái, Thiên Tài Thiên Thọ), tháng + giờ (Đẩu Quân), cố
   định (Thiên La Địa Võng, Thiên Thương Thiên Sứ).
6. Tứ hóa theo can năm; vòng Tràng Sinh (theo Cục), Lộc Tồn (Bác Sĩ...), Thái
   Tuế (kèm Thiên Không); Tuần (theo tuần Giáp của can chi năm), Triệt (theo
   can năm); đại hạn 10 năm từ Mệnh, khởi ở tuổi bằng số Cục, dương nam / âm
   nữ đi thuận, âm nam / dương nữ đi nghịch.
7. Độ sáng (miếu M / vượng V / đắc Đ / bình B / hãm H) của chính tinh và một số
   phụ tinh tra bảng ``do_sang`` trong ``assets/tu_vi.yaml`` (mỗi sao là chuỗi
   12 ký tự theo cung Tý..Hợi; "-" = không xét).

Chưa an tiểu hạn, lưu niên và vài sao hiếm (Thiên Khố...) - ghi rõ ở cuối lá
số. AI chỉ luận giải lá số ĐÃ AN, không an lại.
"""

from __future__ import annotations

import datetime as dt
import functools
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from services import can_chi, lunar
from core.text_normalize import fold

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
# Theo can năm Giáp..Quý (chi 0=Tý..11=Hợi).
_LOC_TON = [2, 3, 5, 6, 5, 6, 8, 9, 11, 0]
# Thiên Khôi / Thiên Việt: "Giáp Mậu ngưu dương, Ất Kỷ thử hầu, Bính Đinh trư kê,
# Canh Tân mã hổ, Nhâm Quý thố xà" (Tử Vi Đẩu Số Toàn Tập, phổ biến ở lá số VN).
# Biến thể Toàn Thư xếp Canh cùng Giáp Mậu (Sửu/Mùi) - không dùng.
_KHOI_VIET = {0: (1, 7), 4: (1, 7), 1: (0, 8), 5: (0, 8), 2: (11, 9), 3: (11, 9),
              6: (6, 2), 7: (6, 2), 8: (3, 5), 9: (3, 5)}  # fmt: skip
# Thiên Quan / Thiên Phúc / Thiên Trù: khớp giữa mọi nguồn VN (lethuc, sơn khiêm,
# tracuutuvi, cohoc, lasotuvi). Lưu Hà theo Thái Thứ Lang (lyso.vn, xemtuong,
# lasotuvi): bản Việt Viêm Tử khác ở can Đinh/Canh/Tân (Thân/Thìn/Dậu).
_THIEN_QUAN = [7, 4, 5, 2, 3, 9, 11, 9, 10, 6]
_THIEN_PHUC = [9, 8, 0, 11, 3, 2, 6, 5, 6, 5]
_LUU_HA = [9, 10, 7, 4, 5, 6, 8, 3, 11, 2]
_THIEN_TRU = [5, 6, 0, 5, 6, 8, 2, 6, 9, 10]
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
BRIGHTNESS_NAMES = {"M": "miếu", "V": "vượng", "Đ": "đắc", "B": "bình", "H": "hãm"}
_TEXTS_PATH = Path(__file__).resolve().parent.parent / "assets" / "tu_vi.yaml"
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

USAGE = (
    "Cú pháp: /tuvi <ngày/tháng/năm sinh> <giờ sinh> <nam|nữ>\n"
    "Ví dụ: /tuvi 15/3/1990 9h30 nam · /tuvi 15/3/1990 7h tối nữ\n"
    "Ngày âm: /tuvi âm 20/2/1990 giờ Tỵ nữ (thêm 'n' sau tháng nhuận: âm 20/6n/2025)\n"
    "Sinh 23h-24h ghi '23h': an theo ngày hôm sau (giờ Tý đầu).\n"
    "/tuvi_chitiet <như trên> - AI luận giải lá số (tốn 1 lượt AI)."
)
DISCLAIMER = "ℹ️ Tử vi mang tính tham khảo, giải trí."
_NOTE = (
    "Chưa an tiểu hạn, lưu niên và vài sao hiếm (Thiên Khố, Thiên Trù khác phái...); "
    "Lưu Hà, Thiên Khôi/Việt tuổi Canh theo phái Thái Thứ Lang."
)


@dataclass
class Star:
    """Một sao trong cung: tên gốc, Tứ hóa đi kèm (nếu có), độ sáng (M/V/Đ/B/H)."""

    name: str
    hoa: str | None = None
    brightness: str | None = None

    @property
    def is_main(self) -> bool:
        return self.name in CHINH_TINH

    @property
    def label(self) -> str:
        """Tên hiển thị như cũ: "Thái Dương (Hóa Lộc)"."""
        return f"{self.name} ({self.hoa})" if self.hoa else self.name

    @property
    def text(self) -> str:
        """Tên + độ sáng cho bản chữ: "Thái Dương [M] (Hóa Lộc)"."""
        bright = f" [{self.brightness}]" if self.brightness else ""
        return f"{self.name}{bright}" + (f" ({self.hoa})" if self.hoa else "")


@dataclass
class Chart:
    solar: dt.date | None
    lunar_day: int
    lunar_month: int
    lunar_year: int
    leap: bool
    hour_chi: int
    male: bool
    early_ty: bool = False  # sinh 23h-24h: lunar_* đã là ngày hôm sau, solar là ngày nhập
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

    @property
    def element(self) -> str:
        return _CUC_ELEMENT[self.cuc]

    @property
    def an_month(self) -> int:
        """Tháng dùng để an sao: tháng nhuận từ ngày 16 tính sang tháng sau."""
        if self.leap and self.lunar_day >= 16:
            return self.lunar_month % 12 + 1
        return self.lunar_month

    @property
    def hour_text(self) -> str:
        if self.early_ty:
            return f"giờ {CHI[self.hour_chi]} đầu (23h-24h, an theo ngày hôm sau)"
        return f"giờ {CHI[self.hour_chi]}"

    def palace_at(self, pos: int) -> str:
        return PALACES[(pos - self.menh) % 12]

    def palace_can(self, pos: int) -> int:
        return (_DAN_CAN[self.can_y % 5] + (pos - 2) % 12) % 10

    def star_list(self, pos: int) -> list[Star]:
        """Sao trong cung ``pos`` kèm Tứ hóa và độ sáng, chính tinh đứng trước."""
        out = []
        for label in self.stars[pos % 12]:
            name, _, hoa = label.partition(" (")
            out.append(Star(name, hoa.rstrip(")") or None, brightness(name, pos)))
        return sorted(out, key=lambda s: not s.is_main)


def tu_vi_position(day: int, cuc: int) -> int:
    """Cung an Tử Vi: bội số nhỏ nhất của Cục ≥ ngày sinh, thương q đếm từ Dần,
    số dư lẻ lùi / chẵn tiến bấy nhiêu cung."""
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
    early_ty: bool = False,
) -> Chart:
    """Lá số từ ngày ÂM đã quy đổi (với sinh 23h-24h, gọi với ngày âm của hôm
    sau và early_ty=True; ``solar`` chỉ để hiển thị)."""
    c = Chart(solar, lunar_day, lunar_month, lunar_year, leap, hour_chi, male, early_ty)
    h, m, d = hour_chi, c.an_month, lunar_day
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
    tp = (4 - tv) % 12  # Thiên Phủ đối xứng Tử Vi qua trục Dần - Thân
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
    put(6 + h, "Thai Phụ")  # trước Văn Khúc 2 cung
    put(2 + h, "Phong Cáo")  # sau Văn Khúc 2 cung
    # Theo tháng
    put(4 + (m - 1), "Tả Phù")
    put(10 - (m - 1), "Hữu Bật")
    put(9 + (m - 1), "Thiên Hình")
    put(1 + (m - 1), "Thiên Riêu")
    put(1 + (m - 1), "Thiên Y")
    put(8 + (m - 1), "Thiên Giải")
    put(7 + (m - 1), "Địa Giải")
    # Theo ngày (từ vị trí sao tháng/giờ)
    put(4 + (m - 1) + (d - 1), "Tam Thai")  # từ Tả Phù thuận tới ngày sinh
    put(10 - (m - 1) - (d - 1), "Bát Tọa")  # từ Hữu Bật nghịch
    put(10 - h + (d - 1) - 1, "Ân Quang")  # từ Văn Xương thuận tới ngày sinh, lùi 1
    put(4 + h - (d - 1) + 1, "Thiên Quý")  # từ Văn Khúc nghịch tới ngày sinh, tiến 1
    # Theo can năm
    loc = _LOC_TON[cy]
    put(loc, "Lộc Tồn")
    put(loc + 1, "Kình Dương")
    put(loc - 1, "Đà La")
    put(loc + 8, "Quốc Ấn")  # từ Lộc Tồn thuận tới cung thứ 9
    put(loc - 7, "Đường Phù")  # từ Lộc Tồn nghịch tới cung thứ 8
    put(loc + 3, "Lưu Niên Văn Tinh")  # từ Lộc Tồn thuận tới cung thứ 4
    khoi, viet = _KHOI_VIET[cy]
    put(khoi, "Thiên Khôi")
    put(viet, "Thiên Việt")
    put(_THIEN_QUAN[cy], "Thiên Quan")
    put(_THIEN_PHUC[cy], "Thiên Phúc")
    put(_LUU_HA[cy], "Lưu Hà")
    put(_THIEN_TRU[cy], "Thiên Trù")
    # Theo chi năm (nhóm tam hợp)
    group = next(g for g in ((2, 6, 10), (8, 0, 4), (5, 9, 1), (11, 3, 7)) if zy in g)
    put({(2, 6, 10): 8, (8, 0, 4): 2, (5, 9, 1): 11, (11, 3, 7): 5}[group], "Thiên Mã")
    put({(2, 6, 10): 3, (8, 0, 4): 9, (5, 9, 1): 6, (11, 3, 7): 0}[group], "Đào Hoa")
    put({(2, 6, 10): 10, (8, 0, 4): 4, (5, 9, 1): 1, (11, 3, 7): 7}[group], "Hoa Cái")
    put({(2, 6, 10): 11, (8, 0, 4): 5, (5, 9, 1): 2, (11, 3, 7): 8}[group], "Kiếp Sát")
    # Hỏa Tinh, Linh Tinh: khởi theo nhóm tam hợp rồi đếm theo giờ; dương nam âm
    # nữ Hỏa thuận - Linh nghịch, âm nam dương nữ ngược lại.
    hoa_start = {(2, 6, 10): 1, (8, 0, 4): 2, (5, 9, 1): 3, (11, 3, 7): 9}[group]
    linh_start = 3 if group == (2, 6, 10) else 10
    put(hoa_start + sign * h, "Hỏa Tinh")
    put(linh_start - sign * h, "Linh Tinh")
    put(3 - zy, "Hồng Loan")  # khởi Mão nghịch
    put(9 - zy, "Thiên Hỷ")  # đối cung Hồng Loan
    put(6 - zy, "Thiên Khốc")  # khởi Ngọ nghịch
    put(6 + zy, "Thiên Hư")  # khởi Ngọ thuận
    put(4 + zy, "Long Trì")  # khởi Thìn thuận
    put(10 - zy, "Phượng Các")  # khởi Tuất nghịch
    put(10 - zy, "Giải Thần")  # đồng cung Phượng Các
    put(9 + zy, "Thiên Đức")  # khởi Dậu thuận
    put(5 + zy, "Nguyệt Đức")  # khởi Tỵ thuận
    co_qua = {0: (2, 10), 1: (2, 10), 11: (2, 10), 2: (5, 1), 3: (5, 1), 4: (5, 1),
              5: (8, 4), 6: (8, 4), 7: (8, 4), 8: (11, 7), 9: (11, 7), 10: (11, 7)}  # fmt: skip
    put(co_qua[zy][0], "Cô Thần")
    put(co_qua[zy][1], "Quả Tú")
    put({0: 5, 6: 5, 3: 5, 9: 5, 2: 9, 8: 9, 5: 9, 11: 9}.get(zy, 1), "Phá Toái")
    put(c.menh + zy, "Thiên Tài")  # từ Mệnh thuận theo chi năm
    put(c.than + zy, "Thiên Thọ")  # từ Thân thuận theo chi năm
    # Đẩu Quân: từ cung Thái Tuế đếm nghịch tới tháng sinh, rồi thuận tới giờ sinh.
    put(zy - (m - 1) + h, "Đẩu Quân")
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

    # Vòng Lộc Tồn (Bác Sĩ...), Thái Tuế (+ Thiên Không), Tràng Sinh (theo Cục)
    for i, name in enumerate(_BAC_SI):
        put(loc + sign * i, name)
    for i, name in enumerate(_THAI_TUE):
        put(zy + i, name)
    put(zy + 1, "Thiên Không")  # cung liền sau Thái Tuế (đồng cung Thiếu Dương)
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


def brightness(star: str, pos: int) -> str | None:
    """Độ sáng M/V/Đ/B/H của ``star`` ở cung ``pos`` (None nếu không xét)."""
    row = str(_texts().get("do_sang", {}).get(star, ""))
    if len(row) != 12:
        return None
    level = row[pos % 12]
    return level if level in BRIGHTNESS_NAMES else None


def _split_stars(names: list[str]) -> tuple[list[str], list[str]]:
    main = [n for n in names if n.split(" (")[0] in CHINH_TINH]
    other = [n for n in names if n not in main]
    return main, other


def format_chart(c: Chart) -> str:
    born = can_chi.year_profile(c.lunar_year)
    gender = ("Dương" if c.can_y % 2 == 0 else "Âm") + (" Nam" if c.male else " Nữ")
    solar = f"{c.solar:%d/%m/%Y} dương, " if c.solar else ""
    lines = [
        f"🔮 Lá số Tử Vi - {solar}{lunar.lunar_text(c.lunar_day, c.lunar_month, c.lunar_year, c.leap)} "
        f"âm, {c.hour_text}, {gender}",
        f"Bản mệnh: {born['nap_am']} ({born['menh']}) · Cục: {_CUC_NAME[c.cuc]} · "
        f"{can_chi.ngu_hanh_relation(c.element, born['menh'])}",
        f"Mệnh chủ: {_MENH_CHU[c.menh]} · Thân chủ: {_THAN_CHU[c.chi_y]} · "
        f"Thân cư {c.palace_at(c.than)}",
        "Tứ hóa: " + ", ".join(f"{k} {v}" for k, v in c.tu_hoa.items()),
        "",
    ]
    for i in range(12):
        pos = (c.menh + i) % 12
        stars = c.star_list(pos)
        main = [s.text for s in stars if s.is_main]
        other = [s.text for s in stars if not s.is_main]
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
    main_menh = [s for s in c.star_list(c.menh) if s.is_main]
    lines.append("")
    if main_menh:
        for s in main_menh:
            level = f" ({BRIGHTNESS_NAMES[s.brightness]})" if s.brightness else ""
            lines.append(f"📌 {s.name}{level} thủ Mệnh: {texts.get(s.name, '')}")
    else:
        lines.append("📌 Mệnh vô chính diệu: " + str(_texts().get("vo_chinh_dieu", "")))
    lines.append(
        f"Độ sáng trong [ ]: M miếu, V vượng, Đ đắc, B bình, H hãm. {_NOTE}"
        f"\n\n{DISCLAIMER} Gõ /tuvi_chitiet để AI luận giải."
    )
    return "\n".join(lines)


def chart_data(c: Chart) -> dict:
    """Lá số dạng dict khoá camelCase cho trang web và JSON API (hợp đồng công
    khai: chỉ THÊM trường, không đổi/bỏ trường cũ)."""
    born = can_chi.year_profile(c.lunar_year)
    texts = _texts()
    palaces = []
    for i in range(12):
        pos = (c.menh + i) % 12
        stars = c.star_list(pos)
        start_age, end_age = c.dai_han[pos]
        palaces.append(
            {
                "name": PALACES[i],
                "branch": CHI[pos],
                "branchIndex": pos,
                "stem": CAN[c.palace_can(pos)],
                "mainStars": [s.label for s in stars if s.is_main],
                "minorStars": [s.label for s in stars if not s.is_main],
                # Từng sao kèm Tứ hóa và độ sáng (M/V/Đ/B/H hoặc null): chính tinh trước.
                "stars": [
                    {
                        "name": s.name,
                        "isMain": s.is_main,
                        "hoa": s.hoa,
                        "brightness": s.brightness,
                        "brightnessName": BRIGHTNESS_NAMES.get(s.brightness or "", None),
                    }
                    for s in stars
                ],
                "isThan": pos == c.than,
                "hasTuan": pos in c.tuan,
                "hasTriet": pos in c.triet,
                "daiHan": {
                    "from": start_age,
                    "to": end_age,
                    "fromAge": start_age,
                    "toAge": end_age,
                },
                "trangSinh": c.trang_sinh[pos],
            }
        )
    menh_main = [s for s in c.star_list(c.menh) if s.is_main]
    return {
        "solarDate": c.solar.isoformat() if c.solar else None,
        "lunarDate": {
            "day": c.lunar_day,
            "month": c.lunar_month,
            "year": c.lunar_year,
            "isLeapMonth": c.leap,
            "text": lunar.lunar_text(c.lunar_day, c.lunar_month, c.lunar_year, c.leap),
        },
        "hourBranch": CHI[c.hour_chi],
        "hourText": c.hour_text,
        "isEarlyTy": c.early_ty,
        "anMonth": c.an_month,
        "gender": "male" if c.male else "female",
        "yinYang": ("Dương" if c.can_y % 2 == 0 else "Âm") + (" Nam" if c.male else " Nữ"),
        "napAm": born["nap_am"],
        "element": born["menh"],
        "cuc": _CUC_NAME[c.cuc],
        "cucElement": c.element,
        "cucRelation": can_chi.ngu_hanh_relation(c.element, born["menh"]),
        "isThuan": c.thuan,
        "menhChu": _MENH_CHU[c.menh],
        "thanChu": _THAN_CHU[c.chi_y],
        "thanPalace": c.palace_at(c.than),
        "tuHoa": c.tu_hoa,
        "palaces": palaces,
        "menhNotes": [
            {
                "star": s.name,
                "name": s.name,
                "brightness": s.brightness,
                "brightnessName": BRIGHTNESS_NAMES.get(s.brightness or "", None),
                "meaning": texts.get("chinh_tinh", {}).get(s.name, ""),
            }
            for s in menh_main
        ],
        "isMenhEmpty": not menh_main,
        "menhEmptyNote": texts.get("vo_chinh_dieu", "") if not menh_main else "",
        "brightnessLegend": BRIGHTNESS_NAMES,
        "note": _NOTE,
        "disclaimer": DISCLAIMER,
    }


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


def hour_to_chi(hour: int) -> int:
    """Giờ 0-23 -> chi giờ: 23h-1h Tý, 1h-3h Sửu, ... 21h-23h Hợi."""
    return ((hour + 1) // 2) % 12


_PERIOD_RE = re.compile(r"\s*(?:\d{1,2})?\s*(sang|trua|chieu|toi|dem|khuya|pm)\b")


def _apply_period(hour: int, after: str) -> int:
    """Giờ 24h từ buổi đi sau số giờ: "3h chiều", "9 giờ tối", "8h đêm", "7:30 pm";
    "12h đêm/khuya" = 0h. Không nhận "am" vì "âm" (lịch) bỏ dấu cũng là "am"."""
    period = _PERIOD_RE.match(after)
    if not period:
        return hour
    word = period.group(1)
    if hour == 12 and word in ("dem", "khuya"):
        return 0
    if hour < 12 and (
        word in ("chieu", "toi", "pm")
        or (word in ("dem", "khuya") and hour >= 6)
        or (word == "trua" and hour < 6)
    ):
        return hour + 12
    return hour


def parse(argument: str) -> Chart | str:
    """Lá số từ "15/3/1990 9h30 nam" / "âm 20/2/1990 giờ Tỵ nữ", hoặc câu báo lỗi."""
    low = (argument or "").lower().strip()
    m = _DATE_RE.search(low)
    if not m:
        return USAGE
    # "âm"/"âm lịch" ở bất cứ đâu (có dấu); không dấu chỉ nhận "am lich" hoặc
    # "am" đứng đầu - "9 am" kiểu tiếng Anh không được biến ngày dương thành âm.
    is_lunar = (
        bool(re.search(r"\b(?:âm|âm lịch|al)\b", low))
        or bool(re.match(r"^am\b", low))
        or "am lich" in low
        or bool(m.group(3))
    )
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(4))
    if not 1900 <= y <= 2100:
        return "Năm sinh cần trong khoảng 1900-2100."
    rest = (low[: m.start()] + " " + low[m.end() :]).strip()
    gio = _GIO_CHI_RE.search(rest)
    early_ty = False
    if gio:
        word = gio.group(1)
        if word in ("ty", "ti"):
            return "Giờ Tý hay giờ Tỵ? Gõ có dấu hoặc ghi giờ cụ thể (vd 23h, 10h)."
        hour_chi = _CHI_WORDS[word]
    else:
        folded = fold(rest)
        hm = _HOUR_RE.search(folded)
        if not hm or int(hm.group(1)) > 23:
            return "Thiếu giờ sinh.\n" + USAGE
        hour = int(hm.group(1))
        hour = _apply_period(hour, folded[hm.end() :])
        hour_chi = hour_to_chi(hour)
        early_ty = hour == 23
    if re.search(r"\b(nữ|nu|gái|gai)\b", rest):
        male = False
    elif re.search(r"\b(nam|trai)\b", rest):
        male = True
    else:
        return "Thiếu giới tính (nam/nữ).\n" + USAGE
    if is_lunar:
        leap = bool(m.group(3))
        if not (1 <= d <= 30 and 1 <= mo <= 12):
            return "Ngày âm lịch không hợp lệ."
        converted = lunar.lunar_to_solar(d, mo, y, leap)
        if converted is None:
            return "Ngày âm lịch không hợp lệ."
        sd, sm, sy = converted
        solar = dt.date(sy, sm, sd)
        if lunar.solar_to_lunar(sd, sm, sy) != (d, mo, y, leap):
            return f"Tháng {mo} âm năm {y} không có ngày {d}."
    else:
        try:
            solar = dt.date(y, mo, d)
        except ValueError:
            return "Ngày sinh không hợp lệ.\n" + USAGE
    return chart_for(solar, hour_chi, male, early_ty)


def chart_for(solar: dt.date, hour_chi: int, male: bool, early_ty: bool = False) -> Chart:
    """Lá số từ ngày sinh DƯƠNG; early_ty (sinh 23h-24h) an theo ngày hôm sau
    nhưng vẫn hiển thị ngày đã nhập."""
    an_day = solar + dt.timedelta(days=1) if early_ty else solar
    ld, lm, ly, leap = lunar.solar_to_lunar(an_day.day, an_day.month, an_day.year)
    return build_chart(ld, lm, ly, hour_chi, male, leap, solar, early_ty)


def build_ai_prompt(c: Chart) -> str:
    return (
        "Bạn là thầy tử vi Việt Nam (trường phái Tử Vi Đẩu Số), viết tiếng Việt, giọng "
        "điềm đạm, thực tế, không mê tín, không hù doạ, không phán chuyện sinh tử. Lá số "
        "dưới đây ĐÃ ĐƯỢC AN SẴN bằng code - TUYỆT ĐỐI KHÔNG tự an lại hay đổi vị trí sao, "
        "chỉ luận giải dựa trên đúng dữ liệu này (kể cả độ sáng miếu/vượng/đắc/hãm ghi "
        "trong ngoặc vuông). Viết khoảng 500-700 từ, chia mục ngắn: Tổng quan (Mệnh, "
        "Thân, Cục và bản mệnh); Tính cách; Công danh - Tài lộc (Quan Lộc, Tài Bạch); Gia "
        "đạo - Tình duyên (Phu Thê, Tử Tức, Phụ Mẫu); Sức khoẻ (Tật Ách); Đại hạn hiện "
        "tại và lời khuyên. Không dùng bảng markdown. Cuối bài nhắc đây là tham khảo.\n\n"
        f"Năm hiện tại: {dt.datetime.now(_VN_TZ).year}.\nLá số:\n{format_chart(c)}"
    )


async def explain_with_ai(c: Chart) -> str:
    """Luận giải bằng AI (provider-chain, có hạn chót lượt). Raise khi AI lỗi."""
    from ai import orchestrator
    from ai.timeouts import turn_deadline

    with turn_deadline():
        response = await orchestrator.ask(build_ai_prompt(c))
    return (getattr(response, "text", "") or "").strip()
