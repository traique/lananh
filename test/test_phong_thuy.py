"""Test services/phong_thuy.py (/loban thước Lỗ Ban, /cungphi cung phi Bát Trạch).

Ca kiểm: Nhật Nguyệt (nhatnguyet.org), CC BY 4.0. File test/data/nhatnguyet/phong-thuy.json
trích từ ca-kiem/fengshui.json (scripts/make_golden_nhatnguyet.py, không sửa tay): 100 ca
lo-ban, 100 ca cung-phi, bảng ba thước (cac-loai-thuoc) và tên cung nhỏ (ten-cung-nho).
So mọi trường module có làm; bỏ chữ Hán (cungHan, tenHan) và bính âm (binhAm) vì bot
không giữ hai thứ này (chữ hiện cho người dùng chỉ là tiếng Việt).
"""

import datetime as dt
import json
import re
import unicodedata
from pathlib import Path

import pytest

from services import phong_thuy as pt

_DATA = json.loads(
    (Path(__file__).resolve().parent / "data" / "nhatnguyet" / "phong-thuy.json").read_text(
        encoding="utf-8"
    )
)
_BY_OP: dict[str, list] = {}
for _case in _DATA["ca"]:
    _BY_OP.setdefault(_case["vao"]["phep"], []).append(_case)


