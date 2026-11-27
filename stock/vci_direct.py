"""Gọi THẲNG REST API iq-insight-service của VCI cho định giá & BCTC,
bypass thư viện vnstock.

Vì sao module này tồn tại (đọc trước khi sửa - đã xác minh 08/09/2026):

1. Endpoint mà vnstock<=3.5.1 dùng (trading.vietcap.com.vn/data-mt/graphql)
   đã bị VCI tắt: vẫn trả HTTP 200 nhưng body rỗng {} -> mọi lệnh gọi
   Finance/Company trên vnstock 3.5.1 chết với KeyError: 'data' cho MỌI mã.
   Đã tái hiện bằng chính payload GraphQL gốc của vnstock 3.5.1.
2. vnstock 4.0.7 đã chuyển sang REST iq.vietcap.com.vn, nhưng các method
   công khai Finance.ratio()/income_statement() cắt dữ liệu còn 4 kỳ bằng
   .head(4) trên danh sách quý trả về CŨ->MỚI -> "4 kỳ gần nhất" mà nó trả
   lại là 2018-Q1..Q4 (tái hiện với CII: ratio() trả đúng 4 cột 2018, dù
   API raw có 41 quý tới 2026-Q2). Các method công khai không nhận limit,
   không lấy được tail() -> không dùng được.
3. Do đó các endpoint dưới đây được gọi thẳng qua 1 httpx.Client đồng bộ dùng
   chung (giữ kết nối TLS giữa các lượt gọi; mọi hàm ở đây chạy trong
   asyncio.to_thread). Tất cả là GET công khai KHÔNG cần cookie/handshake
   (test bằng curl không cookie vẫn trả đủ dữ liệu):
     GET {BASE}/{symbol}/statistics-financial
         -> data = list 41+ quý, mỗi quý 1 dict ratio raw.
     GET {BASE}/{symbol}/financial-statement?section=INCOME_STATEMENT
         -> data.quarters = list quý, mỗi quý 1 dict mã ISA.
     GET {BASE}/details?ticker={symbol}
         -> data = dict công ty + giá hiện tại (currentPrice...). Endpoint
            này NẰM TRỰC TIẾP dưới /company, không theo /{symbol}.

Tên trường QUAN TRỌNG (đã đối chiếu với bảng mapping
financial-statement/metrics của VCI cho mã loại CT, 08/09/2026):
- statistics-financial (ratio) - bản raw của VCI, KHÔNG có trường eps:
  pe, pb, roe (phân số 0-1), dividendYield (phân số 0-1, đang trả 0.0
  cho cả mã có trả cổ tức -> không tin cậy),
  debtToEquity (ratio, không phải %), currentRatio, ps, roa, roic,
  marketCap, numberOfSharesMktCap, yearReport (int), quarter (int 1-4).
  EPS không lấy từ đây; fundamentals.py tự suy ra EPS = currentPrice/pe.
- financial-statement INCOME_STATEMENT - mã cột ISA (cố định theo chuẩn
  VCI, cùng nghĩa cho mọi mã loại CT):
  isa3  = Doanh thu thuần (ngân hàng KHÔNG có - isa1..isa15 null với mã
          loại NH, chỉ populate từ isa16 trở đi)
  isa20 = Lãi/(lỗ) thuần sau thuế
  isa23 = EPS cơ bản theo KQKD - TỒN TẠI NHƯNG SAI LỆCH với một số mã
          (CII báo 12 khi TTM thật ~170; CTD báo 721/~5200; VCB/HPG thì
          khớp) - KHÔNG dùng làm EPS, chỉ đối chiếu khi cần.
  yearReport (int), lengthReport (1-4 cho quý, 5 cho cả năm).

Danh sách quý từ API theo thứ tự CŨ->MỚI; các hàm ở đây sort lại GIẢM DẦN
(quý mới nhất luôn ở [0]) để giữ nguyên quy ước mà phần parse trong
fundamentals.py từng giả định.

Không gọi khi không có mạng: mọi exception được để nảy lên - caller
(fundamentals/corporate_actions) tự catch và degrade (None/[]) như cũ.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_BASE_URL = "https://iq.vietcap.com.vn/api/iq-insight-service/v1/company"
_HEADERS = {
    # iq.vietcap.com.vn không bắt bắt buộc UA cụ thể, nhưng dùng UA trình
    # duyệt để hạn chế rủi ro bị WAF chặn theo pattern client lạ.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://trading.vietcap.com.vn/",
}
_REQUEST_TIMEOUT_SEC = 10

# 1 client đồng bộ dùng chung cho mọi endpoint VCI: trước đây mỗi lượt gọi là 1
# requests.get mới (TCP + TLS mới, ~13 lượt cho 1 /phantich lạnh). Client của httpx
# an toàn khi dùng từ nhiều thread (các hàm ở đây chạy qua asyncio.to_thread); pool
# nhỏ vì số request VCI đồng thời đã bị providers.get_vnstock_semaphore chặn.
_HTTP_LIMITS = httpx.Limits(max_connections=6, max_keepalive_connections=3, keepalive_expiry=30)
_client: httpx.Client | None = None
_client_guard = threading.Lock()
# Thử lại đúng 1 lần, nghỉ ngắn, khi VCI rate-limit/quá tải (429/502/503/504) hoặc kết
# nối hỏng (kết nối giữ lâu bị server đóng). Retry-After của 429 được tôn trọng tới 2s.
_RETRY_STATUS = frozenset({429, 502, 503, 504})
_RETRY_DELAY_SEC = 0.5
_RETRY_AFTER_MAX_SEC = 2.0


def _http() -> httpx.Client:
    global _client
    client = _client
    if client is None:
        with _client_guard:
            if _client is None:
                _client = httpx.Client(
                    timeout=_REQUEST_TIMEOUT_SEC, limits=_HTTP_LIMITS, follow_redirects=True
                )
            client = _client
    return client


def close_http_client() -> None:
    """Đóng client dùng chung (lúc tắt app/test); lượt gọi sau tự tạo lại."""
    global _client
    with _client_guard:
        client, _client = _client, None
    if client is not None:
        client.close()


def _retry_delay(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        return min(max(float(raw), 0.0), _RETRY_AFTER_MAX_SEC)
    except ValueError:
        return _RETRY_DELAY_SEC


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """Gọi VCI qua client dùng chung, thử lại 1 lần khi lỗi thoáng qua; lỗi HTTP còn
    lại -> raise (httpx.HTTPStatusError) như raise_for_status của requests trước đây."""
    try:
        response = _http().request(method, url, **kwargs)
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        time.sleep(_RETRY_DELAY_SEC)
        response = _http().request(method, url, **kwargs)
    else:
        if response.status_code in _RETRY_STATUS:
            logger.info("VCI %s %s HTTP %s - thử lại 1 lần", method, url, response.status_code)
            time.sleep(_retry_delay(response))
            response = _http().request(method, url, **kwargs)
    response.raise_for_status()
    return response


# Cache raw theo symbol, TTL đồng bộ với _FUNDAMENTALS_CACHE_TTL của
# fundamentals (30 phút): cùng 1 mã được phân tích lại trong nửa giờ thì
# valuation và growth (chạy song song trong fetch_fundamentals) tái dùng
# chung 1 lần fetch income statement thay vì bắn 2 request trùng nhau.
_CACHE_TTL_SEC = 30 * 60
_CACHE_MAX_ENTRIES = 128
# _cache_lock CHỈ bảo vệ thao tác đọc/ghi dict (micro-giây), TUYỆT ĐỐI không
# giữ trong lúc gọi mạng: trước đây 1 lock toàn cục bị giữ suốt request (timeout
# 10s) nên mọi lệnh gọi VCI của MỌI mã/endpoint bị xếp hàng tuần tự, và thread
# mồ côi sau asyncio.wait_for timeout vẫn giữ lock -> timeout dây chuyền.
_cache_lock = threading.Lock()
# Lock theo TỪNG khoá (endpoint, mã): 2 luồng cùng miss đúng 1 khoá chỉ fetch 1
# lần, luồng của khoá khác không phải chờ. Dict có trần, chỉ dọn lock rảnh.
_inflight_guard = threading.Lock()
_inflight_locks: dict[tuple[int, str], threading.Lock] = {}
_INFLIGHT_MAX = 256
_ratio_cache: dict[str, tuple[float, list[dict]]] = {}
_income_cache: dict[str, tuple[float, list[dict]]] = {}
_details_cache: dict[str, tuple[float, dict]] = {}

# Chỉ cache ĐÚNG các trường stock/fundamentals.py đọc (+ trường sắp xếp quý) và số
# quý nó dùng: hàng raw của VCI có 100-150 trường x 41+ quý, nhân 128 mã x 2
# endpoint là hàng chục MB RAM cho dữ liệu không ai đọc. Thêm trường mới ở
# fundamentals thì thêm vào đây.
RATIO_HISTORY_QUARTERS = 20  # percentile P/E ~5 năm (fundamentals._PE_HISTORY_QUARTERS)
_RATIO_KEYS = (
    "pe",
    "pb",
    "roe",
    "dividendYield",
    "debtToEquity",
    "debtPerEquity",
    "currentRatio",
    "yearReport",
    "quarter",
)
INCOME_QUARTERS = 5  # QoQ cần quý [0],[1]; YoY cần quý [4]
_INCOME_KEYS = ("isa3", "isa20", "yearReport", "lengthReport")
_DETAILS_KEYS = ("currentPrice",)


def _project(row: dict, keys: tuple[str, ...]) -> dict:
    return {key: row[key] for key in keys if key in row}


def _get_json(path: str, params: dict | None = None) -> dict:
    """GET {BASE}/{path} và trả về JSON. path là phần sau /v1/company/,
    ví dụ "{symbol}/statistics-financial" hoặc "details" (endpoint này nằm
    trực tiếp dưới /company, không theo {symbol})."""
    return _request("GET", f"{_BASE_URL}/{path}", params=params, headers=_HEADERS).json()


def _key_lock(cache: dict, key: str) -> threading.Lock:
    with _inflight_guard:
        lock_key = (id(cache), key)
        lock = _inflight_locks.get(lock_key)
        if lock is None:
            if len(_inflight_locks) >= _INFLIGHT_MAX:
                for stale_key, stale_lock in list(_inflight_locks.items()):
                    if not stale_lock.locked():
                        _inflight_locks.pop(stale_key, None)
            lock = _inflight_locks[lock_key] = threading.Lock()
        return lock


def _cache_lookup(cache: dict, key: str):
    with _cache_lock:
        hit = cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_SEC:
            return True, hit[1]
    return False, None


def _cache_store(cache: dict, key: str, rows) -> None:
    with _cache_lock:
        now = time.monotonic()
        for stale in [k for k, (ts, _) in cache.items() if now - ts >= _CACHE_TTL_SEC]:
            cache.pop(stale, None)
        if len(cache) >= _CACHE_MAX_ENTRIES and key not in cache:
            cache.pop(min(cache, key=lambda k: cache[k][0]), None)
        cache[key] = (now, rows)


def _cached(cache: dict, symbol: str, fetch):
    """Cache theo symbol: hit trả ngay; miss thì chỉ khoá RIÊNG khoá đó trong
    lúc fetch (luồng khác cùng khoá chờ rồi dùng lại kết quả), không chặn
    các mã/endpoint khác."""
    key = symbol.strip().upper()
    found, value = _cache_lookup(cache, key)
    if found:
        return value
    with _key_lock(cache, key):
        found, value = _cache_lookup(cache, key)
        if found:
            return value
        rows = fetch()  # gọi mạng NGOÀI _cache_lock
        _cache_store(cache, key, rows)
        return rows


def _sorted_desc(rows: list[dict], year_key: str, quarter_key: str) -> list[dict]:
    def _key(r: dict) -> tuple[int, int]:
        try:
            year = int(r.get(year_key) or 0)
        except (TypeError, ValueError):
            year = 0
        try:
            quarter = int(r.get(quarter_key) or 0)
        except (TypeError, ValueError):
            quarter = 0
        return year, quarter

    return sorted((r for r in rows if isinstance(r, dict)), key=_key, reverse=True)


def fetch_details(symbol: str) -> dict:
    """Giá hiện tại của công ty ({"currentPrice": ...} - chỉ giữ trường
    fundamentals dùng để suy EPS). LƯU Ý khác 2 endpoint còn lại: details nằm ở
    /company/details với query param ?ticker= (KHÔNG phải /company/{symbol}/details
    - URL đó trả 404). Có thể raise (mạng/HTTP/JSON lỗi) - caller tự xử lý."""

    def _fetch() -> dict | None:
        data = _get_json("details", params={"ticker": symbol})
        details = data.get("data") if isinstance(data, dict) else None
        return _project(details, _DETAILS_KEYS) if isinstance(details, dict) else None

    return _cached(_details_cache, symbol, _fetch) or {}


def fetch_statistical_ratios(symbol: str) -> list[dict]:
    """RATIO_HISTORY_QUARTERS quý ratio gần nhất của {symbol}, sort MỚI->CŨ (quý mới
    nhất ở [0]), mỗi quý chỉ giữ các trường trong _RATIO_KEYS.
    Có thể raise (mạng/HTTP/JSON lỗi) - caller tự xử lý."""

    def _fetch() -> list[dict]:
        data = _get_json(f"{symbol}/statistics-financial")
        rows = data.get("data") if isinstance(data, dict) else None
        newest = _sorted_desc(rows or [], "yearReport", "quarter")[:RATIO_HISTORY_QUARTERS]
        return [_project(row, _RATIO_KEYS) for row in newest]

    return _cached(_ratio_cache, symbol, _fetch)


def fetch_income_statement(symbol: str) -> list[dict]:
    """INCOME_QUARTERS quý gần nhất của báo cáo KQKD (INCOME_STATEMENT), sort
    MỚI->CŨ, chỉ giữ isa3 (doanh thu thuần), isa20 (LN sau thuế) và trường kỳ báo cáo.
    Có thể raise (mạng/HTTP/JSON lỗi) - caller tự xử lý."""

    def _fetch() -> list[dict]:
        data = _get_json(f"{symbol}/financial-statement", params={"section": "INCOME_STATEMENT"})
        payload = data.get("data") if isinstance(data, dict) else None
        rows = payload.get("quarters") if isinstance(payload, dict) else None
        newest = _sorted_desc(rows or [], "yearReport", "lengthReport")[:INCOME_QUARTERS]
        return [_project(row, _INCOME_KEYS) for row in newest]

    return _cached(_income_cache, symbol, _fetch)


# ─── Lịch sự kiện quyền SẮP TỚI (cổ tức, phát hành/quyền mua, ĐHCĐ) ──────────
# Company.events() của vnstock gọi cùng endpoint nhưng cố định toDate = hôm nay
# (không thấy sự kiện tương lai) và corporate_actions chỉ giữ ex_date của cổ tức
# tiền/sự kiện cổ phiếu (bỏ quyền mua, ĐHCĐ, ngày chốt quyền). Hàm này gọi
# thẳng /v1/events với cửa sổ ngày tường minh.
#
# CHƯA xác minh với API thật (gọi mạng cần chủ repo đồng ý) toDate lọc theo ngày
# công bố hay ngày sự kiện: cửa sổ lấy RỘNG cả 2 phía (từ _EVENTS_LOOKBACK_DAYS
# ngày trước tới days_ahead ngày sau) rồi lọc lại tại chỗ theo ngày sự kiện, đúng
# với cả 2 cách hiểu. Tên trường dò theo từ khoá (như corporate_actions).
_EVENTS_URL = "https://iq.vietcap.com.vn/api/iq-insight-service/v1/events"
_EVENT_CODES = "DIV,ISS,AGME,AGMR,EGME"
_EVENTS_LOOKBACK_DAYS = 60
_EVENTS_PAGE_SIZE = 50
_EVENTS_CACHE_TTL_SEC = 12 * 3600
_events_cache: dict[str, tuple[float, list]] = {}


@dataclass(frozen=True)
class UpcomingEvent:
    symbol: str
    code: str  # DIV | ISS | AGME | AGMR | EGME | ""
    title: str
    ex_date: str  # YYYY-MM-DD hoặc ""
    record_date: str
    issue_date: str  # ngày họp/ngày thực hiện (tuỳ loại sự kiện)
    value: float | None  # cổ tức tiền (giá trị thô của nguồn)

    @property
    def key_date(self) -> str:
        """Ngày quan trọng nhất để báo trước: GDKHQ, không có thì ngày chốt, ngày họp."""
        return self.ex_date or self.record_date or self.issue_date


def _event_date(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Epoch mili-giây; đổi theo giờ VN (mốc 0h UTC hay 0h VN đều ra đúng ngày).
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=_VN_TZ).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    return text[:10] if len(text) >= 10 and text[4] == "-" else ""


def _pick(row: dict, *keywords: str):
    lowered = {str(k).lower(): v for k, v in row.items()}
    for keyword in keywords:
        for key, value in lowered.items():
            if keyword in key and value not in (None, ""):
                return value
    return None


def parse_upcoming_events(symbol: str, rows: list) -> list[UpcomingEvent]:
    """Hàm thuần: dòng JSON của /v1/events -> UpcomingEvent (bỏ dòng thiếu ngày)."""
    out: list[UpcomingEvent] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        event = UpcomingEvent(
            symbol=symbol,
            code=str(_pick(row, "eventcode", "eventlistcode") or "").strip().upper(),
            title=str(
                _pick(row, "eventtitlevi", "eventtitle", "eventnamevi", "eventname", "title") or ""
            ).strip()[:160],
            ex_date=_event_date(_pick(row, "exright")),
            record_date=_event_date(_pick(row, "recorddate")),
            issue_date=_event_date(_pick(row, "issuedate")),
            value=_float_or_none(_pick(row, "valuepershare", "dividend", "cashvalue")),
        )
        if event.key_date:
            out.append(event)
    return out


def _float_or_none(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def fetch_upcoming_events(symbol: str, days_ahead: int = 45) -> list[UpcomingEvent]:
    """Sự kiện quyền có ngày sự kiện từ hôm nay tới ``days_ahead`` ngày tới, cache
    12 giờ theo mã (có trần). Có thể raise (mạng/HTTP/JSON) - caller tự xử lý."""
    key = symbol.strip().upper()
    with _cache_lock:
        hit = _events_cache.get(key)
        if hit and time.monotonic() - hit[0] < _EVENTS_CACHE_TTL_SEC:
            return list(hit[1])
    today = datetime.now(_VN_TZ).date()
    response = _request(
        "GET",
        _EVENTS_URL,
        params={
            "ticker": key,
            "fromDate": (today - timedelta(days=_EVENTS_LOOKBACK_DAYS)).strftime("%Y%m%d"),
            "toDate": (today + timedelta(days=days_ahead)).strftime("%Y%m%d"),
            "eventCode": _EVENT_CODES,
            "page": 0,
            "size": _EVENTS_PAGE_SIZE,
        },
        headers=_HEADERS,
    )
    data = response.json()
    payload = data.get("data") if isinstance(data, dict) else None
    rows = payload.get("content") if isinstance(payload, dict) else payload
    limit = (today + timedelta(days=days_ahead)).isoformat()
    events = [
        e
        for e in parse_upcoming_events(key, rows if isinstance(rows, list) else [])
        if today.isoformat() <= e.key_date <= limit
    ]
    with _cache_lock:
        now = time.monotonic()
        for stale in [
            k for k, (ts, _) in _events_cache.items() if now - ts >= _EVENTS_CACHE_TTL_SEC
        ]:
            _events_cache.pop(stale, None)
        if len(_events_cache) >= _CACHE_MAX_ENTRIES and key not in _events_cache:
            _events_cache.pop(min(_events_cache, key=lambda k: _events_cache[k][0]), None)
        _events_cache[key] = (now, events)
    return list(events)


# ─── Lịch sự kiện quyền ĐÃ QUA (điều chỉnh chuỗi giá - stock/corporate_actions.py) ──
# Thay Company(symbol).events() của vnstock (cùng endpoint /v1/events, mặc định 10
# năm tới hôm nay, trang đầu 50 dòng, mọi loại sự kiện) để việc nền nhẹ (cảnh báo
# giá/danh mục, bản tin sáng) không nạp vnstock + pandas (~50 MB RSS). Chỉ lấy
# DIV (cổ tức tiền) + ISS (phát hành cổ phiếu) - đúng 2 loại corporate_actions dùng,
# nên 100 dòng chứa nhiều sự kiện liên quan hơn trang 50 dòng lẫn mọi loại trước đây.
_HISTORY_EVENT_CODES = "DIV,ISS"
_HISTORY_YEARS = 10
_HISTORY_PAGE_SIZE = 100
_DATE_KEYS = ("public_date", "issue_date", "record_date", "exright_date", "display_date")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def snake_key(name: str) -> str:
    """exrightDate -> exright_date (cùng cách vnstock đặt tên cột events())."""
    return _CAMEL_RE.sub("_", re.sub(r"[\s.\-]+", "_", str(name))).lower()


def normalize_event_rows(rows: list) -> list[dict]:
    """Dòng JSON /v1/events -> dict khoá snake_case, ngày epoch ms -> YYYY-MM-DD
    (giờ VN), như DataFrame events() của vnstock mà corporate_actions từng đọc."""
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = {snake_key(k): v for k, v in row.items()}
        for key in _DATE_KEYS:
            if key in item and item[key] not in (None, ""):
                item[key] = _event_date(item[key])
        out.append(item)
    return out


def fetch_event_history(symbol: str) -> list[dict]:
    """Sự kiện DIV/ISS trong 10 năm tới hôm nay (đã chuẩn hoá khoá/ngày). Có thể
    raise (mạng/HTTP/JSON) - corporate_actions tự bắt và coi như không có sự kiện."""
    key = symbol.strip().upper()
    today = datetime.now(_VN_TZ).date()
    response = _request(
        "GET",
        _EVENTS_URL,
        params={
            "ticker": key,
            "fromDate": (today - timedelta(days=365 * _HISTORY_YEARS)).strftime("%Y%m%d"),
            "toDate": today.strftime("%Y%m%d"),
            "eventCode": _HISTORY_EVENT_CODES,
            "page": 0,
            "size": _HISTORY_PAGE_SIZE,
        },
        headers=_HEADERS,
    )
    data = response.json()
    payload = data.get("data") if isinstance(data, dict) else None
    rows = payload.get("content") if isinstance(payload, dict) else payload
    return normalize_event_rows(rows if isinstance(rows, list) else [])


# ─── Danh sách mã + bảng giá trading.vietcap.com.vn (thay vnstock) ──────────────
# vnstock 4.x bị khoá từ 10/11/2026 (kho vnstocks.com trả 410). Hai endpoint công
# khai dưới đây là nơi Listing/Trading(source="VCI") của vnstock gọi; không cần
# cookie/khoá (đã thử 16/11/2026, trả JSON nén gzip - httpx tự giải nén):
#   GET  {_PRICE_API}/getAll  -> list {symbol, type: STOCK|CW|ETF|BOND|FU|...,
#        board: HSX|HNX|UPCOM|DELISTED|BOND, organName...} (~3.800 dòng)
#   POST {_PRICE_API}/getList {"symbols": [...]} -> list {listingInfo, bidAsk, matchPrice}
#        matchPrice: foreignBuyVolume/foreignSellVolume (phiên gần nhất, về 0 trước
#        giờ mở cửa), currentRoom/totalRoom (số cổ phiếu NĐTNN còn được mua / tối đa).
_PRICE_API = "https://trading.vietcap.com.vn/api/price/symbols"
_PRICE_HEADERS = {**_HEADERS, "Origin": "https://trading.vietcap.com.vn"}


def fetch_listing() -> list[dict]:
    """Mọi mã trên VCI (kể cả mã đã huỷ niêm yết, chứng quyền, trái phiếu). Có thể
    raise (mạng/HTTP/JSON) - caller tự xử lý và nên cache kết quả đã rút gọn."""
    rows = _request("GET", f"{_PRICE_API}/getAll", headers=_PRICE_HEADERS).json()
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def fetch_match_price(symbol: str) -> dict:
    """Khối ``matchPrice`` trên bảng giá VCI của 1 mã ({} nếu không có). Có thể raise."""
    rows = _request(
        "POST",
        f"{_PRICE_API}/getList",
        json={"symbols": [symbol.strip().upper()]},
        headers=_PRICE_HEADERS,
    ).json()
    first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    match = first.get("matchPrice")
    return match if isinstance(match, dict) else {}
