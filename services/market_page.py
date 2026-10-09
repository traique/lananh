"""Đăng tự động lên Facebook Page riêng (key MARKET), tách khỏi luồng Shopee.

Chuyển từ 2 workflow n8n:
- ``stock``: nến DNSE -> chỉ báo -> AI nhận định -> ảnh biểu đồ VN-INDEX + caption.
  Mặc định 08:45 và 15:20, thứ Hai-thứ Sáu.
- ``news``: RSS CafeF -> AI tổng hợp thành bài đăng (kèm ảnh bài nổi bật nếu có),
  rồi comment bản viết lại của chính bài nổi bật đó. Mặc định 08:30 hằng ngày.

Giờ đăng đổi qua MARKET_STOCK_TIMES_VN / MARKET_NEWS_TIMES_VN. ``run_manual`` chạy
ngoài lịch cho lệnh /fb_market và trang admin (mặc định chỉ xem thử, không đăng).

Page cấu hình bằng FACEBOOK_PAGE_ID_MARKET / FACEBOOK_PAGE_ACCESS_TOKEN_MARKET và
không nằm trong configured_page_keys() mặc định nên /fb_ok không đăng lên đây.
"""
import asyncio
import logging
import math
import re
import statistics
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
import httpx
from bs4 import BeautifulSoup

from ai import orchestrator
from core import config
from core import database as db
from services import web_reader
from services.facebook_page_service import (
    MARKET_PAGE_KEY,
    FacebookPublicationUncertain,
    configured_page_keys,
    post_comment,
    publish_page_post,
)

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_task: asyncio.Task | None = None

_DEFAULT_NEWS_TIMES = (time(8, 30),)
_DEFAULT_STOCK_TIMES = (time(8, 45), time(15, 20))


class MarketPageError(RuntimeError):
    """Lỗi vận hành báo được cho người gọi (page chưa cấu hình, đang có job chạy)."""


def _parse_times(raw: str, default: tuple[time, ...]) -> tuple[time, ...]:
    try:
        parsed = tuple(
            datetime.strptime(part.strip(), "%H:%M").time()
            for part in raw.split(",")
            if part.strip()
        )
    except ValueError:
        logger.warning("market_page: giờ đăng '%s' sai định dạng HH:MM, dùng mặc định.", raw)
        return default
    return parsed or default


def _schedule() -> tuple[tuple[str, time, range], ...]:
    """(job, giờ VN, các thứ chạy; Monday=0). Không có lịch nghỉ lễ: ngày lễ trong
    tuần báo cáo sẽ lặp lại phiên gần nhất."""
    news = _parse_times(config.MARKET_NEWS_TIMES_VN, _DEFAULT_NEWS_TIMES)
    stock = _parse_times(config.MARKET_STOCK_TIMES_VN, _DEFAULT_STOCK_TIMES)
    return tuple(("news", at, range(7)) for at in news) + tuple(
        ("stock", at, range(5)) for at in stock
    )


# Một job chạy tại một thời điểm: tránh lệnh thủ công đè lên lượt theo lịch.
_run_lock = asyncio.Lock()

_MIN_POST_CHARS = 80
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ─── Tuân thủ: không khuyến nghị đầu tư, luôn dẫn nguồn + miễn trừ trách nhiệm ──────
# Phần nguồn/miễn trừ do code tự gắn (không nhờ AI) để bài nào cũng có, không phụ thuộc
# việc AI có nhớ viết hay không.

_DISCLAIMER = (
    "⚠️ Miễn trừ trách nhiệm: Nội dung được tổng hợp tự động bằng AI từ dữ liệu và tin tức "
    "công khai, chỉ mang tính chất tham khảo và cung cấp thông tin. Đây KHÔNG phải lời khuyên "
    "hay khuyến nghị đầu tư mua, bán hoặc nắm giữ bất kỳ chứng khoán nào. Số liệu có thể có độ "
    "trễ hoặc sai sót. Nhà đầu tư tự cân nhắc và chịu hoàn toàn trách nhiệm với quyết định của "
    "mình; Page không chịu trách nhiệm đối với mọi tổn thất phát sinh từ việc sử dụng thông tin này."
)
_DISCLAIMER_SHORT = (
    "⚠️ Nội dung do AI biên tập lại từ bài gốc, chỉ mang tính tham khảo, không phải khuyến nghị "
    "đầu tư; quyền đối với bài gốc thuộc về CafeF và tác giả."
)
# Cụm từ khuyến nghị giao dịch rõ ràng. Cố ý hẹp: "khối ngoại bán ròng", "áp lực chốt lời"
# là mô tả thị trường bình thường, không được chặn nhầm.
_ADVICE_RE = re.compile(
    r"(nên|khuyến nghị|khuyến cáo|đề xuất|gợi ý)\s+(mua|bán|nắm giữ|giải ngân|"
    r"(gia tăng|tăng|giảm|hạ)\s+tỷ trọng)|giá mục tiêu|tỷ trọng\s+(cổ phiếu|tiền mặt)",
    re.IGNORECASE,
)


