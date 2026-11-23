"""Test services/tuvi.py: an lá số Tử Vi Đẩu Số bằng code."""

import datetime as dt
import json
import re
from types import SimpleNamespace

import pytest

from services import lunar, tuvi

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
    assert "📌 Phá Quân (đắc) thủ Mệnh" in text
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


def test_phu_tinh_bo_sung_15_3_1990():
    """Thiên Quan/Phúc, Thiên Trù theo can Canh; Quốc Ấn, Đường Phù từ Lộc Tồn;
    Đẩu Quân, Thiên Không, Giải Thần theo chi năm Ngọ (đối chiếu lasotuvi và
    bảng Thái Thứ Lang)."""
    c = tuvi.parse("15/3/1990 9h30 nam")  # Canh Ngọ, tháng 2, giờ Tỵ, Lộc Tồn ở Thân
    assert _star(c, "Thiên Quan") == HOI and _star(c, "Thiên Phúc") == NGO
    assert _star(c, "Thiên Trù") == DAN and _star(c, "Lưu Hà") == THAN
    assert _star(c, "Quốc Ấn") == THIN and _star(c, "Đường Phù") == SUU
    assert _star(c, "Lưu Niên Văn Tinh") == HOI  # Lộc Tồn Thân + 3
    assert _star(c, "Đẩu Quân") == TUAT  # Ngọ lùi 1 tháng = Tỵ, tiến 5 giờ = Tuất
    assert _star(c, "Thiên Không") == 7  # cung liền sau Thái Tuế (Ngọ -> Mùi)
    assert _star(c, "Giải Thần") == _star(c, "Phượng Các") == THIN


@pytest.mark.parametrize(
    "can,quan,phuc",
    [(0, 7, 9), (1, 4, 8), (2, 5, 0), (3, 2, 11), (4, 3, 3), (5, 9, 2), (6, 11, 6),
     (7, 9, 5), (8, 10, 6), (9, 6, 5)],
)  # fmt: skip
def test_bang_thien_quan_thien_phuc(can, quan, phuc):
    assert (tuvi._THIEN_QUAN[can], tuvi._THIEN_PHUC[can]) == (quan, phuc)


def test_bang_luu_ha_thien_tru_theo_thai_thu_lang():
    # Giáp Dậu, Ất Tuất, Bính Mùi, Đinh Thìn, Mậu Tỵ, Kỷ Ngọ, Canh Thân, Tân Mão, Nhâm Hợi, Quý Dần
    assert tuvi._LUU_HA == [9, 10, 7, 4, 5, 6, 8, 3, 11, 2]
    # Giáp Tỵ, Ất Ngọ, Bính Tý, Đinh Tỵ, Mậu Ngọ, Kỷ Thân, Canh Dần, Tân Ngọ, Nhâm Dậu, Quý Tuất
    assert tuvi._THIEN_TRU == [5, 6, 0, 5, 6, 8, 2, 6, 9, 10]


def test_do_sang_chinh_tinh():
    # Thái Dương miếu ở Ngọ, hãm ở Tý; Tử Vi không có hãm địa; Lộc Tồn không xét.
    assert tuvi.brightness("Thái Dương", NGO) == "M"
    assert tuvi.brightness("Thái Dương", 0) == "H"
    assert "H" not in tuvi._texts()["do_sang"]["Tử Vi"]
    assert tuvi.brightness("Lộc Tồn", DAN) is None
    table = tuvi._texts()["do_sang"]
    assert set(tuvi.CHINH_TINH) <= set(table)
    for name, row in table.items():
        assert len(row) == 12 and set(row) <= set("MVĐBH-"), name


def test_format_chart_co_do_sang_va_sao_moi():
    c = tuvi.parse("15/3/1990 9h30 nam")
    text = tuvi.format_chart(c)
    assert "★ Phá Quân [Đ]" in text and "Thái Dương [Đ] (Hóa Lộc)" in text
    assert "Tứ hóa: Hóa Lộc Thái Dương" in text
    assert "📌 Phá Quân (đắc) thủ Mệnh" in text
    assert "Đẩu Quân" in text and "Thiên Không" in text and "Quốc Ấn" in text
    stars = c.star_list(c.menh)
    assert stars[0].name == "Phá Quân" and stars[0].is_main and stars[0].brightness == "Đ"
    hoa = next(s for s in c.star_list(SUU) if s.name == "Thái Dương")
    assert hoa.hoa == "Hóa Lộc" and hoa.label == "Thái Dương (Hóa Lộc)"


