"""Phong thủy cho việc đo đạc nội thất: thước Lỗ Ban (/loban) và cung phi Bát Trạch (/cungphi).

Bảng thước và quy tắc cung phi chuyển ngữ từ python/nhatnguyet/_thuoc_lo_ban.py, fengshui.py của Nhật Nguyệt (MIT License, Copyright (c) 2026 Nhat Nguyet - nhatnguyet.org)
(phép tra thước, phép tính số cung phi, bảng tám hướng). Câu giải thích ngắn của
từng cung, từng sao do bot tự viết (assets/phong_thuy.yaml). Tính bằng code,
offline, không gọi AI; đối chiếu ca kiểm ở test/test_phong_thuy.py.

Thước Lỗ Ban: ba thước chia cung đều nhau; số đo lấy phần dư theo chiều dài thước
rồi đọc cung chứa nó, đúng mép giữa hai cung thì tính vào cung sau.
- Thông thủy 52,2 cm, 8 cung x 5 cung nhỏ: khoảng rỗng (lòng cửa, cửa sổ, ô thoáng).
- Dương trạch 42,9 cm, 8 cung x 4 cung nhỏ: khối đặc (bệ, bậc, giường, tủ, bàn).
- Âm phần 38,8 cm, 10 cung, không chia cung nhỏ: bàn thờ, đồ thờ, mộ phần.
Tính tới 0,1 mm như lõi tham chiếu (mỗi cung thước 52,2 cm dài 65,25 mm, không làm
tròn 65 mm như nhiều bảng in). Cung xấu kèm số đo tốt gần nhất về hai phía (bước 1 mm).

Cung phi Bát Trạch theo NĂM ÂM LỊCH (sinh trước Tết thuộc năm trước, lịch giờ Việt
Nam): năm Giáp Tý 1864 mở đầu Thượng nguyên, nam khởi cung 1 lùi mỗi năm một cung,
nữ khởi cung 5 tiến mỗi năm một cung; ra 5 (không có quẻ) thì nam lấy Khôn 2, nữ
lấy Cấn 8. Khảm 1, Chấn 3, Tốn 4, Ly 9 là Đông tứ mệnh, còn lại Tây tứ mệnh. Tám
hướng theo bảng du niên: 4 tốt (Sinh Khí, Thiên Y, Diên Niên, Phục Vị) rồi 4 xấu
(Họa Hại, Lục Sát, Ngũ Quỷ, Tuyệt Mệnh) - thứ tự của lõi tham chiếu, không phải
thang điểm (các sách xếp hạng sao khác nhau).

Chữ hiện ra (bot, web, API) chỉ dùng tiếng Việt: bảng ở đây không giữ chữ Hán.
"""

from __future__ import annotations

import datetime as dt
import functools
import math
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

from core.text_normalize import fold, nfc
from services import lunar

_TEXTS_PATH = Path(__file__).resolve().parent.parent / "assets" / "phong_thuy.yaml"
MIN_YEAR, MAX_YEAR = 1900, 2100
MAX_LENGTH_MM = 100_000  # 100 m, thừa cho mọi số đo nhà cửa

USAGE_LOBAN = (
    "Cú pháp: /loban <số đo> [mm|cm|m] [loại đồ]\n"
    "Ví dụ: /loban 81 cửa · /loban 1m27 bàn thờ · /loban 850mm tủ · /loban 1,8m giường\n"
    "Không ghi đơn vị là cm (80 = 80 cm, 1m2 = 1,2 m). Không ghi loại đồ thì tra cả 3 "
    "thước: thông thủy 52,2 cm (lòng cửa, cửa sổ), dương trạch 42,9 cm (giường, tủ, bàn, "
    "bệ), âm phần 38,8 cm (bàn thờ, mộ).\n"
    "Mỗi lần tra một số đo (chiều rộng, chiều cao tra riêng)."
)
USAGE_CUNGPHI = (
    "Cú pháp: /cungphi <ngày sinh dd/mm/yyyy hoặc năm sinh> <nam|nữ>\n"
    "Ví dụ: /cungphi 15/3/1990 nam · /cungphi 1990 nữ · /cungphi âm 20/1/1990 nữ\n"
    "Chỉ ghi năm thì tính là năm âm lịch (sinh trước Tết thuộc năm trước)."
)
DISCLAIMER_LOBAN = "ℹ️ Thước Lỗ Ban là quy tắc truyền thống, chỉ mang tính tham khảo."
DISCLAIMER_CUNGPHI = "ℹ️ Cung phi Bát Trạch là quy tắc truyền thống, chỉ mang tính tham khảo."
_YEAR_ERROR = f"Năm sinh cần trong khoảng {MIN_YEAR}-{MAX_YEAR}."
_LENGTH_ERROR = "Số đo cần lớn hơn 0 và không quá 100 m."


