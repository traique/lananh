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
    # Rút gọn từng từ: NGUYEN (Y kề nguyên âm -> phụ âm) 32->5, VAN 10->1, AN 6 -> 12 -> 3.
    assert (r.soul, r.personality, r.expression) == (1, 2, 3)
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
    # Chủ đạo 10: tuổi đỉnh 1 = 36 - 10 = 26, rồi 35, 44, 53 (kim tự tháp VN).
    assert [(s.from_age, s.to_age, s.peak_age) for s in r.pinnacles] == [
        (0, 26, 26),
        (27, 35, 35),
        (36, 44, 44),
        (45, None, 53),
    ]
    assert [s.contains(36) for s in r.pinnacles] == [False, False, True, False]
    assert r.maturity == 4  # 1 (chủ đạo rút gọn) + 3 (sứ mệnh)
    assert r.name_counts[5] == 5 and r.combined_counts[1] == 4
    text = numerology.format_reading(r)
    assert "Số trưởng thành (sau 35-40 tuổi): 4" in text
    assert "3. 36-44 tuổi, đỉnh ở tuổi 44: 7" in text and "◀ hiện tại" in text
    assert "Biểu đồ tổng hợp" in text


def test_tuoi_dinh_cao_theo_so_chu_dao():
    # Chủ đạo 11 -> tuổi đỉnh 25, 34, 43, 52 (tracuuthansohoc); 22 -> 4 -> 32; 33 -> 6 -> 30.
    r11 = numerology.compute(dt.date(1975, 12, 4), "", TODAY)
    assert r11.life_path == 11 and [s.peak_age for s in r11.pinnacles] == [25, 34, 43, 52]
    assert [numerology.first_peak_age(lp) for lp in (2, 9, 10, 11, 22, 33)] == [
        34,
        27,
        26,
        25,
        32,
        30,
    ]
    assert numerology.stage_label(numerology.Stage(1, 54, None, 61)) == "từ 54 tuổi, đỉnh ở tuổi 61"


def test_dinh_cao_1_2_rut_gon_han_dinh_3_4_giu_11():
    # 15/5/1949 (ví dụ Decoz): M=5, D=6, Y=5 -> đỉnh 1 = 11 -> 2 (VN rút gọn), đỉnh 3 = 2+2 = 4.
    r = numerology.compute(dt.date(1949, 5, 15), "", TODAY)
    assert [s.value for s in r.pinnacles] == [2, 2, 4, 1]
    # Đỉnh 3 = 11 được giữ: 29/9/1990: M=9, D=2, Y=1 -> 2, 3, 5, 1 (không có 11); 4/7/1981: M=7, D=4, Y=1 -> 2, 5, 7, 8
    r = numerology.compute(
        dt.date(1981, 2, 9), "", TODAY
    )  # M=2, D=9, Y=1981->19->1: P1 11->2, P2 10->1, P3 3, P4 3
    assert [s.value for s in r.pinnacles] == [2, 1, 3, 3]


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


def test_rut_gon_tung_tu_va_chu_y():
    # Ví dụ trên tracuuthansohoc.com: Lê Thị Hoài Lam -> linh hồn 22; Nguyễn Hoàng Minh -> sứ mệnh 22;
    # Trần Minh Hiếu -> nhân cách 5; Nguyen Minh Anh -> linh hồn 9 (Y trong NGUYEN là phụ âm).
    assert numerology.compute(dt.date(1990, 1, 1), "Lê Thị Hoài Lam", TODAY).soul == 22
    assert numerology.compute(dt.date(1990, 1, 1), "Nguyễn Hoàng Minh", TODAY).expression == 22
    assert numerology.compute(dt.date(1990, 1, 1), "Trần Minh Hiếu", TODAY).personality == 5
    assert numerology.compute(dt.date(1990, 1, 1), "Nguyen Minh Anh", TODAY).soul == 9
    # Y đứng một mình là nguyên âm (LY: Y=7); kề nguyên âm là phụ âm (HUY: nguyên âm chỉ U=3).
    assert numerology.compute(dt.date(1990, 1, 1), "Ly", TODAY).soul == 7
    assert numerology.compute(dt.date(1990, 1, 1), "Huy", TODAY).soul == 3
    assert numerology.compute(
        dt.date(1990, 1, 1), "Đỗ Yến", TODAY
    ).personality == numerology.reduce_number(
        numerology.reduce_number(4) + numerology.reduce_number(7 + 5)
    )  # Đ -> D=4; YEN: Y phụ âm 7, N 5


def test_so_ngay_sinh_rut_gon_ve_1_9():
    assert numerology.compute(dt.date(1990, 3, 29), "", TODAY).day_number == 2  # 29 -> 11 -> 2
    assert numerology.compute(dt.date(1990, 3, 22), "", TODAY).day_number == 4


def test_so_truong_thanh_giu_so_bac_thay():
    # 4/12/1975: chủ đạo 11 -> cộng 11 (không rút về 2) với số sứ mệnh.
    r = numerology.compute(dt.date(1975, 12, 4), "Lê An", TODAY)
    assert r.life_path == 11
    assert r.maturity == numerology.reduce_number(11 + r.expression)
    # Chủ đạo 10 tính là 1.
    r10 = numerology.compute(dt.date(1990, 3, 15), "Lê An", TODAY)
    assert r10.maturity == numerology.reduce_number(1 + r10.expression)


def test_mac_dinh_tinh_theo_gio_viet_nam(monkeypatch):
    class FakeDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            utc = dt.datetime(2026, 12, 31, 17, 30, tzinfo=dt.timezone.utc)  # 00:30 1/1/2027 VN
            return utc.astimezone(tz) if tz else utc.replace(tzinfo=None)

    monkeypatch.setattr(numerology.dt, "datetime", FakeDatetime)
    r = numerology.compute(dt.date(1990, 3, 15))
    assert (r.for_year, r.as_of, r.personal_year, r.personal_month) == (
        2027, dt.date(2027, 1, 1), 2, 3,
    )  # fmt: skip
