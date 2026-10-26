"""Quy tắc lặp của nhắc việc v2 và ngày nhớ: tính lần kế tiếp bằng code.

Quy tắc là 1 dict JSON (cột reminder_series.recurrence, migrations/019)::

    {"type": "daily", "at": "07:00"}
    {"type": "weekly", "days": [0, 2, 4], "at": "08:00"}          # 0 = Thứ Hai
    {"type": "monthly", "day": 31, "at": "09:00"}                 # tháng thiếu -> ngày cuối
    {"type": "lunar_monthly", "days": [1, 15], "at": "06:30"}     # mùng 1 và rằm
    {"type": "yearly", "month": 11, "day": 20, "at": "08:00"}     # 29/2 -> 28/2 năm thường
    {"type": "lunar_yearly", "month": 3, "day": 12, "leap": "regular",
     "day30": "last", "at": "08:00"}

Chính sách âm lịch (giỗ, sinh nhật âm...):
- ``leap``: "regular" (mặc định) - luôn theo tháng THƯỜNG, kể cả người mất vào
  tháng nhuận (tục phổ biến: giỗ làm vào tháng chính); "leap" - năm nào có
  đúng tháng nhuận đó thì theo tháng nhuận, năm khác theo tháng thường.
- ``day30``: "last" (mặc định) - tháng thiếu (29 ngày) không có ngày 30 thì
  làm ngày 29; "skip" - bỏ qua tháng/năm đó.
Tháng nhuận cũng là 1 tháng âm: "mùng 1 hằng tháng" nhắc cả mùng 1 tháng nhuận.

Khoảng cách tối thiểu giữa 2 lần nhắc là 1 ngày theo cấu tạo (không có kiểu
lặp theo giờ/phút) - thoả trần "tối thiểu 1 giờ" của nhắc việc lặp.
"""

from __future__ import annotations

import calendar
import datetime as dt
from zoneinfo import ZoneInfo

from services import lunar

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TYPES = ("daily", "weekly", "monthly", "lunar_monthly", "yearly", "lunar_yearly")
_MIN_YEAR, _MAX_YEAR = 1900, 2099
_WEEKDAY_SHORT = ("T2", "T3", "T4", "T5", "T6", "T7", "CN")


class RecurrenceError(ValueError):
    """Quy tắc lặp không hợp lệ (câu báo tiếng Việt, đưa thẳng cho người dùng)."""


def _parse_at(raw) -> dt.time:
    try:
        hour, minute = (int(x) for x in str(raw).split(":", 1))
        return dt.time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise RecurrenceError("Giờ nhắc không hợp lệ (cần HH:MM).") from exc