@functools.lru_cache(maxsize=1)
def _texts() -> dict:
    with _TEXTS_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _fmt(value: float, decimals: int = 1) -> str:
    """Số kiểu Việt, bỏ số 0 thừa: 80.45 -> "80,45", 80.0 -> "80" (decimals >= 1)."""
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".").replace(".", ",")


def _num(x: float) -> int | float:
    """Số gọn cho JSON: bỏ sai số dấu phẩy động, 820.0 thành 820."""
    r = round(float(x), 6)
    return int(r) if r.is_integer() else r


# ─── Thước Lỗ Ban ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Sector:
    """Một cung trên thước: tên, tốt/xấu, các cung nhỏ chia đều trong cung."""

    name: str
    good: bool
    subs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ruler:
    id: str
    name: str
    length_mm: float
    sectors: tuple[Sector, ...]

    @property
    def sector_mm(self) -> float:
        return self.length_mm / len(self.sectors)


# Cung và cung nhỏ theo thứ tự trên thước (True = cung tốt).
_THONG_THUY = (
    Sector("Quý Nhân", True, ("Quyền lộc", "Trung tín", "Tác quan", "Phát đạt", "Thông minh")),
    Sector("Hiểm Họa", False, ("Tán thành", "Thời nhân", "Thất hiếu", "Tai họa", "Trường bệnh")),
    Sector("Thiên Tai", False, ("Hoàn tử", "Quan tài", "Thân bệnh", "Thất tài", "Cô quả")),
    Sector("Thiên Tài", True, ("Thi thơ", "Văn học", "Thanh quý", "Tác lộc", "Thiên lộc")),
    Sector("Phúc Lộc", True, ("Tử tôn", "Phú quý", "Tấn bửu", "Thập thiện", "Văn chương")),
    Sector("Cô Độc", False, ("Bạc nghịch", "Vô vọng", "Ly tán", "Tửu thực", "Dâm dục")),
    Sector("Thiên Tặc", False, ("Phòng bệnh", "Chiêu ôn", "Ôn tai", "Ngục tù", "Quan tài")),
    Sector("Tể Tướng", True, ("Đại tài", "Thi thơ", "Hoạnh tài", "Hiếu tử", "Quý nhân")),
)
_DUONG_TRACH = (
    Sector("Tài", True, ("Tài đức", "Bảo khố", "Lục hợp", "Nghênh phúc")),
    Sector("Bệnh", False, ("Thoát tài", "Công sự", "Lao chấp", "Cô quả")),
    Sector("Ly", False, ("Trưởng khố", "Kiếp tài", "Quan quỷ", "Thất thoát")),
    Sector("Nghĩa", True, ("Thêm đinh", "Ích lợi", "Quý tử", "Đại cát")),
    Sector("Quan", True, ("Thuận khoa", "Hoạnh tài", "Tiến ích", "Phú quý")),
    Sector("Kiếp", False, ("Tử biệt", "Thoái khẩu", "Ly hương", "Tài thất")),
    Sector("Hại", False, ("Tai chí", "Tử tuyệt", "Bệnh lâm", "Khẩu thiệt")),
    Sector("Bản", True, ("Tài chí", "Đăng khoa", "Tiến bảo", "Hưng vượng")),
)
# Các nguồn không thống nhất cung nhỏ của thước âm phần nên lõi tham chiếu không chia.
_AM_PHAN = tuple(
    Sector(name, good)
    for name, good in (("Đinh", True), ("Hại", False), ("Vượng", True), ("Khổ", False),
                       ("Nghĩa", True), ("Quan", True), ("Tử", False), ("Hưng", True),
                       ("Thất", False), ("Tài", True))
)  # fmt: skip
RULERS: dict[str, Ruler] = {
    "thong_thuy": Ruler("thong_thuy", "Thước thông thủy 52,2cm", 522.0, _THONG_THUY),
    "duong_trach": Ruler("duong_trach", "Thước dương trạch 42,9cm", 429.0, _DUONG_TRACH),
    "am_phan": Ruler("am_phan", "Thước âm phần 38,8cm", 388.0, _AM_PHAN),
}


