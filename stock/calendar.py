"""Lịch giao dịch HOSE/HNX/UPCOM: cuối tuần + ngày lễ nghỉ giao dịch.

Dùng cho: gate "dữ liệu cũ" (đếm PHIÊN bị thiếu thay vì ngày lịch - Tết nghỉ
9-10 ngày không được coi là dữ liệu cũ), nhãn "trong giờ giao dịch" (ngày lễ
không có phiên) và dò ngày GDKHQ theo phiên giao dịch.

Ngày lễ âm lịch (Tết, Giỗ Tổ) và ngày nghỉ bù do Chính phủ chốt từng năm nên
ghi bảng theo năm - cập nhật mỗi năm khi Sở công bố lịch nghỉ (từ 1/11 bot tự
nhắc chủ bot nếu năm sau chưa có, xem ``missing_next_year``). Năm chưa có trong
bảng dùng bản DỰ PHÒNG tính từ âm lịch (``_provisional_lunar_holidays``): chỉ
phần lõi năm nào cũng nghỉ (đêm giao thừa tới mùng 3, Giỗ Tổ 10/3) - không đoán
28 Tết, mùng 4-5 hay ngày nghỉ thêm dịp 2/9 vì đánh dấu nhầm 1 phiên thật là
ngày lễ còn tệ hơn thiếu 1 ngày lễ. Có thể bổ sung qua env
``STOCK_EXTRA_HOLIDAYS=YYYY-MM-DD,YYYY-MM-DD``.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Ngày lễ dương lịch cố định (tháng, ngày). Rơi vào cuối tuần thì nghỉ bù thứ
# Hai kế tiếp theo quy định Bộ luật Lao động - Sở theo lịch nghỉ nhà nước.
_FIXED_HOLIDAYS = ((1, 1), (4, 30), (5, 1), (9, 2))
# Ngày lễ dương lịch mới, áp dụng TỪ năm ghi kèm: 24/11 Ngày Văn hoá Việt Nam
# (Nghị quyết 28/2026/QH16, hiệu lực 1/7/2026 - người lao động nghỉ hưởng nguyên
# lương; 24/11/2026 là thứ Ba, không hoán đổi). Sở chưa ra thông báo nghỉ giao
# dịch riêng cho ngày này - cần xác nhận lại khi HOSE/HNX công bố.
_FIXED_HOLIDAYS_SINCE = (((11, 24), 2026),)

# Ngày lễ âm lịch / nghỉ bù theo thông báo từng năm (chỉ ghi ngày làm việc).
_LUNAR_HOLIDAYS: dict[int, tuple[str, ...]] = {
    2024: (
        "2024-02-08",
        "2024-02-09",
        "2024-02-12",
        "2024-02-13",
        "2024-02-14",  # Tết Giáp Thìn
        "2024-04-18",  # Giỗ Tổ Hùng Vương
        "2024-09-03",  # nghỉ thêm dịp Quốc khánh
    ),
    2025: (
        "2025-01-27",
        "2025-01-28",
        "2025-01-29",
        "2025-01-30",
        "2025-01-31",  # Tết Ất Tỵ
        "2025-04-07",  # Giỗ Tổ Hùng Vương
        "2025-09-01",  # nghỉ thêm dịp Quốc khánh
    ),
    2026: (
        "2026-02-16",
        "2026-02-17",
        "2026-02-18",
        "2026-02-19",
        "2026-02-20",  # Tết Bính Ngọ
        "2026-04-27",  # Giỗ Tổ Hùng Vương (10/3 âm rơi Chủ nhật 26/4, nghỉ bù)
        "2026-09-01",  # nghỉ thêm dịp Quốc khánh
    ),
    # Theo thông báo Bộ Nội vụ (Công văn 10065/VPCP-KGVX, 2/10/2026): Tết nghỉ
    # 7 ngày từ thứ Năm 4/2 tới thứ Tư 10/2/2027 (28 Chạp - mùng 5, gồm 2 ngày
    # nghỉ bù, không hoán đổi) -> 11-12/2 vẫn là phiên giao dịch, KHÔNG ghi vào
    # đây. Đối chiếu lại khi HOSE/HNX công bố lịch nghỉ (thường tháng 12-1).
    2027: (
        "2027-02-04",
        "2027-02-05",
        "2027-02-08",
        "2027-02-09",
        "2027-02-10",  # Tết Đinh Mùi
        "2027-04-16",  # Giỗ Tổ Hùng Vương (10/3 âm, thứ Sáu)
        "2027-09-03",  # nghỉ thêm dịp Quốc khánh (2-3/9)
    ),
}
MARKET_OPEN_MINUTE = 9 * 60
MARKET_CLOSE_MINUTE = 15 * 60


def _extra_holidays() -> frozenset[date]:
    raw = os.getenv("STOCK_EXTRA_HOLIDAYS", "")
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(date.fromisoformat(part))
        except ValueError:
            continue
    return frozenset(out)


def _with_weekend_compensation(d: date) -> tuple[date, ...]:
    """Ngày lễ + ngày nghỉ bù thứ Hai nếu ngày lễ rơi vào cuối tuần."""
    if d.weekday() == 5:
        return d, d + timedelta(days=2)  # thứ Bảy -> nghỉ bù thứ Hai
    if d.weekday() == 6:
        return d, d + timedelta(days=1)  # Chủ nhật -> nghỉ bù thứ Hai
    return (d,)


def _provisional_lunar_holidays(year: int) -> set[date]:
    """Dự phòng cho năm chưa có trong ``_LUNAR_HOLIDAYS``: CHỈ phần lõi năm nào
    cũng nghỉ - ngày thường từ đêm giao thừa (ngày liền trước mùng 1) tới mùng 3,
    và Giỗ Tổ 10/3 âm (cuối tuần -> nghỉ bù thứ Hai). Khung Tết thật do Chính
    phủ chốt từng năm (2025: 28 Chạp - mùng 3, 2027: 28 Chạp - mùng 5) nên bản
    này luôn là TẬP CON của lịch nghỉ thật: thiếu ngày thì gate chỉ chặt hơn,
    không bao giờ coi 1 phiên thật là ngày lễ."""
    from core.lunar_math import lunar_to_solar

    days: set[date] = set()
    tet_solar = lunar_to_solar(1, 1, year)
    if tet_solar is not None:
        tet = date(tet_solar[2], tet_solar[1], tet_solar[0])
        for offset in range(-1, 3):
            d = tet + timedelta(days=offset)
            if d.weekday() < 5:
                days.add(d)
    gio_to_solar = lunar_to_solar(10, 3, year)
    if gio_to_solar is not None:
        gio_to = date(gio_to_solar[2], gio_to_solar[1], gio_to_solar[0])
        days.update(_with_weekend_compensation(gio_to))
    return days


@lru_cache(maxsize=16)
def holidays_for_year(year: int) -> frozenset[date]:
    """Tập ngày nghỉ giao dịch (không kể cuối tuần) của 1 năm."""
    days: set[date] = set()
    for month, day in _FIXED_HOLIDAYS:
        days.update(_with_weekend_compensation(date(year, month, day)))
    for (month, day), since in _FIXED_HOLIDAYS_SINCE:
        if year >= since:
            days.update(_with_weekend_compensation(date(year, month, day)))
    if year in _LUNAR_HOLIDAYS:
        for text in _LUNAR_HOLIDAYS[year]:
            days.add(date.fromisoformat(text))
    else:
        days.update(_provisional_lunar_holidays(year))
        if year >= datetime.now(VN_TZ).year:
            logger.warning(
                "Lịch giao dịch chưa có bảng ngày lễ âm lịch năm %s - dùng bản dự phòng "
                "tính từ âm lịch (chỉ giao thừa-mùng 3 và Giỗ Tổ). Cập nhật "
                "_LUNAR_HOLIDAYS trong stock/calendar.py khi Sở công bố lịch nghỉ.",
                year,
            )
    return frozenset(days)


# Từ ngày này trong năm, bot nhắc chủ bot nếu năm sau chưa có bảng ngày lễ.
_REMIND_FROM_MONTH_DAY = (11, 1)


def missing_next_year(today: date) -> int | None:
    """Năm kế tiếp nếu đã từ 1/11 mà ``_LUNAR_HOLIDAYS`` chưa có năm đó (để nhắc
    chủ bot cập nhật); None nếu chưa tới lúc hoặc đã có bảng."""
    if (today.month, today.day) < _REMIND_FROM_MONTH_DAY:
        return None
    next_year = today.year + 1
    return None if next_year in _LUNAR_HOLIDAYS else next_year


def is_holiday(d: date) -> bool:
    return d in holidays_for_year(d.year) or d in _extra_holidays()


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_holiday(d)


def previous_trading_day(d: date) -> date:
    """Phiên giao dịch gần nhất TRƯỚC ``d`` (không tính chính ``d``)."""
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    """Phiên giao dịch đầu tiên SAU ``d`` (không tính chính ``d``)."""
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def trading_days_between(start: date, end: date) -> int:
    """Số phiên giao dịch trong khoảng (start, end] - tức số phiên đã diễn ra
    SAU ``start`` tới hết ``end``. Hai ngày cách nhau qua Tết cho 0 phiên."""
    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    # Trần vòng lặp: chuỗi dữ liệu không bao giờ cũ quá vài năm theo nghĩa có ích.
    limit = 0
    while d <= end and limit < 4000:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
        limit += 1
    return count


def missed_sessions(last_bar_date: date, now: datetime | date | None = None) -> int:
    """Số phiên đã kết thúc sau bar cuối mà chuỗi chưa có. Phiên hôm nay chỉ
    tính khi đã quá giờ đóng cửa (sau 15h) - trong giờ giao dịch dữ liệu hôm
    qua là bình thường, không phải "cũ"."""
    if now is None:
        now = datetime.now(VN_TZ)
    if isinstance(now, datetime):
        today = now.astimezone(VN_TZ).date() if now.tzinfo else now.date()
        minutes = now.hour * 60 + now.minute
        end = today if minutes >= MARKET_CLOSE_MINUTE else today - timedelta(days=1)
    else:
        today = now
        end = today
    return trading_days_between(last_bar_date, end)


def is_market_hours(now: datetime | None = None) -> bool:
    """T2-T6 không phải ngày lễ, 9h00-15h00 (gộp cả nghỉ trưa - chỉ để gắn
    nhãn hiển thị và loại nến đang hình thành, không phải khớp lệnh)."""
    now = now or datetime.now(VN_TZ)
    if not is_trading_day(now.date()):
        return False
    minutes = now.hour * 60 + now.minute
    return MARKET_OPEN_MINUTE <= minutes <= MARKET_CLOSE_MINUTE
