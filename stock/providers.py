"""Dữ liệu thị trường chứng khoán Việt Nam - port từ repo stock-portfolio.

Nguồn dùng ở đây đều CÔNG KHAI, KHÔNG CẦN API KEY:
- DNSE chart-api: OHLCV theo ngày, dùng cho phân tích kỹ thuật.
- DNSE price-api (GraphQL): giá khớp lệnh gần nhất, realtime, chỉ áp dụng cho cổ phiếu.
- Google News RSS: tin tức theo mã.
"""
import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

DNSE_OHLC_BASE = "https://services.entrade.com.vn/chart-api/v2/ohlcs"
DNSE_TICK_API = "https://api.dnse.com.vn/price-api/query"
PRICE_SCALE = 1000  # DNSE trả giá theo nghìn VND cho cổ phiếu
REQUEST_TIMEOUT = 8.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_SYMBOL_RE = re.compile(r"[A-Z0-9]{1,10}")

_INDEX_SYMBOLS = {"VNINDEX", "VN30", "HNXINDEX", "HNX30", "UPCOMINDEX", "UPINDEX"}

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def is_index_symbol(symbol: str) -> bool:
    return symbol.upper().replace("-", "").replace("^", "") in _INDEX_SYMBOLS


def dnse_symbol(symbol: str) -> str:
    s = symbol.upper()
    return "VNINDEX" if s in ("VNINDEX", "^VNINDEX", "VN-INDEX") else s


@dataclass
class OhlcvSeries:
    symbol: str
    closes: list = field(default_factory=list)
    highs: list = field(default_factory=list)
    lows: list = field(default_factory=list)
    volumes: list = field(default_factory=list)
    dates: list = field(default_factory=list)
    source: str = 'unknown'
    is_adjusted: bool | None = None

    @property
    def price(self) -> float:
        return self.closes[-1] if self.closes else 0.0


_OHLCV_CACHE_TTL = 90  # giây
_ohlcv_cache: dict[tuple, tuple[float, "OhlcvSeries"]] = {}


def _evict_expired(cache: dict, ttl: float) -> None:
    """Dọn entry hết hạn mỗi lần ghi - cache trước đó không có cơ chế dọn nên
    phình vô hạn theo số mã từng được tra cứu suốt vòng đời process (bot chạy
    liên tục, không restart theo lịch). Không cần lock/task nền riêng vì chỉ
    gọi ngay trước khi ghi entry mới, đủ để chặn phình không giới hạn."""
    now = time.monotonic()
    expired = [k for k, (ts, _) in cache.items() if now - ts >= ttl]
    for k in expired:
        cache.pop(k, None)


async def fetch_ohlcv(symbol: str, days: int = 90) -> OhlcvSeries:
    """Cache ngắn hạn theo (symbol, days) để khử trùng lặp khi cùng 1 mã (vd
    VNINDEX, hoặc 1 mã nằm trong nhiều ngành) được fetch nhiều lần trong
    cùng 1 lượt phân tích (analyze_symbol + sector context chạy song song)."""
    sym = symbol.strip().upper()
    key = (sym, days)
    cached = _ohlcv_cache.get(key)
    if cached and time.monotonic() - cached[0] < _OHLCV_CACHE_TTL:
        return cached[1]
    primary = await _fetch_ohlcv_dnse(sym, days)
    from stock.validation import ohlcv_contract_errors
    primary_errors = ohlcv_contract_errors(primary.closes, primary.highs, primary.lows, primary.volumes, primary.dates)
    if primary.closes and not primary_errors:
        series = primary
    else:
        _provider_failures["dnse"] += 1
        logger.warning("DNSE unusable for %s (%s); trying vnstock/VCI", sym, primary_errors or "empty")
        fallback = await _fetch_ohlcv_vnstock(sym, days)
        fallback_errors = ohlcv_contract_errors(fallback.closes, fallback.highs, fallback.lows, fallback.volumes, fallback.dates)
        if fallback.closes and not fallback_errors:
            series = fallback
        else:
            _provider_failures["vnstock"] += 1
            series = OhlcvSeries(symbol=sym, source="unavailable")
    _evict_expired(_ohlcv_cache, _OHLCV_CACHE_TTL)
    _ohlcv_cache[key] = (time.monotonic(), series)
    return series


