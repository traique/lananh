"""So lá số Tử Vi (services/tuvi.py) với ca kiểm của Nhật Nguyệt: lá số gốc (cục,
Mệnh/Thân, mọi sao kèm độ sáng chính tinh và Tứ Hóa, Tuần/Triệt, ba vòng), đại hạn,
vận năm (tuổi mụ, lưu niên, tiểu hạn, đại hạn của năm, lưu Tứ Hóa) và 5 sao lưu.

Ca kiểm: Nhật Nguyệt (nhatnguyet.org), CC BY 4.0 - test/data/nhatnguyet/tu-vi.json,
trích từ ca-kiem/tu-vi.json ("ca", phái mặc định) và ca-kiem/tu-vi-phai.json có
schools.leapMonth = split-at-15 ("caPhai"), đã sửa đổi; khoá "dinhDang" mô tả định dạng.

Chỉ bỏ qua đúng chỗ phái của ca kiểm khác phái repo này (``_REPO_SCHOOLS``):
- Tứ Hóa gốc khi năm sinh can Canh, lưu Tứ Hóa khi năm xem can Canh: repo theo Tân
  Biên (Khoa Thái Âm, Kỵ Thiên Đồng = transformations "quanshu"); ca kiểm mặc định ghi
  ngược Khoa/Kỵ. Ca có transformations = quanshu thì so đủ.
- Lá sinh tháng nhuận mà tháng an theo phái của ca kiểm khác split-at-15 (trong "ca":
  nhuận từ ngày 16, ca kiểm an như tháng chính, repo an như tháng sau): bỏ cả lá.
- Ca "caPhai" chọn thêm fireBell = quanshu (Hỏa Tinh, Linh Tinh) hay heavenlyKitchen =
  chinese (Thiên Trù tuổi Quý): bỏ đúng các sao đó.
"""

import datetime as dt
import json
from collections import Counter
from pathlib import Path

import pytest

from services import tuvi

_DATA = json.loads(
    (Path(__file__).parent / "data" / "nhatnguyet" / "tu-vi.json").read_text(encoding="utf-8")
)
_CASES = [(kind, i, case) for kind in ("ca", "caPhai") for i, case in enumerate(_DATA[kind])]
_DEFAULTS = {"fireBell": "vietnamese", "leapMonth": "whole", "transformations": "vietnamese",
             "heavenlyKitchen": "vietnamese", "injuryMessenger": "standard"}  # fmt: skip
_REPO_SCHOOLS = {**_DEFAULTS, "leapMonth": "split-at-15", "transformations": "quanshu"}
_SKIP_STARS = {
    "hoa_linh": {"Hỏa Tinh", "Linh Tinh"},
    "thien_tru_quy": {"Thiên Trù"},
    "thien_thuong_su": {"Thiên Thương", "Thiên Sứ"},
}
_NAME = {"Bác Sĩ": "Bác Sỹ", "Thiên Riêu": "Thiên Diêu"}  # tên của repo -> tên ca kiểm
_NAP_AM = {"Lô Trung Hỏa": "Lư Trung Hỏa", "Kim Bạch Kim": "Kim Bạc Kim"}
_BRIGHT = {"M": "Miếu", "V": "Vượng", "Đ": "Đắc", "B": "Bình", "H": "Hãm"}


def _placement_month(lunar: dict, rule: str) -> int:
    if not lunar["leap"] or rule == "whole" or (rule == "split-at-15" and lunar["day"] <= 15):
        return lunar["month"]
    return lunar["month"] % 12 + 1


