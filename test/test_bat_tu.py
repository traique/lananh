"""Test services/bat_tu.py: lá số Bát Tự (Tứ Trụ) bằng code.

Ca kiểm: Nhật Nguyệt (nhatnguyet.org), CC BY 4.0 - test/data/nhatnguyet/bat-tu.json
(28 lá trích từ ca-kiem/bat-tu.json của engine 2.3.0, bỏ thần sát và khoá dịch; xem
khoá "dinhDang"). So mọi trường module làm: tứ trụ, tàng can, thập thần, trường sinh,
tự tọa, nạp âm, nhật chủ, mệnh cung, thai nguyên, tiết khí, đại vận, tiểu vận. Bỏ
``lyDo`` (lời giải thích đại vận; bản chữ của bot tự diễn đạt).
"""

import datetime as dt
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import bat_tu, kymon, lunar

_GOLDEN = json.loads(
    (Path(__file__).parent / "data" / "nhatnguyet" / "bat-tu.json").read_text(encoding="utf-8")
)
# Bot viết nạp âm như /tuvi (services/can_chi.NAP_AM): Lô Trung Hỏa, Kim Bạch Kim - cùng
# chữ gốc với cách viết Lư Trung Hỏa, Kim Bạc Kim của Nhật Nguyệt. Tên tiết theo
# lunar.TIET_KHI ("Đại tuyết" thay vì "Đại Tuyết").
_NAP_AM_ALIAS = {"Lư Trung Hỏa": "Lô Trung Hỏa", "Kim Bạc Kim": "Kim Bạch Kim"}
_TERMS = {name.lower(): name for name in lunar.TIET_KHI}
# services/solar_terms.py lệch lõi Nhật Nguyệt vài giây (VSOP87 rút gọn của Meeus, chương
# động 4 số hạng): ở 4/28 lá, thời khắc tiết mốc của đại vận làm tròn ra phút khác 1 phút
# (số ngày, tuổi và ngày khởi vận vẫn khớp). Ngày sinh (năm, tháng, ngày) của các lá đó:
_TERM_MINUTE_OFF = {(1911, 1, 15), (1939, 7, 16), (1965, 11, 12), (1988, 1, 10)}
_BANNED = re.compile(r"trung\s+qu[ốo]c|trung\s+hoa|china|chinese|bắc kinh|[\u4e00-\u9fff]", re.I)


def _norm(value):
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, str):
        return _NAP_AM_ALIAS.get(value, _TERMS.get(value.lower(), value))
    return value


def _pair(p: dict) -> dict:
    return {
        "can": p["stem"],
        "canAm": p["stemYinYang"] == "Âm",
        "canHanh": p["stemElement"],
        "chi": p["branch"],
        "chiHanh": p["branchElement"],
        "label": p["text"],
        "napAm": p["napAmElement"],
        "napAmTen": p["napAm"],
    }


def _pillar(p: dict) -> dict:
    return _pair(p) | {
        "tangCan": [
            {"can": h["stem"], "hanh": h["element"], "thapThan": h["tenGod"], "vaiTro": h["role"]}
            for h in p["hiddenStems"]
        ],
        "thapThanCan": p["tenGod"],
        "truongSinh": p["growth"],
        "tuToa": p["selfSeat"],
    }


def _date(d: dt.date) -> dict:
    return {"day": d.day, "month": d.month, "year": d.year}