def test_menh_chu_than_chu():
    c = tuvi.parse("15/3/1990 9h30 nam")  # Mệnh ở Tuất -> Lộc Tồn; năm Ngọ -> Linh Tinh
    assert tuvi._MENH_CHU[c.menh] == "Lộc Tồn" and tuvi._THAN_CHU[c.chi_y] == "Linh Tinh"
    assert "Mệnh chủ: Lộc Tồn · Thân chủ: Linh Tinh" in tuvi.format_chart(c)
    assert tuvi._THAN_CHU[0] == "Hỏa Tinh" and tuvi._MENH_CHU[0] == "Tham Lang"


def test_hour_to_chi():
    assert [tuvi.hour_to_chi(h) for h in (23, 0, 1, 2, 3, 12, 13, 22)] == [0, 0, 1, 1, 2, 6, 7, 11]


@pytest.mark.parametrize(
    "arg,hour_name,early",
    [
        ("15/3/1990 12h đêm nam", "Tý", False),  # 12h đêm = 0h, cùng ngày
        ("15/3/1990 12 giờ khuya nam", "Tý", False),
        ("15/3/1990 7:30 pm nam", "Tuất", False),
        ("15/3/1990 12h trưa nam", "Ngọ", False),
        ("15/3/1990 23h30 nam", "Tý", True),  # giờ Tý đầu: an theo ngày hôm sau
        ("15/3/1990 11h đêm nam", "Tý", True),
        ("âm 20/2/1990 12h âm nữ", "Ngọ", False),  # "âm" không bị coi là "am" (pm)
    ],
)
def test_parse_gio_dem_pm_va_ty_dau(arg, hour_name, early):
    c = tuvi.parse(arg)
    assert tuvi.CHI[c.hour_chi] == hour_name and c.early_ty is early


def test_gio_ty_dau_an_theo_ngay_hom_sau():
    c = tuvi.parse("15/3/1990 23h30 nam")
    assert c.solar == dt.date(1990, 3, 15)  # vẫn hiển thị ngày đã nhập
    assert (c.lunar_day, c.lunar_month) == (20, 2)  # an theo 16/3 = 20/2 âm
    assert "giờ Tý đầu (23h-24h, an theo ngày hôm sau)" in tuvi.format_chart(c)
    same_as_next_day = tuvi.parse("16/3/1990 0h30 nam")
    assert c.stars == same_as_next_day.stars and c.menh == same_as_next_day.menh


def test_thang_nhuan_tu_16_an_theo_thang_sau():
    before = tuvi.parse("âm 10/6n/2025 9h nam")  # 1-15: như tháng 6
    after = tuvi.parse("âm 20/6n/2025 9h nam")  # 16 trở đi: như tháng 7
    assert before.an_month == 6 and before.menh == DAN
    assert after.an_month == 7 and after.menh == MAO
    assert after.lunar_month == 6 and after.leap  # hiển thị vẫn là tháng 6 nhuận
    assert tuvi.build_chart(16, 12, 2000, 0, True, leap=True).an_month == 1


def test_chart_data_hop_dong_json():
    data = tuvi.chart_data(tuvi.parse("15/3/1990 9h30 nam"))
    assert data["palaces"][0]["mainStars"] == ["Phá Quân"]
    assert data["palaces"][0]["stars"][0]["brightness"] == "Đ"
    assert data["hourText"] == "giờ Tỵ" and data["isEarlyTy"] is False and data["anMonth"] == 2
    assert data["gender"] == "male" and data["isThuan"] is True


