"""Lá số Bát Tự (Tứ Trụ) bằng code (/battu) + AI luận giải (/battu_chitiet).

Chuyển ngữ một phần từ python/nhatnguyet/bat_tu.py của Nhật Nguyệt (MIT License,
Copyright (c) 2026 Nhat Nguyet - nhatnguyet.org): bảng tàng can, thập thần, vòng
trường sinh, mệnh cung, thai nguyên, đại vận, tiểu vận. Không làm thần sát (mỗi
phái một khác). Tính bằng code, offline; AI chỉ diễn giải lá số ĐÃ LẬP.

Quy ước (như lõi tham chiếu; đối chiếu ca kiểm Nhật Nguyệt ở test/test_bat_tu.py):

1. Giờ sinh là giờ đồng hồ UTC+7. Trụ ngày đổi lúc 23h (đầu giờ Tý): sinh 23h-24h
   lấy trụ ngày hôm sau; trụ giờ theo Ngũ thử độn từ can ngày đó.
2. Trụ năm đổi đúng thời khắc Lập xuân (không theo Tết); trụ tháng theo 12 tiết
   (Lập xuân mở tháng Dần, Kinh trập tháng Mão...) và Ngũ hổ độn. Thời khắc tiết
   khí lấy ở services/solar_terms.py (sai số vài giây). ``four_pillars`` dùng chung
   với /kymon.
3. Tàng can theo lối nhân nguyên tư lệnh (bản khí, trung khí, dư khí). Thập thần
   suy từ ngũ hành và âm dương so với nhật chủ (cùng âm dương: Tỷ Kiên, Thực Thần,
   Thiên Tài, Thất Sát, Thiên Ấn). Vòng trường sinh: can dương thuận, can âm nghịch,
   Mậu theo Bính, Kỷ theo Đinh. "Trường sinh" của một trụ là giai đoạn của NHẬT CHỦ
   trên chi trụ; "tự tọa" là của chính can trụ trên chi trụ.
4. Mệnh cung (cần giờ sinh): chi = (5 - chi tháng - chi giờ) mod 12 với Tý = 0, can
   theo Ngũ hổ độn từ can năm. Thai nguyên: can tháng tiến 1, chi tháng tiến 3.
5. Đại vận đi từ trụ tháng: dương nam, âm nữ thuận; âm nam, dương nữ nghịch (âm
   dương của can trụ năm). Đếm từ lúc sinh tới tiết kế tiếp (thuận) hoặc từ tiết vừa
   qua tới lúc sinh (nghịch), thời khắc tiết làm tròn tới phút; 3 ngày tính 1 năm
   (1 ngày = 4 tháng, 1 giờ = 5 ngày). Không rõ giờ sinh thì tạm lấy 12h00.
6. Tiểu vận năm xem: từ trụ giờ đi số bước bằng tuổi mụ, theo chiều đại vận; tuổi
   mụ tính theo năm của trụ năm.
"""

from __future__ import annotations

import datetime as dt
import functools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

import yaml

from core.text_normalize import fold
from services import can_chi, lunar, solar_terms

CAN = can_chi.CAN
CHI = can_chi.CHI
UTC7 = dt.timezone(dt.timedelta(hours=7))
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MIN_YEAR, MAX_YEAR = 1900, 2100
MAX_VIEW_YEAR = 2200
LUCK_COUNT = 10
_TEXTS_PATH = Path(__file__).resolve().parent.parent / "assets" / "bat_tu.yaml"

PILLAR_KEYS = ("year", "month", "day", "hour")
PILLAR_NAMES = {"year": "Năm", "month": "Tháng", "day": "Ngày", "hour": "Giờ"}
ELEMENTS = ("Kim", "Mộc", "Thủy", "Hỏa", "Thổ")  # thứ tự đếm ngũ hành
STEM_ELEMENTS = ("Mộc", "Mộc", "Hỏa", "Hỏa", "Thổ", "Thổ", "Kim", "Kim", "Thủy", "Thủy")
BRANCH_ELEMENTS = ("Thủy", "Thổ", "Mộc", "Mộc", "Thổ", "Hỏa", "Hỏa", "Thổ", "Kim", "Kim",
                   "Thổ", "Thủy")  # fmt: skip
# Thập thần theo quan hệ ngũ hành của can với nhật chủ. Chỉ số hành theo vòng tương
# sinh Mộc Hỏa Thổ Kim Thủy là can // 2; (hành can - hành nhật chủ) mod 5: 0 cùng hành,
# 1 nhật chủ sinh, 2 nhật chủ khắc, 3 khắc nhật chủ, 4 sinh nhật chủ. Mỗi nhóm ghi
# (cùng âm dương, khác âm dương).
TEN_GODS = (
    ("Tỷ Kiên", "Kiếp Tài"),
    ("Thực Thần", "Thương Quan"),
    ("Thiên Tài", "Chính Tài"),
    ("Thất Sát", "Chính Quan"),
    ("Thiên Ấn", "Chính Ấn"),
)
TEN_GOD_GROUPS = ("Tỷ Kiếp", "Thực Thương", "Tài", "Quan Sát", "Ấn")
DAY_MASTER = "Nhật chủ"
GROWTH_STAGES = ("Tràng Sinh", "Mộc Dục", "Quan Đới", "Lâm Quan", "Đế Vượng", "Suy",
                 "Bệnh", "Tử", "Mộ", "Tuyệt", "Thai", "Dưỡng")  # fmt: skip
