"""Đối chiếu services/numerology.py với ca kiểm thần số học của Nhật Nguyệt.

Ca kiểm: Nhật Nguyệt (nhatnguyet.org), CC BY 4.0. File
test/data/nhatnguyet/than-so-hoc.json trích 100 ca từ ca-kiem/than-so-hoc.json
(ngày xem 1/1/2026); không sửa tay file này.

Chỉ so các trường cùng quy ước với bot: số ngày sinh, năm/tháng/ngày cá nhân,
4 thử thách, biểu đồ ngày sinh và số nợ nghiệp ngày sinh. Các trường còn lại
khác quy ước có chủ đích (số chủ đạo có 10, số theo tên rút gọn từng từ, luật
chữ Y, đỉnh cao và tuổi đỉnh theo kim tự tháp VN, số thái độ rút về 1-9) - xem
docstring services/numerology.py.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from services import numerology

_DATA = json.loads(
    (Path(__file__).resolve().parent / "data" / "nhatnguyet" / "than-so-hoc.json").read_text(
        encoding="utf-8"
    )
)
_CASES = _DATA["ca"]


def test_file_ca_kiem_ghi_nguon():
    assert len(_CASES) == 100
    assert "CC BY 4.0" in _DATA["giayPhep"] and "Nhật Nguyệt" in _DATA["ghiNguon"]


@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=[f"{i}-{c['vao']['dd']}.{c['vao']['mm']}.{c['vao']['yyyy']}" for i, c in enumerate(_CASES)],
)
def test_khop_ca_kiem_nhat_nguyet(case):
    vao, ra = case["vao"], case["ra"]
    r = numerology.compute(
        dt.date(vao["yyyy"], vao["mm"], vao["dd"]),
        vao["hoTen"],
        dt.date(vao["namXem"], vao["thangXem"], vao["ngayXem"]),
    )
    got = {
        "soNgaySinh": r.day_number,
        "soNamCaNhan": r.personal_year,
        "soThangCaNhan": r.personal_month,
        "soNgayCaNhan": r.personal_day,
        "soThachThuc": {f"thachThuc{i}": s.value for i, s in enumerate(r.challenges, 1)},
        "luoiPythago": {str(d): r.counts[d] for d in range(1, 10)},
        "soNoNghiepNgaySinh": r.karmic_debt,  # ca kiểm bỏ khoá này khi không có nợ nghiệp
    }
    assert got == {key: ra.get(key) for key in got}
    # Hợp đồng web/API trả đúng hai số mới.
    data = numerology.reading_data(r)
    assert data["personalDay"]["value"] == ra["soNgayCaNhan"]
    karmic = data["karmicDebt"]["value"] if data["karmicDebt"] else None
    assert karmic == ra.get("soNoNghiepNgaySinh")