def _as_golden(c: bat_tu.Chart) -> dict:
    """Lá số (qua chart_data) viết lại theo khoá của ca kiểm Nhật Nguyệt."""
    data = bat_tu.chart_data(c)
    luck = data["luck"]
    term_at = dt.datetime.fromisoformat(luck["term"]["at"])
    la_so = {
        "coGioSinh": data["hasHour"],
        "gio": c.birth.hour,
        "phut": c.birth.minute,
        "gioiTinh": "Nam" if data["gender"] == "male" else "Nữ",
        "ngaySinh": _date(dt.date.fromisoformat(data["birth"]["date"])),
        "namTruNam": data["pillarYear"],
        "nhatChu": data["dayMaster"]["stem"],
        "nhatChuHanh": data["dayMaster"]["element"],
        "nhatChuAmDuong": data["dayMaster"]["yinYang"],
        "thaiNguyen": _pair(data["conception"]),
        "tietKhi": data["solarTerm"],
        "tietDauThang": data["monthTerm"]["name"],
        "daiVan": {
            "thuan": luck["forward"],
            "tietMoc": luck["term"]["name"],
            "tietMocNgay": _date(term_at.date()),
            "tietMocGio": term_at.hour,
            "tietMocPhut": term_at.minute,
            "soNgay": luck["days"],
            "tuoiKhoiNam": luck["startAge"]["years"],
            "tuoiKhoiThang": luck["startAge"]["months"],
            "ngayKhoiVan": _date(dt.date.fromisoformat(luck["startDate"])),
            "cacVan": [
                _pair(v)
                | {
                    "thuTu": v["order"],
                    "tuoiTu": v["fromAge"],
                    "namTu": v["fromYear"],
                    "namDen": v["toYear"],
                    "thapThanCan": v["tenGod"],
                    "truongSinh": v["growth"],
                }
                for v in luck["pillars"]
            ],
        },
    }
    for key, name in (("year", "truNam"), ("month", "truThang"), ("day", "truNgay")):
        la_so[name] = _pillar(data["pillars"][key])
    if data["pillars"]["hour"]:
        la_so["truGio"] = _pillar(data["pillars"]["hour"])
    if data["lifePalace"]:
        la_so["menhCung"] = _pair(data["lifePalace"])
    out = {"laSo": la_so}
    if data["annual"]:
        a = data["annual"]
        out["tieuVan"] = _pair(a) | {
            "nam": a["year"],
            "tuoiMu": a["age"],
            "thapThanCan": a["tenGod"],
            "thuan": a["forward"],
        }
    return out


def _birth(vao: dict) -> dt.date:
    if "gio" in vao:
        return dt.datetime(vao["yyyy"], vao["mm"], vao["dd"], vao["gio"], vao["phut"])
    return dt.date(vao["yyyy"], vao["mm"], vao["dd"])


def _case_id(case: dict) -> str:
    v = case["vao"]
    hour = f"{v['gio']:02d}{v['phut']:02d}" if "gio" in v else "khong-gio"
    return f"{v['yyyy']}-{v['mm']:02d}-{v['dd']:02d}-{hour}-{v['gioiTinh']}"


def _term_moment(dai_van: dict) -> dt.datetime:
    d = dai_van["tietMocNgay"]
    return dt.datetime(
        d["year"], d["month"], d["day"], dai_van["tietMocGio"], dai_van["tietMocPhut"],
        tzinfo=bat_tu.UTC7,
    )  # fmt: skip


@pytest.mark.parametrize("case", _GOLDEN["ca"], ids=_case_id)
def test_ca_kiem_nhat_nguyet(case):
    vao = case["vao"]
    want = _norm(case["ra"])
    want["laSo"]["daiVan"].pop("lyDo")
    chart = bat_tu.build_chart(_birth(vao), vao["gioiTinh"] == "Nam", vao["namXem"])
    got = _as_golden(chart)
    got_luck, want_luck = got["laSo"]["daiVan"], want["laSo"]["daiVan"]
    if (vao["yyyy"], vao["mm"], vao["dd"]) in _TERM_MINUTE_OFF:
        gap = _term_moment(got_luck) - _term_moment(want_luck)
        assert abs(gap) == dt.timedelta(minutes=1)
        for key in ("tietMocNgay", "tietMocGio", "tietMocPhut"):
            got_luck[key] = want_luck[key]
    assert got == want


