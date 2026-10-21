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
