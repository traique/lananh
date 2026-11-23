"""Trích bộ ca kiểm gọn từ ``ca-kiem/`` của Nhật Nguyệt làm test hồi quy.

    python scripts/make_golden_nhatnguyet.py <thư mục repo nhatnguyet-lunar-ziwei-bazi-numerology>

Nguồn: https://github.com/Nhat-Nguyet/nhatnguyet-lunar-ziwei-bazi-numerology (thư mục
``ca-kiem/``, engine 2.3.0). Giấy phép CC BY 4.0 - Dữ liệu: Nhật Nguyệt (nhatnguyet.org).
Đã sửa đổi: chỉ lấy một phần ca (chọn tất định theo bước nhảy, chạy lại ra đúng file
cũ), bỏ các khoá tiếng Anh/Trung (``...En``, ``...Zh``), các đoạn diễn giải dài và cách
cục Tử Vi (chuỗi mô tả của cách cục trùng bộ tri thức CC BY-NC), rồi nén về định dạng gọn
mô tả trong khoá ``dinhDang`` của từng file. Ghi vào ``test/data/nhatnguyet/``; test đọc
ở ``test/test_golden_nhatnguyet.py`` và test của từng tính năng.

Ca kiểm là đáp án của MỘT engine (theo các trường phái nó chọn), không phải chân lý:
chỗ repo này chọn phái khác được ghi trong docs/quy-uoc-truong-phai.md và test bỏ qua
đúng những ca đó.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "test" / "data" / "nhatnguyet"
SOURCE = "https://github.com/Nhat-Nguyet/nhatnguyet-lunar-ziwei-bazi-numerology (ca-kiem/)"
ATTRIBUTION = "Dữ liệu: Nhật Nguyệt (nhatnguyet.org)"
LICENSE = "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/) - đã trích và sửa đổi"
_TRANSLATED = re.compile(r"[a-z0-9](En|Zh)$")


def _strip(value, drop: set[str]):
    """Bỏ khoá dịch (...En/...Zh) và khoá diễn giải dài, đệ quy."""
    if isinstance(value, dict):
        return {
            k: _strip(v, drop)
            for k, v in value.items()
            if not _TRANSLATED.search(k) and k not in drop
        }
    if isinstance(value, list):
        return [_strip(v, drop) for v in value]
    return value


def _every(items: list, count: int) -> list:
    """``count`` phần tử rải đều (tất định) trên ``items``."""
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def _load(src: Path, name: str) -> dict:
    return json.loads((src / "ca-kiem" / name).read_text(encoding="utf-8"))


def _header(raw: dict, fmt: str) -> dict:
    return {
        "nguon": SOURCE,
        "ghiNguon": ATTRIBUTION,
        "giayPhep": LICENSE,
        "phienBanDongCo": raw.get("phienBanDongCo"),
        "mon": raw.get("mon"),
        "soCaGoc": raw.get("soCa"),
        "dinhDang": fmt,
    }


def _am_lich(src: Path) -> dict:
    raw = _load(src, "am-lich.json")
    d2a = {7: [], 8: [], 9: []}
    a2d = {7: [], 8: [], 9: []}
    years = []
    for case in raw["ca"]:
        v, r = case["vao"], case["ra"]
        tz = int(v.get("muiGio") or 7)
        if v["phep"] == "duong-sang-am":
            am = r["am"]
            row = [v["yyyy"], v["mm"], v["dd"], tz, am["day"], am["month"], am["year"], am["leap"]]
            if tz == 7:
                row += [
                    r["canChiNam"]["label"],
                    r["canChiThang"]["label"],
                    r["canChiNgay"]["label"],
                    r["tietKhi"],
                ]
            d2a[tz].append(row)
        elif v["phep"] == "am-sang-duong":
            dg = r["duong"]
            a2d[tz].append(
                [v["nam"], v["thang"], v["ngay"], v["nhuan"], tz, r["hopLe"]]
                + [dg["year"], dg["month"], dg["day"]]
            )
        elif v["phep"] == "moc-tiet-khi":
            years.append(case)
    terms = []
    for case in _every(years, 25) + [c for c in years if c["vao"]["nam"] in (1953, 2026)]:
        for t in case["ra"]:
            n = t["ngay"]
            moment = (
                f"{n['year']:04d}-{n['month']:02d}-{n['day']:02d}T{t['gio']:02d}:{t['phut']:02d}"
            )
            terms.append([case["vao"]["nam"], t["index"], moment])
    return _header(
        raw,
        "duongSangAm: [năm, tháng, ngày dương, múi giờ, ngày âm, tháng âm, năm âm, nhuận"
        " (+ can chi năm, tháng, ngày, tiết khí lúc 12h trưa khi múi giờ 7)]; amSangDuong: "
        "[năm âm, tháng âm, ngày âm, nhuận, múi giờ, hợp lệ, năm, tháng, ngày dương] - Nhật "
        "Nguyệt coi cờ nhuận của tháng không nhuận là tháng thường (hợp lệ); mocTietKhi: "
        "[năm dương, chỉ số tiết (0 = Xuân phân), giờ Việt Nam tới phút]",
    ) | {
        "duongSangAm": _every(d2a[7], 400) + _every(d2a[8], 80) + _every(d2a[9], 40),
        "amSangDuong": _every(a2d[7], 240) + _every(a2d[8], 40) + _every(a2d[9], 20),
        "mocTietKhi": terms,
    }


def _gio_hoang_dao(src: Path) -> dict:
    raw = _load(src, "auspicious-hours.json")
    rows = []
    for case in _every(raw["ca"], 120):
        v, r = case["vao"], case["ra"]
        bits = "".join("1" if h["hoangDao"] else "0" for h in r["gio"])
        rows.append(
            [v["yyyy"], v["mm"], v["dd"], r["canChiNgay"], bits, [h["than"] for h in r["gio"]]]
        )
    return _header(
        raw,
        "ca: [năm, tháng, ngày, can chi ngày, 12 ký tự 1 = giờ hoàng đạo (từ giờ Tý), 12 thần"
        " trực giờ (Nhật Nguyệt viết Nguyên Vũ = Huyền Vũ, Câu Trần = Câu Trận)]",
    ) | {"ca": rows}


def _tu_vi_case(case: dict, drop: set[str]) -> dict:
    c = _strip(case, drop)
    ra = c["ra"]
    ra.pop("cachCuc", None)
    ra.get("hanNam", {}).pop("nguyetHan", None)
    for cung in ra["laSo"]["cung"]:
        cung["sao"] = [
            [s["ten"], s.get("loai", ""), s.get("mieuVuong") or "", s.get("tuHoa") or ""]
            for s in cung.get("sao", [])
        ]
    return c


def _tu_vi(src: Path) -> dict:
    raw = _load(src, "tu-vi.json")
    phai = _load(src, "tu-vi-phai.json")
    drop = set(raw["khoaBo"]) | set(phai["khoaBo"])
    split = [
        c for c in phai["ca"] if (c["vao"].get("schools") or {}).get("leapMonth") == "split-at-15"
    ]
    return _header(
        raw,
        "ca: như ca-kiem/tu-vi.json (vao, ra.laSo, ra.daiVan, ra.hanNam không có nguyetHan,"
        " ra.saoLuu); bỏ cachCuc; mỗi sao trong laSo.cung[].sao là [tên, loại, miếu vượng,"
        " tứ hóa]. caPhai: ca tu-vi-phai.json có schools.leapMonth = split-at-15 (quy tắc"
        " tháng nhuận repo này dùng)",
    ) | {
        "ca": [_tu_vi_case(c, drop) for c in _every(raw["ca"], 20)],
        "caPhai": [_tu_vi_case(c, drop) for c in _every(split, 8)],
    }


def _bat_tu(src: Path) -> dict:
    raw = _load(src, "bat-tu.json")
    drop = set(raw["khoaBo"]) | {"thanSat"}
    cases = [c for c in raw["ca"] if not c["ra"].get("loi")]
    return _header(
        raw,
        "ca: như ca-kiem/bat-tu.json, bỏ thần sát (thanSat, mỗi phái một khác) và khoá dịch",
    ) | {"ca": [_strip(c, drop) for c in _every(cases, 28)]}


def _than_so_hoc(src: Path) -> dict:
    raw = _load(src, "than-so-hoc.json")
    drop = set(raw["khoaBo"])
    return _header(raw, "ca: như ca-kiem/than-so-hoc.json, bỏ khoá dịch") | {
        "ca": [_strip(c, drop) for c in _every(raw["ca"], 100)]
    }


def _le_hoi(src: Path) -> dict:
    raw = _load(src, "le-hoi.json")
    keep = {1917, 1965, 1985, 2023, 2026, 2027, 2054, 2100}
    return _header(
        raw,
        "ca: [{vao: {nam}, ra: [{id, ngay YYYY-MM-DD}]}] như ca-kiem/le-hoi.json, các năm "
        "có tháng nhuận/Tết lệch đáng chú ý",
    ) | {"ca": [c for c in raw["ca"] if c["vao"]["nam"] in keep]}


def _phong_thuy(src: Path) -> dict:
    raw = _load(src, "fengshui.json")
    drop = set(raw["khoaBo"]) | {"nghia", "dungDe", "yNghia", "moTa"}
    by_op: dict[str, list] = {}
    for case in raw["ca"]:
        by_op.setdefault(case["vao"].get("phep", ""), []).append(case)
    pick = (
        _every(by_op["lo-ban"], 100)
        + _every(by_op["cung-phi"], 100)
        + by_op["cac-loai-thuoc"]
        + by_op["ten-cung-nho"]
    )
    return _header(
        raw,
        "ca: như ca-kiem/fengshui.json, chỉ các phép lo-ban, cung-phi, cac-loai-thuoc, "
        "ten-cung-nho; bỏ các câu nghĩa/mô tả dài và khoá dịch",
    ) | {"ca": [_strip(c, drop) for c in pick]}


SUITES = {
    "am-lich.json": _am_lich,
    "gio-hoang-dao.json": _gio_hoang_dao,
    "tu-vi.json": _tu_vi,
    "bat-tu.json": _bat_tu,
    "than-so-hoc.json": _than_so_hoc,
    "le-hoi.json": _le_hoi,
    "phong-thuy.json": _phong_thuy,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="thư mục repo Nhật Nguyệt (có ca-kiem/)")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in SUITES.items():
        data = build(args.source)
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        (OUT / name).write_text(text + "\n", encoding="utf-8")
        print(f"{name}: {len(text) // 1024} KB")


if __name__ == "__main__":
    main()
