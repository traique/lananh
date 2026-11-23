"""Đối chiếu lịch âm, can chi, tiết khí, giờ hoàng đạo và tứ trụ với ca kiểm độc lập.

Ca kiểm: Nhật Nguyệt (nhatnguyet.org), CC BY 4.0 - bản trích gọn trong
test/data/nhatnguyet/ (sinh lại bằng scripts/make_golden_nhatnguyet.py). Đây là đáp án
của một engine khác viết độc lập (cùng thuật toán Hồ Ngọc Đức cho ngày âm, VSOP87 cho
tiết khí): chỗ hai bên chọn quy ước khác nhau được ghi ngay trong từng test.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from core import lunar_math
from services import kymon, lunar, solar_terms

DATA = Path(__file__).parent / "data" / "nhatnguyet"
VN = dt.timezone(dt.timedelta(hours=7))


def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def am_lich() -> dict:
    return _load("am-lich.json")


def test_doi_duong_sang_am_ba_mui_gio(am_lich):
    rows = am_lich["duongSangAm"]
    assert {r[3] for r in rows} == {7, 8, 9}
    bad = [
        r
        for r in rows
        if lunar_math.solar_to_lunar(r[2], r[1], r[0], float(r[3])) != (r[4], r[5], r[6], r[7])
    ]
    assert bad == []


def test_can_chi_va_tiet_khi_cua_ngay(am_lich):
    # Tiết khí của ca kiểm là tiết lúc 12h trưa; lunar.solar_term của bot lấy lúc hết
    # ngày (ngày giao tiết mang tên tiết mới, như lịch Hồ Ngọc Đức). Nên so cách lấy
    # trưa bằng solar_terms.term_at - cùng module kinh độ Mặt Trời.
    bad = []
    for y, m, d, _tz, _ld, lm, ly, _leap, nam, thang, ngay, tiet in (
        r for r in am_lich["duongSangAm"] if r[3] == 7
    ):
        noon = dt.datetime(y, m, d, 12, tzinfo=VN)
        got = (
            lunar.year_can_chi(ly),
            lunar.month_can_chi(lm, ly),
            lunar.day_can_chi(d, m, y),
            solar_terms.term_at(noon).name.casefold(),
        )
        if got != (nam, thang, ngay, tiet.casefold()):
            bad.append((y, m, d, got))
    assert bad == []


def test_doi_am_sang_duong(am_lich):
    bad = []
    for nam, thang, ngay, nhuan, tz, hop_le, y, m, d in am_lich["amSangDuong"]:
        got = lunar_math.lunar_to_solar(ngay, thang, nam, nhuan, float(tz))
        expected = (d, m, y) if hop_le else None
        if got == expected:
            continue
        # Nhật Nguyệt coi "tháng X nhuận" của năm không nhuận tháng X là tháng thường;
        # bot báo không có tháng nhuận đó (None) - chặt hơn, có chủ đích.
        regular = lunar_math.lunar_to_solar(ngay, thang, nam, False, float(tz))
        if nhuan and got is None and regular == expected:
            continue
        bad.append((nam, thang, ngay, nhuan, tz, got, expected))
    assert bad == []


def test_moc_tiet_khi_lech_khong_qua_1_phut(am_lich):
    # Ca kiểm làm tròn tới phút; module của bot lệch IMCCE trung bình 7 giây, tối đa 24.
    worst = 0.0
    for year, index, moment in am_lich["mocTietKhi"]:
        expected = dt.datetime.fromisoformat(moment).replace(tzinfo=VN)
        start = solar_terms.term_start(index, year)
        if abs((start - expected).total_seconds()) > 40 * 86400:  # tiết đầu năm sau
            start = solar_terms.term_start(index, year + 1)
        worst = max(worst, abs((start - expected).total_seconds()))
    assert worst <= 90  # phút làm tròn (30 s) + sai số hai engine


def test_gio_hoang_dao():
    data = _load("gio-hoang-dao.json")
    bad = []
    for y, m, d, can_chi_ngay, bits, _gods in data["ca"]:
        mine = {h.split(" ")[0] for h in lunar.lucky_hours(d, m, y)}
        theirs = {lunar.CHI[i] for i, bit in enumerate(bits) if bit == "1"}
        if mine != theirs or lunar.day_can_chi(d, m, y) != can_chi_ngay:
            bad.append((y, m, d))
    assert bad == []


def test_tu_tru_ky_mon_khop_bat_tu():
    # Mặc định Nhật Nguyệt đổi trụ ngày lúc 23h (next-day-from-23) như Kỳ Môn của bot.
    data = _load("bat-tu.json")
    checked = 0
    for case in data["ca"]:
        vao, la_so = case["vao"], case["ra"]["laSo"]
        if "gio" not in vao:
            continue
        moment = dt.datetime(vao["yyyy"], vao["mm"], vao["dd"], vao["gio"], vao["phut"], tzinfo=VN)
        chart = kymon.build_chart(moment)
        got = [kymon.pillar_text(p) for p in (chart.year, chart.month, chart.day, chart.hour)]
        expected = [la_so[k]["label"] for k in ("truNam", "truThang", "truNgay", "truGio")]
        assert got == expected, (vao, got, expected)
        checked += 1
    assert checked >= 20