def _ruler_texts(ruler_id: str) -> dict:
    value = _texts().get("thuoc", {}).get(ruler_id, {})
    return value if isinstance(value, dict) else {}


def _mod(a: float, m: float) -> float:
    r = a - m * int(a / m)
    return r + m if r < 0 else r


def _round1(v: float) -> float:
    return int(v * 10 + 0.5) / 10


def _locate(ruler: Ruler, position: float) -> tuple[int, int]:
    """(chỉ số cung, chỉ số cung nhỏ hoặc -1) của một vị trí 0 <= position < chiều dài
    thước - cùng thứ tự phép tính dấu phẩy động với lõi tham chiếu để khớp từng mép."""
    n = len(ruler.sectors)
    size = ruler.length_mm / n
    idx = min(int(position / size), n - 1)
    subs = ruler.sectors[idx].subs
    if not subs:
        return idx, -1
    sub_size = size / len(subs)
    return idx, min(int((position - idx * size) / sub_size), len(subs) - 1)


def _is_good(ruler: Ruler, length_mm: float) -> bool:
    return ruler.sectors[_locate(ruler, _mod(length_mm, ruler.length_mm))[0]].good


def _point(ruler: Ruler, length_mm: float, offset: int) -> dict:
    idx, j = _locate(ruler, _mod(length_mm, ruler.length_mm))
    sector = ruler.sectors[idx]
    return {
        "lengthMm": _num(length_mm),
        "lengthCm": _num(_round1(length_mm / 10)),
        "offsetMm": offset,
        "sector": sector.name,
        "subSector": sector.subs[j] if j >= 0 else None,
    }


def _nearest_good(ruler: Ruler, length_mm: float) -> list[dict]:
    """Số đo tốt gần nhất phía dưới (nếu còn > 0) rồi phía trên, mỗi bước 1 mm."""
    found = []
    steps = int(ruler.length_mm) + 1
    for d in range(1, steps + 1):
        if length_mm - d <= 0:
            break
        if _is_good(ruler, length_mm - d):
            found.append(_point(ruler, length_mm - d, -d))
            break
    for d in range(1, steps + 1):
        if _is_good(ruler, length_mm + d):
            found.append(_point(ruler, length_mm + d, d))
            break
    return found


def ruler_reading(length_mm: float, ruler: str) -> dict:
    """Đọc một số đo (mm, dùng nguyên không làm tròn) trên một thước ("thong_thuy",
    "duong_trach", "am_phan"). ``sector.fromMm``/``toMm`` là đoạn của cung trong vòng
    thước chứa số đo; ``nearestAuspicious`` chỉ có khi cung xấu. ValueError khi sai."""
    r = RULERS.get(ruler)
    if r is None:
        raise ValueError(f"Không có thước {ruler!r}: dùng {', '.join(RULERS)}.")
    if (
        isinstance(length_mm, bool)
        or not isinstance(length_mm, int | float)
        or not math.isfinite(length_mm)
        or length_mm <= 0
    ):
        raise ValueError(_LENGTH_ERROR)
    position = _mod(length_mm, r.length_mm)
    idx, j = _locate(r, position)
    sector = r.sectors[idx]
    start = r.length_mm * int(length_mm / r.length_mm) + idx * r.sector_mm
    texts = _ruler_texts(r.id)
    return {
        "ruler": r.id,
        "rulerName": r.name,
        "rulerMm": _num(r.length_mm),
        "use": texts.get("dung_de", ""),
        "cycle": int(length_mm / r.length_mm) + 1,
        "positionMm": _num(_round1(position)),
        "sector": {
            "index": idx,
            "name": sector.name,
            "auspicious": sector.good,
            "meaning": texts.get("cung", {}).get(sector.name, ""),
            "fromMm": _num(start),
            "toMm": _num(start + r.sector_mm),
        },
        "subSector": sector.subs[j] if j >= 0 else None,
        "auspicious": sector.good,
        "nearestAuspicious": [] if sector.good else _nearest_good(r, length_mm),
    }