def _skips(case: dict) -> set[str]:
    """Những phần của ca bị bỏ vì phái của ca kiểm khác phái repo này."""
    born = case["ra"]["laSo"]
    schools = {**_DEFAULTS, **(case["vao"].get("schools") or {})}
    out = set()
    lunar = born["ngaySinhAm"]
    if _placement_month(lunar, schools["leapMonth"]) != _placement_month(
        lunar, _REPO_SCHOOLS["leapMonth"]
    ):
        out.add("thang_nhuan")
    if schools["transformations"] != _REPO_SCHOOLS["transformations"]:
        if born["canChiNam"].startswith("Canh "):
            out.add("tu_hoa_goc_can_canh")
        if case["ra"]["hanNam"]["canChiNam"].startswith("Canh "):
            out.add("luu_tu_hoa_nam_canh")
    if schools["fireBell"] != _REPO_SCHOOLS["fireBell"]:
        out.add("hoa_linh")
    if schools["heavenlyKitchen"] != _REPO_SCHOOLS["heavenlyKitchen"]:
        if born["canChiNam"].startswith("Quý "):
            out.add("thien_tru_quy")
    if schools["injuryMessenger"] != _REPO_SCHOOLS["injuryMessenger"]:
        if born["amDuongMenh"] in ("Âm Nam", "Dương Nữ"):
            out.add("thien_thuong_su")
    return out


def _chart(vao: dict) -> tuvi.Chart:
    solar = dt.date(vao["yyyy"], vao["mm"], vao["dd"])
    return tuvi.chart_for(solar, vao["gio"], vao["gioiTinh"] == "Nam", view_year=vao["namXem"])


def _kind_of(star: tuvi.Star) -> str:
    if star.is_main:
        return "chinh-tinh"
    if star.name in tuvi._BAC_SI:
        return "bac-sy"
    return "thai-tue" if star.name in tuvi._THAI_TUE else "phu-tinh"


def _user_stars(c: tuvi.Chart, pos: int, with_hoa: bool, drop: set[str]) -> list[tuple]:
    out = [(c.trang_sinh[pos], "truong-sinh", "", "")]
    for s in c.star_list(pos):
        bright = _BRIGHT[s.brightness] if s.is_main and s.brightness else ""
        hoa = (s.hoa or "") if with_hoa else ""
        out.append((_NAME.get(s.name, s.name), _kind_of(s), bright, hoa))
    return sorted(star for star in out if star[0] not in drop)


def _ref_stars(palace: dict, with_hoa: bool, drop: set[str]) -> list[tuple]:
    return sorted(
        (ten, loai, mv, hoa if with_hoa else "")
        for ten, loai, mv, hoa in palace["sao"]
        if ten not in drop
    )


def _ref_hoa(h: dict) -> tuple:
    pos = h["viTri"] if h["coTrenLaSo"] else None
    return (h["hoa"], h["sao"], pos, h["chiCung"], h["tenCung"])


def _id(case: tuple) -> str:
    kind, i, c = case
    v = c["vao"]
    return f"{kind}{i}-{v['dd']}/{v['mm']}/{v['yyyy']}-gio{v['gio']}-{v['gioiTinh']}"


