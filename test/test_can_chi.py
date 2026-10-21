"""Test services/can_chi.py: nạp âm, hợp xung, sao hạn, Tam tai, Kim lâu, Hoang ốc,
ngày hoàng đạo (tra bảng, không AI)."""

import datetime as dt

import pytest

from services import can_chi


@pytest.mark.parametrize(
    "year,can_chi_name,nap_am",
    [
        (1984, "Giáp Tý", "Hải Trung Kim"),
        (1986, "Bính Dần", "Lô Trung Hỏa"),
        (1990, "Canh Ngọ", "Lộ Bàng Thổ"),
        (1995, "Ất Hợi", "Sơn Đầu Hỏa"),
        (2000, "Canh Thìn", "Bạch Lạp Kim"),
        (2026, "Bính Ngọ", "Thiên Hà Thủy"),
        (2043, "Quý Hợi", "Đại Hải Thủy"),
    ],
)
def test_nap_am(year, can_chi_name, nap_am):
    p = can_chi.year_profile(year)
    assert p["can_chi"] == can_chi_name
    assert p["nap_am"] == nap_am


def test_sinh_truoc_tet_thuoc_nam_am_truoc():
    assert can_chi.lunar_year_of(dt.date(1990, 1, 15)) == 1989
    assert can_chi.lunar_year_of(dt.date(1990, 3, 15)) == 1990


def test_hop_xung():
    r = can_chi.chi_relations(6)  # Ngọ
    assert r["tam_hop"] == ["Dần", "Tuất"]
    assert r["luc_hop"] == ["Mùi"]
    assert r["xung"] == ["Tý"]
    assert "lục xung" in can_chi.compare_years(1990, 1984)  # Ngọ - Tý
    assert "tam hợp" in can_chi.compare_years(1990, 1986)  # Ngọ - Dần
    assert "Hỏa sinh Thổ" in can_chi.compare_years(1990, 1995)


@pytest.mark.parametrize(
    "age,male,star",
    [
        (10, True, "La Hầu"),
        (10, False, "Kế Đô"),
        (18, True, "Mộc Đức"),
        (18, False, "Thủy Diệu"),
        (25, True, "Kế Đô"),
        (25, False, "Thái Dương"),
    ],  # fmt: skip
)
def test_sao_han(age, male, star):
    assert can_chi.sao_han(age, male).startswith(star)


def test_tam_tai_kim_lau_hoang_oc():
    assert can_chi.tam_tai_years(0) == (2, 3, 4)  # tuổi Tý: Dần Mão Thìn
    assert can_chi.tam_tai_years(6) == (8, 9, 10)  # tuổi Ngọ: Thân Dậu Tuất
    assert can_chi.kim_lau(28) is not None  # 28 % 9 = 1 -> Kim lâu Thân
    assert can_chi.kim_lau(29) is None
    assert can_chi.hoang_oc(10) == "Nhất Cát"
    assert can_chi.hoang_oc(11) == "Nhì Nghi"
    assert can_chi.hoang_oc(25) == "Nhất Cát"


def test_ngay_hoang_dao_theo_thang_am():
    # 21/10/2026: tháng 9 âm (Thanh Long khởi ở Thìn), ngày Mậu Thìn -> Thanh Long.
    assert can_chi.day_god(dt.date(2026, 10, 21)) == ("Thanh Long", True)
    # 24/10/2026: ngày Tân Mùi -> Chu Tước (hắc đạo).
    assert can_chi.day_god(dt.date(2026, 10, 24)) == ("Chu Tước", False)
    assert "Tam nương" in can_chi.day_quality(dt.date(2026, 10, 22))  # 13 âm
