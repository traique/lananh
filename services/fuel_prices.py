"""Giá xăng dầu trong nước tính bằng code (không gọi AI): bảng Petrolimex và PVOIL.

Nguồn: https://giaxanghomnay.com/ (như route oil-live của repo tham khảo
traique/stock-portfolio). Trang có:
- bảng Petrolimex: Xăng/dầu | Tăng giảm | Tăng giảm kỳ trước | Giá vùng 1 | Giá vùng 2
- bảng PVOIL: Xăng/dầu | Tăng giảm | Tăng giảm kỳ trước | Giá
- dòng "Giá của Petrolimex cập nhật YYYY-MM-DD HH:MM:SS"
- vài bảng khác (lịch sử điều chỉnh, giá dầu thế giới) - bỏ qua.

Tên mặt hàng đổi theo từng kỳ điều hành ("Xăng E5 RON 92", "Xăng E10 RON 95 Mức 5",
"DO 0,05S Mức 2"...) nên KHÔNG hard-code tên: đọc mọi bảng, nhận cột theo tiêu đề
(bỏ dấu), giữ nguyên tên dòng như nguồn ghi. Bảng có "vùng 1/vùng 2" là Petrolimex,
bảng có "kỳ trước" + 1 cột "Giá" là PVOIL; bảng lịch sử ("giá cũ/giá mới") và bảng
thế giới (không có "kỳ trước") bị loại.

Tài nguyên: dùng cache/fetch của services/vn_market_data (1 bản trong RAM, cache lỗi
60 giây, body tối đa 1 MB, timeout ngắn), TTL 1 giờ - giá chỉ đổi theo kỳ điều hành.
Lỗi thì raise MarketDataError; nơi gọi nói thật hoặc tra web có nhãn "tham khảo".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.text_normalize import fold
from services import vn_market_data as market

FUEL_URL = "https://giaxanghomnay.com/"
_TTL_SEC = 3600
_MAX_BYTES = 1024 * 1024
_MAX_ROWS = 20
# Giá bán lẻ hợp lý (đồng/lít): ngoài khoảng này là đọc nhầm cột/đơn vị.
_PRICE_RANGE = (5_000, 100_000)


@dataclass(frozen=True)
class FuelRow:
    name: str  # như nguồn ghi, vd "Xăng E5 RON 92"
    change: int | None  # "Tăng giảm" (kỳ hiện tại), đồng/lít
    prev_change: int | None  # "Tăng giảm kỳ trước"
    zone1: int | None  # Petrolimex vùng 1 / PVOIL: giá duy nhất
    zone2: int | None = None


@dataclass(frozen=True)
class FuelTable:
    updated: str  # "00:00 27/10/2026" hoặc rỗng
    petrolimex: tuple[FuelRow, ...]
    pvoil: tuple[FuelRow, ...] = ()
    source: str = "giaxanghomnay.com"


def _int(raw: str) -> int | None:
    """ "25,060" / "25.060" / "-1,610" / "+450" / "0" -> số nguyên; rỗng -> None."""
    text = re.sub(r"[\s▲▼↑↓đ]", "", raw or "")
    m = re.fullmatch(r"([+-]?)(\d{1,3}(?:[.,]\d{3})+|\d+)", text)
    if not m:
        return None
    value = int(re.sub(r"[.,]", "", m.group(2)))
    return -value if m.group(1) == "-" else value


def _cells(row) -> list[str]:
    return [" ".join(c.get_text(" ").split()) for c in row.find_all(["th", "td"])]


def _columns(header: list[str]) -> dict[str, int]:
    cols: dict[str, int] = {}
    for i, raw in enumerate(header):
        key = re.sub(r"[^a-z0-9/ ]", "", fold(raw)).strip()
        if i == 0 and ("xang" in key or "dau" in key or "mat hang" in key):
            cols["name"] = 0
        elif "gia cu" in key or "gia moi" in key or "thoi gian" in key:
            cols["history"] = i
        elif "ky truoc" in key:
            cols["prev"] = i
        elif "tang giam" in key or "thay doi" in key:
            cols.setdefault("change", i)
        elif "vung 1" in key:
            cols["zone1"] = i
        elif "vung 2" in key:
            cols["zone2"] = i
        elif key in ("gia", "gia ban", "gia ban le"):
            cols["price"] = i
    return cols


def _read_table(table) -> tuple[str, list[FuelRow]] | None:
    """("petrolimex"|"pvoil", các dòng) nếu là bảng giá trong nước, ngược lại None."""
    rows = table.find_all("tr")
    if not rows:
        return None
    cols = _columns(_cells(rows[0]))
    if "name" not in cols or "prev" not in cols or "history" in cols:
        return None
    if "zone1" in cols:
        kind, price_col = "petrolimex", cols["zone1"]
    elif "price" in cols:
        kind, price_col = "pvoil", cols["price"]
    else:
        return None
    out: list[FuelRow] = []
    for row in rows[1 : _MAX_ROWS + 1]:
        cells = _cells(row)
        if len(cells) <= price_col or not cells[0]:
            continue
        price = _int(cells[price_col])
        if price is None or not _PRICE_RANGE[0] <= price <= _PRICE_RANGE[1]:
            continue
        zone2 = (
            _int(cells[cols["zone2"]]) if "zone2" in cols and len(cells) > cols["zone2"] else None
        )
        change = _int(cells[cols["change"]]) if "change" in cols else None
        out.append(
            FuelRow(
                name=cells[0][:60],
                change=change,
                prev_change=_int(cells[cols["prev"]]),
                zone1=price,
                zone2=zone2,
            )
        )
    return (kind, out) if out else None


_UPDATED_RE = re.compile(
    r"Petrolimex\s+cập nhật\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}:\d{2}))?", re.IGNORECASE
)
_UPDATED_VN_RE = re.compile(
    r"(?:cập nhật|điều chỉnh)[^0-9]{0,30}(?:(\d{1,2}:\d{2})\s*(?:ngày\s*)?)?"
    r"(\d{1,2})/(\d{1,2})/(\d{4})",
    re.IGNORECASE,
)


def _updated(text: str) -> str:
    m = _UPDATED_RE.search(text)
    if m:
        year, month, day, clock = m.groups()
        return f"{clock + ' ' if clock else ''}{int(day):02d}/{int(month):02d}/{year}"
    m = _UPDATED_VN_RE.search(text)
    if m:
        clock, day, month, year = m.groups()
        return f"{clock + ' ' if clock else ''}{int(day):02d}/{int(month):02d}/{year}"
    return ""


def parse_fuel_html(content: bytes | str) -> FuelTable:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "lxml")
    found: dict[str, list[FuelRow]] = {}
    for table in soup.find_all("table"):
        parsed = _read_table(table)
        if parsed is not None and parsed[0] not in found:
            found[parsed[0]] = parsed[1]
    if not found.get("petrolimex"):
        raise market.MarketDataError("Không thấy bảng giá xăng Petrolimex trên trang nguồn")
    text = " ".join(soup.get_text(" ").split())
    return FuelTable(
        updated=_updated(text),
        petrolimex=tuple(found["petrolimex"]),
        pvoil=tuple(found.get("pvoil", ())),
    )


async def _load() -> FuelTable:
    return parse_fuel_html(await market.fetch("GET", FUEL_URL, max_bytes=_MAX_BYTES))


async def get_fuel() -> FuelTable:
    return await market.cached("fuel", _TTL_SEC, _load)


# ─── Lọc + định dạng ────────────────────────────────────────────────────────

KINDS = ("xang", "diesel", "dau_hoa")


def kind_of(name: str) -> str:
    key = fold(name)
    if "dau hoa" in key or re.search(r"\bko\b", key):
        return "dau_hoa"
    if "xang" in key or "ron" in key:
        return "xang"
    if re.search(r"\bdo\b|diesel|dau do", key):
        return "diesel"
    return "khac"


def _change(value: int | None) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "giữ nguyên"
    return f"{'▲' if value > 0 else '▼'}{market.vnd(abs(value))}"


def _line(row: FuelRow, *, zones: bool) -> str:
    price = (
        f"vùng 1 {market.vnd(row.zone1)} · vùng 2 {market.vnd(row.zone2)}"
        if zones and row.zone2
        else market.vnd(row.zone1)
    )
    notes = []
    if row.change:
        notes.append(f"kỳ này {_change(row.change)}")
    if row.prev_change is not None:
        notes.append(f"kỳ trước {_change(row.prev_change)}")
    return f"• {row.name}: {price}" + (f" ({', '.join(notes)})" if notes else "")


def format_fuel(table: FuelTable, kind: str | None = None) -> str:
    """Bảng giá xăng dầu (đồng/lít). ``kind`` = "xang" | "diesel" | "dau_hoa" để lọc."""

    def pick(rows: tuple[FuelRow, ...]) -> list[FuelRow]:
        return [r for r in rows if kind is None or kind_of(r.name) == kind]

    petrolimex, pvoil = pick(table.petrolimex), pick(table.pvoil)
    if not petrolimex and not pvoil:  # lọc hết (tên lạ): hiện đủ bảng còn hơn im
        petrolimex, pvoil = list(table.petrolimex), list(table.pvoil)
    head = "⛽ Giá xăng dầu Petrolimex" + (f" (cập nhật {table.updated})" if table.updated else "")
    lines = [head]
    lines.extend(_line(r, zones=True) for r in petrolimex)
    if pvoil:
        lines.append("PVOIL:")
        lines.extend(_line(r, zones=False) for r in pvoil)
    lines.append(
        f"(Nguồn {table.source} theo bảng giá Petrolimex/PVOIL; đơn vị đồng/lít; vùng 2 = "
        "địa bàn xa kho, giá cao hơn chút)"
    )
    return "\n".join(lines)


def fuel_headline(table: FuelTable) -> str:
    """1 dòng cho bản tin sáng: giá vùng 1 các mặt hàng Petrolimex."""
    parts = [f"{r.name} {market.vnd(r.zone1)}" for r in table.petrolimex[:6]]
    stamp = f" (cập nhật {table.updated})" if table.updated else ""
    return "Xăng dầu Petrolimex vùng 1: " + " · ".join(parts) + f" đ/lít{stamp}"