def rulers_data() -> list[dict]:
    """Ba thước và các cung của một vòng thước (để vẽ thước trên web)."""
    out = []
    for r in RULERS.values():
        texts = _ruler_texts(r.id)
        sectors = [
            {
                "index": i,
                "name": s.name,
                "auspicious": s.good,
                "meaning": texts.get("cung", {}).get(s.name, ""),
                "fromMm": _num(i * r.sector_mm),
                "toMm": _num((i + 1) * r.sector_mm),
                "subSectors": list(s.subs),
            }
            for i, s in enumerate(r.sectors)
        ]
        out.append(
            {
                "ruler": r.id,
                "name": r.name,
                "lengthMm": _num(r.length_mm),
                "use": texts.get("dung_de", ""),
                "sectorMm": _num(r.sector_mm),
                "sectors": sectors,
            }
        )
    return out


def loban_data(length_mm: float, ruler: str | None = None) -> dict:
    """Kết quả /loban dạng dict khoá camelCase cho web và JSON API: số đo (mm, làm tròn
    0,1 mm) trên một thước hoặc cả 3 (ruler None). ValueError khi số đo/thước sai."""
    if isinstance(length_mm, bool) or not isinstance(length_mm, int | float):
        raise ValueError(_LENGTH_ERROR)
    mm = math.floor(length_mm * 10 + 0.5) / 10 if math.isfinite(length_mm) else 0.0
    if not 0 < mm <= MAX_LENGTH_MM:
        raise ValueError(_LENGTH_ERROR)
    ids = [ruler] if ruler is not None else list(RULERS)
    return {
        "lengthMm": _num(mm),
        "lengthCm": _num(mm / 10),
        "rulers": [ruler_reading(mm, r) for r in ids],
        "disclaimer": DISCLAIMER_LOBAN,
    }


_UNIT_MM = {"mm": 1, "li": 1, "ly": 1, "cm": 10, "phân": 10, "phan": 10, "dm": 100,
            "tấc": 100, "tac": 100, "m": 1000, "mét": 1000, "met": 1000}  # fmt: skip
# Loại đồ -> thước. Xét lần lượt âm phần, dương trạch, thông thủy: "bàn thờ" là đồ
# thờ chứ không phải bàn, "cửa tủ"/"khung cửa" là khối đặc chứ không phải lòng cửa.
_ITEM_WORDS = (
    (
        "am_phan",
        ("thờ", "mộ", "mồ mả", "huyệt", "bát hương", "bát nhang", "lư hương", "bài vị",
         "linh vị", "thần tài", "ông địa", "gia tiên", "âm phần", "âm trạch"),
    ),
    (
        "duong_trach",
        ("giường", "tủ", "bàn", "kệ", "ghế", "sofa", "sô pha", "bệ", "bậc", "thềm",
         "cầu thang", "bếp", "bục", "khung", "phủ bì", "dương trạch"),
    ),
    (
        "thong_thuy",
        ("cửa", "cổng", "ô thoáng", "lỗ thoáng", "giếng trời", "lọt lòng", "thông thủy",
         "thông thuỷ", "khoảng rỗng"),
    ),
)  # fmt: skip
_WORD_RE = re.compile(r"[^\W\d_]+")
_LOBAN_NAME_RE = re.compile(r"\b(?:lỗ|lo)\s*ban\b")
# Chữ không nói lên loại đồ: có những chữ này thôi thì không báo "chưa rõ loại".
_FILLER = frozenset(
    fold(w)
    for w in ("đo", "thước", "cái", "chiếc", "cho", "của", "chiều", "rộng", "cao", "dài",
              "sâu", "ngang", "kích", "khoảng", "là", "số", "xem", "tra", *_UNIT_MM)
)  # fmt: skip


def _word_match(word: str, target: str) -> bool:
    # Gõ không dấu thì so bỏ dấu; có dấu thì so đúng chữ ("mở" không phải "mộ").
    return word == fold(target) if word.isascii() else word == target


def ruler_for(item: str) -> str | None:
    """Thước hợp với loại đồ ("cửa sổ" -> "thong_thuy", "tủ" -> "duong_trach",
    "bàn thờ" -> "am_phan"), None nếu không nhận ra."""
    words = _WORD_RE.findall(_LOBAN_NAME_RE.sub(" ", nfc(item or "").lower()))
    for ruler, phrases in _ITEM_WORDS:
        for phrase in phrases:
            target = phrase.split()
            for i in range(len(words) - len(target) + 1):
                if all(_word_match(words[i + k], t) for k, t in enumerate(target)):
                    return ruler
    return None