async def _fetch_ohlcv_dnse(symbol: str, days: int = 90) -> OhlcvSeries:
    sym = symbol.strip().upper()
    is_index = is_index_symbol(sym)
    endpoint = f"{DNSE_OHLC_BASE}/{'index' if is_index else 'stock'}"
    to_ts = int(datetime.now(timezone.utc).timestamp())
    from_ts = to_ts - int(days * 1.6 + 10) * 86400

    params = {
        "from": from_ts,
        "to": to_ts,
        "symbol": dnse_symbol(sym),
        "resolution": "1D",
    }
    try:
        res = await get_http_client().get(
            endpoint, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        if res.status_code != 200:
            return OhlcvSeries(symbol=sym)
        data = res.json()
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.warning("fetch_ohlcv(%s) lỗi: %s", sym, e)
        return OhlcvSeries(symbol=sym)

    t = data.get("t") or []
    h = data.get("h") or []
    l = data.get("l") or []
    c = data.get("c") or []
    v = data.get("v") or []
    if not t:
        return OhlcvSeries(symbol=sym)

    scale = 1 if is_index else PRICE_SCALE
    bars = []
    for i in range(len(t)):
        try:
            close = float(c[i])
        except (IndexError, TypeError, ValueError):
            continue
        if not (close > 0):
            continue
        high = float(h[i]) if i < len(h) and h[i] else close
        low = float(l[i]) if i < len(l) and l[i] else close
        vol = float(v[i]) if i < len(v) and v[i] else 0.0
        bars.append((int(t[i]), high, low, close, vol))

    bars.sort(key=lambda b: b[0])
    bars = bars[-days:]

    closes = [round(b[3] * scale) for b in bars]
    highs = [round(b[1] * scale) for b in bars]
    lows = [round(b[2] * scale) for b in bars]
    volumes = [b[4] for b in bars]
    dates = [datetime.fromtimestamp(b[0], tz=_VN_TZ).strftime("%Y-%m-%d") for b in bars]

    return OhlcvSeries(symbol=sym, closes=closes, highs=highs, lows=lows, volumes=volumes, dates=dates, source="dnse")

_provider_failures = {"dnse": 0, "vnstock": 0}
def provider_health_snapshot(): return dict(_provider_failures)

def _fetch_ohlcv_vnstock_sync(symbol, days):
    try:
        from vnstock import Vnstock
        end=datetime.now(_VN_TZ).date(); start=end-timedelta(days=int(days*1.7)+30)
        df=Vnstock().stock(symbol=dnse_symbol(symbol), source="VCI").quote.history(start=start.isoformat(), end=end.isoformat(), interval="1D")
        if df is None or df.empty: return OhlcvSeries(symbol=symbol, source="vnstock-vci")
        cols={str(c).strip().lower():c for c in df.columns}
        def pick(*names): return next((cols[n] for n in names if n in cols), None)
        dc,cc,hc,lc,vc=pick("time","date","trading_date"),pick("close"),pick("high"),pick("low"),pick("volume","match_volume")
        if any(x is None for x in (dc,cc,hc,lc,vc)): return OhlcvSeries(symbol=symbol, source="vnstock-vci")
        rows=[]
        for _,r in df.iterrows():
            try: rows.append((str(r[dc])[:10],float(r[hc]),float(r[lc]),float(r[cc]),float(r[vc])))
            except (TypeError,ValueError,KeyError): pass
        rows=sorted(rows)[-days:]
        if not rows: return OhlcvSeries(symbol=symbol, source="vnstock-vci")
        median=sorted(r[3] for r in rows)[len(rows)//2]; scale=1 if is_index_symbol(symbol) or median>=1000 else PRICE_SCALE
        return OhlcvSeries(symbol, [round(r[3]*scale) for r in rows], [round(r[1]*scale) for r in rows], [round(r[2]*scale) for r in rows], [r[4] for r in rows], [r[0] for r in rows], "vnstock-vci")
    except Exception:
        logger.warning("vnstock fallback failed for %s", symbol, exc_info=True); return OhlcvSeries(symbol=symbol, source="vnstock-vci")
async def _fetch_ohlcv_vnstock(symbol, days):
    try: return await asyncio.wait_for(asyncio.to_thread(_fetch_ohlcv_vnstock_sync, symbol, days), timeout=20)
    except (TimeoutError, asyncio.TimeoutError): return OhlcvSeries(symbol=symbol, source="vnstock-vci")
_fetch_ohlcv_uncached = _fetch_ohlcv_dnse

def _fetch_symbol_universe_sync():
    try:
        from vnstock import Listing
        df=Listing(source="VCI").all_symbols(); cols={str(c).lower():c for c in df.columns}
        c=next((cols[k] for k in ("symbol","ticker","code") if k in cols),None)
        return sorted({str(v).strip().upper() for v in df[c] if c is not None and _SYMBOL_RE.fullmatch(str(v).strip().upper())})
    except Exception: return []
async def fetch_symbol_universe():
    try: symbols=await asyncio.wait_for(asyncio.to_thread(_fetch_symbol_universe_sync), timeout=30)
    except (TimeoutError, asyncio.TimeoutError): symbols=[]
    if symbols: return symbols
    from stock.sector import ALL_KNOWN_SYMBOLS
    return sorted(ALL_KNOWN_SYMBOLS)

async def fetch_current_price(symbol: str) -> float:
    series = await fetch_ohlcv(symbol, days=5)
    return series.price


_VERIFY_CACHE_TTL = 24 * 3600  # giây - cache riêng cho verify (khác cache OHLCV 90s dùng cho phân tích)
_verify_cache: dict[str, tuple[float, bool]] = {}


async def verify_symbol_exists(symbol: str) -> bool:
    sym = symbol.strip().upper()
    cached = _verify_cache.get(sym)
    if cached and time.monotonic() - cached[0] < _VERIFY_CACHE_TTL:
        return cached[1]
    price = await fetch_current_price(sym)
    result = price > 0
    _evict_expired(_verify_cache, _VERIFY_CACHE_TTL)
    _verify_cache[sym] = (time.monotonic(), result)
    return result


@dataclass
class Quote:
    symbol: str
    price: float
    prev_close: float
    change: float
    change_pct: float
    date: str
    is_realtime: bool = False


async def fetch_realtime_tick(symbol: str) -> float | None:
    """Giá khớp lệnh gần nhất qua GraphQL công khai của DNSE, CHỈ chấp nhận
    tick của đúng hôm nay - không lùi ngày tìm tick cũ hơn, vì fetch_quote()
    gắn cứng is_realtime=True + ngày hôm nay cho bất kỳ giá nào trả về ở đây;
    lùi ngày sẽ hiển thị giá của phiên trước (cuối tuần/lễ/trước giờ mở cửa)
    như thể đang "khớp lệnh realtime" ngay lúc này. Không có tick hôm nay ->
    trả None, để fetch_quote() tự dùng OHLCV (đã gắn đúng ngày thật)."""
    sym = symbol.strip().upper()
    if is_index_symbol(sym) or not _SYMBOL_RE.fullmatch(sym):
        return None

    day_str = datetime.now(_VN_TZ).strftime("%Y-%m-%d")
    client = get_http_client()
    try:
        query = (
            "query GetKrxTicksBySymbols {GetKrxTicksBySymbols("
            f'symbols: "{sym}", date: "{day_str}", limit: 1, board: 2'
            ") {ticks {matchPrice}}}"
        )
        res = await client.post(
            DNSE_TICK_API,
            json={"operationName": "GetKrxTicksBySymbols", "query": query},
            headers={"Content-Type": "application/json"},
        )
        if res.status_code == 200:
            data = res.json()
            ticks = (
                (data.get("data") or {})
                .get("GetKrxTicksBySymbols", {})
                .get("ticks", [])
            )
            if ticks:
                match_price = ticks[0].get("matchPrice")
                if match_price:
                    return round(float(match_price) * PRICE_SCALE)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as e:
        logger.warning("fetch_realtime_tick(%s) lỗi: %s", sym, e)
    return None


async def fetch_quote(symbol: str) -> Quote | None:
    sym = symbol.strip().upper()
    series = await fetch_ohlcv(sym, days=5)
    if not series.closes:
        return None

    realtime_price = await fetch_realtime_tick(sym)
    is_realtime = realtime_price is not None
    today_str = datetime.now(_VN_TZ).strftime("%Y-%m-%d")

    if is_realtime:
        price = realtime_price
        date = today_str
        if series.dates and series.dates[-1] == today_str and len(series.closes) >= 2:
            prev_close = series.closes[-2]
        else:
            prev_close = series.closes[-1]
    else:
        price = series.closes[-1]
        date = series.dates[-1] if series.dates else ""
        prev_close = series.closes[-2] if len(series.closes) >= 2 else price

    change = round(price - prev_close, 2)
    change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0
    return Quote(
        symbol=sym, price=price, prev_close=prev_close,
        change=change, change_pct=change_pct, date=date, is_realtime=is_realtime,
    )


# ─── Tin tức (Google News RSS) ──────────────────────────────────────────────

NEWS_RECENT_DAYS = 30
NEWS_MAX_ITEMS = 8

NOISE_KEYWORDS = ["cw", "chứng quyền", "cmw"]
NEGATION_WORDS = ["không", "chưa", "chẳng", "chớ", "đừng", "thay vì", "ngoại trừ"]
POS_WORDS = ["tăng", "lãi", "mua", "tích cực", "kỷ lục", "phục hồi", "tăng trưởng", "bứt phá", "vượt", "khởi sắc"]
NEG_WORDS = ["giảm", "lỗ", "bán", "rủi ro", "phạt", "vi phạm", "sụt", "bán tháo", "tụt", "hạ", "yếu"]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def sentiment_score(title: str) -> float:
    t = title.lower()
    joined = " ".join(t.split())
    score = 0
    for pos in POS_WORDS:
        idx = joined.find(pos)
        if idx == -1:
            continue
        before = joined[:idx]
        score += -1 if any(neg in before[-20:] for neg in NEGATION_WORDS) else 1
    for neg_w in NEG_WORDS:
        idx = joined.find(neg_w)
        if idx == -1:
            continue
        before = joined[:idx]
        score += 1 if any(neg in before[-20:] for neg in NEGATION_WORDS) else -1
    return _clamp(score / 3, -1, 1)


@dataclass
class NewsHeadline:
    title: str
    source: str
    pub_date: str
    url: str
    sentiment: float = 0.0


def _extract_tag(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", xml, re.IGNORECASE)
    if not m:
        return ""
    return re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", m.group(1)).strip()


async def fetch_news(symbol: str) -> list[NewsHeadline]:
    query = f"{symbol} cổ phiếu"
    url = "https://news.google.com/rss/search"
    try:
        res = await get_http_client().get(
            url,
            params={"q": query, "hl": "vi", "gl": "VN", "ceid": "VN:vi"},
            headers={"User-Agent": USER_AGENT},
        )
        if res.status_code != 200:
            return []
        text = res.text
    except httpx.HTTPError as e:
        logger.warning("fetch_news(%s) lỗi: %s", symbol, e)
        return []

    items = re.findall(r"<item>[\s\S]*?</item>", text, re.IGNORECASE)
    if not items:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_RECENT_DAYS)
    seen = set()
    news: list[NewsHeadline] = []
    for item in items:
        if len(news) >= NEWS_MAX_ITEMS:
            break
        raw_title = _extract_tag(item, "title")
        title = raw_title.split(" - ")[0].strip()
        if not title or len(title) <= 5:
            continue
        lower_t = title.lower()
        if any(k in lower_t for k in NOISE_KEYWORDS):
            continue
        key = lower_t.strip()
        if key in seen:
            continue
        pub_date_raw = _extract_tag(item, "pubDate")
        try:
            pub_dt = parsedate_to_datetime(pub_date_raw)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
        except (ValueError, TypeError):
            pass
        seen.add(key)
        source = _extract_tag(item, "source") or (raw_title.split(" - ")[-1].strip() if " - " in raw_title else "")
        link = _extract_tag(item, "link")
        news.append(NewsHeadline(title=title, source=source, pub_date=pub_date_raw, url=link, sentiment=sentiment_score(title)))

    return news


def calc_news_impact(news: list[NewsHeadline]) -> float:
    if not news:
        return 0.0
    avg = sum(n.sentiment for n in news) / len(news)
    return _clamp(avg * math.log(len(news) + 1), -2, 2)