def test_hoa_linh_trang_sinh_bac_si_theo_chieu_thuan_nghich():
    nam = tuvi.parse("15/3/1990 9h30 nam")  # Canh Ngọ, dương nam -> thuận
    nu = tuvi.parse("15/3/1990 9h30 nữ")  # dương nữ -> nghịch
    assert (_star(nam, "Hỏa Tinh"), _star(nam, "Linh Tinh")) == (NGO, TUAT)
    assert (_star(nu, "Hỏa Tinh"), _star(nu, "Linh Tinh")) == (THAN, THAN)
    assert nam.trang_sinh[DAU] == "Mộc Dục" and nu.trang_sinh[7] == "Mộc Dục"
    assert _star(nam, "Lực Sĩ") == DAU and _star(nu, "Lực Sĩ") == 7


@pytest.mark.parametrize(
    "year,triet,tuan",
    [(1984, (8, 9), (10, 11)), (1985, (6, 7), (10, 11)), (1986, (4, 5), (10, 11)),
     (1987, (2, 3), (10, 11)), (1988, (0, 1), (10, 11)), (1989, (8, 9), (10, 11)),
     (1993, (0, 1), (10, 11)), (1994, (8, 9), (8, 9)), (1975, (6, 7), (0, 1)),
     (2026, (4, 5), (2, 3))],
)  # fmt: skip
def test_tuan_triet_theo_can_chi_nam(year, triet, tuan):
    c = tuvi.build_chart(1, 1, year, 0, True)
    assert (c.triet, c.tuan) == (triet, tuan)


def test_triet_hien_tren_ban_chu_va_json():
    c = tuvi.parse("15/3/1990 9h30 nam")  # Canh -> Triệt Ngọ Mùi = Tài Bạch, Tử Tức
    assert "▸ TÀI BẠCH (Nhâm Ngọ) [Triệt]" in tuvi.format_chart(c)
    data = tuvi.chart_data(c)
    assert [p["name"] for p in data["palaces"] if p["hasTriet"]] == ["Tài Bạch", "Tử Tức"]


@pytest.mark.parametrize(
    "year,loc,quyen,khoa,ky",
    [(1984, "Liêm Trinh", "Phá Quân", "Vũ Khúc", "Thái Dương"),
     (1985, "Thiên Cơ", "Thiên Lương", "Tử Vi", "Thái Âm"),
     (1986, "Thiên Đồng", "Thiên Cơ", "Văn Xương", "Liêm Trinh"),
     (1987, "Thái Âm", "Thiên Đồng", "Thiên Cơ", "Cự Môn"),
     (1988, "Tham Lang", "Thái Âm", "Hữu Bật", "Thiên Cơ"),
     (1989, "Vũ Khúc", "Tham Lang", "Thiên Lương", "Văn Khúc"),
     (1990, "Thái Dương", "Vũ Khúc", "Thái Âm", "Thiên Đồng"),
     (1991, "Cự Môn", "Thái Dương", "Văn Khúc", "Văn Xương"),
     (1992, "Thiên Lương", "Tử Vi", "Tả Phù", "Vũ Khúc"),
     (1993, "Phá Quân", "Cự Môn", "Thái Âm", "Tham Lang")],
)  # fmt: skip
def test_tu_hoa_10_can_va_gan_vao_ca_phu_tinh(year, loc, quyen, khoa, ky):
    c = tuvi.build_chart(1, 1, year, 0, True)
    assert list(c.tu_hoa.values()) == [loc, quyen, khoa, ky]
    labels = [n for names in c.stars.values() for n in names if "(Hóa" in n]
    expected = [f"{s} ({h})" for s, h in zip((loc, quyen, khoa, ky), tuvi._HOA_NAMES, strict=True)]
    assert sorted(labels) == sorted(expected)


def test_hoa_khoa_nam_mau_nam_o_huu_bat():
    data = tuvi.chart_data(tuvi.build_chart(1, 1, 1988, 0, True))
    tai = next(p for p in data["palaces"] if p["name"] == "Tài Bạch")
    assert "Hữu Bật (Hóa Khoa)" in tai["minorStars"]