def test_ca_kiem_du_28_la_va_du_cac_nhanh():
    cases = _GOLDEN["ca"]
    assert len(cases) == 28 and "thanSat" not in json.dumps(cases)
    assert {c["vao"]["gioiTinh"] for c in cases} == {"Nam", "Nữ"}
    assert any("gio" not in c["vao"] for c in cases)  # không rõ giờ sinh
    assert any(c["vao"].get("gio") == 23 for c in cases)  # đầu giờ Tý
    assert any("tieuVan" not in c["ra"] for c in cases if "gio" in c["vao"])  # năm xem < sinh
    assert {c["ra"]["laSo"]["daiVan"]["thuan"] for c in cases} == {True, False}


@pytest.mark.parametrize("case", _GOLDEN["ca"], ids=_case_id)
def test_cong_thuc_khoi_van_theo_tiet_moc_cua_ca_kiem(case):
    """Số ngày, tuổi và ngày khởi vận chỉ từ lúc sinh và thời khắc tiết mốc của ca kiểm
    (tách khỏi sai số vài giây của solar_terms)."""
    vao, dai_van = case["vao"], case["ra"]["laSo"]["daiVan"]
    born = dt.datetime(
        vao["yyyy"], vao["mm"], vao["dd"], vao.get("gio", 12), vao["phut"], tzinfo=bat_tu.UTC7
    )
    _, days, years, months, start = bat_tu.luck_start(born, _term_moment(dai_van))
    assert (days, years, months, _date(start)) == (
        dai_van["soNgay"],
        dai_van["tuoiKhoiNam"],
        dai_van["tuoiKhoiThang"],
        dai_van["ngayKhoiVan"],
    )


@pytest.mark.parametrize(
    "moment",
    [
        "2026-01-11 08:30",
        "1990-03-15 23:30",  # đầu giờ Tý: trụ ngày hôm sau
        "2000-02-04 19:39",  # 1 phút trước Lập xuân
        "2000-02-04 19:41",
        "1996-12-29 21:45",
    ],
)
def test_four_pillars_dung_chung_voi_ky_mon(moment):
    local = dt.datetime.strptime(moment, "%Y-%m-%d %H:%M").replace(tzinfo=kymon.VN_TZ)
    c = kymon.build_chart(local)
    assert bat_tu.four_pillars(local) == (c.year, c.month, c.day, c.hour)


def test_four_pillars_doi_nam_o_lap_xuan_va_ngay_luc_23h():
    before = bat_tu.four_pillars(dt.datetime(2000, 2, 4, 19, 39, tzinfo=bat_tu.UTC7))
    after = bat_tu.four_pillars(dt.datetime(2000, 2, 4, 19, 41, tzinfo=bat_tu.UTC7))
    assert [bat_tu.pillar_text(p) for p in before[:2]] == ["Kỷ Mão", "Đinh Sửu"]
    assert [bat_tu.pillar_text(p) for p in after[:2]] == ["Canh Thìn", "Mậu Dần"]
    late = bat_tu.four_pillars(dt.datetime(1971, 8, 6, 23, 22, tzinfo=bat_tu.UTC7))
    next_day = bat_tu.four_pillars(dt.datetime(1971, 8, 7, 0, 30, tzinfo=bat_tu.UTC7))
    assert late.day == next_day.day and late.hour == next_day.hour  # Giáp Tý, giờ Giáp Tý
    with pytest.raises(ValueError):
        bat_tu.four_pillars(dt.datetime(2000, 1, 1, 8))


def test_bang_thap_than_va_truong_sinh():
    giap = 0
    assert [bat_tu.ten_god(giap, s) for s in range(10)] == [
        "Tỷ Kiên", "Kiếp Tài", "Thực Thần", "Thương Quan", "Thiên Tài",
        "Chính Tài", "Thất Sát", "Chính Quan", "Thiên Ấn", "Chính Ấn",
    ]  # fmt: skip
    # Tràng Sinh: Giáp ở Hợi, Ất ở Ngọ, Bính/Mậu ở Dần, Đinh/Kỷ ở Dậu, Canh ở Tỵ, Tân ở Tý,
    # Nhâm ở Thân, Quý ở Mão.
    starts = [11, 6, 2, 9, 2, 9, 5, 0, 8, 3]
    assert [bat_tu.growth_stage(s, b) for s, b in enumerate(starts)] == ["Tràng Sinh"] * 10
    assert bat_tu.growth_stage(0, 2) == "Lâm Quan" and bat_tu.growth_stage(1, 3) == "Lâm Quan"