@dataclass(frozen=True)
class LobanQuery:
    """Tham số /loban đã đọc: số đo (mm, làm tròn 0,1 mm), đơn vị hiển thị ("mm" hay
    "cm"), thước theo loại đồ (None = cả 3), chữ loại đồ và cờ "chưa rõ loại đồ"."""

    length_mm: float
    unit: str = "cm"
    ruler: str | None = None
    item: str = ""
    unknown_item: bool = False


# Số đo kèm đơn vị: "80", "80cm", "80,5 cm", "850mm", "1,2m", "1m2" / "1 mét 25" (mét + phần lẻ).
_LENGTH_RE = re.compile(
    r"(?<!\d)(?<!\d[.,])(\d+(?:[.,]\d+)?)\s*"
    r"(?:(m|mét|met)(?![^\W\d_])\s*(\d+)(?!\d|[.,]\d)"
    r"|(mm|cm|dm|m|mét|met|phân|phan|tấc|tac|li|ly)(?![^\W\d_]))?"
)
_MORE_RE = re.compile(r"\s*[x×*]\s*\d")


def parse_loban(argument: str) -> LobanQuery | str:
    """Đọc "<số đo> [đơn vị] [loại đồ]" (số đo đứng đâu cũng được), hoặc câu báo lỗi."""
    low = nfc(argument or "").lower().strip()
    m = _LENGTH_RE.search(low)
    if not m:
        return USAGE_LOBAN
    if _MORE_RE.match(low, m.end()):
        return "Mỗi lần tra một số đo (chiều rộng, chiều cao tra riêng).\n" + USAGE_LOBAN
    number, extra, unit = m.group(1), m.group(3), m.group(4)
    if extra:  # 1m2 = 1,2 m; 2m05 = 2,05 m
        if not number.isdigit():
            return USAGE_LOBAN
        value, factor, unit = Decimal(f"{number}.{extra}"), 1000, "m"
    else:
        value, factor = Decimal(number.replace(",", ".")), _UNIT_MM.get(unit or "cm", 10)
    mm = (value * factor).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if not 0 < mm <= MAX_LENGTH_MM:
        return f"{_LENGTH_ERROR}\n{USAGE_LOBAN}"
    rest = re.sub(r"[^\w\s]", " ", low[: m.start()] + " " + low[m.end() :])
    item = re.sub(r"\s+", " ", rest).strip()[:40].strip()
    ruler = ruler_for(item)
    words = _WORD_RE.findall(_LOBAN_NAME_RE.sub(" ", item))
    unknown = ruler is None and any(fold(w) not in _FILLER for w in words)
    return LobanQuery(
        length_mm=float(mm),
        unit="mm" if unit in ("mm", "li", "ly") else "cm",
        ruler=ruler,
        item=item,
        unknown_item=unknown,
    )


def _amount(mm: float, unit: str) -> str:
    """Số đo theo đơn vị hiển thị, không kèm đơn vị: 805 -> "80,5" (cm) / "805" (mm)."""
    return _fmt(mm, 1) if unit == "mm" else _fmt(mm / 10, 2)


def _length(mm: float, unit: str) -> str:
    return f"{_amount(mm, unit)} {unit}"


def _cell(ruler: Ruler, length_mm: float) -> tuple[int, int]:
    """(vòng thước, chỉ số cung) của một số đo, đúng như phép tra."""
    return int(length_mm / ruler.length_mm), _locate(ruler, _mod(length_mm, ruler.length_mm))[0]


def _span(ruler: Ruler, from_mm: float, to_mm: float, fine: bool) -> tuple[float, float]:
    """Số đo đầu và cuối thuộc cung [from_mm, to_mm), bước 1 mm (0,1 mm khi số đo có lẻ
    phần mười), nhỏ nhất là 1 bước. Mép trên thuộc cung sau; riêng số đo đúng mép có khi
    bị phép chia dấu phẩy động (như lõi tham chiếu) xếp sang cung bên cạnh, nên dò lại
    bằng chính phép tra để đoạn in ra luôn khớp kết quả tra."""
    step = 1 if fine else 10  # tính theo 0,1 mm
    first = max(math.ceil(round(from_mm * 10, 4) / step), 1) * step
    last = (math.ceil(round(to_mm * 10, 4) / step) - 1) * step
    key = _cell(ruler, (from_mm + to_mm) / 2)
    if first > step and _cell(ruler, (first - step) / 10) == key:
        first -= step
    elif _cell(ruler, first / 10) != key:
        first += step
    if _cell(ruler, (last + step) / 10) == key:
        last += step
    elif _cell(ruler, last / 10) != key:
        last -= step
    return first / 10, last / 10


