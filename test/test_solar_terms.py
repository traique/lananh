"""Test services/solar_terms.py: thời khắc tiết khí chính xác tới phút."""

import datetime as dt

import pytest

from services import lunar, solar_terms

UTC = dt.UTC
BJ = dt.timezone(dt.timedelta(hours=8))
VN = dt.timezone(dt.timedelta(hours=7))


@pytest.mark.parametrize(
    "index,year,expected",
    [
        # Xuân phân / Hạ chí / Thu phân / Đông chí theo USNO (giờ UTC, làm tròn phút).
        (0, 2023, "2023-03-20 21:24"), (6, 2023, "2023-06-21 14:57"),
        (12, 2023, "2023-09-23 06:50"), (18, 2023, "2023-12-22 03:27"),
        (0, 2024, "2024-03-20 03:06"), (6, 2024, "2024-06-20 20:51"),
        (12, 2024, "2024-09-22 12:44"), (18, 2024, "2024-12-21 09:20"),
        (0, 2025, "2025-03-20 09:01"), (6, 2025, "2025-06-21 02:42"),
        (12, 2025, "2025-09-22 18:19"), (18, 2025, "2025-12-21 15:03"),
        (0, 2026, "2026-03-20 14:46"), (6, 2026, "2026-06-21 08:24"),
        (12, 2026, "2026-09-23 00:05"), (18, 2026, "2026-12-21 20:50"),
    ],
)  # fmt: skip
def test_phan_chi_khop_usno_trong_1_phut(index, year, expected):
    got = solar_terms.term_start(index, year)
    want = dt.datetime.strptime(expected, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    assert abs((got - want).total_seconds()) <= 60


@pytest.mark.parametrize(
    "year,expected",
    [
        # Lập xuân theo Đài thiên văn Hồng Kông (giờ Bắc Kinh UTC+8).
        (2024, "2024-02-04 16:27"),
        (2025, "2025-02-03 22:10"),
        (2026, "2026-02-04 04:02"),
    ],
)
def test_lap_xuan(year, expected):
    want = dt.datetime.strptime(expected, "%Y-%m-%d %H:%M").replace(tzinfo=BJ)
    assert abs((solar_terms.li_chun(year) - want).total_seconds()) <= 60


def test_tieu_thu_1998_khop_moc_cua_sach():
    # Thực lệ "Thần kỳ chi môn" 1998-07-07: Tiểu thử giao 15:30:25 giờ Bắc Kinh.
    got = solar_terms.term_start(7, 1998).astimezone(BJ)
    assert got.strftime("%Y-%m-%d %H:%M") == "1998-07-07 15:30"


def test_term_at_doi_dung_thoi_khac_giao_tiet():
    start = solar_terms.term_start(18, 2026)  # Đông chí 2026-12-21 20:50 UTC
    before = solar_terms.term_at(start - dt.timedelta(minutes=1))
    after = solar_terms.term_at(start + dt.timedelta(minutes=1))
    assert before.name == "Đại tuyết" and after.name == "Đông chí"
    assert after.start == start and before.end == start
    assert after.end == solar_terms.term_start(19, 2027)
    assert after.longitude == 270 and after.index == 18


def test_term_at_dung_nhu_lich_ngay_o_giua_tiet():
    # Giữa tiết (không phải ngày giao) thì trùng services.lunar.solar_term.
    for day in (dt.date(2026, 1, 12), dt.date(2026, 5, 13), dt.date(2026, 10, 28)):
        noon = dt.datetime(day.year, day.month, day.day, 12, tzinfo=VN)
        assert solar_terms.term_at(noon).name == lunar.solar_term(day.day, day.month, day.year)


def test_tiet_dau_nam_thuoc_dung_nam_duong():
    # Tiểu hàn/Đại hàn/Lập xuân/Vũ thủy/Kinh trập rơi tháng 1-3 của chính năm đó.
    for index, month in ((19, 1), (20, 1), (21, 2), (22, 2), (23, 3)):
        assert solar_terms.term_start(index, 2026).month == month
    moment = dt.datetime(2026, 1, 2, tzinfo=VN)
    term = solar_terms.term_at(moment)
    assert term.name == "Đông chí" and term.start.year == 2025


def test_can_mui_gio_va_khoang_hop_le():
    with pytest.raises(ValueError):
        solar_terms.jd_from_datetime(dt.datetime(2026, 1, 1))
    # Biên phạm vi web (1900-2100) vẫn tính được và tăng đều 15° mỗi tiết.
    for year in (1900, 2100):
        starts = [solar_terms.term_start(i, year) for i in range(24)]
        assert all(s.year in (year, year - 1, year + 1) for s in starts)
    assert 0 <= solar_terms.apparent_longitude(2451545.0) < 360