@pytest.mark.parametrize(
    "arg,when,male",
    [
        ("15/3/1990 9h30 nam", "1990-03-15 09:30", True),
        ("15-03-1990 21:15 nữ", "1990-03-15 21:15", False),
        ("15/3/1990 12h đêm nam", "1990-03-15 00:00", True),  # 12h đêm = 0h, cùng ngày
        ("15/3/1990 7:30 pm nữ", "1990-03-15 19:30", False),
        ("15/3/1990 9 giờ 30 phút tối nữ", "1990-03-15 21:30", False),
        ("15/3/1990 10g sáng nam", "1990-03-15 10:00", True),
        ("15/3/1990 giờ Dần nam", "1990-03-15 04:00", True),  # giữa giờ Dần
        ("âm 20/2/1990 giờ Tỵ nữ", "1990-03-16 10:00", False),
        ("âm 20/6n/2025 9h nữ", "2025-08-13 09:00", False),  # tháng 6 nhuận
        ("15/3/1990 nữ", None, False),  # không rõ giờ
    ],
)
def test_parse_ngay_gio_gioi_tinh(arg, when, male, monkeypatch):
    monkeypatch.setattr(bat_tu, "_now", lambda: dt.datetime(2026, 11, 23, tzinfo=bat_tu.VN_TZ))
    c = bat_tu.parse(arg)
    assert isinstance(c, bat_tu.Chart), c
    assert c.male is male and c.view_year == 2026
    if when is None:
        assert not c.has_hour and c.birth.hour == 12 and "hour" not in c.pillars
    else:
        assert c.has_hour and c.birth.strftime("%Y-%m-%d %H:%M") == when
        assert c.birth.utcoffset() == dt.timedelta(hours=7)


@pytest.mark.parametrize(
    "arg,view",
    [
        ("15/3/1990 9h nam năm xem 2030", 2030),
        ("15/3/1990 9h nam xem 2031", 2031),
        ("15/3/1990 9h nam 2027", 2027),  # "nam" vẫn là giới tính
        ("15/3/1990 9h nu nam xem 2028", 2028),
    ],
)
def test_parse_nam_xem(arg, view):
    c = bat_tu.parse(arg)
    assert isinstance(c, bat_tu.Chart)
    assert c.view_year == view and c.annual is not None and c.annual.year == view


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("", "Cú pháp"),
        ("15/3/1990 9h", "Thiếu giới tính"),
        ("15/3/1990 9 nam", "ghi kèm h"),
        ("15/3/1990 25h nam", "Giờ sinh không hợp lệ"),
        ("15/3/1990 9h75 nam", "Giờ sinh không hợp lệ"),
        ("1/1/2000 gio ty nam", "Giờ Tý hay giờ Tỵ"),
        ("31/2/1990 9h nam", "Ngày sinh không hợp lệ"),
        ("1/1/1899 9h nam", "1900-2100"),
        ("âm 30/2/2026 9h nam", "không có ngày 30"),
        ("âm 20/12/2100 9h nam", "1900-2100"),  # rơi sang tháng 1/2101 dương
        ("15/3/1990 9h nam năm xem 1800", "Năm xem"),
    ],
)
def test_parse_loi(arg, expected):
    result = bat_tu.parse(arg)
    assert isinstance(result, str) and expected in result


