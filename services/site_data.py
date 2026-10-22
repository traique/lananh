"""Dữ liệu cho web công khai (trang /lich, /tu-vi, /than-so-hoc và /api/v1/*).

Hàm thuần, không gọi AI, không đụng DB. Kết quả là dict khoá camelCase (hợp
đồng JSON công khai của /api/v1 - chỉ THÊM trường mới, không đổi/bỏ trường
cũ). Lỗi đầu vào raise ``InputError`` (code máy đọc + câu tiếng Việt); tầng web
đổi thành HTTP 422 với cùng một dạng {"error": {code, message}}.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from services import calendar_query, can_chi, lunar, numerology, tuvi

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MIN_YEAR, MAX_YEAR = 1900, 2100
GENDERS = ("male", "female")
HOUR_BRANCHES = [
    (0, "Tý", "0h-1h"), (1, "Sửu", "1h-3h"), (2, "Dần", "3h-5h"), (3, "Mão", "5h-7h"),
    (4, "Thìn", "7h-9h"), (5, "Tỵ", "9h-11h"), (6, "Ngọ", "11h-13h"), (7, "Mùi", "13h-15h"),
    (8, "Thân", "15h-17h"), (9, "Dậu", "17h-19h"), (10, "Tuất", "19h-21h"), (11, "Hợi", "21h-23h"),
]  # fmt: skip
# Ô chọn giờ trên web: thêm "Tý đầu" (23h-24h, an theo ngày hôm sau) với mã riêng.
EARLY_TY = 12
HOUR_OPTIONS = [*HOUR_BRANCHES, (EARLY_TY, "Tý đầu", "23h-24h, an theo ngày hôm sau")]


class InputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def today() -> dt.date:
    return dt.datetime.now(_VN_TZ).date()


def _check_year(year: int) -> None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise InputError("YEAR_OUT_OF_RANGE", f"Năm cần trong khoảng {MIN_YEAR}-{MAX_YEAR}.")


_YMD_RE = re.compile(
    r"\s*(?:(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})|(?P<d2>\d{1,2})/(?P<m2>\d{1,2})/(?P<y2>\d{4}))\s*"
)


def parse_date(raw: str) -> dt.date:
    """'YYYY-MM-DD' (input type=date, không cần đệm 0) hoặc 'DD/MM/YYYY' -> ngày
    dương hợp lệ trong 1900-2100."""
    year, month, day = parse_ymd(raw)
    try:
        value = dt.date(year, month, day)
    except ValueError:
        raise InputError("INVALID_DATE", "Ngày không tồn tại trên lịch dương.") from None
    _check_year(value.year)
    return value


def day_info(day: dt.date) -> dict:
    _check_year(day.year)
    info = lunar.date_info(day)
    god, is_good = can_chi.day_god(day)
    ld = info["lunar"]["day"]
    return {
        "date": info["date"],
        "weekday": info["weekday"],  # tên thứ ("Thứ Tư")
        "weekdayIndex": day.weekday(),  # 0 = Thứ Hai
        "lunar": {
            "day": ld,
            "month": info["lunar"]["month"],
            "year": info["lunar"]["year"],
            "isLeapMonth": info["lunar"]["leap_month"],
            "text": info["lunar"]["text"],
            "dayCanChi": info["lunar"]["day_can_chi"],
            "monthCanChi": info["lunar"]["month_can_chi"],
            "yearCanChi": info["lunar"]["year_can_chi"],
        },
        "solarTerm": info["solar_term"],
        "luckyHours": info["lucky_hours"],
        "dayGod": god,
        "isHoangDao": is_good,
        "isTamNuong": ld in can_chi.TAM_NUONG,
        "isNguyetKy": ld in can_chi.NGUYET_KY,
        "holidays": calendar_query.holidays_on(day),
        "nextTet": info["next_tet"],
        "daysToTet": info["days_to_tet"],
    }


def month_grid(year: int, month: int) -> dict:
    _check_year(year)
    if not 1 <= month <= 12:
        raise InputError("INVALID_MONTH", "Tháng cần từ 1 đến 12.")
    days = []
    for day in calendar_query.month_days(year, month):
        ld, lm, _, leap = lunar.solar_to_lunar(day.day, day.month, day.year)
        _, is_good = can_chi.day_god(day)
        days.append(
            {
                "date": day.isoformat(),
                "day": day.day,
                "weekday": day.weekday(),  # 0 = Thứ Hai (số, khác /days trả tên)
                "weekdayName": lunar.WEEKDAYS[day.weekday()],
                "lunarDay": ld,
                "lunarMonth": lm,
                "isLeapMonth": leap,
                "isHoangDao": is_good,
                "holidays": calendar_query.holidays_on(day),
            }
        )
    prev_idx, next_idx = year * 12 + month - 2, year * 12 + month
    return {
        "year": year,
        "month": month,
        "days": days,
        "previous": f"{prev_idx // 12:04d}-{prev_idx % 12 + 1:02d}",
        "next": f"{next_idx // 12:04d}-{next_idx % 12 + 1:02d}",
    }


def lunar_to_solar(lunar_day: int, lunar_month: int, lunar_year: int, is_leap: bool) -> dict:
    _check_year(lunar_year)
    if not (1 <= lunar_day <= 30 and 1 <= lunar_month <= 12):
        raise InputError("INVALID_LUNAR_DATE", "Ngày âm 1-30, tháng âm 1-12.")
    day = calendar_query.lunar_to_solar_exact(lunar_day, lunar_month, lunar_year, is_leap)
    if day is None:
        raise InputError("LUNAR_DATE_NOT_FOUND", "Ngày âm lịch này không tồn tại.")
    return day_info(day)


def calendar_answer(question: str) -> str:
    question = (question or "").strip()
    if len(question) > 200:
        raise InputError("QUESTION_TOO_LONG", "Câu hỏi tối đa 200 ký tự.")
    return calendar_query.answer(question)


def numerology_reading(birth: dt.date, name: str = "") -> dict:
    _check_year(birth.year)
    if len(name) > 80:
        raise InputError("NAME_TOO_LONG", "Họ tên tối đa 80 ký tự.")
    if name.strip() and not numerology.has_letters(name):
        raise InputError("INVALID_NAME", "Họ tên cần viết bằng chữ Latin (A-Z, có dấu được).")
    return numerology.reading_data(numerology.compute(birth, name, today()))


def parse_ymd(raw: str) -> tuple[int, int, int]:
    """'YYYY-MM-DD' hoặc 'DD/MM/YYYY' -> (năm, tháng, ngày) KHÔNG kiểm tra theo
    lịch dương (ngày âm như 30/2 vẫn hợp lệ)."""
    if not (raw or "").strip():
        raise InputError("INVALID_DATE", "Thiếu ngày (birthDate), dạng YYYY-MM-DD hoặc DD/MM/YYYY.")
    m = _YMD_RE.fullmatch(raw)
    if not m:
        raise InputError("INVALID_DATE", "Ngày có dạng YYYY-MM-DD hoặc DD/MM/YYYY.")
    if m.group("y"):
        return int(m.group("y")), int(m.group("m")), int(m.group("d"))
    return int(m.group("y2")), int(m.group("m2")), int(m.group("d2"))


def build_tuvi(
    year: int,
    month: int,
    day: int,
    hour_branch: int,
    gender: str,
    is_lunar: bool = False,
    is_leap: bool = False,
    early_ty: bool = False,
) -> tuvi.Chart:
    """Lá số (đối tượng Chart) từ ngày sinh dương, hoặc (is_lunar) ngày/tháng/năm ÂM.
    early_ty: sinh 23h-24h (giờ Tý đầu) - an theo ngày hôm sau, chỉ hợp lệ với
    hour_branch = 0. Raise InputError khi dữ liệu sai."""
    if gender not in GENDERS:
        raise InputError("INVALID_GENDER", "Giới tính là male hoặc female.")
    if not 0 <= hour_branch <= 11:
        raise InputError("INVALID_HOUR", "hourBranch là số 0 (Tý) đến 11 (Hợi).")
    if early_ty and hour_branch != 0:
        raise InputError("INVALID_HOUR", "Giờ Tý đầu (23h-24h) chỉ đi với giờ Tý (0).")
    _check_year(year)
    male = gender == "male"
    if is_lunar:
        if not (1 <= day <= 30 and 1 <= month <= 12):
            raise InputError("INVALID_LUNAR_DATE", "Ngày âm 1-30, tháng âm 1-12.")
        solar = calendar_query.lunar_to_solar_exact(day, month, year, is_leap)
        if solar is None:
            raise InputError("LUNAR_DATE_NOT_FOUND", "Ngày âm lịch này không tồn tại.")
    else:
        try:
            solar = dt.date(year, month, day)
        except ValueError:
            raise InputError("INVALID_DATE", "Ngày dương lịch không tồn tại.") from None
    return tuvi.chart_for(solar, hour_branch, male, early_ty)


def tuvi_chart(
    year: int,
    month: int,
    day: int,
    hour_branch: int,
    gender: str,
    is_lunar: bool = False,
    is_leap: bool = False,
    early_ty: bool = False,
) -> dict:
    """Lá số dạng dict (cho trang web/JSON API), xem build_tuvi."""
    return tuvi.chart_data(
        build_tuvi(year, month, day, hour_branch, gender, is_lunar, is_leap, early_ty)
    )