def _ruler_lines(r: dict, length_mm: float, unit: str) -> list[str]:
    sector = r["sector"]
    sub = f" · cung nhỏ {r['subSector']}" if r["subSector"] else ""
    fine = not float(length_mm).is_integer()
    first, last = _span(RULERS[r["ruler"]], sector["fromMm"], sector["toMm"], fine)
    lines = [
        f"▸ {r['rulerName']} - {r['use']}",
        f"{'✅ TỐT' if r['auspicious'] else '❌ XẤU'} · cung {sector['name']}{sub}",
        f"{sector['name']}: {sector['meaning']}",
        f"Vòng thước thứ {r['cycle']}, vị trí {_length(r['positionMm'], unit)} · cung "
        f"{sector['name']}: {_amount(first, unit)}-{_length(last, unit)}",
    ]
    options = []
    for p in sorted(r["nearestAuspicious"], key=lambda p: abs(p["offsetMm"])):
        name = p["sector"] + (f" - {p['subSector']}" if p["subSector"] else "")
        way = "thêm" if p["offsetMm"] > 0 else "bớt"
        options.append(
            f"{_length(p['lengthMm'], unit)} ({way} {_length(abs(p['offsetMm']), unit)}, "
            f"cung {name})"
        )
    if options:
        lines.append("👉 Số tốt gần nhất: " + " · ".join(options))
    return lines


def loban_text(argument: str) -> str:
    """Trả lời lệnh /loban <số đo> [đơn vị] [loại đồ] (câu báo lỗi kèm cú pháp khi sai)."""
    query = parse_loban(argument)
    if isinstance(query, str):
        return query
    data = loban_data(query.length_mm, query.ruler)
    mm, unit = data["lengthMm"], query.unit
    head = f"📏 Thước Lỗ Ban · {_length(mm, unit)} ({_length(mm, 'cm' if unit == 'mm' else 'mm')})"
    lines = [head + (f" · {query.item}" if query.item else "")]
    if query.unknown_item:
        lines.append(
            f'📝 Chưa rõ "{query.item}" là lòng cửa, đồ đặc hay đồ thờ nên tra cả 3 thước.'
        )
    for r in data["rulers"]:
        lines.append("")
        lines.extend(_ruler_lines(r, mm, unit))
    lines.append("")
    if query.ruler is None:
        lines.append(
            "💡 Ghi thêm loại đồ để chỉ tra thước hợp: /loban 80 cửa · /loban 80 tủ · "
            "/loban 80 bàn thờ"
        )
    lines.append(DISCLAIMER_LOBAN)
    return "\n".join(lines)


# ─── Cung phi Bát Trạch ─────────────────────────────────────────────────────

_KUA_EPOCH = 1864  # năm Giáp Tý mở đầu Thượng nguyên
_DIRECTIONS = ("Bắc", "Đông Bắc", "Đông", "Đông Nam", "Nam", "Tây Nam", "Tây", "Tây Bắc")
_TRIGRAMS = {1: ("Khảm", "Thủy"), 2: ("Khôn", "Thổ"), 3: ("Chấn", "Mộc"), 4: ("Tốn", "Mộc"),
             6: ("Càn", "Kim"), 7: ("Đoài", "Kim"), 8: ("Cấn", "Thổ"), 9: ("Ly", "Hỏa")}  # fmt: skip
