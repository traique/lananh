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


def test_ngay_van_hoa_24_11_tu_nam_2026():
    # Nghị quyết 28/2026/QH16: 24/11 hằng năm là ngày nghỉ, hiệu lực 1/7/2026.
    assert not cal.is_trading_day(date(2026, 11, 24))  # thứ Ba
    assert cal.is_trading_day(date(2026, 11, 23))
    assert cal.is_trading_day(date(2025, 11, 24))  # trước khi có ngày lễ mới
    # 24/11/2029 là thứ Bảy -> nghỉ bù thứ Hai 26/11.
    assert not cal.is_trading_day(date(2029, 11, 26))


def test_bang_nghi_2027_theo_thong_bao_bo_noi_vu():
    for day in (4, 5, 8, 9, 10):
        assert not cal.is_trading_day(date(2027, 2, day))
    # 11-12/2/2027 vẫn là phiên giao dịch (Tết chỉ tới mùng 5, không hoán đổi).
    assert cal.is_trading_day(date(2027, 2, 11))
    assert cal.is_trading_day(date(2027, 2, 12))
    assert cal.is_trading_day(date(2027, 2, 3))
    assert not cal.is_trading_day(date(2027, 4, 16))  # Giỗ Tổ
    assert not cal.is_trading_day(date(2027, 9, 3))  # nghỉ thêm dịp Quốc khánh
    # 1/5/2027 thứ Bảy -> nghỉ bù thứ Hai 3/5 (đã tính sẵn từ ngày lễ cố định).
    assert not cal.is_trading_day(date(2027, 5, 3))


def test_nam_chua_co_bang_dung_ban_du_phong_chi_phan_loi():
    # Tết Mậu Thân: mùng 1 = thứ Tư 26/1/2028 -> lõi 25/1 (giao thừa) .. 28/1 (mùng 3).
    for day in (25, 26, 27, 28):
        assert not cal.is_trading_day(date(2028, 1, day))
    # 28 Chạp và mùng 4-5 không đoán (đánh dấu nhầm phiên thật tệ hơn thiếu ngày lễ).
    assert cal.is_trading_day(date(2028, 1, 24))
    assert cal.is_trading_day(date(2028, 1, 31))
    # Giỗ Tổ 10/3 âm năm 2028 rơi thứ Ba 4/4.
    assert not cal.is_trading_day(date(2028, 4, 4))


def test_ban_du_phong_luon_la_tap_con_cua_bang_that():
    for year in (2024, 2025, 2026, 2027):
        # So ngày thường (ngày lễ rơi cuối tuần vốn không có phiên).
        provisional = {d for d in cal._provisional_lunar_holidays(year) if d.weekday() < 5}
        official = cal.holidays_for_year(year)
        assert provisional <= official, year


def test_nhac_cap_nhat_lich_nam_sau_tu_1_11():
    assert cal.missing_next_year(date(2026, 10, 31)) is None
    assert cal.missing_next_year(date(2026, 11, 1)) is None  # đã có bảng 2027
    assert cal.missing_next_year(date(2027, 10, 31)) is None
    assert cal.missing_next_year(date(2027, 11, 1)) == 2028
    assert cal.missing_next_year(date(2027, 12, 31)) == 2028
