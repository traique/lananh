"""Test services/le_hoi.py: 206 lễ hội Việt Nam (bộ dữ liệu le-hoi-viet-nam).

Ca kiểm ngày theo năm: Nhật Nguyệt (nhatnguyet.org), CC BY 4.0 - bản trích ở
test/data/nhatnguyet/le-hoi.json (scripts/make_golden_nhatnguyet.py).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from services import le_hoi

GOLDEN = Path(__file__).parent / "data" / "nhatnguyet" / "le-hoi.json"


def test_ngay_theo_nam_khop_ca_kiem():
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["ca"]
    assert len(cases) >= 8
    for case in cases:
        year = case["vao"]["nam"]
        got = [{"id": f.id, "ngay": day.isoformat()} for f, day in le_hoi.occurrences(year)]
        assert got == case["ra"], year


def test_du_lieu_du_206_le_va_34_tinh():
    items = le_hoi.festivals()
    assert len(items) == 206 and len({f.id for f in items}) == 206
    assert len(le_hoi.provinces()) == 34
    assert sum(f.kind == "theo-nam" for f in items) == 42
    lim = le_hoi.get("hoi-lim")
    assert lim and lim.provinces == ("Bắc Ninh",) and (lim.day, lim.month) == (13, 1)
    assert lim.date_label() == "13 tháng Giêng âm lịch"


def test_tim_theo_ten_khong_dau_va_ten_ngan():
    assert le_hoi.find("hoi lim nam nay ngay nao").id == "hoi-lim"
    assert le_hoi.find("le hoi chua huong").id == "le-hoi-chua-huong"
    assert le_hoi.find("chua huong khai hoi ngay nao").id == "le-hoi-chua-huong"
    assert le_hoi.find("cho vieng").id == "cho-vieng"
    # Tên 1 từ ("Lim") không thành khoá riêng: câu thường không bị hiểu là hỏi lễ hội.
    assert le_hoi.find("buon ngu lim dim") is None
    assert le_hoi.find("hom nay troi dep") is None


def test_tim_tinh_ca_ten_goi_khac():
    assert le_hoi.find_province("le hoi o bac ninh") == "bac-ninh"
    assert le_hoi.find_province("le hoi sai gon") == "ho-chi-minh"
    assert le_hoi.find_province("le hoi o hue thang nay") == "hue"
    assert le_hoi.find_province("le hoi thang gieng") is None


def test_ngay_30_thang_thieu_va_29_2_bo_nam_do():
    # Năm âm 2026: tháng Chạp có 30 ngày; tháng 1/2023 thiếu (29 ngày).
    fake = le_hoi.Festival(
        "x", "Lễ thử", "lunar", 30, 1, 0, 0, (), (), "", "", "khac", False, False, False, "",
        "", ("le thu",),
    )  # fmt: skip
    assert le_hoi.date_in_year(fake, 2023) is None
    leap_day = le_hoi.Festival(
        "y", "Lễ 29/2", "solar", 29, 2, 0, 0, (), (), "", "", "khac", False, False, False, "",
        "", ("le 29 2",),
    )  # fmt: skip
    assert le_hoi.date_in_year(leap_day, 2028) == dt.date(2028, 2, 29)
    assert le_hoi.date_in_year(leap_day, 2027) is None


def test_lan_toi_va_cau_tra_loi():
    today = dt.date(2026, 11, 23)
    lim = le_hoi.get("hoi-lim")
    day = le_hoi.next_date(lim, today)
    assert day == dt.date(2027, 2, 18)  # 13 tháng Giêng Đinh Mùi (Tết 06/02/2027)
    text = le_hoi.describe(lim, today)
    assert "Hội Lim (Bắc Ninh)" in text and "18/02/2027" in text and "còn 87 ngày" in text
    assert "Hội diễn ra 17/02 - 19/02/2027" in text
    assert text.endswith(le_hoi.ATTRIBUTION)
    # Lễ do ban tổ chức công bố: không tính ngày, in lịch đã công bố.
    carnaval = le_hoi.describe(le_hoi.get("carnaval-ha-long"), today)
    assert "ban tổ chức công bố" in carnaval and "30/04/2026" in carnaval


def test_ngay_van_hoa_viet_nam_la_ngay_nghi():
    day = dt.date(2026, 11, 24)
    names = [f.name for f in le_hoi.on_day(day)]
    assert "Ngày Văn hóa Việt Nam" in names
    text = le_hoi.describe(le_hoi.get("ngay-van-hoa-viet-nam"), dt.date(2026, 11, 23))
    assert "nghỉ lễ" in text and "còn 1 ngày" in text


def test_danh_sach_theo_khoang_ngay_va_tinh():
    start, end = dt.date(2027, 2, 6), dt.date(2027, 3, 7)  # tháng Giêng Đinh Mùi
    items = le_hoi.between(start, end)
    assert items == sorted(items, key=lambda x: (x[0], not x[1].major, x[1].name))
    assert any(f.id == "hoi-lim" for _, f in items)
    bac_ninh = le_hoi.between(start, end, "bac-ninh")
    assert bac_ninh and all("bac-ninh" in f.province_codes for _, f in bac_ninh)
    text = le_hoi.list_text(bac_ninh, "Lễ hội tháng Giêng ở Bắc Ninh")
    assert "Hội Lim - Bắc Ninh" in text and "13/1 âm" in text
    # Tháng Chạp năm âm trước rơi vào đầu năm dương: vẫn có trong năm dương đó.
    january = le_hoi.between(dt.date(2027, 1, 1), dt.date(2027, 1, 31))
    assert any(f.id == "tet-ong-cong-ong-tao" for _, f in january)