_EAST_GROUP = (1, 3, 4, 9)
_STARS = (
    ("sinh_khi", "Sinh Khí"),
    ("thien_y", "Thiên Y"),
    ("dien_nien", "Diên Niên"),
    ("phuc_vi", "Phục Vị"),
    ("hoa_hai", "Họa Hại"),
    ("luc_sat", "Lục Sát"),
    ("ngu_quy", "Ngũ Quỷ"),
    ("tuyet_menh", "Tuyệt Mệnh"),
)
# Hướng của 8 sao (theo thứ tự _STARS: 4 tốt rồi 4 xấu) cho từng số cung phi.
_BAT_TRACH = {
    1: ("Đông Nam", "Đông", "Nam", "Bắc", "Tây", "Tây Bắc", "Đông Bắc", "Tây Nam"),
    2: ("Đông Bắc", "Tây", "Tây Bắc", "Tây Nam", "Đông", "Nam", "Đông Nam", "Bắc"),
    3: ("Nam", "Bắc", "Đông Nam", "Đông", "Tây Nam", "Đông Bắc", "Tây Bắc", "Tây"),
    4: ("Bắc", "Nam", "Đông", "Đông Nam", "Tây Bắc", "Tây", "Tây Nam", "Đông Bắc"),
    6: ("Tây", "Đông Bắc", "Tây Nam", "Tây Bắc", "Đông Nam", "Bắc", "Đông", "Nam"),
    7: ("Tây Bắc", "Tây Nam", "Đông Bắc", "Tây", "Bắc", "Đông Nam", "Nam", "Đông"),
    8: ("Tây Nam", "Tây Bắc", "Tây", "Đông Bắc", "Nam", "Đông", "Bắc", "Đông Nam"),
    9: ("Đông", "Đông Nam", "Bắc", "Nam", "Đông Bắc", "Tây Nam", "Tây", "Tây Bắc"),
}


def _kua_raw(lunar_year: int, male: bool) -> int:
    n = lunar_year - _KUA_EPOCH
    return (-n) % 9 + 1 if male else (n + 4) % 9 + 1


def kua_number(lunar_year: int, male: bool) -> int:
    """Số cung phi 1-9 (không có 5) theo năm ÂM LỊCH và giới tính."""
    k = _kua_raw(lunar_year, male)
    return (2 if male else 8) if k == 5 else k


def cungphi_data(birth: dt.date | int, male: bool) -> dict:
    """Cung phi dạng dict khoá camelCase cho web và JSON API. ``birth`` là ngày sinh
    DƯƠNG lịch (năm âm lấy theo Tết) hoặc số năm ÂM lịch. ValueError khi ngoài phạm vi."""
    notes = []
    birth_date = lunar_date = None
    if isinstance(birth, dt.date):
        if isinstance(birth, dt.datetime):
            birth = birth.date()
        if not MIN_YEAR <= birth.year <= MAX_YEAR + 1:
            raise ValueError(_YEAR_ERROR)
        ld, lm, year, leap = lunar.solar_to_lunar(birth.day, birth.month, birth.year)
        birth_date = birth.isoformat()
        lunar_date = {
            "day": ld,
            "month": lm,
            "year": year,
            "isLeapMonth": leap,
            "text": lunar.lunar_text(ld, lm, year, leap),
        }
        if year != birth.year:
            notes.append(
                f"Sinh trước Tết nên tính theo năm âm lịch {year} ({lunar.year_can_chi(year)})."
            )
    elif isinstance(birth, int) and not isinstance(birth, bool):
        year = birth
        notes.append(
            f"Chỉ có năm sinh nên coi là năm âm lịch {year}. Sinh trước Tết tính năm trước: "
            f"sinh tháng 1-2/{year} dương lịch mà trước Tết thì là năm âm {year - 1}, gõ đủ "
            "ngày sinh để tính chính xác."
        )
    else:
        raise ValueError("Cần ngày sinh dương lịch hoặc năm sinh âm lịch.")
    if not MIN_YEAR - 1 <= year <= MAX_YEAR:  # sinh tháng 1/1900 thuộc năm âm 1899
        raise ValueError(_YEAR_ERROR)
    kua = kua_number(year, male)
    if _kua_raw(year, male) == 5:
        fallback = "nam lấy cung Khôn (2)" if male else "nữ lấy cung Cấn (8)"
        notes.append(f"Số cung tính ra 5 (không ứng với quẻ nào) nên {fallback}.")
    trigram, element = _TRIGRAMS[kua]
    east = kua in _EAST_GROUP
    meanings = _texts().get("sao", {})
    directions = []
    for (star, star_name), direction in zip(_STARS, _BAT_TRACH[kua], strict=True):
        i = _DIRECTIONS.index(direction)
        directions.append(
            {
                "star": star,
                "starName": star_name,
                "direction": direction,
                "fromDeg": (i * 45 + 337.5) % 360,
                "toDeg": i * 45 + 22.5,
                "meaning": meanings.get(star, ""),
            }
        )
    return {
        "birthDate": birth_date,
        "lunarDate": lunar_date,
        "lunarYear": year,
        "yearCanChi": lunar.year_can_chi(year),
        "gender": "male" if male else "female",
        "kua": kua,
        "trigram": trigram,
        "element": element,
        "group": "dong_tu_menh" if east else "tay_tu_menh",
        "groupName": "Đông tứ mệnh" if east else "Tây tứ mệnh",
        "goodDirections": directions[:4],
        "badDirections": directions[4:],
        "notes": notes,
        "howTo": _texts().get("cach_dung_huong", ""),
        "disclaimer": DISCLAIMER_CUNGPHI,
    }


