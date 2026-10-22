"""Test services/numerology.py (/tsh)."""

import datetime as dt

import pytest

from services import numerology

TODAY = dt.date(2026, 10, 21)


@pytest.mark.parametrize(
    "birth,expected",
    [
        (dt.date(1990, 3, 15), 10),  # 1+5+3+1+9+9+0 = 28 -> 10
        (dt.date(1992, 11, 29), 7),  # 34 -> 7
        (dt.date(1985, 9, 29), 7),  # 43 -> 7
        (dt.date(1975, 12, 4), 11),  # 29 -> 11
    ],
)
def test_con_so_chu_dao(birth, expected):
    assert numerology.life_path(birth) == expected


def test_giu_so_bac_thay():
    assert numerology.life_path(dt.date(1983, 9, 2)) == 5  # 0+2+0+9+1+9+8+3 = 32 -> 5
    assert numerology.life_path(dt.date(1979, 2, 9)) == 10  # 37 -> 10
    assert numerology.reduce_number(29) == 11
    assert numerology.reduce_number(22) == 22
    assert numerology.reduce_number(22, keep_master=False) == 4


def test_ten_va_bieu_do():
    r = numerology.compute(dt.date(1990, 3, 15), "Nguyễn Văn An", TODAY)
    assert (r.soul, r.personality, r.expression) == (8, 4, 3)
    assert r.counts[1] == 2 and r.counts[9] == 2 and r.counts[2] == 0
    assert r.arrows == ["1-5-9"]
    assert r.personal_year == 1  # 1+5 + 3 + 2+0+2+6 = 19 -> 1
    text = numerology.format_reading(r)
    assert "Con số chủ đạo: 10" in text
    assert "Mũi tên Quyết tâm" in text
    assert "Số thiếu: 2, 4, 6, 7, 8" in text


def test_mui_ten_trong():
    r = numerology.compute(dt.date(2000, 1, 1), "", TODAY)  # chữ số: 1,1,2
    assert "7-8-9" in r.empty_arrows and "3-6-9" in r.empty_arrows


def test_parse():
    assert numerology.parse("abc") == numerology.USAGE
    assert numerology.parse("31/2/1990").startswith("Ngày sinh không hợp lệ")
    assert numerology.parse("15/03/1990  Trần Thị B ") == (dt.date(1990, 3, 15), "Trần Thị B")


def test_file_y_nghia_du_so():
    texts = numerology._texts()
    for n in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 22, 33):
        assert texts["chu_dao"][n]
    for n in (*range(1, 12), 22, 33):
        assert texts["so"][n]
    assert set(texts["mui_ten"]) == set(numerology._ARROWS)


async def test_ai_chi_dien_giai_so_da_tinh(monkeypatch):
    from types import SimpleNamespace

    from ai import orchestrator

    prompts = []

    async def fake_ask(prompt):
        prompts.append(prompt)
        return SimpleNamespace(text="Diễn giải")

    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    r = numerology.compute(dt.date(1990, 3, 15), "", TODAY)
    assert await numerology.explain_with_ai(r) == "Diễn giải"
    assert "KHÔNG tự tính lại" in prompts[0] and "Con số chủ đạo: 10" in prompts[0]


def test_dinh_cao_thu_thach_truong_thanh():
    # 15/3/1990: ngày 6, tháng 3, năm 1+9+9+0=19 -> 1; chủ đạo 10 -> bản rút gọn 1.
    r = numerology.compute(dt.date(1990, 3, 15), "Nguyễn Văn An", TODAY)
    assert r.life_path_single == 1 and r.age == 36
    assert [s.value for s in r.pinnacles] == [9, 7, 7, 4]
    assert [s.value for s in r.challenges] == [3, 5, 2, 2]
    assert [(s.from_age, s.to_age) for s in r.pinnacles] == [
        (0, 35),
        (36, 44),
        (45, 53),
        (54, None),
    ]
    assert [s.contains(36) for s in r.pinnacles] == [False, True, False, False]
    assert r.maturity == 4  # 1 (chủ đạo rút gọn) + 3 (sứ mệnh)
    assert r.name_counts[5] == 5 and r.combined_counts[1] == 4
    text = numerology.format_reading(r)
    assert "Số trưởng thành (sau 35-40 tuổi): 4" in text
    assert "2. 36-44 tuổi: 7" in text and "◀ hiện tại" in text
    assert "Biểu đồ tổng hợp" in text


def test_tuoi_dinh_cao_theo_so_chu_dao():
    # Chủ đạo 11 -> rút gọn 2 -> đỉnh 1 tới 34 tuổi; 22 -> 4 -> tới 32 tuổi.
    r11 = numerology.compute(dt.date(1975, 12, 4), "", TODAY)
    assert r11.life_path == 11 and r11.pinnacles[0].to_age == 34
    r22 = numerology.compute(dt.date(1984, 8, 2), "", TODAY)  # 0+2+0+8+1+9+8+4 = 32 -> 5
    assert r22.life_path == 5
    assert numerology._stages([1, 2, 3, 4], 4)[0].to_age == 32
    assert numerology.stage_label(numerology.Stage(1, 54, None)) == "từ 54 tuổi"


def test_khong_ten_thi_khong_co_so_theo_ten():
    r = numerology.compute(dt.date(1990, 3, 15), "", TODAY)
    assert r.maturity is None and r.name_counts is None and r.combined_counts is None
    assert len(r.pinnacles) == 4 and len(r.challenges) == 4


def test_file_y_nghia_giai_doan():
    texts = numerology._texts()
    for n in (*range(1, 10), 11, 22):
        assert texts["dinh_cao"][n]
    for n in range(0, 9):
        assert texts["thu_thach"][n]


def test_so_truong_thanh_giu_so_bac_thay():
    # 4/12/1975: chủ đạo 11 -> cộng 11 (không rút về 2) với số sứ mệnh.
    r = numerology.compute(dt.date(1975, 12, 4), "Lê An", TODAY)
    assert r.life_path == 11
    assert r.maturity == numerology.reduce_number(11 + r.expression)
    # Chủ đạo 10 tính là 1.
    r10 = numerology.compute(dt.date(1990, 3, 15), "Lê An", TODAY)
    assert r10.maturity == numerology.reduce_number(1 + r10.expression)