def _has_investment_advice(text: str) -> bool:
    return _ADVICE_RE.search(text) is not None


def _vn_date(iso_date: str) -> str:
    return date.fromisoformat(iso_date).strftime("%d/%m/%Y")


# ─── Báo cáo chứng khoán ────────────────────────────────────────────────────

_DNSE_URL = "https://services.entrade.com.vn/chart-api/v2/ohlcs/{kind}"
_QUICKCHART_URL = "https://quickchart.io/chart"
_INDEX_SYMBOLS = frozenset({"VNINDEX", "VN30", "HNXINDEX", "HNX30", "UPCOMINDEX", "UPINDEX"})
# Chỉ tải mã thực sự đi vào bài: VNINDEX + cổ phiếu. HNXINDEX/UPCOMINDEX từng nằm trong
# danh sách n8n nhưng DNSE trả 400 và báo cáo không dùng tới chúng.
_REPORT_SYMBOLS = (
    "VNINDEX", "VN30",
    "VIC", "VHM", "VRE", "FPT", "MWG", "HPG",
    "VCB", "BID", "CTG", "MBB", "TCB",
    "SSI", "VND", "HCM", "VCI", "GVR", "IJC",
)
_HISTORY_DAYS = 180
_BARS_KEPT = 120
_CHART_BARS = 30
_DNSE_CONCURRENCY = 5