def test_dien_giai_la_so_bang_loi_thuong():
    c = tuvi.parse("15/3/1990 9h30 nam")
    r = tuvi.reading(c, dt.date(2026, 10, 27))  # Bính Ngọ 2026: 37 tuổi âm
    assert r["age"] == 37 and len(r["palaces"]) == 12 and r["howTo"]
    assert r["overview"][0].startswith("Bản mệnh Lộ Bàng Thổ (Thổ), Thổ ngũ cục: cục và mệnh cùng")
    assert r["overview"][1].startswith("Thân cư Phu Thê:")
    assert "đang đi đại hạn cung Điền Trạch (35-44 tuổi)" in r["overview"][2]
    by_name = {p["name"]: p for p in r["palaces"]}
    menh, dien = by_name["Mệnh"], by_name["Điền Trạch"]
    assert menh["isMenh"] and menh["domain"].startswith("bản thân")
    assert menh["lines"][0] == "Chính tinh Phá Quân đắc (hao tinh, chủ phá cũ đổi mới)."
    assert any(line.startswith("Tuần làm giảm lực") for line in menh["lines"])
    # Thái Dương, Thái Âm đắc + Hóa Lộc + Hóa Khoa: đọc nhanh là thuận, đánh dấu đại hạn.
    assert dien["isCurrent"] and dien["verdict"] == "Thuận" and dien["tone"] == "pos"
    assert "Hóa Lộc (Thái Dương): thêm tài lộc, may mắn." in dien["lines"]
    # Cung không chính tinh mượn sao cung đối diện.
    assert by_name["Tử Tức"]["lines"][0].startswith(
        "Vô chính diệu: không có chính tinh, mượn sao cung Điền Trạch đối diện (Thái Dương"
    )
    assert by_name["Phu Thê"]["isThan"]
    assert {p["verdict"] for p in r["palaces"]} <= {"Thuận", "Trung bình", "Nhiều trắc trở"}
    young = tuvi.reading(c, dt.date(1992, 6, 1))  # 3 tuổi âm, đại hạn đầu từ 5 tuổi
    assert "chưa vào đại hạn đầu (từ 5 tuổi)" in young["overview"][-1]
    assert not any(p["isCurrent"] for p in young["palaces"])


def test_van_nam_15_3_1990_nam_2026():
    c = tuvi.parse("15/3/1990 9h30 nam năm 2026")  # Canh Ngọ, Mệnh Tuất, Thổ ngũ cục, thuận
    v = tuvi.year_luck(c)
    assert (v["year"], v["canChi"], v["age"]) == (2026, "Bính Ngọ", 37)
    assert v["luuNien"] == {"branch": "Ngọ", "branchIndex": NGO, "palace": "Tài Bạch"}
    # Năm Ngọ (Dần Ngọ Tuất): tiểu hạn 1 tuổi ở Thìn, nam đi thuận 36 cung -> lại Thìn.
    assert v["tieuHan"] == {"branch": "Thìn", "branchIndex": THIN, "palace": "Thiên Di"}
    assert v["daiHan"] == {
        "branch": "Sửu", "branchIndex": SUU, "palace": "Điền Trạch", "fromAge": 35, "toAge": 44,
    }  # fmt: skip
    assert [(h["hoa"], h["star"], h["palace"]) for h in v["luuTuHoa"]] == [
        ("Hóa Lộc", "Thiên Đồng", "Phụ Mẫu"),
        ("Hóa Quyền", "Thiên Cơ", "Nô Bộc"),
        ("Hóa Khoa", "Văn Xương", "Tật Ách"),
        ("Hóa Kỵ", "Liêm Trinh", "Phu Thê"),
    ]
    luu = {s["id"]: s for s in v["saoLuu"]}
    assert " ".join(luu) == "luu-thai-tue luu-loc-ton luu-kinh-duong luu-da-la luu-thien-ma"
    assert luu["luu-thai-tue"]["branchIndex"] == NGO and luu["luu-thai-tue"]["byYearStem"] is False
    assert luu["luu-loc-ton"]["branchIndex"] == TY_
    assert luu["luu-loc-ton"]["withDaiHan"] == "tam-hop"
    assert luu["luu-da-la"]["withTieuHan"] == "dong-cung"
    assert luu["luu-thien-ma"]["branchIndex"] == THAN
    assert luu["luu-thien-ma"]["relationText"] == "tam hợp tiểu hạn"
    text = tuvi.format_chart(c)
    assert "📅 Vận năm 2026 (Bính Ngọ), 37 tuổi âm:" in text
    assert "  · Đại hạn: cung Điền Trạch (Sửu), 35-44 tuổi" in text
    assert "  · Tiểu hạn: cung Thiên Di (Thìn) · Lưu niên: cung Tài Bạch (Ngọ)" in text
    assert "Hóa Lộc Thiên Đồng → Phụ Mẫu (Hợi)" in text
    assert "Lưu Thái Tuế → Tài Bạch (Ngọ), đồng cung lưu niên" in text
    assert "Chưa an tiểu hạn" not in text and "Chưa an nguyệt hạn" in text


