"""Âm lịch Việt Nam (thuật toán Hồ Ngọc Đức, múi giờ UTC+7), chạy offline.

Chuyển từ gói vn-info (MIT License, Copyright (c) 2026). Hàm chính:
    solar_to_lunar(d, m, y)          -> (ngày, tháng, năm âm, nhuận?)
    lunar_to_solar(d, m, y, leap)    -> (ngày, tháng, năm dương) hoặc None
    date_info(date)                  -> dict đầy đủ: ngày âm, can chi, tiết khí,
                                        giờ hoàng đạo, số ngày tới Tết
    describe_date(info)              -> đoạn văn tiếng Việt làm grounding cho chat

Tiết khí tính theo vị trí Mặt Trời lúc 0 giờ ngày hôm sau (giờ Việt Nam), giống
lịch của Hồ Ngọc Đức, nên có thể lệch một ngày so với giờ chính xác của tiết.
"""

from __future__ import annotations

import datetime as dt
import math

# Toán âm lịch thuần nằm ở core/lunar_math.py (tầng thấp như stock/calendar.py
# dùng được); re-export để mọi chỗ gọi ``lunar.<tên>`` cũ giữ nguyên.
from core.lunar_math import (  # noqa: F401
    TZ,
    _jd_from_date,
    _jd_to_date,
    _leap_month_offset,
    _lunar_month11,
    _new_moon,
    _new_moon_day,
    _sun_long_index,
    _sun_longitude,
    lunar_to_solar,
    solar_to_lunar,
)

CAN = ["Giáp", "Ất", "Bính", "Đinh", "Mậu", "Kỷ", "Canh", "Tân", "Nhâm", "Quý"]
CHI = ["Tý", "Sửu", "Dần", "Mão", "Thìn", "Tỵ", "Ngọ", "Mùi", "Thân", "Dậu", "Tuất", "Hợi"]


def year_can_chi(lunar_year: int) -> str:
    return f"{CAN[(lunar_year + 6) % 10]} {CHI[(lunar_year + 8) % 12]}"


def day_can_chi(dd: int, mm: int, yy: int) -> str:
    jd = _jd_from_date(dd, mm, yy)
    return f"{CAN[(jd + 9) % 10]} {CHI[(jd + 1) % 12]}"


TIET_KHI = [
    "Xuân phân",
    "Thanh minh",
    "Cốc vũ",
    "Lập hạ",
    "Tiểu mãn",
    "Mang chủng",
    "Hạ chí",
    "Tiểu thử",
    "Đại thử",
    "Lập thu",
    "Xử thử",
    "Bạch lộ",
    "Thu phân",
    "Hàn lộ",
    "Sương giáng",
    "Lập đông",
    "Tiểu tuyết",
    "Đại tuyết",
    "Đông chí",
    "Tiểu hàn",
    "Đại hàn",
    "Lập xuân",
    "Vũ thủy",
    "Kinh trập",
]
_GIO_HD = [
    "110100101100",
    "001101001011",
    "110011010010",
    "101100110100",
    "001011001101",
    "010010110011",
]
_GIO_RANGE = [
    "23h-1h",
    "1h-3h",
    "3h-5h",
    "5h-7h",
    "7h-9h",
    "9h-11h",
    "11h-13h",
    "13h-15h",
    "15h-17h",
    "17h-19h",
    "19h-21h",
    "21h-23h",
]


def leap_month_of(lunar_year: int) -> int:
    """Tháng nhuận của năm âm lịch, 0 nếu không nhuận."""
    for m in range(1, 13):
        if lunar_to_solar(1, m, lunar_year, True):
            return m
    return 0


def month_can_chi(lunar_month: int, lunar_year: int) -> str:
    return f"{CAN[(lunar_year * 12 + lunar_month + 3) % 10]} {CHI[(lunar_month + 1) % 12]}"


def solar_term(dd: int, mm: int, yy: int) -> str:
    """Tiết khí của ngày dương lịch."""
    jd = _jd_from_date(dd, mm, yy)
    return TIET_KHI[int(math.floor(_sun_longitude(jd + 1) / math.pi * 12))]


def lucky_hours(dd: int, mm: int, yy: int) -> list[str]:
    """Giờ hoàng đạo, ví dụ ["Tý (23h-1h)", ...]."""
    chi = (_jd_from_date(dd, mm, yy) + 1) % 12
    pattern = _GIO_HD[chi % 6]
    return [f"{CHI[i]} ({_GIO_RANGE[i]})" for i, c in enumerate(pattern) if c == "1"]


WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]


def lunar_text(ld: int, lm: int, ly: int, leap: bool) -> str:
    return f"ngày {ld} tháng {lm}{' nhuận' if leap else ''} năm {year_can_chi(ly)}"


def next_tet(date: dt.date) -> dt.date:
    """Mùng 1 Tết Nguyên đán kế tiếp (hoặc hôm nay nếu hôm nay là Tết)."""
    for y in (date.year, date.year + 1):
        solar = lunar_to_solar(1, 1, y)
        assert solar is not None  # tháng thường (không nhuận) luôn tồn tại
        tet = dt.date(solar[2], solar[1], solar[0])
        if tet >= date:
            return tet
    raise RuntimeError("unreachable")


def date_info(date: dt.date) -> dict:
    """Đầy đủ thông tin lịch (dương + âm) của một ngày dương lịch."""
    ld, lm, ly, leap = solar_to_lunar(date.day, date.month, date.year)
    tet = next_tet(date)
    return {
        "date": date.isoformat(),
        "date_vn": date.strftime("%d/%m/%Y"),
        "weekday": WEEKDAYS[date.weekday()],
        "lunar": {
            "day": ld,
            "month": lm,
            "year": ly,
            "leap_month": leap,
            "text": lunar_text(ld, lm, ly, leap),
            "year_can_chi": year_can_chi(ly),
            "month_can_chi": month_can_chi(lm, ly),
            "day_can_chi": day_can_chi(date.day, date.month, date.year),
        },
        "solar_term": solar_term(date.day, date.month, date.year),
        "lucky_hours": lucky_hours(date.day, date.month, date.year),
        "next_tet": tet.isoformat(),
        "days_to_tet": (tet - date).days,
    }


def describe_date(info: dict) -> str:
    """Đoạn văn tiếng Việt từ ``date_info`` để chèn làm grounding cho chat."""
    lunar = info["lunar"]
    return (
        f"{info['weekday']} {info['date_vn']} dương lịch, tức {lunar['text']} âm lịch "
        f"(ngày {lunar['day_can_chi']}, tháng {lunar['month_can_chi']}).\n"
        f"Tiết khí: {info['solar_term']}.\n"
        f"Giờ hoàng đạo: {', '.join(info['lucky_hours'])}.\n"
        f"Còn {info['days_to_tet']} ngày tới Tết Nguyên đán "
        f"({dt.date.fromisoformat(info['next_tet']):%d/%m/%Y})."
    )


def find_lunar_date(
    ld: int, lm: int, year: int | None, leap: bool, today: dt.date
) -> tuple[dt.date | None, int, str]:
    """Ngày dương của ngày âm ld/lm (năm ``year``, hoặc lần sắp tới nếu None).

    Trả (ngày dương, năm âm đã dùng, "") hoặc (None, năm, câu báo lỗi): ngày 30
    của tháng thiếu, tháng nhuận không có, ngày/tháng/năm ngoài phạm vi. Dùng
    chung cho chat (lunar_chat), /lich (calendar_query) và router tool."""
    if not (1 <= ld <= 30 and 1 <= lm <= 12):
        return None, year or 0, "Ngày/tháng âm lịch không hợp lệ (ngày 1-30, tháng 1-12)."
    current = solar_to_lunar(today.day, today.month, today.year)[2]
    years = [year] if year is not None else [current, current + 1]
    if not all(1900 <= y <= 2100 for y in years):
        return None, years[0], "Năm âm lịch cần trong khoảng 1900-2100."
    error = ""
    for y in years:
        solar = lunar_to_solar(ld, lm, y, leap)
        if solar is None:
            error = error or f"Năm âm lịch {year_can_chi(y)} ({y}) không có tháng {lm} nhuận."
            continue
        day = dt.date(solar[2], solar[1], solar[0])
        # Tháng thiếu (29 ngày) không có ngày 30: thuật toán trả ngày 1 tháng sau.
        if solar_to_lunar(day.day, day.month, day.year) != (ld, lm, y, leap):
            error = error or f"Tháng {lm} âm lịch năm {year_can_chi(y)} chỉ có 29 ngày."
            continue
        if year is None and day < today:
            continue
        return day, y, ""
    return None, years[-1], error or "Không tìm thấy ngày âm lịch này."
