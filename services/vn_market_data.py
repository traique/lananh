"""Số liệu đời sống Việt Nam tính bằng code: giá vàng SJC, tỷ giá Vietcombank,
kết quả xổ số 3 miền (kèm dò vé).

Trước đây "giá vàng hôm nay", "tỷ giá đô" đi đường tra web: viết lại truy vấn
(1 lượt AI), Tavily, đọc sâu 2 trang (2 lượt AI) rồi chat bị ép sang Groq
compound - tới 4 lượt AI cho 1 con số lấy từ snippet. Xổ số còn tệ hơn: không
câu nào kích hoạt tra web nên model có thể bịa số trúng thưởng. Module này
lấy thẳng từ nguồn công khai, parse bằng thư viện chuẩn, KHÔNG gọi AI:

- Tỷ giá: XML Vietcombank (feed tự ghi "1 request mỗi 5 phút" -> TTL 10 phút).
- Vàng: SJC (POST PriceService.ashx). BTMC không dùng vì API cần key.
- Xổ số: RSS xskt.com.vn theo miền; dò vé làm bằng code, không qua LLM.

Giới hạn tài nguyên (RAM 512 MB): HTTP qua client chung (services.http_client),
timeout ngắn, body có trần byte, mỗi nguồn giữ ĐÚNG 1 bản đã parse trong RAM
(5 khoá cố định) và cache lỗi 60 giây để nguồn chết không bị gọi dồn. Lỗi thì
raise MarketDataError; nơi gọi tự rơi về đường tra web cũ, ghi rõ "tham khảo".
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar
from zoneinfo import ZoneInfo

import httpx

from core.text_normalize import fold
from services import http_client, reply_audience

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
T = TypeVar("T")

FX_URL = "https://portal.vietcombank.com.vn/Usercontrols/TVPortal.TyGia/pXML.aspx"
SJC_URL = "https://sjc.com.vn/GoldPrice/Services/PriceService.ashx"
LOTTERY_FEEDS = {
    "mb": "https://xskt.com.vn/rss-feed/mien-bac-xsmb.rss",
    "mn": "https://xskt.com.vn/rss-feed/mien-nam-xsmn.rss",
    "mt": "https://xskt.com.vn/rss-feed/mien-trung-xsmt.rss",
}
REGION_NAMES = {"mb": "miền Bắc", "mn": "miền Nam", "mt": "miền Trung"}
# Giờ quay thưởng (giờ VN) - hỏi "hôm nay" trước giờ này thì chưa có kết quả.
DRAW_TIMES = {"mn": "16h15", "mt": "17h15", "mb": "18h15"}

_TIMEOUT = httpx.Timeout(6.0, connect=4.0)
_TOTAL_TIMEOUT_SEC = 10.0
_MAX_BYTES = 512 * 1024
_FX_TTL_SEC = 600
_GOLD_TTL_SEC = 600
_LOTTERY_TTL_SEC = 300
_ERROR_TTL_SEC = 60
_MAX_DRAWS = 10
_MAX_GOLD_ROWS = 60
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9",
}


class MarketDataError(RuntimeError):
    """Nguồn số liệu lỗi/không parse được - caller tự rơi về đường tra web."""


@dataclass(frozen=True)
class FxRate:
    code: str
    name: str
    buy: float | None  # mua tiền mặt
    transfer: float | None  # mua chuyển khoản
    sell: float | None  # bán ra


@dataclass(frozen=True)
class FxTable:
    updated: str  # "08:03 26/10/2026" (giờ VN) hoặc rỗng
    rates: dict[str, FxRate]
    source: str = "Vietcombank"


@dataclass(frozen=True)
class GoldRow:
    name: str
    branch: str
    buy: float | None  # đồng/lượng
    sell: float | None


@dataclass(frozen=True)
class GoldTable:
    updated: str
    rows: tuple[GoldRow, ...]
    source: str = "SJC"


@dataclass(frozen=True)
class ProvinceResult:
    name: str
    prizes: dict[str, tuple[str, ...]]  # "ĐB", "1".."8" -> các số trúng


@dataclass(frozen=True)
class LotteryDraw:
    region: str  # "mb" | "mn" | "mt"
    date: dt.date
    provinces: tuple[ProvinceResult, ...]
    link: str = ""


# ─── Cache: mỗi nguồn 1 bản, có cache lỗi ngắn ─────────────────────────────

# khoá cố định ("fx", "gold", "xs:mb"...) -> (monotonic, giá trị | None khi lỗi)
_cache: dict[str, tuple[float, object | None]] = {}
_locks: dict[str, asyncio.Lock] = {}


def clear_cache() -> None:
    _cache.clear()


async def _cached(key: str, ttl: float, loader: Callable[[], Awaitable[T]]) -> T:
    def fresh() -> tuple[bool, object | None]:
        hit = _cache.get(key)
        if hit is None:
            return False, None
        age = time.monotonic() - hit[0]
        return age < (ttl if hit[1] is not None else _ERROR_TTL_SEC), hit[1]

    ok, value = fresh()
    if ok:
        if value is None:
            raise MarketDataError(f"Nguồn {key} vừa lỗi, chờ chút rồi thử lại.")
        return value  # type: ignore[return-value]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        ok, value = fresh()  # lượt khác vừa tải xong trong lúc chờ khoá
        if ok:
            if value is None:
                raise MarketDataError(f"Nguồn {key} vừa lỗi, chờ chút rồi thử lại.")
            return value  # type: ignore[return-value]
        try:
            loaded = await asyncio.wait_for(loader(), _TOTAL_TIMEOUT_SEC)
        except (
            MarketDataError,
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            KeyError,
            TypeError,
            ET.ParseError,
        ) as exc:
            logger.warning("vn_market_data: nguồn %s lỗi: %r", key, exc)
            _cache[key] = (time.monotonic(), None)
            raise MarketDataError(f"Nguồn {key} lỗi: {exc}") from exc
        _cache[key] = (time.monotonic(), loaded)
        return loaded


async def _fetch(method: str, url: str, *, data: dict | None = None) -> bytes:
    """Tải body dạng stream, dừng ngay khi vượt _MAX_BYTES (kể cả sau giải nén)."""
    client = http_client.get_client()
    async with client.stream(method, url, data=data, headers=_HEADERS, timeout=_TIMEOUT) as resp:
        if resp.status_code != 200:
            raise MarketDataError(f"HTTP {resp.status_code} từ {url}")
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > _MAX_BYTES:
                raise MarketDataError(f"Phản hồi quá lớn (> {_MAX_BYTES // 1024} KB)")
            chunks.append(chunk)
    return b"".join(chunks)


def _xml_root(content: bytes) -> ET.Element:
    # Không nhận DOCTYPE/ENTITY: chặn kiểu tấn công "billion laughs" từ nguồn lạ.
    head = content[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise MarketDataError("XML có DOCTYPE/ENTITY, không parse")
    return ET.fromstring(content)


# ─── Tỷ giá Vietcombank ─────────────────────────────────────────────────────


def _num(raw) -> float | None:
    text = str(raw or "").strip().replace(",", "")
    if not text or text == "-":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def parse_fx_xml(content: bytes) -> FxTable:
    root = _xml_root(content)
    if root.tag != "ExrateList":
        raise MarketDataError(f"XML tỷ giá lạ (gốc {root.tag})")
    raw_time = (root.findtext("DateTime") or "").strip()
    updated = ""
    try:
        updated = dt.datetime.strptime(raw_time, "%m/%d/%Y %I:%M:%S %p").strftime("%H:%M %d/%m/%Y")
    except ValueError:
        updated = raw_time[:40]
    rates: dict[str, FxRate] = {}
    for el in root.iter("Exrate"):
        code = (el.get("CurrencyCode") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            continue
        rates[code] = FxRate(
            code=code,
            name=" ".join((el.get("CurrencyName") or "").split()),
            buy=_num(el.get("Buy")),
            transfer=_num(el.get("Transfer")),
            sell=_num(el.get("Sell")),
        )
        if len(rates) >= 40:
            break
    if "USD" not in rates:
        raise MarketDataError("Bảng tỷ giá không có USD")
    return FxTable(updated=updated, rates=rates)


async def _load_fx() -> FxTable:
    return parse_fx_xml(await _fetch("GET", FX_URL))


async def get_fx() -> FxTable:
    return await _cached("fx", _FX_TTL_SEC, _load_fx)


# ─── Giá vàng SJC ───────────────────────────────────────────────────────────


def _gold_value(raw) -> float | None:
    value = _num(raw)
    if value is None:
        return None
    # SJC trả đồng/lượng (vd 148000000.0); phòng khi trả nghìn đồng (148000).
    return value * 1000 if value < 10_000_000 else value


def parse_sjc_json(content: bytes) -> GoldTable:
    data = json.loads(content)
    if not isinstance(data, dict) or not data.get("success"):
        raise MarketDataError("SJC trả success=false")
    rows: list[GoldRow] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("TypeName") or "").split())
        branch = " ".join(str(item.get("BranchName") or "").split())
        buy = _gold_value(item.get("BuyValue"))
        sell = _gold_value(item.get("SellValue"))
        if name and (buy or sell):
            rows.append(GoldRow(name, branch, buy, sell))
        if len(rows) >= _MAX_GOLD_ROWS:
            break
    if not rows:
        raise MarketDataError("SJC không có dòng giá nào")
    return GoldTable(
        updated=" ".join(str(data.get("latestDate") or "").split())[:40], rows=tuple(rows)
    )


async def _load_gold() -> GoldTable:
    today = dt.datetime.now(_VN_TZ).strftime("%d/%m/%Y")
    body = await _fetch("POST", SJC_URL, data={"method": "GetSJCGoldPriceByDate", "toDate": today})
    return parse_sjc_json(body)


async def get_gold() -> GoldTable:
    return await _cached("gold", _GOLD_TTL_SEC, _load_gold)


# ─── Xổ số (RSS xskt.com.vn) ────────────────────────────────────────────────

# Nhãn giải: "ĐB" hoặc 1 chữ số đứng NGAY trước dấu ":". Feed có lúc dính dòng
# ("7: 2228: 37" = giải 7 "222" + giải 8 "37") nên không tách theo dòng.
_PRIZE_LABEL_RE = re.compile(r"(ĐB|DB|\d)\s*:")
_PROVINCE_HEADER_RE = re.compile(r"\[([^\]\n]{2,40})\]")
_LINK_DATE_RE = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})")
_TITLE_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?")


def _parse_prizes(block: str) -> dict[str, tuple[str, ...]]:
    labels = list(_PRIZE_LABEL_RE.finditer(block))
    prizes: dict[str, tuple[str, ...]] = {}
    for i, match in enumerate(labels):
        end = labels[i + 1].start() if i + 1 < len(labels) else len(block)
        numbers = tuple(re.findall(r"\d+", block[match.end() : end]))
        key = "ĐB" if match.group(1) in ("ĐB", "DB") else match.group(1)
        if numbers and key not in prizes:
            prizes[key] = numbers
    return prizes


def parse_draw_text(text: str, region: str) -> tuple[ProvinceResult, ...]:
    """Kết quả 1 kỳ quay: "[Tỉnh]" + các dòng "ĐB: ...", "1: ..."; miền Bắc
    không có tên tỉnh."""
    headers = list(_PROVINCE_HEADER_RE.finditer(text or ""))
    blocks: list[tuple[str, str]] = []
    if headers:
        for i, match in enumerate(headers):
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            blocks.append((match.group(1).strip(), text[match.end() : end]))
    else:
        name = REGION_NAMES.get(region, "")
        blocks.append((name[:1].upper() + name[1:] or "Kết quả", text or ""))
    out: list[ProvinceResult] = []
    for name, block in blocks[:6]:
        prizes = _parse_prizes(block)
        if prizes.get("ĐB"):
            out.append(ProvinceResult(name=name, prizes=prizes))
    return tuple(out)


def _draw_date(link: str, title: str, today: dt.date) -> dt.date | None:
    match = _LINK_DATE_RE.search(link or "")
    try:
        if match:
            return dt.date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        match = _TITLE_DATE_RE.search(title or "")
        if not match:
            return None
        day, month = int(match.group(1)), int(match.group(2))
        if match.group(3):
            return dt.date(int(match.group(3)), month, day)
        guess = dt.date(today.year, month, day)
        # Tiêu đề không có năm: kỳ quay "31/12" đọc vào ngày 1/1 là năm trước.
        return (
            guess if guess <= today + dt.timedelta(days=1) else guess.replace(year=today.year - 1)
        )
    except ValueError:
        return None


def parse_lottery_rss(
    content: bytes, region: str, today: dt.date | None = None
) -> list[LotteryDraw]:
    today = today or dt.datetime.now(_VN_TZ).date()
    root = _xml_root(content)
    draws: list[LotteryDraw] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        day = _draw_date(link, title, today)
        provinces = parse_draw_text(item.findtext("description") or "", region)
        if day is None or not provinces:
            continue
        draws.append(LotteryDraw(region=region, date=day, provinces=provinces, link=link))
        if len(draws) >= _MAX_DRAWS:
            break
    if not draws:
        raise MarketDataError(f"RSS xổ số {region} không có kỳ quay nào parse được")
    draws.sort(key=lambda d: d.date, reverse=True)
    return draws


async def get_lottery(region: str) -> list[LotteryDraw]:
    """Các kỳ quay gần nhất của 1 miền, mới nhất trước."""
    if region not in LOTTERY_FEEDS:
        raise MarketDataError(f"Miền xổ số không hợp lệ: {region}")

    async def load() -> list[LotteryDraw]:
        return parse_lottery_rss(await _fetch("GET", LOTTERY_FEEDS[region]), region)

    return await _cached(f"xs:{region}", _LOTTERY_TTL_SEC, load)


def pick_draw(draws: list[LotteryDraw], day: dt.date | None) -> LotteryDraw | None:
    """Kỳ quay đúng ngày ``day`` (None = mới nhất)."""
    if not draws:
        return None
    if day is None:
        return draws[0]
    return next((d for d in draws if d.date == day), None)


# ─── Dò vé số (bằng code, không qua LLM) ────────────────────────────────────

PRIZE_NAMES = {
    "ĐB": "Giải Đặc biệt",
    "1": "Giải Nhất",
    "2": "Giải Nhì",
    "3": "Giải Ba",
    "4": "Giải Tư",
    "5": "Giải Năm",
    "6": "Giải Sáu",
    "7": "Giải Bảy",
    "8": "Giải Tám",
    "PHU_DB": "Giải phụ Đặc biệt",
    "KK": "Giải khuyến khích",
}
# Cơ cấu giải vé 10.000đ xổ số kiến thiết miền Nam/miền Trung (tham khảo - đối
# chiếu lại tại đại lý). Miền Bắc giải ĐB còn phụ thuộc ký hiệu vé nên không ghi tiền.
PRIZE_AMOUNTS_SOUTH = {
    "ĐB": "2 tỷ",
    "1": "30 triệu",
    "2": "15 triệu",
    "3": "10 triệu",
    "4": "3 triệu",
    "5": "1 triệu",
    "6": "400.000đ",
    "7": "200.000đ",
    "8": "100.000đ",
    "PHU_DB": "50 triệu",
    "KK": "6 triệu",
}
_PRIZE_ORDER = ("ĐB", "1", "2", "3", "4", "5", "6", "7", "8")


def ticket_digits(region: str) -> int:
    return 5 if region == "mb" else 6


def check_ticket(ticket: str, prizes: dict[str, tuple[str, ...]], region: str) -> list[str]:
    """Các giải vé ``ticket`` trúng ở 1 tỉnh (mã giải trong PRIZE_NAMES).

    Mỗi giải so khớp các chữ số CUỐI của vé với số trúng (độ dài số trúng quyết
    định số chữ số so). Miền Nam/Trung thêm giải phụ ĐB (chỉ sai chữ số đầu) và
    khuyến khích (đúng chữ số đầu, sai đúng 1 chữ số ở 5 hàng còn lại); miền Bắc
    thêm khuyến khích (trùng 2 số cuối giải ĐB)."""
    wins: list[str] = []
    for key in _PRIZE_ORDER:
        for number in prizes.get(key, ()):
            if number and len(number) <= len(ticket) and ticket.endswith(number):
                wins.append(key)
    special = (prizes.get("ĐB") or ("",))[0]
    if not special or "ĐB" in wins:
        return wins
    if region == "mb":
        if len(ticket) >= 2 and ticket[-2:] == special[-2:]:
            wins.append("KK")
        return wins
    if len(ticket) == len(special) == 6:
        if ticket[0] != special[0] and ticket[1:] == special[1:]:
            wins.append("PHU_DB")
        elif (
            ticket[0] == special[0]
            and sum(a != b for a, b in zip(ticket[1:], special[1:], strict=True)) == 1
        ):
            wins.append("KK")
    return wins


# ─── Định dạng câu trả lời ──────────────────────────────────────────────────


def vnd(value: float | None, decimals: int = 0) -> str:
    """Số kiểu Việt: 25.650 / 168,12."""
    if value is None:
        return "-"
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def _rate(value: float | None) -> str:
    if value is None:
        return "-"
    return vnd(value, 0 if value >= 1000 else 2)


MAJOR_CURRENCIES = ("USD", "EUR", "JPY", "CNY", "KRW", "GBP", "AUD", "SGD", "THB")


def format_fx(table: FxTable, codes: list[str] | None = None) -> str:
    wanted = [c for c in (codes or MAJOR_CURRENCIES) if c in table.rates]
    lines = ["💱 Tỷ giá Vietcombank" + (f" (cập nhật {table.updated})" if table.updated else "")]
    for code in wanted:
        r = table.rates[code]
        lines.append(
            f"• {code}: mua TM {_rate(r.buy)} · mua CK {_rate(r.transfer)} · bán {_rate(r.sell)}"
        )
    lines.append("(Nguồn Vietcombank, đơn vị đồng; TM = tiền mặt, CK = chuyển khoản)")
    return "\n".join(lines)


def format_conversion(table: FxTable, amount: float, code: str, *, to_vnd: bool) -> str | None:
    rate = table.rates.get(code)
    if rate is None or amount <= 0:
        return None
    stamp = f" (Vietcombank{', ' + table.updated if table.updated else ''})"
    if to_vnd:
        transfer = rate.transfer or rate.buy
        if transfer is None:
            return None
        line = f"💱 {vnd(amount, 0 if amount == int(amount) else 2)} {code} ≈ {vnd(amount * transfer)} đ"
        detail = f"theo giá mua chuyển khoản {_rate(transfer)}"
        if rate.buy and rate.buy != transfer:
            detail += f"; bán tiền mặt cho ngân hàng được ≈ {vnd(amount * rate.buy)} đ"
        return f"{line}\n({detail}){stamp}"
    if rate.sell is None:
        return None
    return (
        f"💱 {vnd(amount)} đ ≈ {vnd(amount / rate.sell, 2)} {code}\n"
        f"(theo giá bán ra {_rate(rate.sell)} đ/{code}){stamp}"
    )


def _thousand(value: float | None) -> str:
    return "-" if value is None else vnd(value / 1000)


def format_gold(table: GoldTable, limit: int = 6) -> str:
    branch = table.rows[0].branch
    rows = [r for r in table.rows if r.branch == branch][:limit] or list(table.rows[:limit])
    head = "🪙 Giá vàng SJC" + (f" (cập nhật {table.updated})" if table.updated else "")
    lines = [head + (f" - {branch}" if branch else "")]
    for r in rows:
        lines.append(f"• {r.name}: mua {_thousand(r.buy)} · bán {_thousand(r.sell)}")
    lines.append("(Nguồn sjc.com.vn, đơn vị nghìn đồng/lượng)")
    return "\n".join(lines)


def gold_headline(table: GoldTable) -> str:
    row = table.rows[0]
    return f"Vàng SJC mua {_thousand(row.buy)} · bán {_thousand(row.sell)} nghìn/lượng"


def fx_headline(table: FxTable) -> str:
    usd = table.rates.get("USD")
    if usd is None:
        return ""
    return f"USD Vietcombank mua CK {_rate(usd.transfer)} · bán {_rate(usd.sell)}"


def _draw_heading(draw: LotteryDraw) -> str:
    weekdays = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
    return (
        f"🎰 Xổ số {REGION_NAMES.get(draw.region, draw.region)} - "
        f"{weekdays[draw.date.weekday()]} {draw.date:%d/%m/%Y}"
    )


def format_draw(draw: LotteryDraw) -> str:
    lines = [_draw_heading(draw)]
    for province in draw.provinces:
        if len(draw.provinces) > 1 or draw.region != "mb":
            lines.append(f"\n[{province.name}]")
        for key in _PRIZE_ORDER:
            numbers = province.prizes.get(key)
            if numbers:
                label = "ĐB" if key == "ĐB" else f"G{key}"
                lines.append(f"{label}: {' - '.join(numbers)}")
    lines.append("\n(Nguồn xskt.com.vn - đối chiếu lại với vé/đại lý trước khi lĩnh thưởng)")
    return "\n".join(lines)


# Tên khác của cùng 1 đài (bỏ dấu, dấu câu thành khoảng trắng): nguồn ghi "TP.HCM"
# hay "Hồ Chí Minh", người dùng gõ "sài gòn"; xổ số Lâm Đồng mang tên "Đà Lạt".
_PROVINCE_ALIASES = (
    ("ho chi minh", "tp ho chi minh", "tp hcm", "tphcm", "hcm", "sai gon"),
    ("da lat", "lam dong"),
    ("hue", "thua thien hue", "tt hue"),
    ("vung tau", "ba ria vung tau", "brvt"),
    ("dak lak", "daklak", "dac lac"),
    ("dak nong", "daknong", "dac nong"),
)


def _name_key(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", fold(text)).split())


def _province_keys(name: str) -> tuple[str, ...]:
    key = _name_key(name)
    return next((group for group in _PROVINCE_ALIASES if key in group), (key,))


def find_province(draw: LotteryDraw, text: str) -> ProvinceResult | None:
    """Tỉnh trong kỳ quay được nhắc tới trong câu (so bỏ dấu, nhận tên khác)."""
    folded = f" {_name_key(text)} "
    for province in draw.provinces:
        if any(key and f" {key} " in folded for key in _province_keys(province.name)):
            return province
    return None


# Số đài quay tối thiểu mỗi ngày (khi cache chưa có kỳ cùng thứ tuần trước để so).
_MIN_PROVINCES = {"mn": 3, "mt": 2, "mb": 1}


def coverage(draw: LotteryDraw, draws: list[LotteryDraw], today: dt.date) -> tuple[int, int] | None:
    """(số đài đã có, số đài thường quay) khi kỳ HÔM NAY còn thiếu đài - nguồn chỉ
    ghi đài đã có giải ĐB nên lúc đang quay kỳ hôm nay chưa đủ đài; ngược lại None."""
    if draw.date != today:
        return None
    usual = max(
        (
            len(d.provinces)
            for d in draws
            if d.date != draw.date and d.date.weekday() == draw.date.weekday()
        ),
        default=_MIN_PROVINCES.get(draw.region, 1),
    )
    have = len(draw.provinces)
    return (have, usual) if have < usual else None


def format_ticket(
    ticket: str,
    draw: LotteryDraw,
    province_hint: str = "",
    *,
    incomplete: tuple[int, int] | None = None,
) -> str:
    """Dò vé trong 1 kỳ. ``incomplete`` = coverage(): kỳ hôm nay còn đài chưa có
    kết quả -> không kết luận "chưa trúng" cho cả kỳ. Xưng hô theo người nhận của
    lượt (reply_audience)."""
    you, me = reply_audience.forms()
    digits = ticket_digits(draw.region)
    if len(ticket) != digits:
        return (
            f"Vé xổ số {REGION_NAMES.get(draw.region)} có {digits} chữ số, {you} gửi lại đủ "
            f"{digits} số giúp {me} nha (vd /xoso {'mb 12345' if digits == 5 else 'mn 123456'})."
        )
    target = find_province(draw, province_hint) if province_hint else None
    provinces = [target] if target else list(draw.provinces)
    if target is not None:
        incomplete = None  # đúng đài người dùng hỏi đã có kết quả
    lines = [f"🎫 Dò vé {ticket} - {_draw_heading(draw)[2:]}"]
    any_win = False
    for province in provinces:
        wins = check_ticket(ticket, province.prizes, draw.region)
        if not wins:
            lines.append(f"• {province.name}: không trúng")
            continue
        any_win = True
        parts = []
        for key in wins:
            label = PRIZE_NAMES[key]
            if draw.region != "mb" and key in PRIZE_AMOUNTS_SOUTH:
                label += f" ({PRIZE_AMOUNTS_SOUTH[key]})"
            if draw.region == "mb" and key == "ĐB":
                label += " - cần khớp thêm ký hiệu vé, không khớp là giải phụ ĐB"
            parts.append(label)
        lines.append(f"• {province.name}: 🎉 TRÚNG {', '.join(parts)}")
    if any_win:
        lines.append(
            f"Số tiền là cơ cấu vé 10.000đ để tham khảo - {you} đối chiếu vé gốc với đại lý "
            "hoặc công ty xổ số trước khi lĩnh thưởng nha."
        )
    elif incomplete is None:
        lines.append(f"Tiếc ghê, lần này vé chưa trúng rồi {you} 😅")
    if incomplete is not None:
        lines.append(
            f"⏳ Kỳ hôm nay nguồn mới có kết quả {incomplete[0]}/{incomplete[1]} đài - đài còn "
            f"lại chưa có kết quả, {you} nhắn {me} dò lại sau ít phút nha."
        )
    lines.append(f"(Nguồn xskt.com.vn, {me} dò bằng code chứ không đoán)")
    return "\n".join(lines)
