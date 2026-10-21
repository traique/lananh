"""Test services/tuvi.py: an lá số Tử Vi Đẩu Số bằng code."""

import datetime as dt
from types import SimpleNamespace

import pytest

from services import tuvi

DAN, MAO, THIN, TY_, NGO, THAN, DAU, TUAT, HOI, SUU = 2, 3, 4, 5, 6, 8, 9, 10, 11, 1


@pytest.mark.parametrize(
    "day,cuc,pos",
    [
        # Bảng an Tử Vi chuẩn: ngày 1 các cục.
        (1, 2, SUU),
        (2, 2, DAN),
        (3, 2, DAN),
        (4, 2, MAO),
        (1, 3, THIN),
        (2, 3, SUU),
        (3, 3, DAN),
        (1, 4, HOI),
        (1, 5, NGO),
        (1, 6, DAU),
        (29, 2, MAO),
        (30, 2, THIN),  # 30/2 = 15: từ Dần đếm tới 15 là Thìn
    ],
)
def test_vi_tri_tu_vi(day, cuc, pos):
    assert tuvi.tu_vi_position(day, cuc) == pos


def _star(chart, name):
    return next(p for p, names in chart.stars.items() if any(n.startswith(name) for n in names))


def test_la_so_15_3_1990_9h30_nam():
    c = tuvi.parse("15/3/1990 9h30 nam")
    # 19/2 Canh Ngọ, giờ Tỵ: Mệnh = Mão (tháng 2) lùi 5 = Tuất; Thân = Mão tiến 5 = Thân.
    assert (c.lunar_day, c.lunar_month, c.lunar_year, c.hour_chi) == (19, 2, 1990, 5)
    assert (c.menh, c.than) == (TUAT, THAN)
    assert c.palace_can(c.menh) == 2  # Bính Tuất -> Ốc Thượng Thổ
    assert c.cuc == 5
    assert _star(c, "Tử Vi") == THIN and _star(c, "Thiên Phủ") == 0
    assert _star(c, "Lộc Tồn") == THAN and _star(c, "Kình Dương") == DAU
    assert _star(c, "Thiên Khôi") == NGO and _star(c, "Thiên Việt") == DAN
    assert c.tu_hoa["Hóa Lộc"] == "Thái Dương" and c.tu_hoa["Hóa Kỵ"] == "Thiên Đồng"
    assert c.trang_sinh[THAN] == "Tràng Sinh"  # Thổ cục, dương nam thuận
    assert c.dai_han[TUAT] == (5, 14) and c.dai_han[HOI] == (15, 24)
    assert c.tuan == (TUAT, HOI)  # Canh Ngọ thuộc tuần Giáp Tý
    text = tuvi.format_chart(c)
    assert "Cục: Thổ ngũ cục" in text
    assert "▸ MỆNH (Bính Tuất) [Tuần] · ĐH 5-14" in text
    assert "📌 Phá Quân thủ Mệnh" in text
    assert sum(len(v) for v in c.stars.values()) > 80


def test_menh_than_thang_gieng_gio_ty_cung_o_dan():
    c = tuvi.build_chart(1, 1, 2000, 0, True)
    assert c.menh == c.than == DAN


def test_am_nu_di_nghich():
    c = tuvi.parse("âm 20/2/1991 giờ Tỵ nữ")  # Tân Mùi: âm nữ -> thuận
    assert c.thuan
    c2 = tuvi.parse("âm 20/2/1990 giờ Tỵ nữ")  # Canh Ngọ: dương nữ -> nghịch
    assert not c2.thuan
    assert c2.dai_han[c2.menh][0] == c2.cuc
    assert c2.dai_han[(c2.menh - 1) % 12][0] == c2.cuc + 10


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("", "Cú pháp"),
        ("15/3/1990 nam", "Thiếu giờ sinh"),
        ("15/3/1990 9h", "Thiếu giới tính"),
        ("1/1/2000 gio ty nam", "Giờ Tý hay giờ Tỵ"),
        ("31/2/1990 9h nam", "Ngày sinh không hợp lệ"),
        ("âm 30/2/2026 9h nam", "không có ngày 30"),
    ],
)
def test_parse_loi(arg, expected):
    assert expected in tuvi.parse(arg)


def test_thang_nhuan_va_gio_23():
    c = tuvi.parse("âm 20/6n/2025 23h nữ")
    assert c.leap and c.hour_chi == 0
    assert c.solar == dt.date(2025, 8, 13)


async def test_ai_chi_luan_giai_la_so_da_an(monkeypatch):
    from ai import orchestrator

    prompts = []

    async def fake_ask(prompt):
        prompts.append(prompt)
        return SimpleNamespace(text="Luận giải")

    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    c = tuvi.parse("15/3/1990 9h30 nam")
    assert await tuvi.explain_with_ai(c) == "Luận giải"
    assert "KHÔNG tự an lại" in prompts[0] and "Thổ ngũ cục" in prompts[0]
