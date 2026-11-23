"""Lễ hội, lễ tết và ngày nghỉ Việt Nam - 206 dịp (bộ dữ liệu le-hoi-viet-nam).

Dữ liệu: Nhật Nguyệt (nhatnguyet.org), CC BY 4.0 - bản rút gọn assets/le_hoi_viet_nam.json
(sinh bằng scripts/make_le_hoi_data.py, chỉ giữ cột tiếng Việt). Đổi sang ngày dương theo
đúng luật của bộ dữ liệu, kiểm bằng ca kiểm le-hoi.json (test/test_le_hoi.py):

- lễ dương lịch giữ ngày tháng; 29/2 của năm không nhuận thì bỏ năm ấy;
- lễ âm lịch tính theo giờ Việt Nam, đặt vào tháng CHÍNH (không tính tháng nhuận);
  năm âm Y cho ngày dương trong năm Y (tháng Chạp có thể sang đầu năm Y+1);
- ngày 30 của tháng âm thiếu không có thật: bỏ năm ấy, không đoán ngày dời;
- 42 dịp "theo năm" do ban tổ chức công bố từng năm: không tính, chỉ in lịch đã công bố.

Nạp lười 1 lần (~90 KB JSON); danh sách theo năm có cache trần. Dùng ở /lich
(services/calendar_query.py) và trang /lich/le-hoi (web_site.py).
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path

from core.text_normalize import fold
from services import lunar

_PATH = Path(__file__).resolve().parent.parent / "assets" / "le_hoi_viet_nam.json"
ATTRIBUTION = "Dữ liệu lễ hội: Nhật Nguyệt (nhatnguyet.org), CC BY 4.0"
MIN_YEAR, MAX_YEAR = 1900, 2100
GROUP_LABELS = {"le-tet": "Lễ tết", "tin-nguong": "Tín ngưỡng", "khac": "Lễ hội, sự kiện"}
# Tiền tố bỏ đi để khớp tên ngắn ("chùa Hương" cho "Lễ hội Chùa Hương"); phần còn lại phải
# đủ 2 từ để "Hội Lim" không thành khoá "lim".
_PREFIXES = ("le hoi ", "hoi ", "le ", "ngay ", "tet ", "festival ", "dai le ")
# Cách gọi khác của tỉnh/thành (34 tỉnh theo Nghị quyết 202/2025/QH15).
_PROVINCE_ALIASES = {
    "sai gon": "ho-chi-minh",
    "tp hcm": "ho-chi-minh",
    "tphcm": "ho-chi-minh",
    "hcm": "ho-chi-minh",
    "thanh pho ho chi minh": "ho-chi-minh",
    "tp ho chi minh": "ho-chi-minh",
    "thua thien hue": "hue",
    "co do hue": "hue",
}


@dataclass(frozen=True)
class Festival:
    id: str
    name: str
    kind: str  # "lunar" | "solar" | "theo-nam" (ban tổ chức công bố từng năm)
    day: int  # ngày/tháng âm hoặc dương của chính lễ; 0 với "theo-nam"
    month: int
    before: int  # số ngày hội trước / sau chính lễ
    after: int
    province_codes: tuple[str, ...]
    provinces: tuple[str, ...]
    venue: str
    ethnic: str
    group: str
    major: bool
    national: bool
    public_holiday: bool
    announced: str
    description: str
    keys: tuple[str, ...]  # tên đã bỏ dấu để so khớp câu hỏi

    @property
    def is_lunar(self) -> bool:
        return self.kind == "lunar"

    @property
    def place(self) -> str:
        if self.provinces:
            return ", ".join(self.provinces)
        return "cả nước" if self.national else ""

    def date_label(self) -> str:
        """ "13 tháng Giêng âm lịch", "24/11 dương lịch" hoặc câu "theo năm"."""
        if self.kind == "lunar":
            return f"{lunar_day_text(self.day, self.month)} âm lịch"
        if self.kind == "solar":
            return f"{self.day:02d}/{self.month:02d} dương lịch"
        return "ngày do ban tổ chức công bố từng năm"


def lunar_day_text(day: int, month: int) -> str:
    """ "mùng 6 tháng Giêng", "15 tháng 8", "23 tháng Chạp"."""
    name = {1: "Giêng", 12: "Chạp"}.get(month, str(month))
    return f"{'mùng ' if day <= 10 else ''}{day} tháng {name}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[()\-,.:;/]", " ", fold(text))).strip()


def _keys(name: str) -> tuple[str, ...]:
    keys = {_clean(name)}
    for part in re.findall(r"\(([^)]*)\)", name):  # "Rằm tháng Giêng (Tết Nguyên Tiêu)"
        keys.add(_clean(part))
    keys.add(_clean(re.sub(r"\([^)]*\)", " ", name)))
    for key in list(keys):
        for prefix in _PREFIXES:
            rest = key[len(prefix) :] if key.startswith(prefix) else ""
            if len(rest.split()) >= 2:
                keys.add(rest)
                if prefix == "le hoi ":
                    keys.add("hoi " + rest)
    return tuple(sorted((k for k in keys if k), key=len, reverse=True))


def _day_month(raw: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", (raw or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


@functools.lru_cache(maxsize=1)
def _load() -> tuple[tuple[Festival, ...], dict[str, str]]:
    data = json.loads(_PATH.read_text(encoding="utf-8"))
    provinces: dict[str, str] = data["tinh"]
    items = []
    for row in data["leHoi"]:
        kind = row.get("loai", "")
        day, month = _day_month(row.get("am" if kind == "lunar" else "duong", ""))
        codes = tuple(row.get("tinh", []))
        items.append(
            Festival(
                id=row["id"],
                name=row["ten"],
                kind=kind,
                day=day,
                month=month,
                before=int(row.get("truoc", 0)),
                after=int(row.get("sau", 0)),
                province_codes=codes,
                provinces=tuple(provinces.get(c, c) for c in codes),
                venue=row.get("noi", ""),
                ethnic=row.get("danToc", ""),
                group=row.get("nhom", ""),
                major=bool(row.get("lon")),
                national=bool(row.get("toanQuoc")),
                public_holiday=bool(row.get("nghiLe")),
                announced=row.get("congBo", ""),
                description=row.get("moTa", ""),
                keys=_keys(row["ten"]),
            )
        )
    return tuple(items), provinces


def festivals() -> tuple[Festival, ...]:
    return _load()[0]


def provinces() -> dict[str, str]:
    """{mã tỉnh: tên} - 34 tỉnh, thành phố."""
    return dict(_load()[1])


def get(festival_id: str) -> Festival | None:
    return next((f for f in festivals() if f.id == festival_id), None)


def date_in_year(f: Festival, year: int) -> dt.date | None:
    """Ngày dương của chính lễ trong năm (năm ÂM với lễ âm lịch), None nếu năm ấy không
    có (29/2, ngày 30 tháng thiếu, lễ theo năm, năm ngoài 1900-2100)."""
    if not (MIN_YEAR <= year <= MAX_YEAR) or not f.day:
        return None
    if f.kind == "solar":
        try:
            return dt.date(year, f.month, f.day)
        except ValueError:
            return None
    if f.kind != "lunar":
        return None
    solar = lunar.lunar_to_solar(f.day, f.month, year)
    if solar is None:
        return None
    day = dt.date(solar[2], solar[1], solar[0])
    if lunar.solar_to_lunar(day.day, day.month, day.year) != (f.day, f.month, year, False):
        return None  # ngày 30 của tháng thiếu
    return day


def occurrences(year: int) -> list[tuple[Festival, dt.date]]:
    """Mọi lễ ngày cố định của năm ``year`` (năm âm với lễ âm), theo thứ tự bộ dữ liệu -
    đúng phép ``le_hoi_trong_nam`` của ca kiểm."""
    return [(f, d) for f in festivals() if (d := date_in_year(f, year)) is not None]


@functools.lru_cache(maxsize=8)
def _by_solar_year(year: int) -> tuple[tuple[dt.date, Festival], ...]:
    found = []
    for f in festivals():
        for y in (year - 1, year):  # tháng Chạp năm âm trước rơi vào đầu năm dương này
            day = date_in_year(f, y)
            if day is not None and day.year == year:
                found.append((day, f))
    return tuple(sorted(found, key=lambda item: (item[0], not item[1].major, item[1].name)))


def between(
    start: dt.date, end: dt.date, province: str | None = None
) -> list[tuple[dt.date, Festival]]:
    """Lễ có chính lễ trong [start, end], sắp theo ngày (lễ lớn trước); lọc theo mã tỉnh."""
    out = []
    for year in range(max(start.year, MIN_YEAR), min(end.year, MAX_YEAR) + 1):
        for day, f in _by_solar_year(year):
            if start <= day <= end and (province is None or province in f.province_codes):
                out.append((day, f))
    return out


def on_day(day: dt.date) -> list[Festival]:
    """Lễ có chính lễ đúng ngày dương ``day`` (lễ lớn trước)."""
    return [f for d, f in between(day, day)]


def next_date(f: Festival, today: dt.date, year: int | None = None) -> dt.date | None:
    """Chính lễ lần tới (kể cả hôm nay), hoặc của năm ``year`` nếu hỏi năm cụ thể."""
    if year is not None:
        return date_in_year(f, year)
    for y in (today.year - 1, today.year, today.year + 1):
        day = date_in_year(f, y)
        if day is not None and day >= today:
            return day
    return None


def find(t: str) -> Festival | None:
    """Lễ có tên (đã bỏ dấu) nằm trong câu ``t`` (đã bỏ dấu, gộp khoảng trắng); khoá dài
    nhất thắng, bằng nhau thì lễ lớn trước. None nếu không có."""
    padded = f" {_clean(t)} "
    best: tuple[int, bool, Festival] | None = None
    for f in festivals():
        for key in f.keys:
            if f" {key} " in padded:
                rank = (len(key), f.major)
                if best is None or rank > best[:2]:
                    best = (len(key), f.major, f)
                break  # khoá đã sắp dài trước
    return best[2] if best else None


@functools.lru_cache(maxsize=1)
def _province_keys() -> tuple[tuple[str, str], ...]:
    keys = {fold(name): code for code, name in _load()[1].items()}
    keys.update(_PROVINCE_ALIASES)
    return tuple(sorted(keys.items(), key=lambda kv: len(kv[0]), reverse=True))


def find_province(t: str) -> str | None:
    """Mã tỉnh nhắc trong câu (đã bỏ dấu), tên dài nhất thắng; None nếu không có."""
    padded = f" {_clean(t)} "
    for key, code in _province_keys():
        if f" {key} " in padded:
            return code
    return None


def _announced_text(f: Festival) -> str:
    """Lịch ban tổ chức đã công bố, ngày ISO đổi sang dd/mm/yyyy."""
    return re.sub(r"(\d{4})-(\d{2})-(\d{2})", r"\3/\2/\1", f.announced)


def _span_text(f: Festival, day: dt.date) -> str:
    if not (f.before or f.after):
        return ""
    first = day - dt.timedelta(days=f.before)
    last = day + dt.timedelta(days=f.after)
    return f" Hội diễn ra {first:%d/%m} - {last:%d/%m/%Y}."


def describe(f: Festival, today: dt.date, year: int | None = None) -> str:
    """Câu trả lời cho 1 lễ (/lich hội Lim, /lich lễ hội chùa Hương năm 2027)."""
    head = f"🎎 {f.name}" + (f" ({f.place})" if f.place else "")
    if f.kind == "theo-nam":
        lines = [f"{head}: ngày tổ chức do ban tổ chức công bố từng năm."]
        if f.announced:
            lines.append(f"Lịch đã công bố: {_announced_text(f)}")
    else:
        day = next_date(f, today, year)
        if day is None:
            when = f"năm {year} không có {f.date_label()}" if year else f.date_label()
            lines = [f"{head}: {when}."]
        else:
            info = lunar.date_info(day)
            days = (day - today).days
            tail = (
                f" - còn {days} ngày"
                if days > 0
                else " - là hôm nay"
                if days == 0
                else f" - đã qua {-days} ngày"
            )
            lines = [
                f"{head}: {info['weekday']} {info['date_vn']} dương lịch, tức "
                f"{info['lunar']['text']} âm lịch{tail}.{_span_text(f, day)}"
            ]
    if f.public_holiday:
        lines.append("Đây là ngày nghỉ lễ của người lao động.")
    if f.venue:
        lines.append(f"Nơi tổ chức: {f.venue}.")
    if f.description:
        lines.append(f.description)
    lines.append(ATTRIBUTION)
    return "\n".join(lines)


def list_text(items: list[tuple[dt.date, Festival]], title: str, limit: int = 15) -> str:
    """Danh sách lễ (ngày dương, ngày âm, tỉnh) cho /lich."""
    if not items:
        return f"🎎 {title}: chưa có lễ hội nào trong dữ liệu.\n{ATTRIBUTION}"
    lines = [f"🎎 {title}:"]
    for day, f in items[:limit]:
        ld, lm, _, leap = lunar.solar_to_lunar(day.day, day.month, day.year)
        lunar_part = f"{ld}/{lm}{' nhuận' if leap else ''} âm"
        place = f" - {f.place}" if f.place else ""
        lines.append(f"• {day:%d/%m} ({lunar_part}) {f.name}{place}")
    if len(items) > limit:
        lines.append(f"… và {len(items) - limit} lễ khác (thêm tên tỉnh để lọc).")
    lines.append(ATTRIBUTION)
    return "\n".join(lines)
