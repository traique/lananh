"""Lịch giao dịch HOSE/HNX/UPCOM: cuối tuần + ngày lễ nghỉ giao dịch.

Dùng cho: gate "dữ liệu cũ" (đếm PHIÊN bị thiếu thay vì ngày lịch - Tết nghỉ
9-10 ngày không được coi là dữ liệu cũ), nhãn "trong giờ giao dịch" (ngày lễ
không có phiên) và dò ngày GDKHQ theo phiên giao dịch.

Ngày lễ âm lịch (Tết, Giỗ Tổ) không tính được bằng công thức đơn giản nên ghi
bảng theo năm - cập nhật mỗi năm khi Sở công bố lịch nghỉ. Năm chưa có trong
bảng chỉ làm gate hơi CHẶT hơn (coi ngày lễ là phiên bị thiếu), không nới
lỏng. Có thể bổ sung qua env ``STOCK_EXTRA_HOLIDAYS=YYYY-MM-DD,YYYY-MM-DD``.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Ngày lễ dương lịch cố định (tháng, ngày). Rơi vào cuối tuần thì nghỉ bù thứ
# Hai kế tiếp theo quy định Bộ luật Lao động - Sở theo lịch nghỉ nhà nước.
_FIXED_HOLIDAYS = ((1, 1), (4, 30), (5, 1), (9, 2))

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


@lru_cache(maxsize=16)
def holidays_for_year(year: int) -> frozenset[date]:
    """Tập ngày nghỉ giao dịch (không kể cuối tuần) của 1 năm."""
    days: set[date] = set()
    for month, day in _FIXED_HOLIDAYS:
        d = date(year, month, day)
        days.add(d)
        if d.weekday() == 5:
            days.add(d + timedelta(days=2))  # thứ Bảy -> nghỉ bù thứ Hai
        elif d.weekday() == 6:
            days.add(d + timedelta(days=1))  # Chủ nhật -> nghỉ bù thứ Hai
    for text in _LUNAR_HOLIDAYS.get(year, ()):
        days.add(date.fromisoformat(text))
    return frozenset(days)


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
