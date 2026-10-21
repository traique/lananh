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


def test_doi_qua_lai_am_duong_khop_1950_2059():
    d, bad = dt.date(1950, 1, 1), []
    while d < dt.date(2060, 1, 1):
        ld, lm, ly, leap = lunar.solar_to_lunar(d.day, d.month, d.year)
        if lunar.lunar_to_solar(ld, lm, ly, leap) != (d.day, d.month, d.year):
            bad.append(d)
        d += dt.timedelta(days=1)
    assert bad == []


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
