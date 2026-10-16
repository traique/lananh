from datetime import date, datetime

from stock import calendar as cal
from stock import validation as val


def test_tet_2026_khong_phai_phien_giao_dich():
    assert not cal.is_trading_day(date(2026, 2, 17))  # mùng 1 Tết
    assert cal.is_trading_day(date(2026, 2, 23))  # thứ Hai sau Tết
    assert not cal.is_trading_day(date(2026, 2, 21))  # thứ Bảy


def test_le_duong_lich_roi_cuoi_tuan_nghi_bu_thu_hai():
    # 1/1/2028 là thứ Bảy -> nghỉ bù thứ Hai 3/1/2028.
    assert not cal.is_trading_day(date(2028, 1, 1))
    assert not cal.is_trading_day(date(2028, 1, 3))
    assert cal.is_trading_day(date(2028, 1, 4))


def test_trading_days_between_qua_tet_bang_khong():
    # Phiên cuối 13/2/2026 (T6), hôm nay 22/2/2026 (CN): 0 phiên bị thiếu.
    assert cal.trading_days_between(date(2026, 2, 13), date(2026, 2, 22)) == 0
    # Tuần thường: T6 6/3 -> CN 15/3 = 5 phiên (9-13/3).
    assert cal.trading_days_between(date(2026, 3, 6), date(2026, 3, 15)) == 5


def test_missed_sessions_khong_tinh_phien_hom_nay_truoc_gio_dong_cua():
    last = date(2026, 3, 12)  # thứ Năm
    in_session = datetime(2026, 3, 13, 10, 0, tzinfo=cal.VN_TZ)
    after_close = datetime(2026, 3, 13, 15, 30, tzinfo=cal.VN_TZ)
    assert cal.missed_sessions(last, in_session) == 0
    assert cal.missed_sessions(last, after_close) == 1


def test_validate_ohlcv_khong_coi_nghi_tet_la_du_lieu_cu():
    n = 40
    closes = [10.0 + i * 0.01 for i in range(n)]
    # Phiên cuối 13/2/2026, kiểm tra ngày 23/2/2026 lúc 8h (10 ngày lịch, 0 phiên thiếu).
    dates = [f"2026-01-{d:02d}" for d in range(1, 32)] + [
        "2026-02-02",
        "2026-02-03",
        "2026-02-04",
        "2026-02-05",
        "2026-02-06",
        "2026-02-09",
        "2026-02-10",
        "2026-02-12",
        "2026-02-13",
    ]
    assert len(dates) == n
    now = datetime(2026, 2, 23, 8, 0, tzinfo=cal.VN_TZ)
    q = val.validate_ohlcv(closes, closes, closes, closes, dates, now=now)
    assert q.is_stale is False
    # Tuần thường cùng khoảng cách lịch -> cũ.
    dates2 = [f"2026-03-{d:02d}" for d in range(1, 32)] + [
        "2026-04-01",
        "2026-04-02",
        "2026-04-03",
        "2026-04-06",
        "2026-04-07",
        "2026-04-08",
        "2026-04-09",
        "2026-04-10",
        "2026-04-13",
    ]
    now2 = datetime(2026, 4, 23, 8, 0, tzinfo=cal.VN_TZ)
    q2 = val.validate_ohlcv(closes, closes, closes, closes, dates2, now=now2)
    assert q2.is_stale is True


def test_is_market_hours_ngay_le():
    assert not cal.is_market_hours(datetime(2026, 4, 30, 10, 0, tzinfo=cal.VN_TZ))
    assert cal.is_market_hours(datetime(2026, 4, 29, 10, 0, tzinfo=cal.VN_TZ))


def test_extra_holidays_env(monkeypatch):
    monkeypatch.setenv("STOCK_EXTRA_HOLIDAYS", "2026-06-03, rac")
    assert not cal.is_trading_day(date(2026, 6, 3))
    assert cal.is_trading_day(date(2026, 6, 4))