def _rounded(value):
    """Làm tròn số thực 6 chữ số: ca kiểm có sai số dấu phẩy động cỡ 1e-13
    (430.75000000000006), module trả số gọn."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    return value


def test_file_ca_kiem_ghi_nguon():
    counts = {op: len(cases) for op, cases in _BY_OP.items()}
    assert counts == {"lo-ban": 100, "cung-phi": 100, "cac-loai-thuoc": 1, "ten-cung-nho": 1}
    assert "CC BY 4.0" in _DATA["giayPhep"] and "Nhật Nguyệt" in _DATA["ghiNguon"]


# ─── Thước Lỗ Ban ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "case",
    _BY_OP["lo-ban"],
    ids=[f"{c['vao']['loaiThuoc']}-{c['vao']['kichThuocMm']}" for c in _BY_OP["lo-ban"]],
)
def test_lo_ban_khop_ca_kiem(case):
    vao, ra = case["vao"], case["ra"]
    r = pt.ruler_reading(vao["kichThuocMm"], vao["loaiThuoc"].lower())
    got = {
        "loaiThuoc": r["ruler"].upper(),
        "tenThuoc": r["rulerName"],
        "chuKyThu": r["cycle"],
        "viTriMm": r["positionMm"],
        "cung": r["sector"]["name"],
        "cungNho": r["subSector"],
        "tot": r["auspicious"],
        "ganNhatTot": [
            {
                "mm": p["lengthMm"],
                "cm": p["lengthCm"],
                "lechMm": p["offsetMm"],
                "cung": p["sector"],
                "cungNho": p["subSector"],
            }
            for p in r["nearestAuspicious"]
        ],
    }
    expected = {k: ra.get(k) for k in got}
    # Thước âm phần không có cung nhỏ: ca kiểm bỏ khoá, module trả None.
    expected["ganNhatTot"] = [{"cungNho": None} | g for g in ra.get("ganNhatTot", [])]
    assert _rounded(got) == _rounded(expected)
    assert r["sector"]["auspicious"] is ra["tot"]


def test_cac_loai_thuoc_khop_ca_kiem():
    (case,) = _BY_OP["cac-loai-thuoc"]
    got = [
        {
            "loai": r["ruler"].upper(),
            "ten": r["name"],
            "doDaiMm": r["lengthMm"],
            "soCung": len(r["sectors"]),
            # Ca kiểm làm tròn 2 chữ số (53,625 -> 53,63); module trả số đúng.
            "daiMoiCungMm": int(r["sectorMm"] * 100 + 0.5) / 100,
            "cung": [
                {"ten": s["name"], "tot": s["auspicious"], "cungNho": s["subSectors"]}
                for s in r["sectors"]
            ],
        }
        for r in pt.rulers_data()
    ]
    expected = [
        {k: b[k] for k in ("loai", "ten", "doDaiMm", "soCung", "daiMoiCungMm")}
        | {"cung": [{k: c[k] for k in ("ten", "tot", "cungNho")} for c in b["cung"]]}
        for b in case["ra"]
    ]
    assert got == expected


def test_ten_cung_nho_khop_ca_kiem():
    (case,) = _BY_OP["ten-cung-nho"]
    names = [n for r in pt.rulers_data() for s in r["sectors"] for n in s["subSectors"]]
    assert list(dict.fromkeys(names)) == [x["ten"] for x in case["ra"]]


@pytest.mark.parametrize("ruler", list(pt.RULERS))
def test_doan_cung_in_ra_dung_bien(ruler):
    # Đoạn "cung X: a-b" in ra phải đúng là mọi số đo (bước 1 mm, hoặc 0,1 mm) thuộc cung
    # đó theo phép tra: hai đầu nằm trong cung, lùi/tiến thêm một bước là sang cung khác
    # (kể cả mép mà phép chia dấu phẩy động xếp lệch, vd 504,4 mm trên thước âm phần).
    r = pt.RULERS[ruler]

    def where(mm):
        reading = pt.ruler_reading(round(mm, 1), ruler)
        return reading["cycle"], reading["sector"]["index"]

    for cycle in range(8):
        for idx in range(len(r.sectors)):
            middle = cycle * r.length_mm + (idx + 0.5) * r.sector_mm
            sector = pt.ruler_reading(middle, ruler)["sector"]
            assert sector["index"] == idx
            for fine, step in ((False, 1.0), (True, 0.1)):
                first, last = pt._span(r, sector["fromMm"], sector["toMm"], fine)
                assert where(first) == where(last) == (cycle + 1, idx)
                if first - step > 0:
                    assert where(first - step) != (cycle + 1, idx)
                assert where(last + step) != (cycle + 1, idx)


def test_so_do_tren_mep_tinh_vao_cung_sau():
    assert pt.ruler_reading(65.25, "thong_thuy")["sector"]["name"] == "Hiểm Họa"
    assert pt.ruler_reading(65.2, "thong_thuy")["sector"]["name"] == "Quý Nhân"
    assert pt.ruler_reading(522, "thong_thuy")["cycle"] == 2
    assert pt.ruler_reading(194, "am_phan")["sector"]["name"] == "Quan"  # 5 x 38,8


@pytest.mark.parametrize(
    "text,length_mm,unit",
    [
        ("80", 800, "cm"),
        ("80cm", 800, "cm"),
        ("80 CM", 800, "cm"),
        ("80,5", 805, "cm"),
        ("80.5cm", 805, "cm"),
        ("1m2", 1200, "cm"),
        ("1,2m", 1200, "cm"),
        ("1.2 m", 1200, "cm"),
        ("2m05", 2050, "cm"),
        ("1 mét 25", 1250, "cm"),
        ("850mm", 850, "mm"),
        ("850 mm", 850, "mm"),
        ("80 phân", 800, "cm"),
        ("8 tấc", 800, "cm"),
        ("80,123 cm", 801.2, "cm"),  # làm tròn 0,1 mm
        ("cửa sổ 120cm", 1200, "cm"),  # số đo đứng sau loại đồ
    ],
)
def test_doc_so_do_va_don_vi(text, length_mm, unit):
    query = pt.parse_loban(text)
    assert isinstance(query, pt.LobanQuery)
    assert (query.length_mm, query.unit) == (length_mm, unit)


@pytest.mark.parametrize(
    "item,ruler",
    [
        ("cửa", "thong_thuy"),
        ("cửa sổ", "thong_thuy"),
        ("cua so", "thong_thuy"),
        ("cổng", "thong_thuy"),
        ("lọt lòng cửa đi", "thong_thuy"),
        ("cửa mở ra ngoài", "thong_thuy"),  # "mở" không phải "mộ"
        ("giường", "duong_trach"),
        ("tủ quần áo", "duong_trach"),
        ("tu bep", "duong_trach"),
        ("bàn ăn", "duong_trach"),
        ("cửa tủ", "duong_trach"),
        ("khung cửa", "duong_trach"),
        ("bàn thờ", "am_phan"),
        ("ban tho", "am_phan"),
        ("tủ thờ", "am_phan"),
        ("bàn thờ thần tài", "am_phan"),
        ("mộ", "am_phan"),
        ("gương", None),
        ("công trình", None),  # có dấu: "công" không phải "cổng"
        ("thước lỗ ban", None),  # "ban" của Lỗ Ban không phải "bàn"
        ("", None),
    ],
)
def test_chon_thuoc_theo_loai_do(item, ruler):
    assert pt.ruler_for(item) == ruler


def test_loban_khong_ghi_loai_in_ca_3_thuoc():
    text = pt.loban_text("80")
    lines = text.splitlines()
    assert lines[0] == "📏 Thước Lỗ Ban · 80 cm (800 mm)"
    for name in ("Thước thông thủy 52,2cm", "Thước dương trạch 42,9cm", "Thước âm phần 38,8cm"):
        assert f"▸ {name} - " in text
    # 800 mm: thông thủy vòng 2 vị trí 278 mm; dương trạch vị trí 371 mm (Hại); âm phần 24 mm.
    assert "✅ TỐT · cung Phúc Lộc · cung nhỏ Phú quý" in text
    assert "Vòng thước thứ 2, vị trí 27,8 cm · cung Phúc Lộc: 78,3-84,8 cm" in text
    assert "❌ XẤU · cung Hại · cung nhỏ Khẩu thiệt" in text
    assert (
        "👉 Số tốt gần nhất: 80,5 cm (thêm 0,5 cm, cung Bản - Tài chí) · "
        "69,7 cm (bớt 10,3 cm, cung Quan - Phú quý)"
    ) in text
    assert "✅ TỐT · cung Đinh\n" in text and "cung Đinh: 77,6-81,4 cm" in text
    assert lines[-1] == pt.DISCLAIMER_LOBAN and text.count(pt.DISCLAIMER_LOBAN) == 1


def test_loban_co_loai_chi_in_thuoc_hop():
    door = pt.loban_text("81 cửa")
    assert "Thước thông thủy" in door and "dương trạch 42,9" not in door
    assert "cung Phúc Lộc · cung nhỏ Tấn bửu" in door and "💡" not in door
    altar = pt.loban_text("1m27 bàn thờ")
    assert altar.startswith("📏 Thước Lỗ Ban · 127 cm (1270 mm) · bàn thờ\n")
    assert "Thước âm phần 38,8cm" in altar and "thông thủy 52,2" not in altar
    assert "✅ TỐT · cung Vượng" in altar
    cabinet = pt.loban_text("850mm tủ")
    assert cabinet.startswith("📏 Thước Lỗ Ban · 850 mm (85 cm) · tủ\n")
    assert "cung Bản: 805-857 mm" in cabinet  # mm thì in cả kết quả theo mm
    for text in (door, altar, cabinet):
        assert text.splitlines()[-1] == pt.DISCLAIMER_LOBAN


def test_loban_so_do_le_phan_muoi_mm():
    text = pt.loban_text("80,35 cm giường")
    assert "cung Hại: 75,08-80,43 cm" in text
    assert "80,45 cm (thêm 0,1 cm, cung Bản - Tài chí)" in text


def test_loban_loai_do_chua_ro():
    text = pt.loban_text("80 gương")
    assert '📝 Chưa rõ "gương"' in text and text.count("▸ Thước") == 3
    # Chữ đệm (chiều rộng...) hay đơn vị lạc không bị coi là loại đồ lạ.
    assert "📝" not in pt.loban_text("80 chiều rộng")
    assert "📝" not in pt.loban_text("1m25cm")


@pytest.mark.parametrize("argument", ["", "   ", "abc", "cửa sổ", "1,2m5"])
def test_loban_sai_cu_phap_tra_usage(argument):
    assert pt.loban_text(argument) == pt.USAGE_LOBAN


@pytest.mark.parametrize("argument", ["0", "0,01 mm", "101m", "200000"])
def test_loban_so_do_ngoai_pham_vi(argument):
    text = pt.loban_text(argument)
    assert text.startswith("Số đo cần lớn hơn 0") and text.endswith(pt.USAGE_LOBAN)


@pytest.mark.parametrize("argument", ["80x210 cửa", "80 x 210", "1m2x2m1", "80*210"])
def test_loban_nhieu_so_do_bao_tra_rieng(argument):
    text = pt.loban_text(argument)
    assert text.startswith("Mỗi lần tra một số đo") and text.endswith(pt.USAGE_LOBAN)


def test_loban_data_cho_web():
    data = pt.loban_data(800.04)
    assert data["lengthMm"] == 800 and data["lengthCm"] == 80
    assert [r["ruler"] for r in data["rulers"]] == ["thong_thuy", "duong_trach", "am_phan"]
    json.dumps(data)  # dữ liệu JSON thuần
    one = pt.loban_data(805, "duong_trach")["rulers"]
    assert len(one) == 1 and one[0]["sector"]["name"] == "Bản" and one[0]["subSector"] == "Tài chí"
    assert one[0]["nearestAuspicious"] == []
    for bad in (0, -5, float("nan"), float("inf"), 200_000, True, "80"):
        with pytest.raises(ValueError):
            pt.loban_data(bad)
    with pytest.raises(ValueError):
        pt.loban_data(800, "khong_co")


# ─── Cung phi Bát Trạch ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "case",
    _BY_OP["cung-phi"],
    ids=[
        f"{c['vao']['dd']}.{c['vao']['mm']}.{c['vao']['yyyy']}-{c['vao']['gioiTinh']}"
        for c in _BY_OP["cung-phi"]
    ],
)
def test_cung_phi_khop_ca_kiem(case):
    vao, ra = case["vao"], case["ra"]
    male = vao["gioiTinh"] == "Nam"
    data = pt.cungphi_data(dt.date(vao["yyyy"], vao["mm"], vao["dd"]), male)

    def directions(items):
        return [{"huong": d["direction"], "sao": d["star"], "tenSao": d["starName"]} for d in items]

    got = {
        "namAm": data["lunarYear"],
        "ketQua": {
            "kua": data["kua"],
            "que": data["trigram"],
            "nhomMenh": data["group"],
            "huongTot": directions(data["goodDirections"]),
            "huongXau": directions(data["badDirections"]),
        },
    }
    assert got == ra
    # Chỉ ghi năm âm lịch cho cùng kết quả.
    by_year = pt.cungphi_data(data["lunarYear"], male)
    assert (by_year["kua"], by_year["goodDirections"]) == (data["kua"], data["goodDirections"])


@pytest.mark.parametrize(
    "year,male,kua",
    [(1990, True, 1), (1990, False, 8), (1984, True, 7), (1984, False, 8), (1995, True, 2)],
)
def test_so_cung_phi(year, male, kua):
    assert pt.kua_number(year, male) == kua


def test_bang_tam_huong_nhat_quan():
    own = {1: "Bắc", 2: "Tây Nam", 3: "Đông", 4: "Đông Nam", 6: "Tây Bắc", 7: "Tây",
           8: "Đông Bắc", 9: "Nam"}  # fmt: skip
    east = {"Bắc", "Đông", "Đông Nam", "Nam"}
    for year in range(2000, 2009):  # 9 năm liên tiếp đi qua mọi số cung (nam + nữ)
        for male in (True, False):
            data = pt.cungphi_data(year, male)
            good = [d["direction"] for d in data["goodDirections"]]
            bad = [d["direction"] for d in data["badDirections"]]
            assert sorted(good + bad) == sorted(east | {"Tây", "Tây Bắc", "Tây Nam", "Đông Bắc"})
            assert good[3] == own[data["kua"]]  # Phục Vị là hướng của chính quẻ
            assert (set(good) == east) is (data["group"] == "dong_tu_menh")


def test_cungphi_chi_nam_va_kua_5():
    text = pt.cungphi_text("1990 nữ")
    lines = text.splitlines()
    assert lines[0] == "🧭 Cung phi Bát Trạch · nữ · năm âm lịch 1990 (Canh Ngọ)"
    assert lines[1] == "Cung Cấn (số 8), hành Thổ · Tây tứ mệnh"
    assert "Sinh trước Tết tính năm trước" in text and "năm âm 1989" in text
    assert "Số cung tính ra 5 (không ứng với quẻ nào) nên nữ lấy cung Cấn (8)." in text
    assert "• Sinh Khí - Tây Nam (202,5°-247,5°): " in text
    assert "• Ngũ Quỷ - Bắc (337,5°-22,5°): " in text
    assert lines[-1] == pt.DISCLAIMER_CUNGPHI and text.count(pt.DISCLAIMER_CUNGPHI) == 1
    male = pt.cungphi_text("1995 nam")
    assert "nên nam lấy cung Khôn (2)" in male and "Cung Khôn (số 2)" in male


def test_cungphi_ngay_sinh_va_truoc_tet():
    text = pt.cungphi_text("15/3/1990 nam")
    assert "sinh 15/03/1990 (ngày 19 tháng 2 năm Canh Ngọ âm lịch)" in text
    assert "Cung Khảm (số 1), hành Thủy · Đông tứ mệnh" in text and "📝" not in text
    before_tet = pt.cungphi_text("20/1/1990 nam")  # Tết Canh Ngọ: 27/01/1990
    assert "📝 Sinh trước Tết nên tính theo năm âm lịch 1989 (Kỷ Tỵ)." in before_tet
    assert "Cung Khôn (số 2)" in before_tet
    data = pt.cungphi_data(dt.date(1990, 1, 20), True)
    assert data["lunarYear"] == 1989 and data["birthDate"] == "1990-01-20"
    json.dumps(data)


def test_cungphi_ngay_am():
    parsed = pt.parse_cungphi("âm 20/1/1990 nữ")
    assert parsed == (dt.date(1990, 2, 15), False)
    assert pt.parse_cungphi("20/1/1990 âm lịch nam") == (dt.date(1990, 2, 15), True)
    assert "ngày 20 tháng 1 năm Canh Ngọ" in pt.cungphi_text("am 20/1/1990 nu")


@pytest.mark.parametrize(
    "argument,expected",
    [
        ("", pt.USAGE_CUNGPHI),
        ("nam", pt.USAGE_CUNGPHI),
        ("1990", "Thiếu giới tính (nam/nữ).\n" + pt.USAGE_CUNGPHI),
        ("31/2/1990 nam", "Ngày sinh không hợp lệ.\n" + pt.USAGE_CUNGPHI),
        ("1899 nữ", "Năm sinh cần trong khoảng 1900-2100."),
        ("1/1/2101 nam", "Năm sinh cần trong khoảng 1900-2100."),
        ("âm 30/2/2025 nam", "Tháng 2 âm lịch năm 2025 không có ngày 30."),
    ],
)
def test_cungphi_loi(argument, expected):
    assert pt.cungphi_text(argument) == expected


def test_cungphi_data_loi():
    for bad in (1800, 2200, True, "1990", None):
        with pytest.raises(ValueError):
            pt.cungphi_data(bad, True)
    assert pt.cungphi_data(dt.date(1900, 1, 15), True)["lunarYear"] == 1899


# ─── Chung ──────────────────────────────────────────────────────────────────

_FORBIDDEN_WORDS = re.compile(r"trung\s+qu[ốo]c|trung\s+hoa|china|chinese|bắc kinh", re.I)


def _forbidden(text: str) -> list[str]:
    """Từ nhắc tới Trung Quốc và mọi chữ Hán (ký tự có tên Unicode bắt đầu bằng "CJK")."""
    hans = [ch for ch in text if unicodedata.name(ch, "").startswith("CJK")]
    return _FORBIDDEN_WORDS.findall(text) + hans


def test_dau_ra_khong_co_chu_han_va_ten_nuoc_khac():
    # Chủ bot yêu cầu chữ hiện cho người dùng (bot, web, API) không có chữ Hán hay nhắc
    # tới Trung Quốc - như test/test_web_site.py kiểm phần web.
    assert _forbidden(f"cung {chr(0x8CA1)} - trung quốc") == ["trung quốc", chr(0x8CA1)]
    outputs = [pt.loban_text(a) for a in ("80", "81 cửa", "850mm tủ", "1m27 bàn thờ", "")]
    outputs += [pt.cungphi_text(a) for a in ("1990 nữ", "20/1/1990 nam", "")]
    outputs.append(json.dumps(pt.rulers_data(), ensure_ascii=False))
    for mm in range(500, 2500, 37):
        outputs.append(json.dumps(pt.loban_data(mm), ensure_ascii=False))
    for year in range(2000, 2009):
        for male in (True, False):
            outputs.append(json.dumps(pt.cungphi_data(year, male), ensure_ascii=False))
    yaml_path = Path(pt.__file__).resolve().parent.parent / "assets" / "phong_thuy.yaml"
    outputs.append(yaml_path.read_text(encoding="utf-8"))
    assert _forbidden("\n".join(outputs)) == []


def test_chu_soan_san_du_cho_moi_cung_va_sao():
    for r in pt.rulers_data():
        assert r["use"], r["ruler"]
        assert all(s["meaning"] for s in r["sectors"]), r["ruler"]
    data = pt.cungphi_data(1990, True)
    assert all(d["meaning"] for d in data["goodDirections"] + data["badDirections"])
    assert data["howTo"]