_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)")
_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def parse_cungphi(argument: str) -> tuple[dt.date | int, bool] | str:
    """(ngày sinh dương lịch hoặc năm âm lịch, nam?) từ "15/3/1990 nam", "1990 nữ",
    "âm 20/1/1990 nữ" (ngày âm đổi sang dương), hoặc câu báo lỗi."""
    low = nfc(argument or "").lower().strip()
    m = _DATE_RE.search(low) or _YEAR_RE.search(low)
    if not m:
        return USAGE_CUNGPHI
    rest = low[: m.start()] + " " + low[m.end() :]
    if re.search(r"\b(?:nữ|nu|gái|gai)\b", rest):
        male = False
    elif re.search(r"\b(?:nam|trai)\b", rest):
        male = True
    else:
        return "Thiếu giới tính (nam/nữ).\n" + USAGE_CUNGPHI
    if m.re is _YEAR_RE:
        year = int(m.group(1))
        return (year, male) if MIN_YEAR <= year <= MAX_YEAR else _YEAR_ERROR
    d, mo, y = (int(x) for x in m.groups())
    if not MIN_YEAR <= y <= MAX_YEAR:
        return _YEAR_ERROR
    # Ngày âm: "âm"/"âm lịch" có dấu ở đâu cũng được; không dấu chỉ nhận "am lich"
    # hoặc "am" đứng đầu (như /tuvi).
    if re.search(r"\bâm\b", rest) or "am lich" in rest or re.match(r"am\b", low):
        if not (1 <= d <= 30 and 1 <= mo <= 12):
            return "Ngày âm lịch không hợp lệ.\n" + USAGE_CUNGPHI
        solar = lunar.lunar_to_solar(d, mo, y)
        if solar is None or lunar.solar_to_lunar(*solar) != (d, mo, y, False):
            return f"Tháng {mo} âm lịch năm {y} không có ngày {d}."
        return dt.date(solar[2], solar[1], solar[0]), male
    try:
        return dt.date(y, mo, d), male
    except ValueError:
        return "Ngày sinh không hợp lệ.\n" + USAGE_CUNGPHI


def _direction_line(d: dict) -> str:
    return (
        f"• {d['starName']} - {d['direction']} ({_fmt(d['fromDeg'])}°-{_fmt(d['toDeg'])}°): "
        f"{d['meaning']}"
    )


def cungphi_text(argument: str) -> str:
    """Trả lời lệnh /cungphi <ngày sinh hoặc năm sinh> <nam|nữ> (câu báo lỗi khi sai)."""
    parsed = parse_cungphi(argument)
    if isinstance(parsed, str):
        return parsed
    data = cungphi_data(*parsed)
    gender = "nam" if data["gender"] == "male" else "nữ"
    if data["birthDate"]:
        born = dt.date.fromisoformat(data["birthDate"])
        when = f"sinh {born:%d/%m/%Y} ({data['lunarDate']['text']} âm lịch)"
    else:
        when = f"năm âm lịch {data['lunarYear']} ({data['yearCanChi']})"
    lines = [
        f"🧭 Cung phi Bát Trạch · {gender} · {when}",
        f"Cung {data['trigram']} (số {data['kua']}), hành {data['element']} · {data['groupName']}",
    ]
    lines.extend(f"📝 {note}" for note in data["notes"])
    lines += ["", "✅ 4 hướng tốt"]
    lines.extend(_direction_line(d) for d in data["goodDirections"])
    lines += ["", "❌ 4 hướng xấu"]
    lines.extend(_direction_line(d) for d in data["badDirections"])
    lines.append("")
    if data["howTo"]:
        lines.append(f"💡 {data['howTo']}")
    lines.append(DISCLAIMER_CUNGPHI)
    return "\n".join(lines)