# Chi Lộc (Lâm Quan) của 10 can: Giáp Dần, Ất Mão, Bính Mậu Tỵ, Đinh Kỷ Ngọ, Canh Thân,
# Tân Dậu, Nhâm Hợi, Quý Tý. Tràng Sinh: can dương lùi 3 chi từ Lộc, can âm tiến 3.
_LOC = (2, 3, 5, 6, 5, 6, 8, 9, 11, 0)
HIDDEN_ROLES = ("Bản khí", "Trung khí", "Dư khí")
# Tàng can (nhân nguyên tư lệnh), chỉ số can theo thứ tự bản khí, trung khí, dư khí.
_HIDDEN = (
    (9,),  # Tý: Quý
    (5, 7, 9),  # Sửu: Kỷ, Tân, Quý
    (0, 2, 4),  # Dần: Giáp, Bính, Mậu
    (1,),  # Mão: Ất
    (4, 9, 1),  # Thìn: Mậu, Quý, Ất
    (2, 6, 4),  # Tỵ: Bính, Canh, Mậu
    (3, 5),  # Ngọ: Đinh, Kỷ
    (5, 1, 3),  # Mùi: Kỷ, Ất, Đinh
    (6, 8, 4),  # Thân: Canh, Nhâm, Mậu
    (7,),  # Dậu: Tân
    (4, 3, 7),  # Tuất: Mậu, Đinh, Tân
    (8, 0),  # Hợi: Nhâm, Giáp
)
_LAP_XUAN = 21  # chỉ số tiết Lập xuân trong lunar.TIET_KHI (0 = Xuân phân)
# Sinh cách thời khắc giao tiết dưới chừng này phút thì ghi chú: giờ sinh lệch ít là
# trụ tháng (Lập xuân: cả trụ năm) đổi.
_NEAR_TERM_MINUTES = 120

USAGE = (
    "Cú pháp: /battu <ngày/tháng/năm sinh dương> [giờ:phút] <nam|nữ> [năm xem]\n"
    "Ví dụ: /battu 15/3/1990 9h30 nam · /battu 15/3/1990 7h tối nữ năm xem 2027\n"
    "Không rõ giờ sinh thì bỏ trống giờ (lá số 3 trụ). Ngày âm: /battu âm 20/2/1990 giờ Tỵ "
    "nữ (thêm 'n' sau tháng nhuận: âm 20/6n/2025)\n"
    "Sinh 23h-24h ghi '23h': trụ ngày tính theo ngày hôm sau (đầu giờ Tý).\n"
    "/battu_chitiet <như trên> - AI luận giải lá số (tốn 1 lượt AI)."
)
DISCLAIMER = "ℹ️ Bát Tự mang tính tham khảo, giải trí."
NOTE = (
    "Trụ năm đổi ở Lập xuân, trụ tháng theo tiết, trụ ngày đổi lúc 23h; giờ sinh là giờ "
    "đồng hồ UTC+7; tàng can theo nhân nguyên tư lệnh; không xét thần sát."
)


class Pillars(NamedTuple):
    """Tứ trụ, mỗi trụ là (chỉ số can, chỉ số chi)."""

    year: tuple[int, int]
    month: tuple[int, int]
    day: tuple[int, int]
    hour: tuple[int, int]


@dataclass(frozen=True)
class HiddenStem:
    stem: int
    role: str  # Bản khí / Trung khí / Dư khí
    ten_god: str


@dataclass(frozen=True)
class Pillar:
    """Một trụ của lá số. Trụ ngày có ten_god = "Nhật chủ"."""

    key: str  # year | month | day | hour
    stem: int
    branch: int
    ten_god: str  # thập thần của can trụ
    hidden: tuple[HiddenStem, ...]
    growth: str  # trường sinh của nhật chủ trên chi trụ
    self_seat: str  # tự tọa: trường sinh của can trụ trên chi trụ

    @property
    def pair(self) -> tuple[int, int]:
        return (self.stem, self.branch)

    @property
    def text(self) -> str:
        return pillar_text(self.pair)

    @property
    def name(self) -> str:
        return PILLAR_NAMES[self.key]


@dataclass(frozen=True)
class LuckPillar:
    order: int  # 1..10
    stem: int
    branch: int
    from_age: int  # tuổi tròn lúc vào vận
    from_year: int
    to_year: int
    ten_god: str
    growth: str  # trường sinh của nhật chủ trên chi vận

    @property
    def pair(self) -> tuple[int, int]:
        return (self.stem, self.branch)

    @property
    def text(self) -> str:
        return pillar_text(self.pair)


@dataclass(frozen=True)
class Luck:
    """Đại vận: chiều, tiết mốc dùng để đếm, số ngày, tuổi và ngày khởi vận, các vận."""

    forward: bool
    term_index: int  # tiết mốc (chỉ số lunar.TIET_KHI)
    term_at: dt.datetime  # thời khắc tiết mốc, làm tròn tới phút
    minutes: int  # số phút giữa lúc sinh và tiết mốc
    days: float  # minutes / 1440, làm tròn 2 chữ số
    start_years: int
    start_months: int
    start_date: dt.date
    pillars: tuple[LuckPillar, ...]

    @property
    def term_name(self) -> str:
        return lunar.TIET_KHI[self.term_index]


@dataclass(frozen=True)
class AnnualLuck:
    """Tiểu vận của năm xem."""

    year: int
    age: int  # tuổi mụ theo năm của trụ năm
    stem: int
    branch: int
    ten_god: str

    @property
    def text(self) -> str:
        return pillar_text((self.stem, self.branch))


@dataclass(frozen=True)
class TermMoment:
    index: int  # chỉ số lunar.TIET_KHI
    at: dt.datetime  # thời khắc giao tiết (tới giây), theo múi giờ của lá số

    @property
    def name(self) -> str:
        return lunar.TIET_KHI[self.index]