@pytest.mark.parametrize("kind,idx,case", _CASES, ids=[_id(c) for c in _CASES])
def test_khop_ca_kiem_nhat_nguyet(kind, idx, case):
    skips = _skips(case)
    if "thang_nhuan" in skips:
        pytest.skip("tháng nhuận: tháng an theo phái của ca kiểm khác split-at-15 của repo")
    vao, ra = case["vao"], case["ra"]
    born = ra["laSo"]
    c = _chart(vao)
    data = tuvi.chart_data(c)

    # Lá số gốc
    lunar = born["ngaySinhAm"]
    want = (lunar["day"], lunar["month"], lunar["year"], lunar["leap"])
    assert (c.lunar_day, c.lunar_month, c.lunar_year, c.leap) == want
    assert c.cuc == born["cuc"] and data["cuc"].casefold() == born["tenCuc"].casefold()
    assert (c.menh, c.than) == (born["cungMenh"], born["cungThan"])
    assert data["yinYang"] == born["amDuongMenh"]
    assert _NAP_AM.get(data["napAm"], data["napAm"]) == born["banMenhNapAm"]
    with_hoa = "tu_hoa_goc_can_canh" not in skips
    drop = set().union(*(_SKIP_STARS[s] for s in skips if s in _SKIP_STARS))
    for palace in born["cung"]:
        pos = palace["viTri"]
        assert c.palace_at(pos) == palace["tenCung"]
        assert f"{tuvi.CAN[c.palace_can(pos)]} {tuvi.CHI[pos]}" == palace["canChi"]
        flags = (palace["laThan"], palace["tuanTrung"], palace["trietLo"])
        assert (pos == c.than, pos in c.tuan, pos in c.triet) == flags
        mine, theirs = _user_stars(c, pos, with_hoa, drop), _ref_stars(palace, with_hoa, drop)
        assert mine == theirs, palace["chi"]

    # Đại hạn: 12 chặng từ Mệnh, tuổi bắt đầu/kết thúc, chính tinh của cung
    for chang in ra["daiVan"]:
        pos = chang["viTri"]
        mains = ", ".join(s.name for s in c.star_list(pos) if s.is_main)
        want = ((chang["tuTuoi"], chang["denTuoi"]), chang["tenCung"], chang.get("chinhTinh", ""))
        assert (c.dai_han[pos], c.palace_at(pos), mains) == want

    # Vận năm
    v = tuvi.year_luck(c, vao["namXem"])
    year = ra["hanNam"]
    assert (v["year"], v["canChi"], v["age"]) == (year["nam"], year["canChiNam"], year["tuoiMu"])
    for mine, theirs in (("luuNien", "luuNien"), ("tieuHan", "tieuVan")):
        want = (year[theirs + "ViTri"], year[theirs + "Chi"], year[theirs + "TenCung"])
        assert (v[mine]["branchIndex"], v[mine]["branch"], v[mine]["palace"]) == want
    dai = v["daiHan"] or {"fromAge": 0, "toAge": 0, "palace": ""}
    want = (year["daiVanTuTuoi"], year["daiVanDenTuoi"], year["daiVanTenCung"])
    assert (dai["fromAge"], dai["toAge"], dai["palace"]) == want
    if "luu_tu_hoa_nam_canh" not in skips:
        got = [
            (h["hoa"], h["star"], h["branchIndex"], h["branch"], h["palace"]) for h in v["luuTuHoa"]
        ]
        assert got == [_ref_hoa(h) for h in year["luuTuHoa"]]

    # 5 sao lưu và quan hệ với cung đại hạn, tiểu hạn, lưu niên
    keys = ("id", "name", "byYearStem", "branchIndex", "branch", "palace")
    rel = ("withDaiHan", "withTieuHan", "withLuuNien")
    ref_keys = ("ma", "ten", "theoCan", "viTri", "chi", "tenCung")
    ref_rel = ("voiDaiVan", "voiTieuVan", "voiLuuNien")
    assert [tuple(s[k] for k in keys + rel) for s in v["saoLuu"]] == [
        tuple(s[k] for k in ref_keys + ref_rel) for s in ra["saoLuu"]
    ]
    assert data["vanNam"] == (v if c.has_year_luck else None)


def test_dem_ca_bo_qua_vi_khac_phai():
    """28 lá (20 "ca" + 8 "caPhai"), không lá nào bị bỏ hẳn. Bỏ Tứ Hóa gốc của 5 lá năm
    sinh Canh (4 + 1; lá Canh còn lại của caPhai chọn transformations = quanshu nên so
    đủ); Hỏa Linh của 4 lá và Thiên Trù của 1 lá tuổi Quý theo lựa chọn fireBell,
    heavenlyKitchen của caPhai; năm xem 2026 là Bính Ngọ nên không bỏ lưu Tứ Hóa. Hai lá
    caPhai sinh tháng nhuận từ
    ngày 16 (split-at-15 như repo) được so đủ."""
    counts = Counter(s for _, _, case in _CASES for s in _skips(case))
    assert len(_CASES) == 28
    assert counts == {"tu_hoa_goc_can_canh": 5, "hoa_linh": 4, "thien_tru_quy": 1}
    leap_16 = [
        c
        for c in _DATA["caPhai"]
        if c["ra"]["laSo"]["ngaySinhAm"]["leap"] and c["ra"]["laSo"]["ngaySinhAm"]["day"] >= 16
    ]
    assert len(leap_16) == 2
    assert all(_chart(c["vao"]).an_month == c["ra"]["laSo"]["placementMonth"] for c in leap_16)