def test_la_so_15_3_1990():
    c = bat_tu.parse("15/3/1990 9h30 nam năm xem 2026")
    assert isinstance(c, bat_tu.Chart)
    assert [p.text for p in c.pillars.values()] == ["Canh Ngọ", "Kỷ Mão", "Kỷ Mão", "Kỷ Tỵ"]
    assert c.pillars["year"].ten_god == "Thương Quan" and c.pillars["day"].ten_god == "Nhật chủ"
    hidden = [(bat_tu.CAN[h.stem], h.role, h.ten_god) for h in c.pillars["year"].hidden]
    assert hidden == [("Đinh", "Bản khí", "Thiên Ấn"), ("Kỷ", "Trung khí", "Tỷ Kiên")]
    assert (c.pillars["hour"].growth, c.pillars["year"].self_seat) == ("Đế Vượng", "Mộc Dục")
    assert bat_tu.pillar_text(c.life_palace) == "Ất Dậu"
    assert bat_tu.pillar_text(c.conception) == "Canh Ngọ"
    assert c.elements == {"Kim": 1, "Mộc": 2, "Thủy": 0, "Hỏa": 2, "Thổ": 3}
    assert c.luck.forward and c.luck.term_name == "Thanh minh"  # dương nam thuận
    assert [v.text for v in c.luck.pillars[:2]] == ["Canh Thìn", "Tân Tỵ"]
    assert (c.annual.age, c.annual.text, c.annual.ten_god) == (37, "Bính Ngọ", "Chính Ấn")
    assert c.notes == []


def test_ghi_chu_sinh_23h():
    c = bat_tu.parse("6/8/1971 23h22 nữ")
    assert c.pillars["day"].text == "Giáp Tý" and c.pillars["hour"].text == "Giáp Tý"
    assert c.notes == [
        "Sinh 23h-24h (đầu giờ Tý): trụ ngày tính sang ngày hôm sau là Giáp Tý; sách khác "
        "đổi trụ ngày lúc 0h thì trụ ngày là Quý Hợi (nhật chủ Quý Thủy), trụ giờ giữ nguyên."
    ]


def test_ghi_chu_sat_gio_giao_tiet():
    after = bat_tu.parse("8/8/1945 5:48 nam")  # Lập thu 05:05
    assert after.pillars["month"].text == "Giáp Thân"
    assert after.notes == [
        "Sinh sau lúc giao tiết Lập thu (05:05 08/08/1945) 43 phút: giờ sinh thật chỉ cần "
        "sớm hơn chừng ấy thì trụ tháng là Quý Mùi."
    ]
    before = bat_tu.parse("4/2/2000 19h nữ")  # Lập xuân 19:40: đổi cả trụ năm
    assert before.notes == [
        "Sinh trước lúc giao tiết Lập xuân (19:40 04/02/2000) 40 phút: giờ sinh thật chỉ "
        "cần muộn hơn chừng ấy thì trụ tháng là Mậu Dần và trụ năm là Canh Thìn."
    ]
    far = bat_tu.parse("4/2/2000 21h41 nữ").notes  # sau Lập xuân 2 giờ 1 phút
    assert [n for n in far if "giao tiết" in n] == [] and "trước Tết" in far[0]
    near = bat_tu.parse("4/2/2000 21h39 nữ").notes
    assert near[0].startswith("Sinh sau lúc giao tiết Lập xuân (19:40 04/02/2000) 119 phút")


def test_ghi_chu_lap_xuan_khac_tet():
    after_li_chun = bat_tu.parse("5/2/2024 8h nữ")
    assert after_li_chun.pillars["year"].text == "Giáp Thìn"
    assert after_li_chun.notes == [
        "Sinh sau Lập xuân (15:27 04/02/2024) nhưng trước Tết (10/02/2024): trụ năm đã là "
        "Giáp Thìn vì Bát Tự đổi năm ở Lập xuân; năm âm lịch (tuổi tính theo Tết, như Tử "
        "Vi) vẫn là Quý Mão."
    ]
    after_tet = bat_tu.parse("30/1/2025 10h nam")
    assert after_tet.pillars["year"].text == "Giáp Thìn" and after_tet.pillar_year == 2024
    assert after_tet.notes == [
        "Sinh sau Tết (29/01/2025) nhưng trước Lập xuân (21:10 03/02/2025): năm âm lịch đã "
        "là Ất Tỵ nhưng trụ năm vẫn là Giáp Thìn vì Bát Tự đổi năm ở Lập xuân, không ở Tết."
    ]
    assert bat_tu.parse("28/1/2025 10h nam").notes == []  # trước cả Tết và Lập xuân