def _num(values: list, index: int) -> float | None:
    try:
        value = float(values[index])
    except (IndexError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse_bars(symbol: str, body: dict) -> list[dict]:
    """DNSE trả giá cổ phiếu theo nghìn đồng, chỉ số theo điểm."""
    scale = 1 if symbol in _INDEX_SYMBOLS else 1000
    t, h, l, c, v = (body.get(key) or [] for key in "thlcv")
    bars = []
    for i in range(len(t)):
        close = _num(c, i)
        ts = _num(t, i)
        if close is None or ts is None or close <= 0:
            continue
        bars.append({
            "ts": ts,
            "date": datetime.fromtimestamp(ts, _VN_TZ).date().isoformat(),
            "high": (_num(h, i) or close) * scale,
            "low": (_num(l, i) or close) * scale,
            "close": close * scale,
            "volume": _num(v, i) or 0.0,
        })
    bars.sort(key=lambda bar: bar["ts"])
    return bars[-_BARS_KEPT:]


def _sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def _ema(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    ema = sum(values[:n]) / n
    k = 2 / (n + 1)
    for value in values[n:]:
        ema = value * k + ema * (1 - k)
    return ema


def _rsi(values: list[float], n: int = 14) -> float | None:
    if len(values) < n + 1:
        return None
    gain = loss = 0.0
    for prev, cur in zip(values[-n - 1:-1], values[-n:]):
        delta = cur - prev
        if delta > 0:
            gain += delta
        else:
            loss -= delta
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _indicators(symbol: str, bars: list[dict]) -> dict:
    closes = [bar["close"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    latest = bars[-1]
    prev = bars[-2] if len(bars) > 1 else None

    ma20, ma50 = _sma(closes, 20), _sma(closes, 50)
    sd20 = statistics.pstdev(closes[-20:]) if len(closes) >= 20 else None
    bb_upper = ma20 + 2 * sd20 if ma20 is not None and sd20 is not None else None
    bb_lower = ma20 - 2 * sd20 if ma20 is not None and sd20 is not None else None
    bb_width = (bb_upper - bb_lower) / ma20 * 100 if bb_upper is not None and ma20 else None

    vol_ma20 = _sma(volumes, 20)
    vol_ratio = latest["volume"] / vol_ma20 if vol_ma20 and latest["volume"] else None
    spread = latest["high"] - latest["low"]
    close_position = (latest["close"] - latest["low"]) / spread if spread > 0 else 0.5

    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    return {
        "symbol": symbol,
        "date": latest["date"],
        "close": latest["close"],
        "change_pct": (
            round((latest["close"] - prev["close"]) / prev["close"] * 100, 2) if prev else None
        ),
        "volume": latest["volume"],
        "vol_ratio": _round(vol_ratio),
        "close_position": round(close_position, 2),
        "ma20": _round(ma20),
        "ma50": _round(ma50),
        "above_ma20": None if ma20 is None else latest["close"] > ma20,
        "rsi14": _round(_rsi(closes)),
        "macd": _round(ema12 - ema26) if ema12 is not None and ema26 is not None else None,
        "bb_upper": _round(bb_upper),
        "bb_lower": _round(bb_lower),
        "bb_width": _round(bb_width),
    }


def _build_report(rows: dict[str, list[dict]]) -> dict | None:
    """None khi thiếu dữ liệu VNINDEX: không có gì đáng đăng."""
    if not rows.get("VNINDEX"):
        return None
    metrics = [_indicators(symbol, bars) for symbol, bars in rows.items() if bars]
    stocks = [m for m in metrics if m["symbol"] not in _INDEX_SYMBOLS]
    moves = [m for m in stocks if m["change_pct"] is not None]
    return {
        "report_date": rows["VNINDEX"][-1]["date"],
        "vnindex": next(m for m in metrics if m["symbol"] == "VNINDEX"),
        "advancers": sum(m["change_pct"] > 0 for m in moves),
        "decliners": sum(m["change_pct"] < 0 for m in moves),
        "unchanged": sum(m["change_pct"] == 0 for m in moves),
        "pct_above_ma20": (
            round(sum(bool(m["above_ma20"]) for m in stocks) / len(stocks) * 100, 1)
            if stocks else 0
        ),
        "gainers": sorted(moves, key=lambda m: m["change_pct"], reverse=True)[:5],
        "losers": sorted(moves, key=lambda m: m["change_pct"])[:5],
    }


def _fmt(value, default="N/A"):
    return default if value is None else value


def _signed(pct: float) -> str:
    return f"+{pct}" if pct > 0 else str(pct)


def _movers(rows: list[dict]) -> str:
    return ", ".join(
        f"{m['symbol']} ({_signed(m['change_pct'])}%, Vol {_fmt(m['vol_ratio'], 1)}x TB20)"
        for m in rows
    ) or "Không có mã đáng chú ý"


def _stock_prompt(report: dict) -> str:
    vn = report["vnindex"]
    volume = f"{int(vn['volume']):,}" if vn["volume"] else "N/A"
    return f"""Bạn là chuyên gia phân tích dữ liệu thị trường chứng khoán Việt Nam.
Hãy viết bài nhận định thị trường chuyên sâu cho phiên ngày {report['report_date']} dựa trên bộ dữ liệu định lượng sau:

1. CHỈ SỐ VN-INDEX & HÀNH VI GIÁ (PRICE ACTION & VOLUME):
- Điểm đóng cửa: {_fmt(vn['close'])} ({_signed(vn['change_pct'] or 0)}%)
- Khối lượng: {volume} CP (tương đương {_fmt(vn['vol_ratio'], 1)}x trung bình 20 phiên).
- Vị trí đóng nến: {vn['close_position']}/1.0 (1.0 là đỉnh phiên, 0.0 là đáy phiên - dùng để đánh giá áp lực bán hay lực cầu kéo cuối phiên).
- Hệ thống chỉ báo: MA20 = {_fmt(vn['ma20'])}, MA50 = {_fmt(vn['ma50'])}, RSI(14) = {_fmt(vn['rsi14'])}, MACD = {_fmt(vn['macd'])}, Dải Bollinger [{_fmt(vn['bb_lower'])} - {_fmt(vn['bb_upper'])}].

2. ĐỘ RỘNG THỊ TRƯỜNG & DÒNG TIỀN NỘI TẠI:
- Độ rộng: {report['advancers']} mã tăng / {report['decliners']} mã giảm / {report['unchanged']} mã tham chiếu.
- Tỷ lệ cổ phiếu giữ xu hướng trên MA20: {report['pct_above_ma20']}%.
- Top tích cực: {_movers(report['gainers'])}
- Top tiêu cực: {_movers(report['losers'])}

YÊU CẦU ĐỊNH DẠNG & NỘI DUNG (Facebook Fanpage):
- Cấu trúc bài viết:
  📌 [TIÊU ĐỀ BẮT MẮT TÓM TẮT TRẠNG THÁI PHIÊN]
  1. Diễn biến & Hành vi Dòng tiền: Phân tích tương quan giá - khối lượng (vol bùng nổ, cạn kiệt hay áp lực xả cuối phiên qua vị trí đóng nến).
  2. Nội tại thị trường: Nhận định độ rộng và phân hóa (có hiện tượng kéo trụ xanh vỏ đỏ lòng hay dòng tiền lan tỏa thực chất).
  3. Xu hướng kỹ thuật: Kiểm định các ngưỡng MA20, MA50, RSI và dải Bollinger.
  4. Các mốc cần theo dõi: Nêu các vùng giá/chỉ báo đáng chú ý (MA20, MA50, biên Bollinger) và các kịch bản có thể xảy ra theo dạng "nếu... thì thị trường có thể...". Chỉ MÔ TẢ, không đưa ra hành động giao dịch.
- Độ dài: Khoảng 300 - 400 từ, định dạng xuống dòng, bullet point dễ đọc trên điện thoại.
- Văn phong: Điềm tĩnh, khách quan, giàu góc nhìn chuyên môn, tránh cảm tính hoặc hô hào.

QUY ĐỊNH BẮT BUỘC (tuân thủ pháp lý):
- TUYỆT ĐỐI KHÔNG khuyến nghị mua/bán/nắm giữ, KHÔNG đề xuất tỷ trọng cổ phiếu/tiền mặt, KHÔNG nêu giá mục tiêu, KHÔNG gợi ý mã cụ thể nên mua hay nên bán. Chỉ phân tích và mô tả dữ liệu.
- Chỉ dùng các số liệu được cung cấp ở trên, không bịa thêm số liệu hay tin tức bên ngoài. Khi nêu số liệu, ghi rõ theo dữ liệu DNSE của phiên {_vn_date(report['report_date'])}.
- KHÔNG tự viết phần "Nguồn", lời miễn trừ trách nhiệm hay hashtag (hệ thống sẽ tự thêm)."""


async def _fetch_bars(
    client: httpx.AsyncClient, gate: asyncio.Semaphore, symbol: str, now: int
) -> list[dict]:
    kind = "index" if symbol in _INDEX_SYMBOLS else "stock"
    params = {
        "from": now - _HISTORY_DAYS * 86400,
        "to": now,
        "symbol": symbol,
        "resolution": "1D",
    }
    async with gate:
        try:
            response = await client.get(_DNSE_URL.format(kind=kind), params=params)
            response.raise_for_status()
            return _parse_bars(symbol, response.json())
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("market_page: DNSE lỗi mã %s (%s).", symbol, exc)
            return []


async def _fetch_all_bars() -> dict[str, list[dict]]:
    now = int(datetime.now(timezone.utc).timestamp())
    gate = asyncio.Semaphore(_DNSE_CONCURRENCY)
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        results = await asyncio.gather(
            *(_fetch_bars(client, gate, symbol, now) for symbol in _REPORT_SYMBOLS)
        )
    return dict(zip(_REPORT_SYMBOLS, results))


def _chart_config(bars: list[dict], report_date: str) -> dict:
    last = bars[-_CHART_BARS:]
    prices = [round(bar["close"], 2) for bar in last]
    return {
        "type": "line",
        "data": {
            "labels": [bar["date"][5:] for bar in last],
            "datasets": [{
                "label": "Điểm số VN-INDEX",
                "data": prices,
                "borderColor": "#2563eb",
                "backgroundColor": "rgba(37, 99, 235, 0.15)",
                "fill": True,
                "tension": 0.25,
                "pointRadius": 3,
                "pointBackgroundColor": "#2563eb",
            }],
        },
        "options": {
            "responsive": True,
            "scales": {
                "y": {
                    "min": math.floor(min(prices) * 0.995),
                    "max": math.ceil(max(prices) * 1.005),
                    "ticks": {"precision": 1},
                },
            },
            "plugins": {
                "legend": {"display": False},
                "title": {
                    "display": True,
                    "text": f"DIỄN BIẾN VN-INDEX ĐẾN {report_date}",
                    "font": {"size": 16},
                },
            },
        },
    }


async def _render_chart(bars: list[dict], report_date: str) -> bytes | None:
    """Biểu đồ chỉ là phần kèm theo: lỗi thì bài vẫn đăng dạng văn bản."""
    payload = {
        "version": "4", "backgroundColor": "#ffffff", "width": 800, "height": 450,
        "format": "png", "chart": _chart_config(bars, report_date),
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(_QUICKCHART_URL, json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("market_page: không tạo được biểu đồ (%s), đăng không kèm ảnh.", exc)
        return None
    if not response.headers.get("content-type", "").startswith("image/"):
        return None
    return response.content


def _stock_footer(report: dict) -> str:
    return (
        f"📊 Nguồn: dữ liệu giá và khối lượng từ DNSE (Entrade), phiên {_vn_date(report['report_date'])}; "
        "các chỉ báo (MA, RSI, MACD, Bollinger) do hệ thống tự tính từ dữ liệu lịch sử.\n\n"
        f"{_DISCLAIMER}\n\n"
        "#chungkhoan #vnindex #nhandinhthitruong #chungkhoanvietnam"
    )


async def _post_stock_report(dry_run: bool = False) -> str | None:
    """Trả nội dung bài (đã đăng, hoặc chỉ tạo khi dry_run); None nếu không có gì để đăng."""
    rows = await _fetch_all_bars()
    report = _build_report(rows)
    if report is None:
        logger.warning("market_page: DNSE không trả dữ liệu VNINDEX, bỏ báo cáo.")
        return None

    response = await orchestrator.ask(_stock_prompt(report))
    text = (getattr(response, "text", None) or "").replace("*", "").strip()
    if len(text) < _MIN_POST_CHARS:
        logger.warning("market_page: AI trả nhận định bất thường (%d ký tự), bỏ.", len(text))
        return None
    if _has_investment_advice(text):
        logger.warning("market_page: nhận định chứa khuyến nghị mua/bán, không đăng.")
        return None
    text = f"{text}\n\n{_stock_footer(report)}"
    if dry_run:
        return text

    chart = await _render_chart(rows["VNINDEX"], report["report_date"])
    media = [("image/png", chart)] if chart else []
    await publish_page_post(text, media, MARKET_PAGE_KEY)
    return text


# ─── Tin CafeF ──────────────────────────────────────────────────────────────

_NEWS_RSS = "https://cafef.vn/thi-truong-chung-khoan.rss"
_NEWS_DIGEST_ITEMS = 15
_ARTICLE_MAX_CHARS = 8000
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_AD_IMAGE_MARKERS = (
    "/ads/", "/quangcao/", "_300x250", "_728x90", "/banner/", "googleads", "doubleclick.net",
)
_NEWS_KEYWORDS = (
    "vn-index", "hnx-index", "upcom-index",
    "tăng mạnh", "giảm sâu", "biến động", "đảo chiều", "bùng nổ", "tích cực", "tiêu cực",
    "thanh khoản", "tỷ đô",
    "chính sách", "nghị định", "thông tư", "lãi suất", "tỷ giá", "ngân hàng nhà nước", "ubcknn",
    "khối ngoại", "bán ròng", "mua ròng",
    "kqkd", "kết quả kinh doanh", "lợi nhuận", "doanh thu",
    "cổ tức", "phát hành", "niêm yết", "hose", "hnx",
    "vic", "vhm", "vre", "hpg", "fpt", "mwg", "gvr",
)
_LEAD_IN_RE = re.compile(r"^Dưới đây là.*?\n+", re.IGNORECASE)
_SOFT_BREAK_RE = re.compile(r"(?<=[^\n])\n(?=[a-zA-Z0-9à-ỹÀ-Ỹ])")


class _Entry(NamedTuple):
    title: str
    summary: str
    link: str
    published: datetime | None


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _parse_entries(feed_bytes: bytes) -> list[_Entry]:
    entries = []
    for item in feedparser.parse(feed_bytes).entries:
        stamp = item.get("published_parsed")
        entries.append(_Entry(
            title=_collapse(item.get("title") or "Không có tiêu đề"),
            summary=_collapse(BeautifulSoup(item.get("summary") or "", "lxml").get_text(" ")),
            link=item.get("link") or "",
            published=datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else None,
        ))
    return entries


def _news_score(entry: _Entry) -> int:
    haystack = f"{entry.title} {entry.summary}".lower()
    return sum(keyword in haystack for keyword in _NEWS_KEYWORDS)


def _pick_entry(entries: list[_Entry], today) -> _Entry | None:
    """Bài nhiều từ khóa nhất trong ngày; không có bài hôm nay thì lấy bài đầu feed."""
    todays = [
        e for e in entries
        if e.published and e.published.astimezone(_VN_TZ).date() == today
    ]
    pool = todays or entries[:1]
    return max(pool, key=_news_score) if pool else None


def _digest_prompt(entries: list[_Entry]) -> str:
    blocks = "".join(
        f"Tin {i}:\nTiêu đề: {e.title}\nTóm tắt: {e.summary or 'Không có tóm tắt'}\n---\n"
        for i, e in enumerate(entries, start=1)
    )
    return f"""Bạn là một AI chuyên gia tổng hợp tin tức thị trường. Nhiệm vụ của bạn là:

Đọc và phân tích toàn bộ nội dung được cung cấp dưới đây.
Xác định các sự kiện, xu hướng hoặc thông tin nổi bật nhất, phù hợp với sở thích của người dùng Facebook.
Tổng hợp thành một bài viết ngắn gọn, súc tích, khoảng 300-500 từ, theo phong cách gần gũi, dễ đọc, thu hút, bao gồm:
- Câu mở đầu hấp dẫn, gây chú ý ngay lập tức.
- Nội dung chính trình bày các thông tin quan trọng, sắp xếp logic, dùng ngôn ngữ tự nhiên, sinh động.
- Giữ giọng điệu trung lập, thân thiện, tránh quá trang trọng, diễn đạt trôi chảy.
- Nếu có mâu thuẫn giữa các nguồn, chọn thông tin đáng tin cậy nhất hoặc bỏ qua để giữ bài viết nhẹ nhàng.

QUY ĐỊNH BẮT BUỘC (tuân thủ pháp lý và dẫn nguồn):
- Mọi thông tin phải xuất phát từ các tin được cung cấp bên dưới (nguồn CafeF); ghi "theo CafeF" khi nêu sự kiện hoặc số liệu quan trọng. Không tự thêm số liệu, dự đoán hay tin ngoài các tin này.
- TUYỆT ĐỐI KHÔNG đưa ra khuyến nghị mua/bán/nắm giữ hay giá mục tiêu. Nếu tin có nhắc khuyến nghị của một tổ chức/cá nhân thì bỏ qua phần đó.
- KHÔNG tự viết phần "Nguồn" hay lời miễn trừ trách nhiệm ở cuối bài (hệ thống sẽ tự thêm).

LƯU Ý QUAN TRỌNG VỀ ĐỊNH DẠNG FACEBOOK:
- Facebook KHÔNG hỗ trợ Markdown, TUYỆT ĐỐI KHÔNG dùng dấu sao (**) hoặc (*) để in đậm.
- Các tiêu đề hay điểm nhấn chỉ cần VIẾT HOA chữ cái đầu hoặc VIẾT HOA CẢ CÂU, kết hợp emoji nhẹ nhàng và gạch đầu dòng (-) thông thường.

Nội dung cần tổng hợp:
Dưới đây là tổng hợp các tin tức thị trường mới nhất:

{blocks}"""


def _rewrite_prompt(article: str) -> str:
    return f"""Bạn là một biên tập viên truyền thông chuyên nghiệp. Hãy viết lại bài báo dưới đây thành bài đăng Facebook/comment ngắn gọn, súc tích và mạch lạc.

QUY TẮC BẮT BUỘC:
1. XUẤT NỘI DUNG TRỰC TIẾP: Tuyệt đối KHÔNG có lời mở đầu hoặc dẫn chuyện như "Dưới đây là bài viết...", "Chào các bạn...". Bắt đầu ngay bằng Tiêu đề.
2. KHÔNG DÙNG DẤU SAO: Tuyệt đối không dùng dấu * hoặc ** để in đậm (Facebook không hỗ trợ). Tiêu đề hãy VIẾT HOA hoặc dùng emoji.
3. QUY TẮC XUỐNG DÒNG: Mỗi đoạn văn phải viết liền mạch, tuyệt đối KHÔNG tự ý ngắt dòng giữa chừng khi câu chưa kết thúc. Chỉ xuống 2 dòng (\\n\\n) khi chuyển sang một ý/tiêu đề mới.
4. Ngôn từ tự nhiên, giữ nguyên số liệu chính xác từ bài gốc, không dùng hashtag, không thêm thông tin ngoài bài gốc.
5. Nếu bài gốc có khuyến nghị mua/bán/nắm giữ hoặc giá mục tiêu của tổ chức/cá nhân nào, hãy bỏ phần đó.
6. KHÔNG tự viết phần "Nguồn" hay lời miễn trừ trách nhiệm (hệ thống sẽ tự thêm).

Dưới đây là nội dung bài báo:
{article}"""


def _clean_news_text(text: str) -> str:
    text = _LEAD_IN_RE.sub("", text).replace("*", "")
    return _SOFT_BREAK_RE.sub(" ", text).strip()


def _first_content_image(body, page_url: str) -> str | None:
    urls = []
    for img in body.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        url = urljoin(page_url, src)
        if url.startswith(("http://", "https://")):
            urls.append(url)
    clean = [u for u in urls if not any(marker in u.lower() for marker in _AD_IMAGE_MARKERS)]
    return (clean or urls or [None])[0]


def _parse_article(html: str, page_url: str) -> tuple[str, str | None]:
    """(nội dung text, URL ảnh đại diện) của trang bài CafeF."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one(".detail-content")
    if body is None:
        return "", None
    image = _first_content_image(body, page_url)
    if image is None:
        og = soup.find("meta", property="og:image")
        image = urljoin(page_url, og["content"]) if og and og.get("content") else None
    for tag in body.find_all(["script", "style", "figcaption"]):
        tag.decompose()
    return _collapse(body.get_text(" "))[:_ARTICLE_MAX_CHARS], image


async def _get(url: str) -> httpx.Response:
    response = await web_reader._get_public(url, headers={"User-Agent": _USER_AGENT})
    if response.status_code != 200:
        raise web_reader.WebReaderError(f"HTTP {response.status_code} khi đọc {url}")
    return response


async def _download_image(url: str) -> tuple[str, bytes] | None:
    try:
        response = await _get(url)
    except (httpx.HTTPError, web_reader.WebReaderError) as exc:
        logger.warning("market_page: không tải được ảnh bài viết (%s).", exc)
        return None
    mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime not in _IMAGE_MIMES or len(response.content) > _MAX_IMAGE_BYTES:
        return None
    return mime, response.content


async def _load_article(entry: _Entry) -> tuple[str, tuple[str, bytes] | None]:
    """(nội dung bài, ảnh đại diện); thiếu phần nào trả rỗng/None phần đó."""
    try:
        page = await _get(entry.link)
    except (httpx.HTTPError, web_reader.WebReaderError) as exc:
        logger.warning("market_page: không đọc được bài '%s' (%s).", entry.link, exc)
        return "", None
    content, image_url = _parse_article(page.text, entry.link)
    return content, await _download_image(image_url) if image_url else None


def _news_footer(today: date) -> str:
    return (
        f"📰 Nguồn: CafeF (cafef.vn), chuyên mục Thị trường chứng khoán, tổng hợp ngày {today:%d/%m/%Y}.\n\n"
        f"{_DISCLAIMER}"
    )


async def _comment_rewrite(post_id: str, entry: _Entry, article: str) -> None:
    # Bài chính đã lên; AI hay Facebook lỗi ở bước comment không được làm job thất bại.
    try:
        response = await orchestrator.ask(_rewrite_prompt(article))
        comment = _clean_news_text(getattr(response, "text", None) or "")
        if comment and _has_investment_advice(comment):
            logger.warning("market_page: bản viết lại chứa khuyến nghị mua/bán, không comment.")
            return
        if comment:
            source = f"📰 Nguồn: CafeF — {entry.title}\n{entry.link}"
            await post_comment(
                post_id, f"{comment}\n\n{source}\n{_DISCLAIMER_SHORT}", MARKET_PAGE_KEY
            )
    except Exception:
        logger.warning("market_page: đăng comment bài nổi bật lỗi.", exc_info=True)


async def _post_news(dry_run: bool = False) -> str | None:
    feed = await _get(_NEWS_RSS)
    entries = _parse_entries(feed.content)
    if not entries:
        logger.warning("market_page: feed CafeF rỗng, bỏ bản tin.")
        return None

    response = await orchestrator.ask(_digest_prompt(entries[:_NEWS_DIGEST_ITEMS]))
    text = _clean_news_text(getattr(response, "text", None) or "")
    if len(text) < _MIN_POST_CHARS:
        logger.warning("market_page: AI trả bản tin bất thường (%d ký tự), bỏ.", len(text))
        return None
    if _has_investment_advice(text):
        logger.warning("market_page: bản tin chứa khuyến nghị mua/bán, không đăng.")
        return None
    today = datetime.now(_VN_TZ).date()
    text = f"{text}\n\n{_news_footer(today)}"
    if dry_run:
        return text

    featured = _pick_entry(entries, today)
    article, image = await _load_article(featured) if featured else ("", None)
    published = await publish_page_post(text, [image] if image else [], MARKET_PAGE_KEY)
    if article:
        await _comment_rewrite(published.post_id, featured, article)
    return text


# ─── Lịch chạy ──────────────────────────────────────────────────────────────

_JOBS = {"stock": _post_stock_report, "news": _post_news}
_LAST_RUN_KEY = "market_page:last:{job}"


def _next_slot(now: datetime) -> tuple[datetime, str]:
    candidates = []
    for job, at, weekdays in _schedule():
        for offset in range(8):
            day = (now + timedelta(days=offset)).date()
            when = datetime.combine(day, at, _VN_TZ)
            if day.weekday() in weekdays and when > now:
                candidates.append((when, job))
                break
    return min(candidates)


async def _record(job: str, outcome: str) -> None:
    """Ghi kết quả lượt chạy gần nhất cho trang admin; lỗi ghi không được cản việc đăng."""
    try:
        stamp = datetime.now(_VN_TZ).strftime("%d/%m %H:%M")
        await db.set_setting(_LAST_RUN_KEY.format(job=job), f"{stamp} — {outcome}")
    except Exception:
        logger.warning("market_page: không ghi được kết quả lượt chạy %s.", job, exc_info=True)


async def _execute(job: str, *, dry_run: bool = False) -> str | None:
    async with _run_lock:
        try:
            text = await _JOBS[job](dry_run=dry_run)
        except Exception as exc:
            if not dry_run:
                await _record(job, f"lỗi {type(exc).__name__}")
            raise
        if not dry_run:
            await _record(job, "đã đăng" if text else "bỏ qua, không có nội dung")
        return text


async def run_once(job: str, when: datetime, *, force: bool = False) -> bool:
    """Chạy 1 lượt theo lịch; mỗi (job, giờ) chỉ đăng một lần dù loop bị đánh thức lại."""
    key = f"market_page:{job}:{when:%Y-%m-%dT%H%M}"
    if not force and await db.get_setting(key):
        return False
    try:
        done = bool(await _execute(job))
    except FacebookPublicationUncertain:
        logger.error("market_page: %s chưa rõ Facebook đã tạo bài chưa, không tự đăng lại.", job)
        done = True
    if done:
        await db.set_setting(key, "1")
    return done


async def run_manual(job: str, *, publish: bool) -> str | None:
    """Chạy ngay ngoài lịch (lệnh /fb_market, trang admin); không đụng cờ chống đăng trùng
    của lịch. publish=False chỉ tạo nội dung để xem, không đăng. Trả nội dung bài."""
    if job not in _JOBS:
        raise MarketPageError(f"Job '{job}' không tồn tại (stock hoặc news).")
    if publish and MARKET_PAGE_KEY not in configured_page_keys(include_dedicated=True):
        raise MarketPageError(
            f"Chưa cấu hình FACEBOOK_PAGE_ID_{MARKET_PAGE_KEY} và "
            f"FACEBOOK_PAGE_ACCESS_TOKEN_{MARKET_PAGE_KEY}."
        )
    if _run_lock.locked():
        raise MarketPageError("Đang có job chứng khoán chạy, thử lại sau ít phút.")
    return await _execute(job, dry_run=not publish)


async def status() -> dict:
    """Ảnh chụp trạng thái cho trang admin."""
    when, job = _next_slot(datetime.now(_VN_TZ))
    return {
        "configured": MARKET_PAGE_KEY in configured_page_keys(include_dedicated=True),
        "busy": _run_lock.locked(),
        "schedule": [
            {"job": j, "time": f"{at:%H:%M}", "days": "T2-T6" if len(days) == 5 else "Hằng ngày"}
            for j, at, days in sorted(_schedule(), key=lambda row: row[1])
        ],
        "next": {"job": job, "at": when.isoformat()},
        "last": {j: await db.get_setting(_LAST_RUN_KEY.format(job=j)) for j in _JOBS},
    }


async def _loop() -> None:
    while True:
        when, job = _next_slot(datetime.now(_VN_TZ))
        await asyncio.sleep(max(0.0, (when - datetime.now(_VN_TZ)).total_seconds()))
        try:
            await run_once(job, when)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("market_page: lỗi khi chạy job %s.", job)


def start() -> None:
    global _task
    if MARKET_PAGE_KEY not in configured_page_keys(include_dedicated=True):
        logger.info(
            "market_page: chưa có FACEBOOK_PAGE_ID_%s/FACEBOOK_PAGE_ACCESS_TOKEN_%s, không bật.",
            MARKET_PAGE_KEY, MARKET_PAGE_KEY,
        )
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
