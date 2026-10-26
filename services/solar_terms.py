"""Thời khắc chính xác của 24 tiết khí (độ chính xác cỡ 1 phút), chạy offline.

``services.lunar`` tính tiết khí theo NGÀY (vị trí Mặt Trời lúc 0 giờ hôm sau,
như lịch Hồ Ngọc Đức, công thức ``lunar._sun_longitude`` sai số ~0,01° ≈ 15
phút) - đủ cho lịch ngày nhưng không đủ cho Kỳ Môn chiết bổ, nơi ngày giao tiết
phải đổi cục đúng GIỜ. Module này tính kinh độ biểu kiến của Mặt Trời:

- Kinh độ hình học: chuỗi VSOP87 rút gọn của Trái Đất (Jean Meeus,
  "Astronomical Algorithms" 2nd ed., chương 25 + phụ lục III; L0..L5, R0..R2
  các số hạng lớn), cộng 180°, đổi sang hệ FK5 (-0,09033").
- Chương động theo kinh độ (Meeus chương 22, bản 4 số hạng, sai số < 0,5") và
  quang sai -20,4898"/R.
- Giờ địa động lực (TT) = UT + ΔT (đa thức Espenak & Meeus 2006, 1900-2150).

Sai số tổng cỡ vài giây cung (< 1 phút thời gian) - đối chiếu xuân phân/hạ
chí/thu phân/đông chí 2023-2026 của USNO trong test/test_solar_terms.py. Tiết
thứ k (0 = Xuân phân, 18 = Đông chí, 21 = Lập xuân) bắt đầu khi kinh độ biểu
kiến đạt 15·k độ; tên lấy từ ``lunar.TIET_KHI`` để dùng chung một bảng.
"""

from __future__ import annotations

import datetime as dt
import functools
import math
from dataclasses import dataclass

from services import lunar

_UNIX_EPOCH_JD = 2440587.5
_J2000 = 2451545.0
_TROPICAL_YEAR = 365.24219
_ARCSEC = 1 / 3600