def test_khong_ro_gio_sinh():
    c = bat_tu.parse("3/2/2025 nữ")  # Lập xuân 21:10 cùng ngày
    assert list(c.pillars) == ["year", "month", "day"]
    assert c.life_palace is None and c.annual is None and sum(c.elements.values()) == 6
    assert c.notes == [
        "Không rõ giờ sinh mà ngày này giao tiết Lập xuân lúc 21:10: sinh trước giờ đó thì "
        "trụ tháng là Đinh Sửu và trụ năm là Giáp Thìn, từ giờ đó trở đi thì trụ tháng là "
        "Mậu Dần và trụ năm là Ất Tỵ (lá số đang tạm lấy 12h00).",
        "Không rõ giờ sinh: lá số 3 trụ (không có trụ giờ, mệnh cung, tiểu vận); đại vận "
        "tạm tính từ 12h00 nên tuổi khởi vận có thể lệch tới ±2 tháng; nếu sinh 23h-24h thì "
        "trụ ngày là Giáp Thìn.",
    ]
    text = bat_tu.format_chart(c)
    assert "Tứ trụ (năm · tháng · ngày): Giáp Thìn · Đinh Sửu · Quý Mão" in text
    assert "Ngũ hành 6 chữ" in text and "Tiểu vận" not in text and "Mệnh cung" not in text


def test_gio_theo_ten_chi_co_ghi_chu():
    c = bat_tu.parse("15/3/1990 giờ Tý nữ")  # ngày Kỷ Mão: giờ Tý là Giáp Tý
    assert c.hour_by_name and c.pillars["hour"].text == "Giáp Tý"
    assert c.notes[0].startswith("Giờ sinh ghi theo tên giờ Tý: tính như 00:00")
    assert "gõ 23h" in c.notes[0]
    assert "Gõ /battu_chitiet 15/3/1990 giờ Tý nữ" in bat_tu.format_chart(c)


def test_tieu_van_can_gio_va_nam_xem_tu_nam_sinh():
    c = bat_tu.parse("15/3/1990 9h30 nam năm xem 1989")
    assert c.annual is None and "trước năm sinh" in bat_tu.format_chart(c)
    first = bat_tu.parse("15/3/1990 9h30 nam năm xem 1990").annual
    assert (first.age, first.text) == (1, "Canh Ngọ")  # trụ giờ Kỷ Tỵ đi thuận 1 bước


def test_format_chart(monkeypatch):
    monkeypatch.setattr(bat_tu, "_now", lambda: dt.datetime(2026, 11, 23, tzinfo=bat_tu.VN_TZ))
    text = bat_tu.format_chart(bat_tu.parse("15/3/1990 9h30 nam"))
    assert text.startswith("☯️ Bát Tự - 15/03/1990 09:30 (UTC+7), Nam\n")
    assert "Tứ trụ (năm · tháng · ngày · giờ): Canh Ngọ · Kỷ Mão · Kỷ Mão · Kỷ Tỵ" in text
    assert "Nhật chủ: Kỷ Thổ (âm) · Mệnh cung: Ất Dậu · Thai nguyên: Canh Ngọ" in text
    assert "▸ NĂM Canh Ngọ (Kim · Hỏa) · Thương Quan · Lộ Bàng Thổ" in text
    assert "Tàng can Đinh (bản khí, Thiên Ấn), Kỷ (trung khí, Tỷ Kiên)" in text
    assert "🔄 Đại vận thuận (dương nam, năm can Canh)" in text
    assert "▸ 26-35 tuổi (2017-2026): Nhâm Ngọ · Chính Tài · Lâm Quan ◀ năm 2026" in text
    assert "Tiểu vận năm 2026 (Bính Ngọ, tuổi mụ 37): Bính Ngọ · Chính Ấn" in text
    assert "📌 Nhật chủ Kỷ (Thổ âm): như đất ruộng vườn" in text
    assert "Lưu ý" not in text  # lá này không có ghi chú quy ước
    assert text.endswith(
        "ℹ️ Bát Tự mang tính tham khảo, giải trí. Gõ /battu_chitiet 15/3/1990 09:30 nam để AI "
        "luận giải."
    )
    noted = bat_tu.format_chart(bat_tu.parse("6/8/1971 23h22 nữ năm xem 2027"))
    assert "⚠️ Lưu ý:\n• Sinh 23h-24h (đầu giờ Tý)" in noted
    assert "Gõ /battu_chitiet 6/8/1971 23:22 nữ năm xem 2027 để AI luận giải." in noted


