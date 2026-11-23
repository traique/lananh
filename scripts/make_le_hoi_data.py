"""Rút gọn bộ dữ liệu lễ hội Việt Nam của Nhật Nguyệt thành assets/le_hoi_viet_nam.json.

    python scripts/make_le_hoi_data.py <thư mục repo nhatnguyet-lunar-ziwei-bazi-numerology>

Nguồn: du-lieu/le-hoi-viet-nam.json + du-lieu/tinh-2025.json của
https://github.com/Nhat-Nguyet/nhatnguyet-lunar-ziwei-bazi-numerology - CC BY 4.0,
Dữ liệu: Nhật Nguyệt (nhatnguyet.org). Đã sửa đổi: chỉ giữ cột tiếng Việt bot dùng (bỏ
cột tiếng Anh/Trung, toạ độ, nguồn từng dòng), đổi tên cột cho gọn. Đọc ở
services/le_hoi.py; test ngày theo năm ở test/test_le_hoi.py.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "le_hoi_viet_nam.json"


def _int(raw: str) -> int:
    return int(raw) if str(raw).strip().isdigit() else 0


def _row(item: dict) -> dict:
    row = {
        "id": item["id"],
        "ten": item["ten"].strip(),
        "loai": item["loai_ngay"],  # lunar | solar | theo-nam
        "am": item["ngay_am"],
        "duong": item["ngay_duong"],
        "truoc": _int(item["ngay_truoc"]),
        "sau": _int(item["ngay_sau"]),
        "tinh": [t.strip() for t in item["tinh"].split(";") if t.strip()],
        "noi": item["noi_to_chuc"].strip(),
        "danToc": item["dan_toc"].strip(),
        "nhom": item["nhom"],
        "phamVi": item["pham_vi"],
        "lon": item["muc_do"] == "major",
        "toanQuoc": item["toan_quoc"] == "co",
        "nghiLe": item["ngay_nghi_le"] == "co",
        "congBo": item["lich_cong_bo"].strip(),
        "moTa": item["mo_ta"].strip(),
    }
    return {k: v for k, v in row.items() if v not in ("", [], 0, False) or k in ("id", "ten")}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="thư mục repo Nhật Nguyệt (có du-lieu/)")
    args = parser.parse_args(argv)
    data_dir = args.source / "du-lieu"
    items = json.loads((data_dir / "le-hoi-viet-nam.json").read_text(encoding="utf-8"))
    meta = json.loads((data_dir / "le-hoi-viet-nam.meta.json").read_text(encoding="utf-8"))
    provinces = json.loads((data_dir / "tinh-2025.json").read_text(encoding="utf-8"))
    out = {
        "nguon": (
            "https://github.com/Nhat-Nguyet/nhatnguyet-lunar-ziwei-bazi-numerology "
            f"(du-lieu/le-hoi-viet-nam.json phiên bản {meta.get('phien_ban')}, "
            f"cập nhật {meta.get('cap_nhat')})"
        ),
        "ghiNguon": "Dữ liệu lễ hội: Nhật Nguyệt (nhatnguyet.org)",
        "giayPhep": "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/) - đã rút gọn",
        "tinh": {t["ma"]: t["vi"] for t in provinces["tinh"]},
        "leHoi": [_row(item) for item in items],
    }
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    OUT.write_text(text + "\n", encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}: {len(out['leHoi'])} lễ, {len(text) // 1024} KB")


if __name__ == "__main__":
    main()
