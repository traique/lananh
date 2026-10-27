"""Danh mục đài xổ số kiến thiết theo tỉnh: tên, tên khác, miền, lịch quay, feed RSS.

Lịch quay cố định trong tuần (giờ VN) theo bảng "Lịch mở thưởng xổ số 3 miền"
của xskt.com.vn (https://xskt.com.vn/lich-mo-thuong-xo-so-3-mien/, tra 27/10/2026),
đối chiếu với tiêu đề các kỳ trong chính feed RSS từng tỉnh (vd Thừa Thiên Huế
quay thứ Hai và Chủ Nhật, Tây Ninh thứ Năm) và lịch trên báo (baoquocte.vn,
doanhnghiephoinhap.vn):

- Miền Nam (16h15): T2 TP.HCM, Đồng Tháp, Cà Mau · T3 Bến Tre, Vũng Tàu, Bạc Liêu ·
  T4 Đồng Nai, Cần Thơ, Sóc Trăng · T5 Tây Ninh, An Giang, Bình Thuận · T6 Vĩnh Long,
  Bình Dương, Trà Vinh · T7 TP.HCM, Long An, Bình Phước, Hậu Giang · CN Tiền Giang,
  Kiên Giang, Đà Lạt (Lâm Đồng).
- Miền Trung (17h15): T2 Thừa Thiên Huế, Phú Yên · T3 Đắk Lắk, Quảng Nam · T4 Đà
  Nẵng, Khánh Hòa · T5 Bình Định, Quảng Trị, Quảng Bình · T6 Gia Lai, Ninh Thuận ·
  T7 Đà Nẵng, Quảng Ngãi, Đắk Nông · CN Kon Tum, Khánh Hòa, Thừa Thiên Huế.
- Miền Bắc (18h15): quay HẰNG NGÀY (1 kỳ chung, nơi quay luân phiên Hà Nội, Quảng
  Ninh, Bắc Ninh, Hải Phòng, Nam Định, Thái Bình) nên chỉ có 1 mục "Miền Bắc".

Công ty xổ số có thể đổi lịch (sáp nhập tỉnh, ngày lễ Tết nghỉ quay): lịch ở đây chỉ
dùng để GỢI Ý ("hôm nay đài này không quay") - kết quả luôn lấy từ feed, không bao
giờ suy ra từ lịch. Feed riêng từng tỉnh: https://xskt.com.vn/rss-feed/<slug>.rss
(cùng định dạng feed miền Bắc: mô tả plain text "ĐB: ...", không có "[Tên tỉnh]").

Module thuần dữ liệu (không I/O, không import services khác) để vn_market_data,
vn_daily_chat, web_site và nút Telegram dùng chung.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from core.text_normalize import fold

FEED_BASE = "https://xskt.com.vn/rss-feed/"
WEEKDAYS = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật")
SHORT_WEEKDAYS = ("T2", "T3", "T4", "T5", "T6", "T7", "CN")
REGION_ORDER = ("mn", "mt", "mb")
REGION_LABELS = {"mb": "Miền Bắc", "mn": "Miền Nam", "mt": "Miền Trung"}
DRAW_TIMES = {"mn": "16h15", "mt": "17h15", "mb": "18h15"}
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)


@dataclass(frozen=True)
class Province:
    key: str  # "tay-ninh" - dùng trong URL web/API
    name: str  # tên hiển thị
    region: str  # "mn" | "mt" | "mb"
    weekdays: tuple[int, ...]  # 0 = Thứ Hai ... 6 = Chủ Nhật
    feed: str  # slug feed xskt.com.vn, vd "tay-ninh-xstn"
    aliases: tuple[str, ...] = ()  # tên khác (bỏ dấu, chữ thường)

    @property
    def feed_url(self) -> str:
        return f"{FEED_BASE}{self.feed}.rss"

    @property
    def index(self) -> int:
        """Vị trí cố định trong PROVINCES (callback_data nút Telegram)."""
        return PROVINCES.index(self)

    def draws_on(self, day: dt.date) -> bool:
        return day.weekday() in self.weekdays

    @property
    def code(self) -> str:
        """Mã đài kiểu "xstn" (đuôi slug feed)."""
        return self.feed.rsplit("-", 1)[-1]

    def match_keys(self) -> tuple[str, ...]:
        # "xskt" (Kon Tum) trùng cách viết tắt "xổ số kiến thiết" nên không nhận làm tên đài.
        code = () if self.code in ("xskt", "xsmb") else (self.code,)
        return (_key(self.name), *self.aliases, *code)


def _p(key, name, region, weekdays, suffix, *aliases) -> Province:
    return Province(key, name, region, tuple(weekdays), f"{key}-{suffix}", tuple(aliases))


# Thứ tự cố định (chỉ THÊM ở cuối): chỉ số dùng trong callback_data "xs:<số>".
PROVINCES: tuple[Province, ...] = (
    # ── Miền Nam ──
    _p("ho-chi-minh", "TP.HCM", "mn", (0, 5), "xshcm",
       "ho chi minh", "tp ho chi minh", "thanh pho ho chi minh", "tp hcm", "tphcm", "hcm",
       "sai gon", "xshcm", "xstphcm"),
    _p("dong-thap", "Đồng Tháp", "mn", (0,), "xsdt"),
    _p("ca-mau", "Cà Mau", "mn", (0,), "xscm"),
    _p("ben-tre", "Bến Tre", "mn", (1,), "xsbt"),
    _p("vung-tau", "Vũng Tàu", "mn", (1,), "xsvt", "ba ria vung tau", "brvt"),
    _p("bac-lieu", "Bạc Liêu", "mn", (1,), "xsbl"),
    _p("dong-nai", "Đồng Nai", "mn", (2,), "xsdn"),
    _p("can-tho", "Cần Thơ", "mn", (2,), "xsct"),
    _p("soc-trang", "Sóc Trăng", "mn", (2,), "xsst"),
    _p("tay-ninh", "Tây Ninh", "mn", (3,), "xstn"),
    _p("an-giang", "An Giang", "mn", (3,), "xsag"),
    _p("binh-thuan", "Bình Thuận", "mn", (3,), "xsbth"),
    _p("vinh-long", "Vĩnh Long", "mn", (4,), "xsvl"),
    _p("binh-duong", "Bình Dương", "mn", (4,), "xsbd"),
    _p("tra-vinh", "Trà Vinh", "mn", (4,), "xstv"),
    _p("long-an", "Long An", "mn", (5,), "xsla"),
    _p("binh-phuoc", "Bình Phước", "mn", (5,), "xsbp"),
    _p("hau-giang", "Hậu Giang", "mn", (5,), "xshg"),
    _p("tien-giang", "Tiền Giang", "mn", (6,), "xstg"),
    _p("kien-giang", "Kiên Giang", "mn", (6,), "xskg"),
    _p("lam-dong", "Đà Lạt", "mn", (6,), "xsld", "lam dong", "xsdl"),
    # ── Miền Trung ──
    _p("thua-thien-hue", "Thừa Thiên Huế", "mt", (0, 6), "xstth",
       "hue", "tt hue", "thua thien hue"),
    _p("phu-yen", "Phú Yên", "mt", (0,), "xspy"),
    _p("dak-lak", "Đắk Lắk", "mt", (1,), "xsdlk", "daklak", "dac lac", "dak lak"),
    _p("quang-nam", "Quảng Nam", "mt", (1,), "xsqnm"),
    _p("da-nang", "Đà Nẵng", "mt", (2, 5), "xsdng"),
    _p("khanh-hoa", "Khánh Hòa", "mt", (2, 6), "xskh", "khanh hoa", "nha trang"),
    _p("binh-dinh", "Bình Định", "mt", (3,), "xsbdi"),
    _p("quang-tri", "Quảng Trị", "mt", (3,), "xsqt"),
    _p("quang-binh", "Quảng Bình", "mt", (3,), "xsqb"),
    _p("gia-lai", "Gia Lai", "mt", (4,), "xsgl"),
    _p("ninh-thuan", "Ninh Thuận", "mt", (4,), "xsnt"),
    _p("quang-ngai", "Quảng Ngãi", "mt", (5,), "xsqng"),
    _p("dak-nong", "Đắk Nông", "mt", (5,), "xsdno", "daknong", "dac nong", "dak nong"),
    _p("kon-tum", "Kon Tum", "mt", (6,), "xskt"),
    # ── Miền Bắc: 1 kỳ chung mỗi ngày ──
    _p("mien-bac", "Miền Bắc", "mb", ALL_DAYS, "xsmb",
       "ha noi", "thu do", "hai phong", "quang ninh", "bac ninh", "nam dinh", "thai binh"),
)  # fmt: skip

BY_KEY = {p.key: p for p in PROVINCES}


def _key(text: str) -> str:
    """Bỏ dấu, dấu câu thành khoảng trắng: "TP. HCM" -> "tp hcm"."""
    return " ".join(re.sub(r"[^\w]+", " ", fold(text)).split())


_BY_ALIAS: dict[str, Province] = {}
for _province in PROVINCES:
    for _alias in _province.match_keys():
        _BY_ALIAS.setdefault(_alias, _province)
# "mien bac" là tên miền (đã có trong nhận diện miền), không nhận như tên tỉnh.
_BY_ALIAS.pop("mien bac", None)
_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in sorted(_BY_ALIAS, key=len, reverse=True)) + r")\b"
)


def match(text: str) -> Province | None:
    """Đài được nhắc tới trong câu (so bỏ dấu, nhận tên khác), tên dài nhất thắng."""
    m = _ALIAS_RE.search(_key(text))
    return _BY_ALIAS[m.group(1)] if m else None


def find_all(text: str) -> list[Province]:
    out: list[Province] = []
    for m in _ALIAS_RE.finditer(_key(text)):
        p = _BY_ALIAS[m.group(1)]
        if p not in out:
            out.append(p)
    return out


def alias_pattern() -> str:
    """Regex (bỏ dấu) các tên đài - để bỏ khỏi câu khi đếm chữ thừa."""
    return _ALIAS_RE.pattern


def same(name: str, province: Province) -> bool:
    """Tên đài trên nguồn (vd "TP. HCM", "Lâm Đồng") có phải ``province`` không."""
    key = _key(name)
    return key in province.match_keys() or _BY_ALIAS.get(key) is province


def get(key: str) -> Province | None:
    """Theo khoá URL ("tay-ninh") hoặc tên/tên khác ("Tây Ninh", "đà lạt")."""
    raw = (key or "").strip()
    if raw in BY_KEY:
        return BY_KEY[raw]
    folded = _key(raw)
    return _BY_ALIAS.get(folded) or BY_KEY.get(folded.replace(" ", "-"))


def by_index(index: int) -> Province | None:
    return PROVINCES[index] if 0 <= index < len(PROVINCES) else None


def in_region(region: str) -> list[Province]:
    return [p for p in PROVINCES if p.region == region]


def drawing_on(day: dt.date, region: str | None = None) -> list[Province]:
    """Đài quay ngày ``day`` (theo lịch cố định), lọc theo miền nếu có."""
    return [p for p in PROVINCES if p.draws_on(day) and (region is None or p.region == region)]


def schedule_text(province: Province) -> str:
    """ "thứ Năm" / "thứ Hai và Chủ Nhật" / "hằng ngày"."""
    if province.weekdays == ALL_DAYS:
        return "hằng ngày"
    names = [WEEKDAYS[d] if d == 6 else WEEKDAYS[d].lower() for d in province.weekdays]
    return " và ".join(names)


def last_draw_day(province: Province, day: dt.date) -> dt.date:
    """Ngày quay gần nhất <= ``day`` theo lịch."""
    for back in range(7):
        candidate = day - dt.timedelta(days=back)
        if province.draws_on(candidate):
            return candidate
    return day
