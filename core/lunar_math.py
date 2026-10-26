"""Toán âm lịch Việt Nam thuần (thuật toán Hồ Ngọc Đức, múi giờ UTC+7), chạy offline.

Tách khỏi services/lunar.py để tầng thấp (stock/calendar.py - dự phòng ngày
Tết/Giỗ Tổ khi bảng ngày nghỉ giao dịch chưa có năm mới) dùng được mà không
import ngược lên services/. Chỉ dùng thư viện chuẩn; services/lunar.py re-export
lại toàn bộ tên ở đây nên code cũ (``lunar.lunar_to_solar``,
``lunar._jd_from_date``...) giữ nguyên.
"""

from __future__ import annotations

import math

TZ = 7.0


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