def test_tieu_han_nam_thuan_nu_nghich_va_chua_vao_dai_han():
    nam = tuvi.year_luck(tuvi.parse("15/3/1990 9h30 nam năm 2027"))
    nu = tuvi.year_luck(tuvi.parse("15/3/1990 9h30 nữ năm 2027"))
    assert (nam["tieuHan"]["branchIndex"], nu["tieuHan"]["branchIndex"]) == (TY_, MAO)
    young = tuvi.parse("15/3/1990 9h30 nam năm 1992")  # 3 tuổi âm, đại hạn đầu từ 5 tuổi
    assert tuvi.year_luck(young)["daiHan"] is None
    assert "  · Đại hạn: chưa vào đại hạn đầu (từ 5 tuổi)" in tuvi.format_chart(young)


def test_nam_xem_truoc_nam_sinh_khong_co_van_nam():
    c = tuvi.parse("15/3/2030 9h nam năm 2026")
    assert not c.has_year_luck and tuvi.chart_data(c)["vanNam"] is None
    assert "📅 Vận năm 2026: năm xem trước năm sinh, chưa có vận năm." in tuvi.format_chart(c)


@pytest.mark.parametrize(
    "arg,male,year",
    [
        ("15/3/1990 9h30 nam năm 2027", True, 2027),
        ("15/3/1990 9h30 nữ xem 2027", False, 2027),
        ("15/3/1990 9h30 nam năm xem 2030.", True, 2030),
        ("15/3/1990 9h30 nam 2027", True, 2027),  # không dấu, chưa có giới tính: "nam" là nam
        ("15/3/1990 9h30 nu nam 2027", False, 2027),  # đã có giới tính: "nam" là "năm"
        ("âm 20/2/1990 giờ Tỵ nữ năm 2027", False, 2027),
    ],
)
def test_parse_nam_xem_o_cuoi(arg, male, year):
    c = tuvi.parse(arg)
    assert (c.male, c.view_year) == (male, year)


def test_nam_xem_mac_dinh_la_nam_am_hien_tai_va_loi():
    assert tuvi.parse("15/3/1990 9h30 nam").view_year == tuvi.current_view_year()
    assert tuvi.current_view_year(dt.date(2027, 2, 5)) == 2026  # chưa qua Tết Đinh Mùi
    assert tuvi.current_view_year(dt.date(2027, 2, 6)) == 2027
    assert tuvi.parse("15/3/1990 9h30 nam năm 1800") == "Năm xem cần trong khoảng 1900-2100."
    assert "năm 2027" in tuvi.USAGE