def test_chart_data_hop_dong_json():
    data = bat_tu.chart_data(bat_tu.parse("15/3/1990 9h30 nam năm xem 2026"))
    json.dumps(data)  # dựng được JSON
    assert data["birth"] == {
        "date": "1990-03-15",
        "time": "09:30",
        "utcOffset": "+07:00",
        "hourByName": False,
    }
    assert data["pillars"]["day"]["tenGod"] == "Nhật chủ"
    assert data["pillars"]["year"]["hiddenStems"][0] == {
        "stem": "Đinh",
        "element": "Hỏa",
        "role": "Bản khí",
        "tenGod": "Thiên Ấn",
    }
    assert data["luck"]["term"] == {"name": "Thanh minh", "at": "1990-04-05T08:13+07:00"}
    assert [v["isCurrent"] for v in data["luck"]["pillars"]].count(True) == 1
    assert data["annual"]["text"] == "Bính Ngọ" and data["annual"]["age"] == 37
    assert data["elements"] == {"Kim": 1, "Mộc": 2, "Thủy": 0, "Hỏa": 2, "Thổ": 3}
    assert set(data["tenGodMeanings"]) == set(bat_tu.TEN_GOD_GROUPS)
    no_hour = bat_tu.chart_data(bat_tu.parse("3/2/2025 nữ"))
    assert no_hour["pillars"]["hour"] is None and no_hour["birth"]["time"] is None
    assert no_hour["lifePalace"] is None and no_hour["annual"] is None
    assert len(no_hour["notes"]) == 2


def test_khong_nhac_trung_quoc_va_chu_han():
    """Chủ bot yêu cầu: chữ hiện cho người dùng (bot, web, API, prompt) không nhắc Trung
    Quốc và không có chữ Hán."""
    pages = [bat_tu.USAGE, bat_tu.NOTE, bat_tu.DISCLAIMER]
    for arg in ("15/3/1990 9h30 nam", "6/8/1971 23h22 nữ", "3/2/2025 nữ", "5/2/2024 8h nữ",
                "8/8/1945 5:48 nam", "15/3/1990 giờ Tý nữ"):  # fmt: skip
        c = bat_tu.parse(arg)
        pages += [
            bat_tu.format_chart(c),
            json.dumps(bat_tu.chart_data(c), ensure_ascii=False),
            bat_tu.build_ai_prompt(c),
        ]
    assert _BANNED.findall("\n".join(pages)) == []


async def test_ai_chi_luan_giai_la_so_da_lap(monkeypatch):
    from ai import orchestrator

    prompts = []

    async def fake_ask(prompt):
        prompts.append(prompt)
        return SimpleNamespace(text="  Luận giải  ")

    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    c = bat_tu.parse("15/3/1990 9h30 nam năm xem 2027")
    assert await bat_tu.explain_with_ai(c) == "Luận giải"
    assert "KHÔNG tự lập lại" in prompts[0] and "Năm xem: 2027." in prompts[0]
    assert bat_tu.format_chart(c) in prompts[0]
