"""Test services/lunar.py: thuật toán âm lịch Hồ Ngọc Đức (offline)."""

import datetime as dt

from services import lunar

TET = {
    2020: (25, 1, 2020),
    2021: (12, 2, 2021),
    2022: (1, 2, 2022),
    2023: (22, 1, 2023),
    2024: (10, 2, 2024),
    2025: (29, 1, 2025),
    2026: (17, 2, 2026),
    2027: (6, 2, 2027),
    2028: (26, 1, 2028),
}


def test_tet_2020_2028_dung_ngay():
    for year, solar in TET.items():
        assert lunar.lunar_to_solar(1, 1, year) == solar
        assert lunar.solar_to_lunar(*solar) == (1, 1, year, False)


def test_doi_qua_lai_am_duong_khop_1900_2100():
    # Kiểm cả 1 <= ngày âm <= 30: "ngày 0" (lỗi cũ 07/05/2054) đổi ngược vẫn ra đúng
    # ngày dương nên chỉ kiểm round-trip thì không bắt được.
    d, bad = dt.date(1900, 1, 1), []
    while d <= dt.date(2100, 12, 31):
        ld, lm, ly, leap = lunar.solar_to_lunar(d.day, d.month, d.year)
        if not 1 <= ld <= 30 or lunar.lunar_to_solar(ld, lm, ly, leap) != (d.day, d.month, d.year):
            bad.append(d)
        d += dt.timedelta(days=1)
    assert bad == []


def test_uoc_thang_soc_vuot_hai_ky_khong_ra_ngay_0():
    # Bản gốc Hồ Ngọc Đức chỉ lùi 1 tháng nên ra "ngày 0" ở đúng 2 ngày 1900-2100 (bài báo
    # của Nhật Nguyệt, docs/paper/hnd_compare.py). Tháng 3/2054 và tháng 2/2062 đủ 30 ngày.
    assert lunar.solar_to_lunar(7, 5, 2054) == (30, 3, 2054, False)
    assert lunar.solar_to_lunar(9, 4, 2062) == (30, 2, 2062, False)
    today = dt.date(2026, 11, 23)
    assert lunar.find_lunar_date(30, 3, 2054, False, today) == (dt.date(2054, 5, 7), 2054, "")
    assert lunar.date_info(dt.date(2054, 5, 7))["lunar"]["text"] == "ngày 30 tháng 3 năm Giáp Tuất"


def test_mui_gio_lich_trung_quoc():
    # Cùng quy tắc, xét ngày sóc theo UTC+8: Tết 1985 lệch hẳn 1 tháng, Tết 2030 lệch 1 ngày.
    assert lunar.lunar_to_solar(1, 1, 1985) == (21, 1, 1985)
    assert lunar.lunar_to_solar(1, 1, 1985, tz=8.0) == (20, 2, 1985)
    assert lunar.lunar_to_solar(1, 1, 2030) == (2, 2, 2030)
    assert lunar.lunar_to_solar(1, 1, 2030, tz=8.0) == (3, 2, 2030)


def test_luu_y_lich_cu_truoc_1968():
    # Tết Ất Tỵ 1965: giờ VN ra 1/2, lịch theo UTC+8 ra 2/2 (ngày miền Bắc đã đón).
    note = lunar.old_calendar_note(1, 2, 1965)
    assert note and "ngày 30 tháng 12 năm Giáp Thìn" in note and note.startswith("Trước 1968")
    assert "UTC+8" in note and "Bắc Kinh" not in note  # chữ lên web: không nhắc nước khác
    # Tết Quý Mão 1903: sóc 16:39 UT = 00:25 hôm sau theo kinh tuyến 116°25′ Đông.
    assert "116°25′" in lunar.old_calendar_note(28, 1, 1903)
    assert lunar.old_calendar_note(3, 2, 1916) is None  # sóc 23:51 theo kinh tuyến cũ: trùng
    # Tết Mậu Thân 1968: miền Bắc 29/1 (UTC+7), miền Nam 30/1.
    assert "miền Nam" in lunar.old_calendar_note(29, 1, 1968)
    assert lunar.old_calendar_lunar(2, 2, 1965) == (1, 1, 1965, False)
    assert lunar.old_calendar_note(15, 6, 1965) is None  # hai cách tính trùng nhau
    assert lunar.old_calendar_note(21, 1, 1985) is None  # từ 1976 chỉ còn lịch UTC+7


def test_thang_nhuan():
    assert lunar.leap_month_of(2023) == 2
    assert lunar.leap_month_of(2025) == 6
    assert lunar.leap_month_of(2026) == 0
    assert lunar.lunar_to_solar(1, 3, 2025, True) is None


def test_can_chi_tiet_khi_gio_hoang_dao():
    assert lunar.year_can_chi(2024) == "Giáp Thìn"
    assert lunar.year_can_chi(2026) == "Bính Ngọ"
    assert lunar.day_can_chi(1, 1, 2000) == "Mậu Ngọ"
    assert lunar.month_can_chi(1, 2026) == "Canh Dần"
    assert lunar.solar_term(22, 12, 2026) == "Đông chí"
    assert lunar.solar_term(4, 2, 2026) == "Lập xuân"


def test_tiet_khi_theo_ngay_dung_gio_giao_chinh_xac():
    # Công thức xấp xỉ cũ lệch ~15 phút nên đặt sai ngày giao tiết ở 37 ngày 1900-2100.
    # Sương giáng 2019 bắt đầu 24/10 00:19 giờ VN: hết ngày 23/10 vẫn là Hàn lộ.
    assert lunar.solar_term(23, 10, 2019) == "Hàn lộ"
    assert lunar.solar_term(24, 10, 2019) == "Sương giáng"
    # Thanh minh 2034 bắt đầu 05/04 00:06 giờ VN: hết ngày 04/04 vẫn là Xuân phân.
    assert lunar.solar_term(4, 4, 2034) == "Xuân phân"
    assert lunar.solar_term(5, 4, 2034) == "Thanh minh"
    hours = lunar.lucky_hours(21, 10, 2026)
    assert len(hours) == 6 and hours[0].startswith("Dần")


def test_date_info_va_describe():
    info = lunar.date_info(dt.date(2026, 9, 2))
    assert info["weekday"] == "Thứ Tư"
    assert info["lunar"]["text"] == "ngày 21 tháng 7 năm Bính Ngọ"
    assert info["next_tet"] == "2027-02-06"
    text = lunar.describe_date(lunar.date_info(dt.date(2026, 10, 21)))
    assert "ngày 12 tháng 9 năm Bính Ngọ" in text
    assert "Còn 108 ngày tới Tết" in text


def test_hom_nay_la_tet_thi_con_0_ngay():
    assert lunar.date_info(dt.date(2026, 2, 17))["days_to_tet"] == 0