# VSOP87 (Trái Đất), mỗi số hạng (A, B, C): A·cos(B + C·τ), τ = nghìn năm Julius
# từ J2000 (TT), đơn vị 1e-8 rad (L) / 1e-8 AU (R). Chép Meeus bảng 32.A.
_L0 = (
    (175347046, 0, 0), (3341656, 4.6692568, 6283.07585), (34894, 4.6261, 12566.1517),
    (3497, 2.7441, 5753.3849), (3418, 2.8289, 3.5231), (3136, 3.6277, 77713.7715),
    (2676, 4.4181, 7860.4194), (2343, 6.1352, 3930.2097), (1324, 0.7425, 11506.7698),
    (1273, 2.0371, 529.691), (1199, 1.1096, 1577.3435), (990, 5.233, 5884.927),
    (902, 2.045, 26.298), (857, 3.508, 398.149), (780, 1.179, 5223.694),
    (753, 2.533, 5507.553), (505, 4.583, 18849.228), (492, 4.205, 775.523),
    (357, 2.92, 0.067), (317, 5.849, 11790.629), (284, 1.899, 796.298),
    (271, 0.315, 10977.079), (243, 0.345, 5486.778), (206, 4.806, 2544.314),
    (205, 1.869, 5573.143), (202, 2.458, 6069.777), (156, 0.833, 213.299),
    (132, 3.411, 2942.463), (126, 1.083, 20.775), (115, 0.645, 0.98),
    (103, 0.636, 4694.003), (102, 0.976, 15720.839), (102, 4.267, 7.114),
    (99, 6.21, 2146.17), (98, 0.68, 155.42), (86, 5.98, 161000.69),
    (85, 1.3, 6275.96), (85, 3.67, 71430.7), (80, 1.81, 17260.15),
    (79, 3.04, 12036.46), (75, 1.76, 5088.63), (74, 3.5, 3154.69),
    (74, 4.68, 801.82), (70, 0.83, 9437.76), (62, 3.98, 8827.39),
    (61, 1.82, 7084.9), (57, 2.78, 6286.6), (56, 4.39, 14143.5),
    (56, 3.47, 6279.55), (52, 0.19, 12139.55), (52, 1.33, 1748.02),
    (51, 0.28, 5856.48), (49, 0.49, 1194.45), (41, 5.37, 8429.24),
    (41, 2.4, 19651.05), (39, 6.17, 10447.39), (37, 6.04, 10213.29),
    (37, 2.57, 1059.38), (36, 1.71, 2352.87), (36, 1.78, 6812.77),
    (33, 0.59, 17789.85), (30, 0.44, 83996.85), (30, 2.74, 1349.87),
    (25, 3.16, 4690.48),
)  # fmt: skip
_L1 = (
    (628331966747, 0, 0), (206059, 2.678235, 6283.07585), (4303, 2.6351, 12566.1517),
    (425, 1.59, 3.523), (119, 5.796, 26.298), (109, 2.966, 1577.344),
    (93, 2.59, 18849.23), (72, 1.14, 529.69), (68, 1.87, 398.15),
    (67, 4.41, 5507.55), (59, 2.89, 5223.69), (56, 2.17, 155.42),
    (45, 0.4, 796.3), (36, 0.47, 775.52), (29, 2.65, 7.11),
    (21, 5.34, 0.98), (19, 1.85, 5486.78), (19, 4.97, 213.3),
    (17, 2.99, 6275.96), (16, 0.03, 2544.31), (16, 1.43, 2146.17),
    (15, 1.21, 10977.08), (12, 2.83, 1748.02), (12, 3.26, 5088.63),
    (12, 5.27, 1194.45), (12, 2.08, 4694.0), (11, 0.77, 553.57),
    (10, 1.3, 6286.6), (10, 4.24, 1349.87), (9, 2.7, 242.73),
    (9, 5.64, 951.72), (8, 5.3, 2352.87), (6, 2.65, 9437.76),
    (6, 4.67, 4690.48),
)  # fmt: skip
_L2 = (
    (52919, 0, 0), (8720, 1.0721, 6283.0758), (309, 0.867, 12566.152),
    (27, 0.05, 3.52), (16, 5.19, 26.3), (16, 3.68, 155.42), (10, 0.76, 18849.23),
    (9, 2.06, 77713.77), (7, 0.83, 775.52), (5, 4.66, 1577.34), (4, 1.03, 7.11),
    (4, 3.44, 5573.14), (3, 5.14, 796.3), (3, 6.05, 5507.55), (3, 1.19, 242.73),
    (3, 6.12, 529.69), (3, 0.31, 398.15), (3, 2.28, 553.57), (2, 4.38, 5223.69),
    (2, 3.75, 0.98),
)  # fmt: skip
_L3 = (
    (289, 5.844, 6283.076), (35, 0, 0), (17, 5.49, 12566.15), (3, 5.2, 155.42),
    (1, 4.72, 3.52), (1, 5.3, 18849.23), (1, 5.97, 242.73),
)  # fmt: skip
_L4 = ((114, 3.142, 0), (8, 4.13, 6283.08), (1, 3.84, 12566.15))
_L5 = ((1, 3.14, 0),)
_R0 = (
    (100013989, 0, 0), (1670700, 3.0984635, 6283.07585), (13956, 3.05525, 12566.1517),
    (3084, 5.1985, 77713.7715), (1628, 1.1739, 5753.3849), (1576, 2.8469, 7860.4194),
    (925, 5.453, 11506.77), (542, 4.564, 3930.21), (472, 3.661, 5884.927),
)  # fmt: skip
_R1 = ((103019, 1.10749, 6283.07585), (1721, 1.0644, 12566.1517), (702, 3.142, 0))
_R2 = ((4359, 5.7846, 6283.0758), (124, 5.579, 12566.152))


@dataclass(frozen=True)
class SolarTerm:
    """Tiết khí đang hiệu lực: số thứ tự (0 = Xuân phân ... 23 = Kinh trập),
    tên, kinh độ bắt đầu (độ) và thời khắc bắt đầu/kết thúc (UTC)."""

    index: int
    name: str
    longitude: int
    start: dt.datetime
    end: dt.datetime


def _series(terms, tau: float) -> float:
    return sum(a * math.cos(b + c * tau) for a, b, c in terms)


def delta_t_seconds(year: float) -> float:
    """ΔT = TT - UT (giây), đa thức Espenak & Meeus (NASA, 2006) cho 1900-2150."""
    y = year
    if y < 1920:
        t = y - 1900
        return -2.79 + 1.494119 * t - 0.0598939 * t**2 + 0.0061966 * t**3 - 0.000197 * t**4
    if y < 1941:
        t = y - 1920
        return 21.20 + 0.84493 * t - 0.076100 * t**2 + 0.0020936 * t**3
    if y < 1961:
        t = y - 1950
        return 29.07 + 0.407 * t - t**2 / 233 + t**3 / 2547
    if y < 1986:
        t = y - 1975
        return 45.45 + 1.067 * t - t**2 / 260 - t**3 / 718
    if y < 2005:
        t = y - 2000
        return (
            63.86 + 0.3345 * t - 0.060374 * t**2 + 0.0017275 * t**3
            + 0.000651814 * t**4 + 0.00002373599 * t**5
        )  # fmt: skip
    if y < 2050:
        t = y - 2000
        return 62.92 + 0.32217 * t + 0.005589 * t**2
    return -20 + 32 * ((y - 1820) / 100) ** 2 - 0.5628 * (2150 - y)


