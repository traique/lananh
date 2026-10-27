"""Dữ liệu thị trường chứng khoán Việt Nam - port từ repo stock-portfolio.

Nguồn dùng ở đây đều CÔNG KHAI, KHÔNG CẦN API KEY:
- DNSE chart-api: OHLCV theo ngày, dùng cho phân tích kỹ thuật.
- DNSE price-api (GraphQL): giá khớp lệnh gần nhất, realtime, chỉ áp dụng cho cổ phiếu.
- VCI gap-chart (REST, httpx): OHLCV dự phòng khi DNSE lỗi.
- Google News RSS: tin tức theo mã.

RAM: đường giá/OHLCV (cảnh báo giá, danh mục, trang thị trường, bản tin sáng) chỉ
dùng httpx - KHÔNG nạp vnstock/pandas. vnstock chỉ còn cho danh sách mã/sàn
(phân tích, backtest) và stock/fundamentals.py (phân tích đầy đủ).
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


_vnstock_auth_attempted = False
_vnstock_semaphore: asyncio.Semaphore | None = None


def get_vnstock_semaphore() -> asyncio.Semaphore:
    """Giới hạn số request đồng thời tới VCI (qua vnstock) trên toàn app.

    1 lượt /phantich có thể bắn nhiều lệnh gọi VCI cùng lúc (valuation,
    foreign, growth, events, tin công ty, + tới 8 mã cùng ngành để so P/E
    trung bình) - không giới hạn thì tổng có thể lên tới hơn chục request
    song song, dễ bị VCI (nguồn miễn phí không chính thức) rate-limit/chặn
    tạm thời khiến TẤT CẢ đều fail cùng lúc (đã quan sát thấy hiện tượng này:
    bundle dữ liệu cơ bản rỗng hoàn toàn cho nhiều mã khác nhau, không phải
    lỗi riêng của 1 mã). Semaphore không đổi timeout của từng request, chỉ
    giới hạn số request cùng lúc được PHÉP bắt đầu."""
    global _vnstock_semaphore
    if _vnstock_semaphore is None:
        from core import config

        _vnstock_semaphore = asyncio.Semaphore(max(1, config.VNSTOCK_MAX_CONCURRENCY))
    return _vnstock_semaphore


def ensure_vnstock_api_key() -> None:
    """Đăng ký VNSTOCK_API_KEY (nếu có set) cho gói Community (60 req/phút,
    8 kỳ BCTC) thay vì mặc định Guest (20 req/phút, 4 kỳ). Gọi 1 lần duy
    nhất (guard bằng flag, không cần lock - gọi trùng ở 2 luồng cùng lúc lúc
    khởi động chỉ tốn 1 lần đăng ký thừa, vô hại) trước bất kỳ lệnh gọi
    `vnstock` nào trong process. Không set biến môi trường -> no-op, vnstock
    chạy Guest như cũ. Yêu cầu vnstock>=3.5.1 (bản đang pin trước đó,
    3.2.0, chưa có module xác thực này)."""
    global _vnstock_auth_attempted
    if _vnstock_auth_attempted:
        return
    _vnstock_auth_attempted = True
    import os

    api_key = os.getenv("VNSTOCK_API_KEY", "").strip()
    if not api_key:
        return
    try:
        from vnstock.core.utils.auth import register_user

        register_user(api_key=api_key)
    except Exception:
        logger.warning("vnstock: đăng ký API key thất bại, tiếp tục ở mức Guest.", exc_info=True)


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
    source: str = "unknown"
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
    expired = [k for k, entry in cache.items() if now - entry[0] >= ttl]
    for k in expired:
        cache.pop(k, None)
    while len(cache) >= 256:
        cache.pop(min(cache, key=lambda key: cache[key][0]), None)


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

    primary_errors = ohlcv_contract_errors(
        primary.closes, primary.highs, primary.lows, primary.volumes, primary.dates
    )
    if primary.closes and not primary_errors:
        series = primary
    else:
        _provider_failures["dnse"] += 1
        logger.warning("DNSE unusable for %s (%s); trying VCI", sym, primary_errors or "empty")
        fallback = await _fetch_ohlcv_vci(sym, days)
        fallback_errors = ohlcv_contract_errors(
            fallback.closes, fallback.highs, fallback.lows, fallback.volumes, fallback.dates
        )
        if fallback.closes and not fallback_errors:
            series = fallback
        else:
            _provider_failures["vci"] += 1
            series = OhlcvSeries(symbol=sym, source="unavailable")
    # Chỉ cache khi có dữ liệu THẬT - cache cả series rỗng "unavailable" sẽ
    # khiến mọi lượt phân tích trong 90s kế tiếp nhận ngay kết quả fail của
    # một lỗi mạng tạm thời mà không hề thử lại nguồn.
    if series.closes:
        _evict_expired(_ohlcv_cache, _OHLCV_CACHE_TTL)
        _ohlcv_cache[key] = (time.monotonic(), series)
    return series


def _merge_same_day_bars(bars: list[tuple]) -> list[tuple]:
    """Gộp các bar DNSE rơi cùng 1 ngày giao dịch (giờ VN) thành 1 bar.

    DNSE thỉnh thoảng trả 2 bar cho cùng 1 phiên (vd 27/12/2022: 1 bar 07:00
    chỉ có vài phút khớp lệnh + 1 bar 09:00 đủ phiên). Trước đây chuỗi bị
    validate_ohlcv từ chối ("ngày trùng lặp") -> rơi sang vnstock/VCI cho hầu
    hết mã khi lấy chuỗi dài, backtest không có dữ liệu. Gộp: high=max,
    low=min, close=bar muộn nhất, volume=tổng. ``bars`` phải đã sort theo ts."""
    merged: list[tuple] = []
    last_day = None
    for ts, high, low, close, vol in bars:
        day = datetime.fromtimestamp(ts, tz=_VN_TZ).date()
        if merged and day == last_day:
            _, p_high, p_low, _, p_vol = merged[-1]
            merged[-1] = (ts, max(p_high, high), min(p_low, low), close, p_vol + vol)
            continue
        merged.append((ts, high, low, close, vol))
        last_day = day
    return merged


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
            endpoint,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        if res.status_code != 200:
            return OhlcvSeries(symbol=sym)
        data = res.json()
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.warning("fetch_ohlcv(%s) lỗi: %s", sym, e)
        return OhlcvSeries(symbol=sym)

    if not isinstance(data, dict):
        logger.warning(
            "fetch_ohlcv(%s): DNSE trả schema không hợp lệ (%s)", sym, type(data).__name__
        )
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
        # Bọc TẤT CẢ các phép ép kiểu (trước đây chỉ close nằm trong try) -
        # 1 giá trị lạ từ DNSE (chuỗi không số, timestamp hỏng) phải bỏ đúng
        # bar đó, không được raise xuyên qua fetch_ohlcv làm chết failover
        # sang vnstock/VCI và toàn lượt phân tích.
        try:
            close = float(c[i])
            high = float(h[i]) if i < len(h) and h[i] else close
            low = float(l[i]) if i < len(l) and l[i] else close
            vol = float(v[i]) if i < len(v) and v[i] else 0.0
            ts = int(t[i])
        except (IndexError, TypeError, ValueError):
            continue
        if not (close > 0):
            continue
        bars.append((ts, high, low, close, vol))

    bars.sort(key=lambda b: b[0])
    bars = _merge_same_day_bars(bars)
    bars = bars[-days:]

    closes = [round(b[3] * scale) for b in bars]
    highs = [round(b[1] * scale) for b in bars]
    lows = [round(b[2] * scale) for b in bars]
    volumes = [b[4] for b in bars]
    dates = [datetime.fromtimestamp(b[0], tz=_VN_TZ).strftime("%Y-%m-%d") for b in bars]

    return OhlcvSeries(
        symbol=sym,
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        dates=dates,
        source="dnse",
    )


_provider_failures = {"dnse": 0, "vci": 0}


def provider_health_snapshot():
    return dict(_provider_failures)


# Nguồn dự phòng OHLCV: gọi THẲNG REST gap-chart của VCI (đúng endpoint mà
# vnstock.explorer.vci.Quote.history dùng) bằng httpx. Trước đây dự phòng đi qua
# vnstock -> nạp pandas + vnstock (~50 MB RSS) ngay khi DNSE trục trặc 1 lần, kể cả
# trong việc nền nhẹ (cảnh báo giá, cảnh báo danh mục) - và không bao giờ nhả ra.
# Giá cổ phiếu VCI trả theo VND (vnstock chia 1000 rồi phần dưới nhân lại), chỉ số
# trả theo điểm; mốc thời gian là epoch giây.
VCI_CHART_URL = "https://trading.vietcap.com.vn/api/chart/OHLCChart/gap-chart"
_VCI_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://trading.vietcap.com.vn/",
    "Origin": "https://trading.vietcap.com.vn/",
}
_VCI_TIMEOUT_SEC = 20.0


def parse_vci_chart(symbol: str, payload, days: int) -> OhlcvSeries:
    """Hàm thuần: JSON gap-chart của VCI -> OhlcvSeries (rỗng nếu sai schema)."""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return OhlcvSeries(symbol=symbol, source="vci")
    item = rows[0]
    t, h, l, c, v = (item.get(k) or [] for k in ("t", "h", "l", "c", "v"))
    if not isinstance(t, list) or not isinstance(c, list):
        return OhlcvSeries(symbol=symbol, source="vci")
    bars = []
    for i in range(min(len(t), len(c))):
        try:
            close = float(c[i])
            high = float(h[i]) if i < len(h) and h[i] else close
            low = float(l[i]) if i < len(l) and l[i] else close
            vol = float(v[i]) if i < len(v) and v[i] else 0.0
            ts = int(float(t[i]))
        except (IndexError, TypeError, ValueError):
            continue
        if close > 0:
            bars.append((ts, high, low, close, vol))
    bars.sort(key=lambda b: b[0])
    bars = _merge_same_day_bars(bars)[-days:]
    if not bars:
        return OhlcvSeries(symbol=symbol, source="vci")
    # An toàn với cả 2 quy ước: giá cổ phiếu dạng nghìn đồng (25.5) thì nhân 1000.
    median = sorted(b[3] for b in bars)[len(bars) // 2]
    scale = 1 if is_index_symbol(symbol) or median >= 1000 else PRICE_SCALE
    return OhlcvSeries(
        symbol=symbol,
        closes=[round(b[3] * scale) for b in bars],
        highs=[round(b[1] * scale) for b in bars],
        lows=[round(b[2] * scale) for b in bars],
        volumes=[b[4] for b in bars],
        dates=[datetime.fromtimestamp(b[0], tz=_VN_TZ).strftime("%Y-%m-%d") for b in bars],
        source="vci",
    )


async def _fetch_ohlcv_vci(symbol: str, days: int) -> OhlcvSeries:
    """OHLCV ngày từ VCI qua httpx (không nạp vnstock/pandas). Không raise."""
    sym = symbol.strip().upper()
    end = datetime.now(timezone.utc) + timedelta(days=1)
    payload = {
        "timeFrame": "ONE_DAY",
        "symbols": [dnse_symbol(sym)],
        "to": int(end.timestamp()),
        # Số phiên cần + dư cho ngày lễ (vnstock đếm ngày làm việc trong khoảng).
        "countBack": int(days * 1.2) + 10,
    }
    try:
        async with get_vnstock_semaphore():
            res = await get_http_client().post(
                VCI_CHART_URL, json=payload, headers=_VCI_HEADERS, timeout=_VCI_TIMEOUT_SEC
            )
        if res.status_code != 200:
            logger.warning("VCI gap-chart(%s) HTTP %s", sym, res.status_code)
            return OhlcvSeries(symbol=sym, source="vci")
        return parse_vci_chart(sym, res.json(), days)
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("VCI gap-chart(%s) lỗi: %s", sym, e)
        return OhlcvSeries(symbol=sym, source="vci")


def _fetch_symbol_universe_sync():
    try:
        ensure_vnstock_api_key()
        from vnstock import Listing

        df = Listing(source="VCI").all_symbols()
        cols = {str(c).lower(): c for c in df.columns}
        c = next((cols[k] for k in ("symbol", "ticker", "code") if k in cols), None)
        return sorted(
            {
                str(v).strip().upper()
                for v in df[c]
                if c is not None and _SYMBOL_RE.fullmatch(str(v).strip().upper())
            }
        )
    except Exception:
        return []


async def fetch_symbol_universe():
    try:
        async with get_vnstock_semaphore():
            symbols = await asyncio.wait_for(
                asyncio.to_thread(_fetch_symbol_universe_sync), timeout=30
            )
    except (TimeoutError, asyncio.TimeoutError):
        symbols = []
    if symbols:
        return symbols
    from stock.sector import ALL_KNOWN_SYMBOLS

    return sorted(ALL_KNOWN_SYMBOLS)


_EXCHANGE_MAP_TTL = 24 * 3600
# Lần lấy thất bại/rỗng chỉ cache ngắn: trước đây map rỗng bị giữ 24h, cả ngày
# mọi mã HOSE dùng bước giá 100đ dù nguồn đã hồi phục sau vài phút.
_EXCHANGE_MAP_NEGATIVE_TTL = 10 * 60
_exchange_map_cache: tuple[float, dict[str, str]] | None = None


def _fetch_exchange_map_sync() -> dict[str, str]:
    try:
        ensure_vnstock_api_key()
        from vnstock import Listing

        df = Listing(source="VCI").symbols_by_exchange()
        cols = {str(c).lower().replace("_", ""): c for c in df.columns}
        symbol_col = next((cols[k] for k in ("symbol", "ticker", "code") if k in cols), None)
        exchange_col = next(
            (cols[k] for k in ("exchange", "exchangename", "market", "floor") if k in cols), None
        )
        if symbol_col is None or exchange_col is None:
            return {}
        result = {}
        for sym, ex in zip(df[symbol_col], df[exchange_col], strict=False):
            symbol = str(sym).strip().upper()
            exchange = str(ex).strip().upper()
            if not _SYMBOL_RE.fullmatch(symbol):
                continue
            if exchange in {"HOSE", "HSX"}:
                result[symbol] = "HOSE"
            elif exchange in {"HNX"}:
                result[symbol] = "HNX"
            elif exchange == "UPCOM":
                result[symbol] = "UPCOM"
        return result
    except Exception:
        logger.debug("Không lấy được exchange map từ vnstock", exc_info=True)
        return {}


async def fetch_symbol_exchange(symbol: str) -> str | None:
    """Best-effort resolve sàn; thiếu dữ liệu thì caller dùng tick 100đ an toàn."""
    global _exchange_map_cache
    now = time.monotonic()
    ttl = (
        _EXCHANGE_MAP_TTL
        if _exchange_map_cache and _exchange_map_cache[1]
        else _EXCHANGE_MAP_NEGATIVE_TTL
    )
    if _exchange_map_cache is None or now - _exchange_map_cache[0] >= ttl:
        try:
            async with get_vnstock_semaphore():
                mapping = await asyncio.wait_for(
                    asyncio.to_thread(_fetch_exchange_map_sync), timeout=30
                )
        except (TimeoutError, asyncio.TimeoutError):
            mapping = {}
        _exchange_map_cache = (now, mapping)
    return _exchange_map_cache[1].get(symbol.strip().upper())


async def fetch_current_price(symbol: str) -> float:
    series = await fetch_ohlcv(symbol, days=5)
    return series.price


_VERIFY_CACHE_TTL_POS = 24 * 3600  # kết quả TỒN TẠI - cache 24h như cũ
_VERIFY_CACHE_TTL_NEG = 10 * 60  # kết quả "không tồn tại" - chỉ cache 10 phút
_verify_cache: dict[str, tuple[float, bool, int]] = {}  # (ts, result, ttl)


async def verify_symbol_exists(symbol: str) -> bool:
    """Kết quả âm dựa trên fetch_current_price (cùng chuỗi failover DNSE →
    vnstock) - một lỗi mạng/rate-limit tạm thời sẽ trả giá 0. Nếu cache âm
    dài như kết quả dương, mã hợp lệ bị coi là "không tồn tại" suốt 24h sau
    một gián đoạn vài giây, nên TTL âm ngắn hơn hẳn."""
    sym = symbol.strip().upper()
    cached = _verify_cache.get(sym)
    if cached and time.monotonic() - cached[0] < cached[2]:
        return cached[1]
    price = await fetch_current_price(sym)
    result = price > 0
    ttl = _VERIFY_CACHE_TTL_POS if result else _VERIFY_CACHE_TTL_NEG
    _evict_expired(_verify_cache, _VERIFY_CACHE_TTL_POS)
    _verify_cache[sym] = (time.monotonic(), result, ttl)
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
            ticks = (data.get("data") or {}).get("GetKrxTicksBySymbols", {}).get("ticks", [])
            if ticks:
                match_price = ticks[0].get("matchPrice")
                if match_price:
                    return round(float(match_price) * PRICE_SCALE)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as e:
        logger.warning("fetch_realtime_tick(%s) lỗi: %s", sym, e)
    return None


async def fetch_quote(symbol: str) -> Quote | None:
    sym = symbol.strip().upper()
    # OHLCV và realtime tick là 2 request độc lập - chạy song song thay vì
    # await tuần tự (trước đây tốn thêm một RTT mạng mỗi lần quote).
    series, realtime_price = await asyncio.gather(
        fetch_ohlcv(sym, days=5),
        fetch_realtime_tick(sym),
    )
    if not series.closes:
        return None
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
        symbol=sym,
        price=price,
        prev_close=prev_close,
        change=change,
        change_pct=change_pct,
        date=date,
        is_realtime=is_realtime,
    )


# ─── Tin tức (Google News RSS) ──────────────────────────────────────────────

NEWS_RECENT_DAYS = 30
NEWS_MAX_ITEMS = 8

NOISE_KEYWORDS = ["cw", "chứng quyền", "cmw"]
NEGATION_WORDS = ["không", "chưa", "chẳng", "chớ", "đừng", "thay vì", "ngoại trừ"]
POS_WORDS = [
    "tăng",
    "lãi",
    "mua",
    "tích cực",
    "kỷ lục",
    "phục hồi",
    "tăng trưởng",
    "bứt phá",
    "vượt",
    "khởi sắc",
]
NEG_WORDS = [
    "giảm",
    "lỗ",
    "bán",
    "rủi ro",
    "phạt",
    "vi phạm",
    "sụt",
    "bán tháo",
    "tụt",
    "hạ",
    "yếu",
]


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
    # None = chưa rõ, dùng heuristic title_mentions_symbol ở nơi build prompt
    # (tin cào Google News). True = nguồn CHÍNH CHỦ đã xác nhận đúng mã (tin
    # công ty lấy trực tiếp từ VCI qua fundamentals.fetch_company_news), bỏ
    # qua heuristic vì title có thể không chứa mã CK mà vẫn đúng công ty.
    confirmed: bool | None = None


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
        source = _extract_tag(item, "source") or (
            raw_title.split(" - ")[-1].strip() if " - " in raw_title else ""
        )
        link = _extract_tag(item, "link")
        news.append(
            NewsHeadline(
                title=title,
                source=source,
                pub_date=pub_date_raw,
                url=link,
                sentiment=sentiment_score(title),
            )
        )

    return news


def calc_news_impact(news: list[NewsHeadline]) -> float:
    if not news:
        return 0.0
    avg = sum(n.sentiment for n in news) / len(news)
    return _clamp(avg * math.log(len(news) + 1), -2, 2)