def _int_in(value, low: int, high: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RecurrenceError(f"{label} không hợp lệ.") from exc
    if not low <= number <= high:
        raise RecurrenceError(f"{label} phải trong khoảng {low}-{high}.")
    return number


def normalize(rule: dict) -> dict:
    """Kiểm tra + chuẩn hoá quy tắc (raise RecurrenceError nếu sai)."""
    if not isinstance(rule, dict):
        raise RecurrenceError("Quy tắc lặp không hợp lệ.")
    kind = rule.get("type")
    if kind not in TYPES:
        raise RecurrenceError("Kiểu lặp chưa hỗ trợ.")
    at = _parse_at(rule.get("at", ""))
    out: dict = {"type": kind, "at": f"{at:%H:%M}"}
    if kind == "weekly":
        days = sorted({_int_in(d, 0, 6, "Thứ trong tuần") for d in rule.get("days") or []})
        if not days:
            raise RecurrenceError("Lặp hằng tuần cần ít nhất 1 thứ.")
        out["days"] = days
    elif kind == "monthly":
        out["day"] = _int_in(rule.get("day"), 1, 31, "Ngày trong tháng")
    elif kind == "lunar_monthly":
        days = sorted({_int_in(d, 1, 30, "Ngày âm lịch") for d in rule.get("days") or []})
        if not days:
            raise RecurrenceError("Lặp hằng tháng âm lịch cần ít nhất 1 ngày.")
        out["days"] = days
        out["day30"] = "skip" if rule.get("day30") == "skip" else "last"
    elif kind in ("yearly", "lunar_yearly"):
        month = _int_in(rule.get("month"), 1, 12, "Tháng")
        day = _int_in(rule.get("day"), 1, 30 if kind == "lunar_yearly" else 31, "Ngày")
        if kind == "yearly":
            # 31/4, 30/2... không bao giờ tồn tại (29/2 thì có, năm nhuận).
            if day > (29 if month == 2 else calendar.monthrange(2001, month)[1]):
                raise RecurrenceError(f"Không có ngày {day}/{month}.")
        out.update(month=month, day=day)
        if kind == "lunar_yearly":
            out["leap"] = "leap" if rule.get("leap") == "leap" else "regular"
            out["day30"] = "skip" if rule.get("day30") == "skip" else "last"
    year = rule.get("year")
    if year not in (None, ""):
        out["year"] = _int_in(year, _MIN_YEAR, _MAX_YEAR, "Năm")
    return out


def _at(day: dt.date, at: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, at, tzinfo=VN_TZ)


def _lunar_day_exists(ld: int, lm: int, ly: int, leap: bool) -> bool:
    solar = lunar.lunar_to_solar(ld, lm, ly, leap)
    if solar is None:
        return False
    day = dt.date(solar[2], solar[1], solar[0])
    return lunar.solar_to_lunar(day.day, day.month, day.year) == (ld, lm, ly, leap)


def lunar_date_in_year(
    ld: int, lm: int, ly: int, *, leap: str = "regular", day30: str = "last"
) -> dt.date | None:
    """Ngày dương của ngày âm ld/lm trong năm âm ly theo chính sách nhuận/ngày 30."""
    if not _MIN_YEAR <= ly <= _MAX_YEAR:
        return None
    use_leap = leap == "leap" and lunar.leap_month_of(ly) == lm
    day = ld
    if ld == 30 and not _lunar_day_exists(30, lm, ly, use_leap):
        if day30 == "skip":
            return None
        day = 29
    solar = lunar.lunar_to_solar(day, lm, ly, use_leap)
    if solar is None:
        return None
    return dt.date(solar[2], solar[1], solar[0])


def _next_daily(at: dt.time, after: dt.datetime) -> dt.datetime:
    candidate = _at(after.date(), at)
    return candidate if candidate > after else _at(after.date() + dt.timedelta(days=1), at)


def _next_weekly(days: list[int], at: dt.time, after: dt.datetime) -> dt.datetime:
    for offset in range(8):
        day = after.date() + dt.timedelta(days=offset)
        if day.weekday() in days and _at(day, at) > after:
            return _at(day, at)
    raise RecurrenceError("Không tìm được lần lặp hằng tuần kế tiếp.")


def _next_monthly(dom: int, at: dt.time, after: dt.datetime) -> dt.datetime:
    year, month = after.year, after.month
    for _ in range(3):
        last = calendar.monthrange(year, month)[1]
        candidate = _at(dt.date(year, month, min(dom, last)), at)
        if candidate > after:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    raise RecurrenceError("Không tìm được lần lặp hằng tháng kế tiếp.")


def _next_lunar_monthly(rule: dict, at: dt.time, after: dt.datetime) -> dt.datetime | None:
    days = set(rule["days"])
    for offset in range(62):
        day = after.date() + dt.timedelta(days=offset)
        ld, lm, ly, leap = lunar.solar_to_lunar(day.day, day.month, day.year)
        hit = ld in days
        if not hit and ld == 29 and 30 in days and rule.get("day30") != "skip":
            hit = not _lunar_day_exists(30, lm, ly, leap)  # tháng thiếu: ngày 30 -> 29
        if hit and _at(day, at) > after:
            return _at(day, at)
    return None


def _next_yearly(month: int, dom: int, at: dt.time, after: dt.datetime) -> dt.datetime:
    for year in range(after.year, after.year + 3):
        last = calendar.monthrange(year, month)[1]
        candidate = _at(dt.date(year, month, min(dom, last)), at)
        if candidate > after:
            return candidate
    raise RecurrenceError("Không tìm được lần lặp hằng năm kế tiếp.")


def _next_lunar_yearly(rule: dict, at: dt.time, after: dt.datetime) -> dt.datetime | None:
    base = after.date()
    ly0 = lunar.solar_to_lunar(base.day, base.month, base.year)[2]
    for ly in range(ly0, ly0 + 3):
        day = lunar_date_in_year(
            rule["day"], rule["month"], ly, leap=rule.get("leap", "regular"), day30=rule["day30"]
        )
        if day is not None and _at(day, at) > after:
            return _at(day, at)
    return None


def next_occurrence(rule: dict, after: dt.datetime) -> dt.datetime | None:
    """Lần nhắc đầu tiên SAU ``after`` (giờ VN); None nếu không còn lần nào
    (vd ngày 30 âm "skip" mãi, hoặc vượt năm 2099)."""
    rule = normalize(rule)
    after = after.astimezone(VN_TZ)
    at = _parse_at(rule["at"])
    kind = rule["type"]
    if kind == "daily":
        return _next_daily(at, after)
    if kind == "weekly":
        return _next_weekly(rule["days"], at, after)
    if kind == "monthly":
        return _next_monthly(rule["day"], at, after)
    if kind == "lunar_monthly":
        return _next_lunar_monthly(rule, at, after)
    if kind == "yearly":
        return _next_yearly(rule["month"], rule["day"], at, after)
    return _next_lunar_yearly(rule, at, after)


def describe(rule: dict) -> str:
    """Mô tả ngắn: "07:00 hằng ngày", "08:00 T2, T4", "06:30 mùng 1, rằm âm lịch"..."""
    try:
        rule = normalize(rule)
    except RecurrenceError:
        return "lặp (quy tắc lỗi)"
    kind, at = rule["type"], rule["at"]
    if kind == "daily":
        return f"{at} hằng ngày"
    if kind == "weekly":
        if rule["days"] == list(range(7)):
            return f"{at} hằng ngày"
        return f"{at} hằng tuần ({', '.join(_WEEKDAY_SHORT[d] for d in rule['days'])})"
    if kind == "monthly":
        return f"{at} ngày {rule['day']} hằng tháng"
    if kind == "lunar_monthly":
        names = [
            "rằm" if d == 15 else f"mùng {d}" if d <= 10 else f"ngày {d}" for d in rule["days"]
        ]
        return f"{at} {', '.join(names)} hằng tháng âm lịch"
    if kind == "yearly":
        return f"{at} ngày {rule['day']:02d}/{rule['month']:02d} hằng năm"
    leap = " (theo tháng nhuận nếu có)" if rule.get("leap") == "leap" else ""
    return f"{at} ngày {rule['day']}/{rule['month']} âm lịch hằng năm{leap}"


def date_label(rule: dict) -> str:
    """Nhãn ngày (không giờ) cho ngày nhớ: "12/3 âm lịch", "20/11"."""
    try:
        rule = normalize(rule)
    except RecurrenceError:
        return ""
    if rule["type"] == "lunar_yearly":
        return f"{rule['day']}/{rule['month']} âm lịch"
    if rule["type"] == "yearly":
        return f"{rule['day']:02d}/{rule['month']:02d}"
    return describe(rule)