def test_ghi_chu_quy_uoc_theo_tung_la_so():
    assert tuvi.parse("15/3/1991 9h30 nam năm 2026").notes == []  # Tân Mùi, không chỗ khác phái
    ty_dau = tuvi.parse("15/3/1991 23h30 nam năm 2026").notes
    assert len(ty_dau) == 1
    assert ty_dau[0].startswith("Sinh 23h-24h (giờ Tý đầu): lá số an theo ngày hôm sau, tức ngày")
    after = tuvi.parse("âm 20/6n/2025 9h nam năm 2026").notes
    assert after[0].startswith("Sinh ngày 20 tháng 6 nhuận: an sao như tháng 7 (Thái Thứ Lang")
    before = tuvi.parse("âm 10/6n/2025 9h nam năm 2026").notes
    assert before[0].startswith("Sinh ngày 10 tháng 6 nhuận: an sao như tháng 6 (ngày 1-15")
    canh = tuvi.parse("15/3/1990 9h30 nam năm 2026").notes
    assert canh[0].startswith("Năm sinh can Canh: Tứ Hóa theo Tân Biên (Hóa Khoa Thái Âm, Hóa Kỵ")
    assert tuvi.parse("15/3/1991 9h30 nam năm 2030").notes == [
        "Năm xem 2030 can Canh: lưu Tứ Hóa cũng theo Tân Biên (Hóa Khoa Thái Âm, Hóa Kỵ "
        "Thiên Đồng); vài sách ghi ngược Khoa và Kỵ."
    ]
    old = lunar.old_calendar_note(4, 6, 1951)  # Tân Mão, lịch âm cũ trước 1968 lệch 1 ngày
    assert old and tuvi.parse("4/6/1951 9h nam năm 2026").notes == [old]
    # 22/3/1947 là mùng 1 tháng 2 nhuận theo UTC+7 (30 tháng 2 theo lịch cũ): đủ hai ghi chú.
    assert len(tuvi.parse("22/3/1947 9h nam năm 2026").notes) == 2
    c = tuvi.parse("âm 20/6n/2025 9h nam năm 2026")
    assert "⚠️ Lưu ý quy ước của lá số này:\n  · Sinh ngày 20 tháng 6 nhuận" in tuvi.format_chart(c)
    assert tuvi.chart_data(c)["notes"] == c.notes


def test_chart_data_them_van_nam_giu_truong_cu():
    data = tuvi.chart_data(tuvi.parse("15/3/1990 9h30 nam năm 2026"))
    assert data["palaces"][0]["mainStars"] == ["Phá Quân"] and data["anMonth"] == 2  # trường cũ
    assert data["viewYear"] == 2026 and data["vanNam"]["age"] == 37
    assert data["vanNam"]["saoLuu"][0] == {
        "id": "luu-thai-tue", "name": "Lưu Thái Tuế", "byYearStem": False, "branch": "Ngọ",
        "branchIndex": NGO, "palace": "Tài Bạch", "withDaiHan": "", "withTieuHan": "",
        "withLuuNien": "dong-cung", "relationText": "đồng cung lưu niên",
    }  # fmt: skip
    assert data["notes"][0].startswith("Năm sinh can Canh")


def test_prompt_ai_dua_so_lieu_van_nam_va_cam_tu_suy():
    prompt = tuvi.build_ai_prompt(tuvi.parse("15/3/1990 9h30 nam năm 2026"))
    assert "Vận năm 2026 (Bính Ngọ): CHỈ dựa vào mục “Vận năm” đã tính sẵn" in prompt
    assert "TUYỆT ĐỐI KHÔNG tự suy hay tính lại tiểu hạn, lưu niên, đại hạn" in prompt
    assert "Năm xem vận: 2026 (Bính Ngọ)" in prompt and "Đại hạn hiện tại" not in prompt
    assert "  · Đại hạn: cung Điền Trạch (Sửu), 35-44 tuổi" in prompt
    assert "Lưu ý quy ước" in prompt and "Năm sinh can Canh" in prompt
    before = tuvi.build_ai_prompt(tuvi.parse("15/3/2030 9h nam năm 2026"))
    assert "năm xem trước năm sinh nên không luận vận năm" in before


def test_chu_hien_thi_khong_nhac_trung_quoc():
    """Chủ trang yêu cầu: chữ cho người dùng (bot, web, API) không nhắc tới Trung Quốc."""
    banned = re.compile(r"trung\s+qu[ốo]c|trung\s+hoa|china|chinese|bắc kinh|[\u4e00-\u9fff]", re.I)
    charts = [
        tuvi.parse(arg)
        for arg in ("15/3/1990 9h30 nam năm 2030", "22/3/1947 23h30 nữ", "âm 20/6n/2025 9h nam")
    ]
    texts = [tuvi.USAGE, *(tuvi.format_chart(c) for c in charts)]
    texts += [json.dumps(tuvi.chart_data(c), ensure_ascii=False) for c in charts]
    assert banned.findall("\n".join(texts)) == []