@dataclass
class Chart:
    birth: dt.datetime  # giờ sinh có múi giờ; không rõ giờ thì 12:00
    has_hour: bool
    male: bool
    view_year: int
    pillars: dict[str, Pillar]  # year, month, day[, hour]
    pillar_year: int  # năm của trụ năm (đổi ở Lập xuân)
    term_index: int  # tiết khí đang hiệu lực lúc sinh
    month_term: TermMoment  # tiết mở tháng sinh
    next_term: TermMoment  # tiết mở tháng kế tiếp
    conception: tuple[int, int]  # thai nguyên
    life_palace: tuple[int, int] | None  # mệnh cung (cần giờ sinh)
    luck: Luck
    annual: AnnualLuck | None  # tiểu vận (cần giờ sinh, năm xem từ năm sinh)
    elements: dict[str, int]  # đếm ngũ hành 8 chữ (6 chữ khi không rõ giờ)
    lunar_date: tuple[int, int, int, bool]  # ngày âm của ngày sinh đã nhập
    hour_by_name: bool = False  # giờ nhập theo tên chi ("giờ Dần"), lấy giữa giờ
    notes: list[str] = field(default_factory=list)

    @property
    def day_master(self) -> int:
        return self.pillars["day"].stem

    @property
    def term_name(self) -> str:
        return lunar.TIET_KHI[self.term_index]


def pillar_text(pillar: tuple[int, int]) -> str:
    return f"{CAN[pillar[0]]} {CHI[pillar[1]]}"


def yin_yang(stem: int) -> str:
    return "Dương" if stem % 2 == 0 else "Âm"


