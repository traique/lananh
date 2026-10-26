"""Đọc thời điểm nhắc việc tiếng Việt bằng code - KHÔNG gọi AI.

Router LLM chỉ chép lại cụm thời gian người dùng nói (``when``) hoặc trả
{date, time}/{in_minutes}; giờ nhắc thật luôn do module này tính (giờ VN, không
có giờ mùa hè), tránh lỗi LLM cộng trừ phút ("8h sáng mai" gửi lúc 22:37).

Hiểu các dạng thường gặp:
- "30 phút nữa", "1 tiếng rưỡi nữa", "nửa tiếng nữa", "sau 2 ngày".
- Giờ: "8h", "8h30", "20:30", "8 giờ 15", "8h rưỡi", "9h kém 15", kèm buổi
  "sáng/trưa/chiều/tối/đêm" (đứng trước hoặc sau giờ). Chỉ có buổi ("sáng mai")
  thì lấy giờ mặc định của buổi đó.
- Ngày: hôm nay/tối nay, mai, mốt/ngày kia, thứ 6 (tuần sau), chủ nhật, cuối
  tuần, 15/11, ngày 15 tháng 11, ngày 15; âm lịch: mùng 1, rằm, rằm tháng 7,
  12/3 âm, ngày 12 tháng 3 âm (dùng lại bộ đọc ngày của services/calendar_query).
- Lặp: mỗi ngày/hằng ngày/mỗi sáng, mỗi thứ 2 / thứ 2,4,6 hằng tuần, ngày 5
  hằng tháng, mùng 1 và rằm (âm lịch hằng tháng), 20/11 hằng năm, 12/3 âm hằng năm.

Chữ được bỏ dấu để so khớp nhưng GIỮ NGUYÊN độ dài (1 ký tự - 1 ký tự) nên vị
trí khớp map thẳng về câu gốc: dùng để cắt nội dung nhắc và để soát lại các chữ
trùng nhau khi bỏ dấu (tôi/tối/tới, mai/mãi, một/mốt, mùng/mừng, âm/ấm...).
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from core.text_normalize import nfc
from services import calendar_query, recurrence

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_AHEAD = dt.timedelta(days=366)
MIN_AHEAD = dt.timedelta(seconds=30)
# Giờ mặc định khi chỉ nói buổi ("sáng mai gọi điện").
PERIOD_HOUR = {"sang": 8, "trua": 12, "chieu": 15, "toi": 20, "dem": 22, "khuya": 23}
_PERIOD_ACCENTED = {
    "sang": "sáng",
    "trua": "trưa",
    "chieu": "chiều",
    "toi": "tối",
    "dem": "đêm",
    "khuya": "khuya",
}
_NUMBER_WORDS = {
    "nua": 0.5,
    "mot": 1,
    "hai": 2,
    "ba": 3,
    "bon": 4,
    "tu": 4,
    "nam": 5,
    "sau": 6,
    "bay": 7,
    "tam": 8,
    "chin": 9,
    "muoi": 10,
    "muoi lam": 15,
    "hai muoi": 20,
    "ba muoi": 30,
    "bon muoi lam": 45,
}
# Chữ số viết bằng chữ có dấu khác hẳn chữ đồng âm khi bỏ dấu.
_NUMBER_ACCENTED = {"mot": "một", "nam": "năm", "sau": "sáu", "tu": "tư", "bay": "bảy"}
_WEEKDAY = {"hai": 0, "ba": 1, "tu": 2, "nam": 3, "sau": 4, "bay": 5}
_MONTH_WORD = {"gieng": 1, "chap": 12}

_NUM = r"\d+(?:[.,]\d+)?|bon muoi lam|muoi lam|hai muoi|ba muoi|nua|mot|hai|ba|bon|tu|nam|sau|bay|tam|chin|muoi"
_DURATION_RE = re.compile(
    r"\b(?:(?P<pre>sau|trong) )?(?P<n>" + _NUM + r") ?(?P<u>phut|p|tieng|gio|h|ngay|tuan)\b"
    r"(?P<ruoi> ruoi)?(?P<post> nua)?"
)
_TIME_RE = re.compile(
    r"\b(?P<h>\d{1,2}) ?(?P<sep>h|g|gio|:) ?"
    r"(?:(?P<m>\d{1,2}) ?(?:phut|p)?\b|(?P<ruoi>ruoi)\b|(?=[^\w]|$))"
    r"(?: ?kem (?P<kem>\d{1,2}) ?(?:phut|p)?\b)?"
)
_PERIOD_RE = re.compile(r"\b(sang|trua|chieu|toi|dem|khuya)\b")
_RECUR_RE = re.compile(
    r"\b(?:(?:moi|hang) (?P<unit>ngay|tuan|thang|nam)|(?P<unit2>ngay|tuan|thang|nam) nao cung"
    r"|moi (?P<period>sang|trua|chieu|toi|dem))\b"
)
_WEEKDAY_RE = re.compile(
    r"\b(?:(?P<moi>moi |cac |vao cac )?thu (?P<w>hai|ba|tu|nam|sau|bay|[2-7])"
    r"(?P<more>(?: ?(?:,|va|-) ?(?:thu )?(?:[2-7]|hai|ba|tu|nam|sau|bay|chu nhat|cn)\b)*)"
    r"|(?P<moi2>moi |cac )?(?P<cn>chu nhat|cn))\b(?: tuan (?P<shift>nay|sau|toi|truoc))?"
)
_LUNAR_A = re.compile(
    r"\b(?:(?P<ram>ram)|(?:mung|mong) (?P<d>\d{1,2}))"
    r"(?P<extra>(?: ?(?:,|va) ?(?:ram|(?:mung|mong) \d{1,2}))*)"
    r"(?: thang (?P<m>\d{1,2}|gieng|chap)(?P<leap> nhuan)?| (?P<tet>tet))?"
    r"(?P<am> (?:am lich|am|al)\b)?"
)
_LUNAR_B = re.compile(
    r"\bngay (?P<d>\d{1,2}) thang (?P<m>\d{1,2}|gieng|chap)(?P<leap> nhuan)? (?:am lich|am|al)\b"
)
_LUNAR_C = re.compile(
    r"\b(?P<d>\d{1,2})[/-](?P<m>\d{1,2})(?P<leap> ?n(?:huan)?)?(?:[/-](?P<y>\d{4}))?"
    r" (?:am lich|am|al)\b"
)
_DAY_OF_MONTH_RE = re.compile(r"\bngay (?P<d>\d{1,2})\b(?! ?(?:thang|[/-]|\d))")
_TODAY_RE = re.compile(r"\b(?:hom nay|bua nay|(?P<p>sang|trua|chieu|toi|dem) nay)\b")
_TOMORROW_RE = re.compile(r"\b(?:ngay mai|hom mai|mai)\b")
_AFTER_TOMORROW_RE = re.compile(r"\b(?:ngay kia|mot)\b")
_WEEKEND_RE = re.compile(r"\b(?:cuoi tuan)(?: (?P<shift>nay|sau|toi))?\b")
_NEXT_MONDAY_RE = re.compile(r"\bdau tuan (?:sau|toi)\b")
_INTENT_RE = re.compile(
    r"\b(?:(?:nho|em oi|oi) )?(?:nhac(?: nho)?|hen gio|dat (?:lich|nhac|bao thuc)|remind(?: me)?)"
    r"(?: (?:giup|gium|dum|ho))?"
    r"(?: (?:anh|em|toi|minh|chi|co|chu|bac|con|chau|tui|me|ban|bo))?"
    r"(?: (?:giup|gium|dum|ho|voi))?(?: (?:la|rang|ve|viec|de))?\b"
)
# Từ nối/từ đệm ở 2 đầu nội dung nhắc (so trên chữ thường CÒN DẤU: "nhà" khác "nha").
_EDGE_HEAD_RE = re.compile(
    r"^(?:(?:lúc|vào lúc|vào|ngày|là|để|thì|và|việc|rằng|về|luc|vao luc|vao|la|de|thi|va|viec)"
    r"(?=\s|$)\s*|[,.:;!?\-]\s*)+"
)
_EDGE_TAIL_RE = re.compile(
    r"(?:(?:^|\s)(?:nhé|nhá|nha|nhe|ạ|với|giúp|giùm|dùm|được không|lúc|vào lúc|vào|"
    r"nhé em|nha em|em nhé|em nha|giúp em|giúp anh|voi|giup|gium|dum|duoc khong|luc|vao)"
    r"|\s*[,.:;!?\-])+$"
)


def _fold_char(ch: str) -> str:
    low = ch.lower()
    if len(low) != 1:
        low = ch
    if low == "đ":
        return "d"
    return unicodedata.normalize("NFD", low)[0]


def fold_keep(text: str) -> str:
    """Bỏ dấu nhưng giữ nguyên độ dài (vị trí khớp map thẳng về câu gốc)."""
    return "".join(_fold_char(c) for c in text)


class _Scan:
    """Câu đã chuẩn hoá: ``orig`` (NFC, gộp khoảng trắng), ``low`` (chữ thường)
    và ``f`` (bỏ dấu) dài bằng nhau; ``work`` là f với các cụm đã nhận che bằng
    khoảng trắng để regex sau không khớp lại."""

    def __init__(self, text: str) -> None:
        self.orig = re.sub(r"\s+", " ", nfc(text or "")).strip()
        self.low = "".join(c.lower() if len(c.lower()) == 1 else c for c in self.orig)
        self.f = "".join(_fold_char(c) for c in self.orig)
        self.work = self.f
        self.has_dia = self.f != self.low
        self.spans: list[tuple[int, int]] = []

    def mask(self, start: int, end: int) -> None:
        if end <= start:
            return
        self.work = self.work[:start] + " " * (end - start) + self.work[end:]
        self.spans.append((start, end))

    def accented(self, start: int, end: int, *allowed: str) -> bool:
        """Câu có dấu thì chữ gốc ở [start, end) phải đúng 1 dạng cho phép."""
        return not self.has_dia or self.low[start:end] in allowed


@dataclass(frozen=True)
class When:
    due_at: dt.datetime | None = None
    has_date: bool = False
    has_time: bool = False
    approx_time: bool = False  # chỉ có buổi -> giờ mặc định của buổi
    recurrence: dict | None = None  # None = nhắc 1 lần
    error: str = ""  # tìm thấy thời điểm nhưng không dùng được (đã qua, thiếu giờ, quá xa)
    spans: tuple[tuple[int, int], ...] = ()

    @property
    def found(self) -> bool:
        return self.due_at is not None or bool(self.error)


def _num(raw: str, scan: _Scan, start: int) -> float | None:
    raw = raw.strip()
    if raw[0].isdigit():
        return float(raw.replace(",", "."))
    if raw in _NUMBER_ACCENTED and not scan.accented(
        start, start + len(raw), _NUMBER_ACCENTED[raw]
    ):
        return None  # "mốt" (ngày kia), "nam" (miền Nam)...
    return _NUMBER_WORDS.get(raw)


def _duration(scan: _Scan, *, require_marker: bool = True) -> dt.timedelta | None:
    for m in _DURATION_RE.finditer(scan.work):
        unit = m.group("u")
        if require_marker and not (
            m.group("post") or (m.group("pre") == "sau" and unit not in ("gio", "h"))
        ):
            continue
        if m.group("n") == "nua" and not scan.accented(m.start("n"), m.end("n"), "nửa"):
            continue
        value = _num(m.group("n"), scan, m.start("n"))
        if value is None:
            continue
        if m.group("ruoi"):
            value += 0.5
        minutes = {"phut": 1, "p": 1, "tieng": 60, "gio": 60, "h": 60, "ngay": 1440, "tuan": 10080}
        scan.mask(m.start(), m.end())
        return dt.timedelta(minutes=value * minutes[unit])
    return None


def _time_of_day(scan: _Scan) -> tuple[int, int, re.Match] | None:
    for m in _TIME_RE.finditer(scan.work):
        if m.group("sep") == ":" and m.group("m") is None:
            continue
        hour, minute = int(m.group("h")), int(m.group("m") or 0)
        if m.group("ruoi"):
            minute = 30
        if m.group("kem"):
            hour, minute = hour - 1, 60 - int(m.group("kem"))
        if not (0 <= hour <= 24 and 0 <= minute <= 59):
            continue
        scan.mask(m.start(), m.end())
        return hour % 24, minute, m
    return None


def _periods(scan: _Scan, time_match: re.Match | None) -> str | None:
    """Buổi gắn với giờ/ngày: chỉ nhận chữ đứng sát giờ, sát chữ ngày (nay, mai,
    thứ...) hoặc sau "mỗi" - "chiều vợ", "sang nhà mẹ" không phải buổi."""
    best: tuple[int, str, int, int] | None = None
    for m in _PERIOD_RE.finditer(scan.work):
        word = m.group(1)
        if not scan.accented(m.start(), m.end(), _PERIOD_ACCENTED[word]):
            continue
        before = scan.work[: m.start()].rstrip()
        after = scan.work[m.end() :].lstrip()
        near_time = time_match is not None and (
            0 <= m.start() - time_match.end() <= 1 or 0 <= time_match.start() - m.end() <= 1
        )
        near_day = bool(
            re.match(
                r"(?:nay|mai|mot|ngay|hom|thu |chu nhat|cn\b|cuoi tuan|mung|mong|ram|\d)", after
            )
        ) or before.endswith(("moi", "hang"))
        # Chữ ngày đã che (vd "mai") vẫn tính: nhìn vào bản bỏ dấu gốc.
        near_day = near_day or bool(
            re.match(r"\s*(?:nay|mai|mot|ngay|hom|thu |chu nhat|cn\b)", scan.f[m.end() :])
        )
        if not (near_time or near_day):
            continue
        if word == "toi" and not scan.has_dia and before.endswith(("nhac", "giup", "cho", "voi")):
            continue  # "nhac toi 8h": "tôi" viết không dấu, không phải buổi tối
        distance = abs(m.start() - time_match.start()) if time_match else 0
        if best is None or distance < best[0]:
            best = (distance, word, m.start(), m.end())
    if best is None:
        return None
    scan.mask(best[2], best[3])
    return best[1]


def _apply_period(hour: int, period: str | None) -> tuple[int, bool]:
    """(giờ 24h, còn mơ hồ sáng/chiều không)."""
    if period is None:
        return hour, 1 <= hour <= 11
    if period in ("chieu", "toi") and hour < 12:
        return hour + 12, False
    if period == "trua" and hour < 11:
        return hour + 12, False
    if period in ("dem", "khuya") and 7 <= hour < 12:
        return hour + 12, False
    if period == "sang" and hour == 12:
        return 0, False
    return hour, False


def _month_value(raw: str | None) -> int | None:
    if raw is None:
        return None
    return int(raw) if raw.isdigit() else _MONTH_WORD.get(raw)


def _lunar_dates(scan: _Scan) -> list[tuple[int, int | None, bool, int | None]]:
    """(ngày âm, tháng âm|None, nhuận, năm|None) theo thứ tự xuất hiện."""
    out: list[tuple[int, int | None, bool, int | None]] = []
    for pattern in (_LUNAR_C, _LUNAR_B):
        for m in pattern.finditer(scan.work):
            am = re.search(r"(?:am lich|am|al)$", scan.work[m.start() : m.end()])
            if am and not scan.accented(
                m.start() + am.start(), m.start() + am.end(), "âm lịch", "âm", "al"
            ):
                continue
            month = _month_value(m.group("m"))
            year = int(m.group("y")) if "y" in m.groupdict() and m.group("y") else None
            if month is None:
                continue
            out.append((int(m.group("d")), month, bool(m.group("leap")), year))
            scan.mask(m.start(), m.end())
    for m in _LUNAR_A.finditer(scan.work):
        head = scan.work[m.start() : m.end()]
        word = re.match(r"ram|mung|mong", head)
        if word and not scan.accented(
            m.start(), m.start() + word.end(), "rằm", "mùng", "mồng", "mùng"
        ):
            continue
        month = _month_value(m.group("m"))
        if m.group("tet"):
            month = 1
        leap = bool(m.group("leap"))
        days = [15 if m.group("ram") else int(m.group("d"))]
        for extra in re.findall(r"ram|(?:mung|mong) (\d{1,2})", m.group("extra") or ""):
            days.append(int(extra) if extra else 15)
        out.extend((day, month, leap, None) for day in days)
        scan.mask(m.start(), m.end())
    return out


def _solar_dates(scan: _Scan, today: dt.date) -> list[tuple[dt.date, bool]]:
    """(ngày, có ghi năm) - dùng chung regex /lich (calendar_query)."""
    out: list[tuple[dt.date, bool]] = []
    for m in calendar_query.SOLAR_DATE_RE.finditer(scan.work):
        dates = calendar_query.solar_dates(scan.work[m.start() : m.end()], today)
        if not dates:
            continue
        explicit_year = bool(m.group("y"))
        day = dates[0]
        if not explicit_year and day < today:
            try:
                day = day.replace(year=day.year + 1)
            except ValueError:
                continue
        out.append((day, explicit_year))
        scan.mask(m.start(), m.end())
    return out


def _weekdays(scan: _Scan, today: dt.date) -> tuple[list[int], dt.date | None, bool, bool]:
    """(các thứ nhắc tới, ngày của thứ đầu tiên, có "mỗi/các", có "tuần ...")."""
    days: list[int] = []
    first: dt.date | None = None
    every = shifted = False
    for m in _WEEKDAY_RE.finditer(scan.work):
        thu = re.search(r"thu ", scan.work[m.start() : m.end()])
        if thu and not scan.accented(m.start() + thu.start(), m.start() + thu.end() - 1, "thứ"):
            continue
        if m.group("cn"):
            found = [6]
        else:
            found = [
                _WEEKDAY[m.group("w")] if not m.group("w").isdigit() else int(m.group("w")) - 2
            ]
            for raw in re.findall(
                r"[2-7]|hai|ba|tu|nam|sau|bay|chu nhat|cn", m.group("more") or ""
            ):
                if raw in ("chu nhat", "cn"):
                    found.append(6)
                else:
                    found.append(int(raw) - 2 if raw.isdigit() else _WEEKDAY[raw])
        every = every or bool(m.group("moi") or m.group("moi2"))
        shifted = shifted or bool(m.group("shift"))
        if first is None:
            folded = scan.work[m.start() : m.end()].strip()
            rel = calendar_query.relative_day(re.sub(r"\s+", " ", folded), today)
            first = rel[0] if rel else None
        days.extend(d for d in found if d not in days)
        scan.mask(m.start(), m.end())
    return days, first, every, shifted


def _relative(scan: _Scan, today: dt.date) -> dt.date | None:
    m = _TODAY_RE.search(scan.work)
    if m and (
        m.group("p") is None
        or scan.accented(m.start("p"), m.end("p"), "sáng", "trưa", "chiều", "tối", "đêm")
    ):
        start = m.start("p") if m.group("p") else m.start()
        scan.mask(m.end() - 3 if m.group("p") else start, m.end())
        return today
    for m in _TOMORROW_RE.finditer(scan.work):
        word_start = m.end() - 3
        if not scan.accented(word_start, m.end(), "mai"):
            continue  # "mãi", "mái"
        if scan.orig[word_start : m.end()] == "Mai" and word_start > 0:
            continue  # tên người giữa câu
        scan.mask(m.start(), m.end())
        return today + dt.timedelta(days=1)
    for m in _AFTER_TOMORROW_RE.finditer(scan.work):
        if m.group(0) == "mot" and not scan.accented(m.start(), m.end(), "mốt"):
            continue
        scan.mask(m.start(), m.end())
        return today + dt.timedelta(days=2)
    m = _NEXT_MONDAY_RE.search(scan.work)
    if m:
        scan.mask(m.start(), m.end())
        return today + dt.timedelta(days=7 - today.weekday())
    m = _WEEKEND_RE.search(scan.work)
    if m:
        scan.mask(m.start(), m.end())
        saturday = today + dt.timedelta(days=(5 - today.weekday()) % 7)
        if m.group("shift") in ("sau", "toi"):
            saturday += dt.timedelta(days=7)
        return saturday
    return None


def _recurrence_words(scan: _Scan) -> tuple[str | None, str | None]:
    """(đơn vị lặp "ngay|tuan|thang|nam" | None, buổi trong "mỗi sáng")."""
    unit = period = None
    for m in _RECUR_RE.finditer(scan.work):
        unit = unit or m.group("unit") or m.group("unit2")
        if m.group("period"):
            unit = unit or "ngay"
            period = m.group("period")
        scan.mask(m.start(), m.end())
    return unit, period


def _next_lunar(day: int, month: int | None, leap: bool, year: int | None, today: dt.date):
    from services import lunar

    if month is not None:
        found, _, _ = lunar.find_lunar_date(day, month, year, leap, today)
        return found
    for offset in range(62):
        candidate = today + dt.timedelta(days=offset)
        if lunar.solar_to_lunar(candidate.day, candidate.month, candidate.year)[0] == day:
            return candidate
    return None


def parse(text: str, now: dt.datetime | None = None) -> When:
    """Thời điểm (và quy tắc lặp) trong câu; When() rỗng nếu không thấy gì."""
    now = (now or dt.datetime.now(VN_TZ)).astimezone(VN_TZ)
    today = now.date()
    scan = _Scan(text)
    if not scan.f:
        return When()

    unit, every_period = _recurrence_words(scan)
    delta = _duration(scan)
    lunar_dates = _lunar_dates(scan)
    solar = _solar_dates(scan, today)
    weekdays, weekday_date, weekday_every, weekday_shifted = _weekdays(scan, today)
    relative = _relative(scan, today)
    dom_match = _DAY_OF_MONTH_RE.search(scan.work)
    dom = None
    if dom_match and 1 <= int(dom_match.group("d")) <= 31:
        dom = int(dom_match.group("d"))
        scan.mask(dom_match.start(), dom_match.end())
    tod = _time_of_day(scan)
    period = _periods(scan, tod[2] if tod else None) or every_period

    hour = minute = None
    ambiguous = approx = False
    if tod:
        hour, ambiguous = _apply_period(tod[0], period)
        minute = tod[1]
    elif period:
        hour, minute, approx = PERIOD_HOUR[period], 0, True
    has_time = hour is not None
    spans = tuple(scan.spans)

    def done(**kwargs) -> When:
        return When(spans=spans, has_time=has_time, approx_time=approx, **kwargs)

    # ── Lặp lại ───────────────────────────────────────────────────────────
    rule: dict | None = None
    many_lunar_days = len(lunar_dates) >= 2 and all(m is None for _, m, _, _ in lunar_dates)
    if unit or weekday_every or many_lunar_days:
        if unit == "ngay" or (unit is None and every_period):
            rule = {"type": "daily"}
        elif unit == "tuan" or weekday_every:
            base_day = weekday_date or relative or today
            rule = {"type": "weekly", "days": weekdays or [base_day.weekday()]}
        elif unit == "thang" or many_lunar_days:
            if lunar_dates:
                rule = {"type": "lunar_monthly", "days": sorted({d for d, _, _, _ in lunar_dates})}
            else:
                day = dom or (solar[0][0].day if solar else (relative or today).day)
                rule = {"type": "monthly", "day": day}
        elif unit == "nam":
            if lunar_dates and lunar_dates[0][1] is not None:
                d, m, leap, _ = lunar_dates[0]
                rule = {"type": "lunar_yearly", "month": m, "day": d}
                rule["leap"] = "leap" if leap else "regular"
            elif solar:
                rule = {"type": "yearly", "month": solar[0][0].month, "day": solar[0][0].day}
            else:
                base_day = relative or today
                rule = {"type": "yearly", "month": base_day.month, "day": base_day.day}
    if rule is not None:
        if not has_time:
            return done(
                has_date=True, recurrence=rule, error="Nhắc lặp lại cần giờ cụ thể (vd 7h sáng)."
            )
        rule["at"] = f"{hour:02d}:{minute:02d}"
        try:
            rule = recurrence.normalize(rule)
            due = recurrence.next_occurrence(rule, now)
        except recurrence.RecurrenceError as exc:
            return done(has_date=True, recurrence=rule, error=str(exc))
        if due is None:
            return done(has_date=True, recurrence=rule, error="Không tìm được lần nhắc kế tiếp.")
        return done(due_at=due, has_date=True, recurrence=rule)

    # ── Một lần ───────────────────────────────────────────────────────────
    if delta is not None and not tod:
        due = now + delta
        if delta < dt.timedelta(minutes=1):
            return done(error="Khoảng thời gian quá ngắn (tối thiểu 1 phút).")
        if delta > MAX_AHEAD:
            return done(error="Chỉ đặt nhắc trong vòng 1 năm tới.")
        return When(due_at=due, has_time=True, spans=spans)

    date: dt.date | None = None
    rolling_weekday = False
    if lunar_dates:
        d, m, leap, year = lunar_dates[0]
        date = _next_lunar(d, m, leap, year, today)
        if date is None:
            return done(has_date=True, error="Ngày âm lịch này không tồn tại.")
    elif solar:
        date = solar[0][0]
    elif weekday_date is not None:
        date = weekday_date
        rolling_weekday = not weekday_shifted
    elif relative is not None:
        date = relative
    elif dom is not None:
        candidate = None
        for shift in range(3):
            month_index = today.year * 12 + today.month - 1 + shift
            try:
                candidate = dt.date(month_index // 12, month_index % 12 + 1, dom)
            except ValueError:
                continue
            if candidate >= today:
                break
        date = candidate
    has_date = date is not None

    if not has_time:
        if has_date:
            return done(has_date=True, error="Cần giờ nhắc cụ thể (vd 8h sáng).")
        return When()
    assert hour is not None and minute is not None

    def at(day: dt.date, h: int) -> dt.datetime:
        return dt.datetime.combine(day, dt.time(h, minute), tzinfo=VN_TZ)

    if has_date:
        assert date is not None
        base = hour + 12 if ambiguous and hour < 7 else hour
        due = at(date, base)
        if due <= now + MIN_AHEAD and rolling_weekday:
            due += dt.timedelta(days=7)
    else:
        candidates = [hour + 12 if ambiguous and hour < 7 else hour]
        if ambiguous and candidates[0] < 12:
            candidates.append(candidates[0] + 12)
        due = None
        for h in candidates:
            if at(today, h) > now + MIN_AHEAD:
                due = at(today, h)
                break
        if due is None:  # giờ hôm nay đã qua, không nói ngày -> ngày mai
            due = at(today + dt.timedelta(days=1), candidates[0])
    if due <= now + MIN_AHEAD:
        return done(has_date=has_date, error="Thời điểm này đã qua rồi.")
    if due - now > MAX_AHEAD:
        return done(has_date=has_date, error="Chỉ đặt nhắc trong vòng 1 năm tới.")
    return done(due_at=due, has_date=has_date)


def parse_snooze(
    text: str, now: dt.datetime | None = None, *, base: dt.datetime | None = None
) -> dt.datetime | None:
    """Thời điểm hoãn: "10p", "10 phút", "1 tiếng", "30" (phút), "sáng mai",
    "tới 8h tối". Trống = 10 phút. None nếu không hiểu. Khoảng thời gian cộng vào
    ``base`` (mặc định bây giờ - vd giờ hẹn cũ khi dời 1 nhắc chưa tới giờ); giờ
    tuyệt đối ("sáng mai") luôn tính theo bây giờ."""
    now = (now or dt.datetime.now(VN_TZ)).astimezone(VN_TZ)
    start = (base or now).astimezone(VN_TZ)
    raw = re.sub(
        r"^(?:toi|den|luc|thanh)\s+", "", "".join(_fold_char(c) for c in nfc(text).strip())
    )
    if not raw:
        return start + dt.timedelta(minutes=10)
    if re.fullmatch(r"\d{1,4}", raw):
        minutes = int(raw)
        return start + dt.timedelta(minutes=minutes) if 1 <= minutes <= 7 * 1440 else None
    scan = _Scan(text)
    delta = _duration(scan, require_marker=False)
    if delta is not None and not scan.work.strip(" ,.") and delta >= dt.timedelta(minutes=1):
        return start + delta if delta <= dt.timedelta(days=7) else None
    when = parse(text, now)
    return when.due_at if when.due_at is not None and when.recurrence is None else None


def strip_spans(text: str, spans: tuple[tuple[int, int], ...]) -> str:
    """Bỏ các cụm thời gian khỏi câu (vị trí theo câu đã gộp khoảng trắng)."""
    orig = re.sub(r"\s+", " ", nfc(text or "")).strip()
    keep = [True] * len(orig)
    for start, end in spans:
        for i in range(max(0, start), min(len(orig), end)):
            keep[i] = False
    return "".join(ch if keep[i] else " " for i, ch in enumerate(orig))


def _trim_connectors(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    while text:
        low = "".join(c.lower() if len(c.lower()) == 1 else c for c in text)
        head = _EDGE_HEAD_RE.match(low)
        tail = _EDGE_TAIL_RE.search(low)
        start = head.end() if head else 0
        end = tail.start() if tail else len(text)
        if start == 0 and end == len(text):
            break
        text = text[start : max(start, end)].strip()
    return text


def extract_reminder(text: str, now: dt.datetime | None = None) -> tuple[str, When] | None:
    """(nội dung nhắc, thời điểm) từ câu "nhắc anh 8h tối mai gọi mẹ" - dùng cho
    /nhac <thời gian> <nội dung> và làm đường dự phòng khi router AI không chạy.
    None nếu không thấy thời điểm nào; nội dung có thể rỗng ("hẹn giờ 10 phút nữa")."""
    when = parse(text, now)
    if not when.found:
        return None
    scan = _Scan(text)
    spans = list(when.spans)
    for m in _INTENT_RE.finditer(scan.f):
        verb = scan.low[m.start() : m.end()]
        if not scan.has_dia or re.search(r"nhắc|hẹn|đặt|remind", verb):
            spans.append((m.start(), m.end()))
    message = _trim_connectors(strip_spans(text, tuple(spans)))
    return message[:1].upper() + message[1:], when


@dataclass(frozen=True)
class CalendarDate:
    """1 ngày trong năm (không giờ) cho ngày nhớ: 12/3 âm, 20/11, 5/6/2015."""

    day: int
    month: int
    year: int | None
    lunar: bool
    leap: bool
    spans: tuple[tuple[int, int], ...] = ()


def parse_calendar_date(text: str) -> CalendarDate | None:
    """Ngày-tháng (dương hoặc âm) đầu tiên trong câu, None nếu không có/thiếu tháng."""
    scan = _Scan(text)
    lunar_dates = _lunar_dates(scan)
    for day, month, leap, year in lunar_dates:
        if month is not None:
            return CalendarDate(day, month, year, True, leap, tuple(scan.spans))
    if lunar_dates:
        return None  # "mùng 1", "rằm" không kèm tháng: không phải 1 ngày trong năm
    for m in calendar_query.SOLAR_DATE_RE.finditer(scan.work):
        day, month = int(m.group("d")), int(m.group("m"))
        year = int(m.group("y")) if m.group("y") else None
        try:
            dt.date(year or 2000, month, day)  # 29/2 hợp lệ (năm 2000 nhuận)
        except ValueError:
            continue
        scan.mask(m.start(), m.end())
        return CalendarDate(day, month, year, False, False, tuple(scan.spans))
    return None


def find_time_of_day(
    text: str, exclude: tuple[tuple[int, int], ...] = ()
) -> tuple[int, int, tuple[tuple[int, int], ...]] | None:
    """(giờ, phút, các vị trí) của giờ trong ngày đầu tiên ("lúc 7h tối" -> 19:00).
    ``exclude``: vị trí đã dùng cho việc khác (ngày-tháng...), không xét lại."""
    scan = _Scan(text)
    for start, end in exclude:
        scan.mask(start, end)
    scan.spans.clear()
    tod = _time_of_day(scan)
    if tod is None:
        return None
    period = _periods(scan, tod[2])
    hour, _ = _apply_period(tod[0], period)
    return hour, tod[1], tuple(scan.spans)


def describe_due(due: dt.datetime, now: dt.datetime | None = None) -> str:
    """ "20:00 Thứ Bảy 25/10" (kèm năm nếu khác năm nay)."""
    from services import lunar

    now = (now or dt.datetime.now(VN_TZ)).astimezone(VN_TZ)
    due = due.astimezone(VN_TZ)
    year = f"/{due.year}" if due.year != now.year else ""
    return f"{due:%H:%M} {lunar.WEEKDAYS[due.weekday()]} {due:%d/%m}{year}"
