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

TZ = 7.0
CAN = ["Giáp", "Ất", "Bính", "Đinh", "Mậu", "Kỷ", "Canh", "Tân", "Nhâm", "Quý"]
CHI = ["Tý", "Sửu", "Dần", "Mão", "Thìn", "Tỵ", "Ngọ", "Mùi", "Thân", "Dậu", "Tuất", "Hợi"]


def _jd_from_date(dd: int, mm: int, yy: int) -> int:
    a = (14 - mm) // 12
    y = yy + 4800 - a
    m = mm + 12 * a - 3
    jd = dd + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    if jd < 2299161:
        jd = dd + (153 * m + 2) // 5 + 365 * y + y // 4 - 32083
    return jd


def _new_moon(k: int) -> float:
    T = k / 1236.85
    T2, T3 = T * T, T * T * T
    dr = math.pi / 180
    Jd1 = 2415020.75933 + 29.53058868 * k + 0.0001178 * T2 - 0.000000155 * T3
    Jd1 += 0.00033 * math.sin((166.56 + 132.87 * T - 0.009173 * T2) * dr)
    M = 359.2242 + 29.10535608 * k - 0.0000333 * T2 - 0.00000347 * T3
    Mpr = 306.0253 + 385.81691806 * k + 0.0107306 * T2 + 0.00001236 * T3
    F = 21.2964 + 390.67050646 * k - 0.0016528 * T2 - 0.00000239 * T3
    C1 = (0.1734 - 0.000393 * T) * math.sin(M * dr) + 0.0021 * math.sin(2 * dr * M)
    C1 -= 0.4068 * math.sin(Mpr * dr) - 0.0161 * math.sin(dr * 2 * Mpr)
    C1 -= 0.0004 * math.sin(dr * 3 * Mpr)
    C1 += 0.0104 * math.sin(dr * 2 * F) - 0.0051 * math.sin(dr * (M + Mpr))
    C1 -= 0.0074 * math.sin(dr * (M - Mpr)) - 0.0004 * math.sin(dr * (2 * F + M))
    C1 -= 0.0004 * math.sin(dr * (2 * F - M)) + 0.0006 * math.sin(dr * (2 * F + Mpr))
    C1 += 0.0010 * math.sin(dr * (2 * F - Mpr)) + 0.0005 * math.sin(dr * (2 * Mpr + M))
    if T < -11:
        deltat = 0.001 + 0.000839 * T + 0.0002261 * T2 - 0.00000845 * T3 - 0.000000081 * T * T3
    else:
        deltat = -0.000278 + 0.000265 * T + 0.000262 * T2
    return Jd1 + C1 - deltat


def _new_moon_day(k: int) -> int:
    return int(math.floor(_new_moon(k) + 0.5 + TZ / 24))


def _sun_longitude(jdn: float) -> float:
    T = (jdn - 2451545.5 - TZ / 24) / 36525
    T2 = T * T
    dr = math.pi / 180
    M = 357.52910 + 35999.05030 * T - 0.0001559 * T2 - 0.00000048 * T * T2
    L0 = 280.46645 + 36000.76983 * T + 0.0003032 * T2
    DL = (1.914600 - 0.004817 * T - 0.000014 * T2) * math.sin(dr * M)
    DL += (0.019993 - 0.000101 * T) * math.sin(dr * 2 * M) + 0.000290 * math.sin(dr * 3 * M)
    L = (L0 + DL) * dr
    L -= math.pi * 2 * math.floor(L / (math.pi * 2))
    return L


def _sun_long_index(jdn: int) -> int:
    return int(math.floor(_sun_longitude(jdn) / math.pi * 6))


def _lunar_month11(yy: int) -> int:
    off = _jd_from_date(31, 12, yy) - 2415021
    k = int(math.floor(off / 29.530588853))
    nm = _new_moon_day(k)
    if _sun_long_index(nm) >= 9:
        nm = _new_moon_day(k - 1)
    return nm


def _leap_month_offset(a11: int) -> int:
    k = int(math.floor((a11 - 2415021.076998695) / 29.530588853 + 0.5))
    i = 1
    arc = _sun_long_index(_new_moon_day(k + i))
    while True:
        last = arc
        i += 1
        arc = _sun_long_index(_new_moon_day(k + i))
        if not (arc != last and i < 14):
            break
    return i - 1


def solar_to_lunar(dd: int, mm: int, yy: int) -> tuple[int, int, int, bool]:
    """Return (lunar_day, lunar_month, lunar_year, is_leap_month)."""
    day_number = _jd_from_date(dd, mm, yy)
    k = int(math.floor((day_number - 2415021.076998695) / 29.530588853))
    month_start = _new_moon_day(k + 1)
    if month_start > day_number:
        month_start = _new_moon_day(k)
    a11 = _lunar_month11(yy)
    b11 = a11
    if a11 >= month_start:
        lunar_year = yy
        a11 = _lunar_month11(yy - 1)
    else:
        lunar_year = yy + 1
        b11 = _lunar_month11(yy + 1)
    lunar_day = day_number - month_start + 1
    diff = (month_start - a11) // 29
    leap = False
    lunar_month = diff + 11
    if b11 - a11 > 365:
        leap_diff = _leap_month_offset(a11)
        if diff >= leap_diff:
            lunar_month = diff + 10
            if diff == leap_diff:
                leap = True
    if lunar_month > 12:
        lunar_month -= 12
    if lunar_month >= 11 and diff < 4:
        lunar_year -= 1
    return lunar_day, lunar_month, lunar_year, leap


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


def _jd_to_date(jd: int) -> tuple[int, int, int]:
    if jd > 2299160:
        a = jd + 32044
        b = (4 * a + 3) // 146097
        c = a - (b * 146097) // 4
    else:
        b, c = 0, jd + 32082
    d = (4 * c + 3) // 1461
    e = c - (1461 * d) // 4
    m = (5 * e + 2) // 153
    return e - (153 * m + 2) // 5 + 1, m + 3 - 12 * (m // 10), b * 100 + d - 4800 + m // 10


def lunar_to_solar(
    lunar_day: int, lunar_month: int, lunar_year: int, leap: bool = False
) -> tuple[int, int, int] | None:
    """Đổi ngày âm sang dương. Trả về None nếu tháng nhuận không tồn tại."""
    if lunar_month < 11:
        a11, b11 = _lunar_month11(lunar_year - 1), _lunar_month11(lunar_year)
    else:
        a11, b11 = _lunar_month11(lunar_year), _lunar_month11(lunar_year + 1)
    k = int(math.floor(0.5 + (a11 - 2415021.076998695) / 29.530588853))
    off = lunar_month - 11
    if off < 0:
        off += 12
    if b11 - a11 > 365:
        leap_off = _leap_month_offset(a11)
        leap_month = leap_off - 2
        if leap_month < 0:
            leap_month += 12
        if leap and lunar_month != leap_month:
            return None
        if leap or off >= leap_off:
            off += 1
    elif leap:
        return None
    return _jd_to_date(_new_moon_day(k + off) + lunar_day - 1)


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