def ten_god(day_stem: int, stem: int) -> str:
    """Thập thần mà can ``stem`` đóng vai với nhật chủ ``day_stem``."""
    pair = TEN_GODS[(stem // 2 - day_stem // 2) % 5]
    return pair[0] if stem % 2 == day_stem % 2 else pair[1]


def growth_stage(stem: int, branch: int) -> str:
    """Giai đoạn vòng trường sinh của can ``stem`` tại chi ``branch``."""
    if stem % 2 == 0:
        return GROWTH_STAGES[(branch - _LOC[stem] + 3) % 12]
    return GROWTH_STAGES[(_LOC[stem] + 3 - branch) % 12]


def hidden_stems(branch: int, day_stem: int) -> tuple[HiddenStem, ...]:
    """Tàng can của chi, bản khí trước, kèm thập thần so với nhật chủ."""
    return tuple(
        HiddenStem(stem, HIDDEN_ROLES[i], ten_god(day_stem, stem))
        for i, stem in enumerate(_HIDDEN[branch])
    )


def _dan_stem(year_stem: int) -> int:
    """Ngũ hổ độn: can của tháng Dần theo can năm."""
    return (2 * (year_stem % 5) + 2) % 10


def _shift(pillar: tuple[int, int], steps: int) -> tuple[int, int]:
    """Trụ cách ``steps`` bước trên vòng lục thập hoa giáp."""
    return ((pillar[0] + steps) % 10, (pillar[1] + steps) % 12)


def four_pillars(local: dt.datetime) -> Pillars:
    """Tứ trụ của thời điểm ``local`` (datetime CÓ múi giờ: ngày, giờ đọc theo đồng hồ
    của múi đó; tiết khí theo thời khắc tuyệt đối). Dùng chung cho /battu và /kymon:
    trụ ngày đổi ở đầu giờ Tý (23h-24h thuộc ngày hôm sau), trụ giờ theo Ngũ thử độn,
    trụ năm đổi đúng thời khắc Lập xuân, trụ tháng theo tiết đang hiệu lực (Lập xuân,
    Vũ thủy = tháng Dần...) và Ngũ hổ độn."""
    if local.tzinfo is None:
        raise ValueError("Cần datetime có múi giờ")
    pillar_day = local.date() + dt.timedelta(days=1 if local.hour == 23 else 0)
    jd = lunar._jd_from_date(pillar_day.day, pillar_day.month, pillar_day.year)
    day = ((jd + 9) % 10, (jd + 1) % 12)
    hour_branch = (local.hour + 1) // 2 % 12
    hour = ((2 * (day[0] % 5) + hour_branch) % 10, hour_branch)  # Ngũ thử độn
    term = solar_terms.term_at(local)
    year_no = local.year if local >= solar_terms.li_chun(local.year) else local.year - 1
    year = (can_chi.year_can(year_no), can_chi.year_chi(year_no))
    month_branch = ((term.index + 3) // 2 + 2) % 12  # Lập xuân/Vũ thủy = Dần ...
    month = ((_dan_stem(year[0]) + (month_branch - 2) % 12) % 10, month_branch)
    return Pillars(year, month, day, hour)


@functools.lru_cache(maxsize=1)
def texts() -> dict:
    with _TEXTS_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _now() -> dt.datetime:
    return dt.datetime.now(VN_TZ)


def _minutes_between(a: dt.datetime, b: dt.datetime) -> int:
    """Số phút từ ``a`` tới ``b`` (qua giờ UTC, không vướng giờ đồng hồ)."""
    return round((b.astimezone(dt.UTC) - a.astimezone(dt.UTC)).total_seconds() / 60)


def _round_minute(moment: dt.datetime) -> dt.datetime:
    """Làm tròn tới phút (từ 30 giây làm tròn lên), như lịch tiết khí ghi giờ:phút."""
    return (moment + dt.timedelta(seconds=30)).replace(second=0, microsecond=0)


def _month_jie(month_branch: int) -> int:
    """Chỉ số tiết mở tháng: Lập xuân (21) mở tháng Dần, Kinh trập (23) tháng Mão..."""
    return (_LAP_XUAN + 2 * ((month_branch - 2) % 12)) % 24


def _term_near(index: int, moment: dt.datetime) -> TermMoment:
    """Lần giao tiết ``index`` gần ``moment`` nhất (một tiết cách lần sau 1 năm)."""
    starts = [solar_terms.term_start(index, y) for y in range(moment.year - 1, moment.year + 2)]
    at = min(starts, key=lambda t: abs(t - moment))
    return TermMoment(index, at.astimezone(moment.tzinfo))


def luck_start(birth: dt.datetime, term_at: dt.datetime) -> tuple[int, float, int, int, dt.date]:
    """(số phút, số ngày làm tròn 2 chữ số, tuổi khởi vận: năm, tháng, ngày khởi vận) từ
    lúc sinh tới thời khắc tiết mốc (đã làm tròn tới phút). 3 ngày tính 1 năm nên 6 giờ
    là 1 tháng, 1 phút là 2 giờ. Tính bằng số nguyên, không sai số dấu phẩy động."""
    minutes = abs(_minutes_between(birth, term_at))
    years, months = divmod(minutes // 360, 12)
    days = (minutes * 200 + 1440) // 2880 / 100
    start = (birth + dt.timedelta(minutes=minutes * 120)).date()
    return minutes, days, years, months, start


def _pillar(key: str, pillar: tuple[int, int], day_stem: int) -> Pillar:
    stem, branch = pillar
    return Pillar(
        key=key,
        stem=stem,
        branch=branch,
        ten_god=DAY_MASTER if key == "day" else ten_god(day_stem, stem),
        hidden=hidden_stems(branch, day_stem),
        growth=growth_stage(day_stem, branch),
        self_seat=growth_stage(stem, branch),
    )


def _luck(
    local: dt.datetime, month: tuple[int, int], forward: bool, anchor: TermMoment, day_stem: int
) -> Luck:
    term_at = _round_minute(anchor.at)
    minutes, days, years, months, start = luck_start(local, term_at)
    step = 1 if forward else -1
    pillars = []
    for i in range(1, LUCK_COUNT + 1):
        stem, branch = _shift(month, step * i)
        from_year = start.year + 10 * (i - 1)
        pillars.append(
            LuckPillar(
                order=i,
                stem=stem,
                branch=branch,
                from_age=years + 10 * (i - 1),
                from_year=from_year,
                to_year=from_year + 9,
                ten_god=ten_god(day_stem, stem),
                growth=growth_stage(day_stem, branch),
            )
        )
    return Luck(forward, anchor.index, term_at, minutes, days, years, months, start, tuple(pillars))


def build_chart(
    born: dt.date, male: bool, view_year: int | None = None, *, hour_by_name: bool = False
) -> Chart:
    """Lá số từ ngày sinh DƯƠNG lịch. ``born`` là datetime = biết giờ sinh (không có
    múi giờ thì hiểu là UTC+7, có múi giờ thì lập theo giờ đồng hồ của múi đó); là date
    = không rõ giờ sinh (3 trụ, đại vận tạm tính từ 12h00). ``view_year`` (năm xem cho
    tiểu vận) mặc định là năm hiện tại giờ Việt Nam. Raise ValueError nếu năm sinh
    ngoài 1900-2100."""
    if isinstance(born, dt.datetime):
        has_hour = True
        local = born if born.tzinfo else born.replace(tzinfo=UTC7)
        local = local.replace(second=0, microsecond=0)
    else:
        has_hour = False
        local = dt.datetime(born.year, born.month, born.day, 12, tzinfo=UTC7)
    if not MIN_YEAR <= local.year <= MAX_YEAR:
        raise ValueError(f"Năm sinh cần trong khoảng {MIN_YEAR}-{MAX_YEAR}.")
    if view_year is None:
        view_year = _now().year
    four = four_pillars(local)
    day_stem = four.day[0]
    keys = PILLAR_KEYS if has_hour else PILLAR_KEYS[:3]
    pillars = {key: _pillar(key, getattr(four, key), day_stem) for key in keys}
    # Trụ năm đổi ở Lập xuân (đầu tháng 2): chi khác chi năm dương lịch là năm trước.
    pillar_year = local.year if four.year[1] == can_chi.year_chi(local.year) else local.year - 1
    jie = _month_jie(four.month[1])
    month_term = _term_near(jie, local)
    next_term = _term_near((jie + 2) % 24, local)
    forward = (four.year[0] % 2 == 0) == male  # dương nam, âm nữ thuận
    luck = _luck(local, four.month, forward, next_term if forward else month_term, day_stem)

    annual = None
    age = view_year - pillar_year + 1
    if has_hour and age >= 1:
        stem, branch = _shift(four.hour, age if forward else -age)
        annual = AnnualLuck(view_year, age, stem, branch, ten_god(day_stem, stem))
    life_palace = None
    if has_hour:
        branch = (5 - four.month[1] - four.hour[1]) % 12
        life_palace = ((_dan_stem(four.year[0]) + (branch - 2) % 12) % 10, branch)
    elements: dict[str, int] = dict.fromkeys(ELEMENTS, 0)
    for p in pillars.values():
        elements[STEM_ELEMENTS[p.stem]] += 1
        elements[BRANCH_ELEMENTS[p.branch]] += 1

    chart = Chart(
        birth=local,
        has_hour=has_hour,
        male=male,
        view_year=view_year,
        pillars=pillars,
        pillar_year=pillar_year,
        term_index=solar_terms.term_at(local).index,
        month_term=month_term,
        next_term=next_term,
        conception=((four.month[0] + 1) % 10, (four.month[1] + 3) % 12),
        life_palace=life_palace,
        luck=luck,
        annual=annual,
        elements=elements,
        lunar_date=lunar.solar_to_lunar(local.day, local.month, local.year),
        hour_by_name=hour_by_name,
    )
    chart.notes = _notes(chart)
    return chart


# ─── Ghi chú quy ước theo từng lá ───────────────────────────────────────────


def _moment_text(moment: dt.datetime) -> str:
    return f"{moment:%H:%M %d/%m/%Y}"


def _term_notes(c: Chart) -> list[str]:
    """Sinh sát thời khắc giao tiết (dưới 2 giờ), hoặc không rõ giờ mà ngày sinh có
    giao tiết: trụ tháng (tiết Lập xuân: cả trụ năm) tùy vào giờ sinh."""
    notes = []
    month, year = c.pillars["month"].pair, c.pillars["year"].pair
    for term, after in ((c.month_term, True), (c.next_term, False)):
        at = _round_minute(term.at)
        shift = -1 if after else 1  # trụ ở phía bên kia thời khắc giao tiết
        other = f"trụ tháng là {pillar_text(_shift(month, shift))}"
        if term.index == _LAP_XUAN:
            other += f" và trụ năm là {pillar_text(_shift(year, shift))}"
        if c.has_hour:
            gap = abs(_minutes_between(c.birth, at))
            if gap >= _NEAR_TERM_MINUTES:
                continue
            gap_text = f"{gap} phút" if gap else "chưa tới 1 phút"
            side, move = ("sau", "sớm") if after else ("trước", "muộn")
            notes.append(
                f"Sinh {side} lúc giao tiết {term.name} ({_moment_text(at)}) {gap_text}: giờ "
                f"sinh thật chỉ cần {move} hơn chừng ấy thì {other}."
            )
        elif at.date() == c.birth.date():
            this = f"trụ tháng là {pillar_text(month)}"
            if term.index == _LAP_XUAN:
                this += f" và trụ năm là {pillar_text(year)}"
            before, since = (other, this) if after else (this, other)
            notes.append(
                f"Không rõ giờ sinh mà ngày này giao tiết {term.name} lúc {at:%H:%M}: sinh "
                f"trước giờ đó thì {before}, từ giờ đó trở đi thì {since} (lá số đang tạm "
                "lấy 12h00)."
            )
    return notes


def _tet_note(c: Chart) -> str | None:
    """Trụ năm đổi ở Lập xuân, năm âm lịch đổi ở Tết: ghi chú khi sinh giữa hai mốc."""
    lunar_year = c.lunar_date[2]
    if lunar_year == c.pillar_year:
        return None
    new_year = max(lunar_year, c.pillar_year)
    solar = lunar.lunar_to_solar(1, 1, new_year)
    if solar is None:
        return None
    tet = dt.date(solar[2], solar[1], solar[0])
    li_chun_at = _round_minute(solar_terms.li_chun(new_year)).astimezone(c.birth.tzinfo)
    if not c.has_hour and li_chun_at.date() == c.birth.date():
        return None  # trụ năm còn tùy giờ sinh: ghi chú giao tiết đã nói
    li_chun = _moment_text(li_chun_at)
    year_text = c.pillars["year"].text
    lunar_text = lunar.year_can_chi(lunar_year)
    if c.pillar_year > lunar_year:
        return (
            f"Sinh sau Lập xuân ({li_chun}) nhưng trước Tết ({tet:%d/%m/%Y}): trụ năm đã là "
            f"{year_text} vì Bát Tự đổi năm ở Lập xuân; năm âm lịch (tuổi tính theo Tết, như "
            f"Tử Vi) vẫn là {lunar_text}."
        )
    return (
        f"Sinh sau Tết ({tet:%d/%m/%Y}) nhưng trước Lập xuân ({li_chun}): năm âm lịch đã là "
        f"{lunar_text} nhưng trụ năm vẫn là {year_text} vì Bát Tự đổi năm ở Lập xuân, không "
        "ở Tết."
    )


def _notes(c: Chart) -> list[str]:
    notes = []
    day = c.pillars["day"]
    if c.has_hour and c.birth.hour == 23:
        other = _shift(day.pair, -1)
        notes.append(
            f"Sinh 23h-24h (đầu giờ Tý): trụ ngày tính sang ngày hôm sau là {day.text}; sách "
            f"khác đổi trụ ngày lúc 0h thì trụ ngày là {pillar_text(other)} (nhật chủ "
            f"{CAN[other[0]]} {STEM_ELEMENTS[other[0]]}), trụ giờ giữ nguyên."
        )
    if c.hour_by_name:
        branch = CHI[c.pillars["hour"].branch]
        text = (
            f"Giờ sinh ghi theo tên giờ {branch}: tính như {c.birth:%H:%M} (giữa giờ {branch}); "
            "trụ giờ không đổi, tuổi khởi vận lệch tối đa ±5 ngày."
        )
        if c.pillars["hour"].branch == 0:
            text += " Sinh 23h-24h thì gõ 23h để trụ ngày tính sang ngày hôm sau."
        notes.append(text)
    notes.extend(_term_notes(c))
    if not c.has_hour:
        notes.append(
            "Không rõ giờ sinh: lá số 3 trụ (không có trụ giờ, mệnh cung, tiểu vận); đại vận "
            "tạm tính từ 12h00 nên tuổi khởi vận có thể lệch tới ±2 tháng; nếu sinh 23h-24h "
            f"thì trụ ngày là {pillar_text(_shift(day.pair, 1))}."
        )
    tet = _tet_note(c)
    if tet:
        notes.append(tet)
    return notes


# ─── Hiển thị ───────────────────────────────────────────────────────────────


def _tz_label(moment: dt.datetime) -> str:
    offset = moment.utcoffset() or dt.timedelta(0)
    sign = "+" if offset >= dt.timedelta(0) else "-"
    hours, minutes = divmod(abs(int(offset.total_seconds())) // 60, 60)
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def command_args(c: Chart) -> str:
    """Tham số lệnh lập lại đúng lá số này (cho dòng "Gõ /battu_chitiet ...")."""
    parts = [f"{c.birth.day}/{c.birth.month}/{c.birth.year}"]
    if c.has_hour:
        hour = c.pillars["hour"].branch
        parts.append(f"giờ {CHI[hour]}" if c.hour_by_name else f"{c.birth:%H:%M}")
    parts.append("nam" if c.male else "nữ")
    if c.view_year != _now().year:
        parts.append(f"năm xem {c.view_year}")
    return " ".join(parts)


def _days_text(days: float) -> str:
    return f"{days:.2f}".replace(".", ",")


def _luck_lines(c: Chart) -> list[str]:
    luck = c.luck
    year_stem = c.pillars["year"].stem
    who = f"{yin_yang(year_stem).lower()} {'nam' if c.male else 'nữ'}"
    term = f"tiết {luck.term_name} ({_moment_text(luck.term_at)})"
    span = f"từ lúc sinh tới {term}" if luck.forward else f"từ {term} tới lúc sinh"
    lines = [
        f"🔄 Đại vận {'thuận' if luck.forward else 'nghịch'} ({who}, năm can {CAN[year_stem]}): "
        f"{span} là {_days_text(luck.days)} ngày, 3 ngày tính 1 năm → khởi vận "
        f"{luck.start_years} tuổi {luck.start_months} tháng (từ {luck.start_date:%d/%m/%Y})."
    ]
    for v in luck.pillars:
        mark = f" ◀ năm {c.view_year}" if v.from_year <= c.view_year <= v.to_year else ""
        lines.append(
            f"▸ {v.from_age}-{v.from_age + 9} tuổi ({v.from_year}-{v.to_year}): {v.text} · "
            f"{v.ten_god} · {v.growth}{mark}"
        )
    view = pillar_text((can_chi.year_can(c.view_year), can_chi.year_chi(c.view_year)))
    if c.annual:
        a = c.annual
        lines.append(f"Tiểu vận năm {a.year} ({view}, tuổi mụ {a.age}): {a.text} · {a.ten_god}")
    elif c.has_hour:
        lines.append(f"Tiểu vận: năm xem {c.view_year} ở trước năm sinh.")
    return lines


def format_chart(c: Chart) -> str:
    day_stem = c.day_master
    if c.has_hour:
        when = f"{c.birth:%d/%m/%Y %H:%M} ({_tz_label(c.birth)})"
    else:
        when = f"{c.birth:%d/%m/%Y}, không rõ giờ sinh"
    ld, lm, ly, leap = c.lunar_date
    names = " · ".join(p.name.lower() for p in c.pillars.values())
    head = [f"Nhật chủ: {CAN[day_stem]} {STEM_ELEMENTS[day_stem]} ({yin_yang(day_stem).lower()})"]
    if c.life_palace:
        head.append(f"Mệnh cung: {pillar_text(c.life_palace)}")
    head.append(f"Thai nguyên: {pillar_text(c.conception)}")
    missing = [name for name, count in c.elements.items() if not count]
    lines = [
        f"☯️ Bát Tự - {when}, {'Nam' if c.male else 'Nữ'}",
        f"Âm lịch: {lunar.lunar_text(ld, lm, ly, leap)}",
        f"Tứ trụ ({names}): " + " · ".join(p.text for p in c.pillars.values()),
        " · ".join(head),
        f"Tiết khí lúc sinh: {c.term_name} · tháng {CHI[c.pillars['month'].branch]} mở từ tiết "
        f"{c.month_term.name} ({_moment_text(_round_minute(c.month_term.at))})",
        f"Ngũ hành {2 * len(c.pillars)} chữ: "
        + " · ".join(f"{name} {count}" for name, count in c.elements.items())
        + (f" (thiếu {', '.join(missing)})" if missing else ""),
        "",
    ]
    for p in c.pillars.values():
        nap_am, _ = can_chi.nap_am(p.stem, p.branch)
        lines.append(
            f"▸ {p.name.upper()} {p.text} ({STEM_ELEMENTS[p.stem]} · "
            f"{BRANCH_ELEMENTS[p.branch]}) · {p.ten_god} · {nap_am}"
        )
        hidden = ", ".join(f"{CAN[h.stem]} ({h.role.lower()}, {h.ten_god})" for h in p.hidden)
        lines.append(f"  Tàng can {hidden} · trường sinh {p.growth} · tự tọa {p.self_seat}")
    lines.append("")
    lines.extend(_luck_lines(c))
    t = texts()
    meaning = t.get("nhat_chu", {}).get(CAN[day_stem], "")
    groups = t.get("thap_than", {})
    lines += [
        "",
        f"📌 Nhật chủ {CAN[day_stem]} ({STEM_ELEMENTS[day_stem]} "
        f"{yin_yang(day_stem).lower()}): {meaning}",
        "Thập thần: " + " · ".join(f"{g} - {groups.get(g, '')}" for g in TEN_GOD_GROUPS) + ".",
        "Trường sinh: giai đoạn của nhật chủ trên chi trụ; tự tọa: của chính can trụ trên chi trụ.",
    ]
    if c.notes:
        lines += ["", "⚠️ Lưu ý:"] + [f"• {note}" for note in c.notes]
    lines.append("")
    lines.append(f"{NOTE}\n\n{DISCLAIMER} Gõ /battu_chitiet {command_args(c)} để AI luận giải.")
    return "\n".join(lines)


def chart_data(c: Chart) -> dict:
    """Lá số dạng dict khoá camelCase cho trang web và JSON API (hợp đồng công khai:
    chỉ THÊM trường, không đổi/bỏ trường cũ)."""
    t = texts()

    def pair(p: tuple[int, int]) -> dict:
        stem, branch = p
        nap_am, nap_am_element = can_chi.nap_am(stem, branch)
        return {
            "stem": CAN[stem],
            "branch": CHI[branch],
            "text": pillar_text(p),
            "stemElement": STEM_ELEMENTS[stem],
            "branchElement": BRANCH_ELEMENTS[branch],
            "stemYinYang": yin_yang(stem),
            "napAm": nap_am,
            "napAmElement": nap_am_element,
        }

    def pillar(p: Pillar) -> dict:
        return pair(p.pair) | {
            "name": p.name,
            "tenGod": p.ten_god,
            "hiddenStems": [
                {
                    "stem": CAN[h.stem],
                    "element": STEM_ELEMENTS[h.stem],
                    "role": h.role,
                    "tenGod": h.ten_god,
                }
                for h in p.hidden
            ],
            "growth": p.growth,
            "selfSeat": p.self_seat,
        }

    def term(m: TermMoment) -> dict:
        return {
            "name": m.name,
            "startsAt": _round_minute(m.at).isoformat(timespec="minutes"),
        }

    luck = c.luck
    day_stem = c.day_master
    ld, lm, ly, leap = c.lunar_date
    annual = None
    if c.annual:
        a = c.annual
        annual = pair((a.stem, a.branch)) | {
            "year": a.year,
            "age": a.age,
            "tenGod": a.ten_god,
            "forward": luck.forward,
        }
    return {
        "birth": {
            "date": c.birth.date().isoformat(),
            "time": f"{c.birth:%H:%M}" if c.has_hour else None,
            "utcOffset": c.birth.isoformat()[-6:],
            "hourByName": c.hour_by_name,
        },
        "lunarDate": {
            "day": ld,
            "month": lm,
            "year": ly,
            "isLeapMonth": leap,
            "text": lunar.lunar_text(ld, lm, ly, leap),
        },
        "hasHour": c.has_hour,
        "gender": "male" if c.male else "female",
        "viewYear": c.view_year,
        "pillarYear": c.pillar_year,
        "pillars": {k: pillar(c.pillars[k]) if k in c.pillars else None for k in PILLAR_KEYS},
        "dayMaster": {
            "stem": CAN[day_stem],
            "element": STEM_ELEMENTS[day_stem],
            "yinYang": yin_yang(day_stem),
            "meaning": t.get("nhat_chu", {}).get(CAN[day_stem], ""),
        },
        "lifePalace": pair(c.life_palace) if c.life_palace else None,
        "conception": pair(c.conception),
        "solarTerm": c.term_name,
        "monthTerm": term(c.month_term),
        "nextTerm": term(c.next_term),
        "elements": dict(c.elements),
        "luck": {
            "forward": luck.forward,
            "direction": "thuận" if luck.forward else "nghịch",
            "term": {"name": luck.term_name, "at": luck.term_at.isoformat(timespec="minutes")},
            "minutes": luck.minutes,
            "days": luck.days,
            "startAge": {"years": luck.start_years, "months": luck.start_months},
            "startDate": luck.start_date.isoformat(),
            "pillars": [
                pair(v.pair)
                | {
                    "order": v.order,
                    "fromAge": v.from_age,
                    "fromYear": v.from_year,
                    "toYear": v.to_year,
                    "tenGod": v.ten_god,
                    "growth": v.growth,
                    "isCurrent": v.from_year <= c.view_year <= v.to_year,
                }
                for v in luck.pillars
            ],
        },
        "annual": annual,
        "tenGodMeanings": {g: t.get("thap_than", {}).get(g, "") for g in TEN_GOD_GROUPS},
        "notes": list(c.notes),
        "note": NOTE,
        "disclaimer": DISCLAIMER,
    }


# ─── Phân tích tham số lệnh ─────────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{1,2})[/.-](\d{1,2})(n)?[/.-](\d{4})")
# Năm xem: "năm xem 2027", "xem 2027", "năm 2027" hoặc số 4 chữ số đứng riêng. "nam"
# không dấu ("nam 2027", "nam xem 2027") để lại làm giới tính.
_VIEW_RE = re.compile(r"(?:\b(?:năm\s+xem|xem\s+năm|năm|xem)\s+)?\b(\d{4})\b")
# Trên chữ đã bỏ dấu: "9h30", "9:30", "9g", "9 gio 30 phut", "21h".
_HOUR_RE = re.compile(r"\b(\d{1,2})\s*(?:h|g|:|gio)(?![a-z])\s*(\d{1,2})?(?:\s*(?:phut|p)\b)?")
_PERIOD_RE = re.compile(r"\s*(sang|trua|chieu|toi|dem|khuya|pm)\b")
_GIO_CHI_RE = re.compile(
    r"\bgi(?:ờ|o)\s+(tý|tí|sửu|dần|mão|mẹo|thìn|tỵ|tị|ngọ|mùi|thân|dậu|tuất|hợi|"
    r"ty|ti|suu|dan|mao|meo|thin|ngo|mui|than|dau|tuat|hoi)\b"
)
_CHI_WORDS = {
    "tý": 0, "tí": 0, "sửu": 1, "suu": 1, "dần": 2, "dan": 2, "mão": 3, "mẹo": 3, "mao": 3,
    "meo": 3, "thìn": 4, "thin": 4, "tỵ": 5, "tị": 5, "ngọ": 6, "ngo": 6, "mùi": 7, "mui": 7,
    "thân": 8, "than": 8, "dậu": 9, "dau": 9, "tuất": 10, "tuat": 10, "hợi": 11, "hoi": 11,
}  # fmt: skip


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


def _solar_date(day: int, month: int, year: int, leap: bool, is_lunar: bool) -> dt.date | str:
    if not is_lunar:
        try:
            return dt.date(year, month, day)
        except ValueError:
            return "Ngày sinh không hợp lệ.\n" + USAGE
    if not (1 <= day <= 30 and 1 <= month <= 12):
        return "Ngày âm lịch không hợp lệ."
    converted = lunar.lunar_to_solar(day, month, year, leap)
    if converted is None:
        return "Ngày âm lịch không hợp lệ."
    sd, sm, sy = converted
    if lunar.solar_to_lunar(sd, sm, sy) != (day, month, year, leap):
        return f"Tháng {month} âm năm {year} không có ngày {day}."
    return dt.date(sy, sm, sd)


def parse(argument: str) -> Chart | str:
    """Lá số từ "15/3/1990 9h30 nam [năm xem 2027]" (bỏ trống giờ = không rõ giờ sinh;
    "âm 20/2/1990 giờ Tỵ nữ" cho ngày âm), hoặc câu báo lỗi."""
    low = (argument or "").lower().strip()
    m = _DATE_RE.search(low)
    if not m:
        return USAGE
    # "âm"/"âm lịch" ở bất cứ đâu (có dấu); không dấu chỉ nhận "am lich" hoặc "am"
    # đứng đầu - "9 am" kiểu tiếng Anh không được biến ngày dương thành âm.
    is_lunar = (
        bool(re.search(r"\b(?:âm|âm lịch|al)\b", low))
        or bool(re.match(r"^am\b", low))
        or "am lich" in low
        or bool(m.group(3))
    )
    year = int(m.group(4))
    if not MIN_YEAR <= year <= MAX_YEAR:
        return f"Năm sinh cần trong khoảng {MIN_YEAR}-{MAX_YEAR}."
    date = _solar_date(int(m.group(1)), int(m.group(2)), year, bool(m.group(3)), is_lunar)
    if isinstance(date, str):
        return date
    rest = (low[: m.start()] + " " + low[m.end() :]).strip()
    view_year = None
    view = _VIEW_RE.search(rest)
    if view:
        view_year = int(view.group(1))
        if not MIN_YEAR <= view_year <= MAX_VIEW_YEAR:
            return f"Năm xem cần trong khoảng {MIN_YEAR}-{MAX_VIEW_YEAR}."
        rest = (rest[: view.start()] + " " + rest[view.end() :]).strip()
    born: dt.date = date
    by_name = _GIO_CHI_RE.search(rest)
    if by_name:
        word = by_name.group(1)
        if word in ("ty", "ti"):
            return "Giờ Tý hay giờ Tỵ? Gõ có dấu hoặc ghi giờ cụ thể (vd 23h, 10h)."
        # Giữa giờ theo tên chi: Tý 0h, Sửu 2h, ... Hợi 22h.
        born = dt.datetime(date.year, date.month, date.day, 2 * _CHI_WORDS[word] % 24)
        rest = (rest[: by_name.start()] + " " + rest[by_name.end() :]).strip()
    else:
        folded = fold(rest)
        hm = _HOUR_RE.search(folded)
        if hm:
            hour, minute = int(hm.group(1)), int(hm.group(2) or 0)
            if hour > 23 or minute > 59:
                return "Giờ sinh không hợp lệ (0-23 giờ, 0-59 phút).\n" + USAGE
            hour = _apply_period(hour, folded[hm.end() :])
            born = dt.datetime(date.year, date.month, date.day, hour, minute)
        elif re.search(r"\b\d{1,2}\b", folded):
            return "Giờ sinh ghi kèm h hoặc : (vd 9h, 9:30, 21h15).\n" + USAGE
    if re.search(r"\b(nữ|nu|gái|gai)\b", rest):
        male = False
    elif re.search(r"\b(nam|trai)\b", rest):
        male = True
    else:
        return "Thiếu giới tính (nam/nữ) - đại vận đi thuận hay nghịch tùy giới tính.\n" + USAGE
    try:
        return build_chart(born, male, view_year, hour_by_name=bool(by_name))
    except ValueError as exc:
        return str(exc)


# ─── AI luận giải ──────────────────────────────────────────────────────────


def build_ai_prompt(c: Chart) -> str:
    return (
        "Bạn là người luận Bát Tự (Tứ Trụ, phép Tử Bình) viết tiếng Việt, giọng điềm đạm, "
        "thực tế, không mê tín, không hù doạ, không phán chuyện sinh tử, không thay lời "
        "khuyên y tế/pháp lý/tài chính chuyên môn. Lá số dưới đây ĐÃ ĐƯỢC LẬP SẴN bằng code "
        "- TUYỆT ĐỐI KHÔNG tự lập lại hay đổi trụ, can chi, tàng can, thập thần, trường sinh, "
        "mệnh cung, đại vận, tiểu vận hoặc tuổi khởi vận; chỉ luận giải dựa trên đúng dữ "
        "liệu này. Viết khoảng 500-700 từ, chia mục ngắn: Nhật chủ và độ vượng/nhược (dựa "
        "vào tháng sinh, trường sinh, tàng can, số ngũ hành); Tính cách; Công danh - tài "
        "lộc; Tình cảm - gia đạo; Sức khoẻ (nói chung theo ngũ hành); Đại vận đang đi, tiểu "
        "vận năm xem và lời khuyên. Nếu lá số có mục Lưu ý (sinh sát giờ giao tiết, sinh "
        "23h-24h, không rõ giờ...) thì nhắc ngắn phần nào có thể đổi. Chỉ dùng thuật ngữ "
        "tiếng Việt, không viết chữ Hán, không dùng bảng markdown. Cuối bài nhắc đây là "
        "tham khảo.\n\n"
        f"Năm xem: {c.view_year}.\nLá số:\n{format_chart(c)}"
    )


async def explain_with_ai(c: Chart) -> str:
    """Luận giải bằng AI (provider-chain, có hạn chót lượt). Raise khi AI lỗi."""
    from ai import orchestrator
    from ai.timeouts import turn_deadline

    with turn_deadline():
        response = await orchestrator.ask(build_ai_prompt(c))
    return (getattr(response, "text", "") or "").strip()