def jd_from_datetime(moment: dt.datetime) -> float:
    """Ngày Julius (UT) của một thời điểm có múi giờ."""
    if moment.tzinfo is None:
        raise ValueError("Cần datetime có múi giờ")
    return _UNIX_EPOCH_JD + moment.timestamp() / 86400


def datetime_from_jd(jd: float) -> dt.datetime:
    """Thời điểm UTC (làm tròn tới giây) của một ngày Julius (UT)."""
    seconds = round((jd - _UNIX_EPOCH_JD) * 86400)
    return dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=seconds)


def apparent_longitude(jd_ut: float) -> float:
    """Kinh độ hoàng đạo biểu kiến của Mặt Trời (độ, 0..360) tại ngày Julius UT."""
    year = 2000 + (jd_ut - _J2000) / 365.25
    jde = jd_ut + delta_t_seconds(year) / 86400
    tau = (jde - _J2000) / 365250
    lon = (
        _series(_L0, tau)
        + _series(_L1, tau) * tau
        + _series(_L2, tau) * tau**2
        + _series(_L3, tau) * tau**3
        + _series(_L4, tau) * tau**4
        + _series(_L5, tau) * tau**5
    ) / 1e8
    radius = (_series(_R0, tau) + _series(_R1, tau) * tau + _series(_R2, tau) * tau**2) / 1e8
    sun = math.degrees(lon) + 180
    t = tau * 10  # thế kỷ Julius
    omega = math.radians(125.04452 - 1934.136261 * t)
    mean_sun = math.radians(280.4665 + 36000.7698 * t)
    mean_moon = math.radians(218.3165 + 481267.8813 * t)
    nutation = (
        -17.20 * math.sin(omega)
        - 1.32 * math.sin(2 * mean_sun)
        - 0.23 * math.sin(2 * mean_moon)
        + 0.21 * math.sin(2 * omega)
    )
    sun += (-0.09033 + nutation - 20.4898 / radius) * _ARCSEC
    return sun % 360


def _angle_diff(target: float, value: float) -> float:
    """target - value quy về [-180, 180)."""
    return (target - value + 180) % 360 - 180


def moment_of_longitude(target: float, near_jd: float) -> float:
    """Ngày Julius UT gần ``near_jd`` nhất mà kinh độ biểu kiến = ``target`` độ
    (lặp Newton theo tốc độ trung bình, hội tụ dưới 1 giây sau vài vòng)."""
    jd = near_jd
    for _ in range(8):
        step = _angle_diff(target, apparent_longitude(jd)) * _TROPICAL_YEAR / 360
        jd += step
        if abs(step) < 1e-6:
            break
    return jd


@functools.lru_cache(maxsize=512)
def _term_start_jd(index: int, year: int) -> float:
    """Ngày Julius UT bắt đầu tiết ``index`` trong năm dương lịch ``year``.
    Xuân phân (0) ~ 20/3; tiết k cách ~15,2 ngày (Tiểu hàn..Kinh trập rơi vào
    đầu năm). Bộ nhớ đệm có trần (512 mục, vài chục KB)."""
    days = (index * 15) * _TROPICAL_YEAR / 360
    guess = lunar._jd_from_date(20, 3, year) + days
    if index >= 19:  # Tiểu hàn, Đại hàn, Lập xuân, Vũ thủy, Kinh trập: đầu năm
        guess -= _TROPICAL_YEAR
    return moment_of_longitude(index * 15.0, guess)


def term_start(index: int, year: int) -> dt.datetime:
    """Thời khắc (UTC) bắt đầu tiết ``index`` (0..23) rơi trong năm dương ``year``."""
    return datetime_from_jd(_term_start_jd(index % 24, year))


def li_chun(year: int) -> dt.datetime:
    """Thời khắc Lập xuân (kinh độ 315°) của năm dương ``year`` (đầu tháng 2)."""
    return term_start(21, year)


def term_at(moment: dt.datetime) -> SolarTerm:
    """Tiết khí đang hiệu lực tại ``moment`` (datetime có múi giờ)."""
    jd = jd_from_datetime(moment)
    index = int(apparent_longitude(jd) // 15) % 24
    year = moment.astimezone(dt.UTC).year
    # Tiết của năm dương nào: thử năm hiện tại và hai năm kề, lấy lần bắt đầu gần
    # nhất <= jd (dung sai 1 giây cho đúng khoảnh khắc giao tiết).
    starts = [_term_start_jd(index, y) for y in (year - 1, year, year + 1)]
    start = max(s for s in starts if s <= jd + 1 / 86400)
    nxt = (index + 1) % 24
    ends = [_term_start_jd(nxt, y) for y in (year - 1, year, year + 1, year + 2)]
    end = min(e for e in ends if e > start)
    return SolarTerm(
        index=index,
        name=lunar.TIET_KHI[index],
        longitude=index * 15,
        start=datetime_from_jd(start),
        end=datetime_from_jd(end),
    )
