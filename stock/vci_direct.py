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
3. Do đó các endpoint dưới đây được gọi thẳng qua requests. Tất cả là GET
   công khai KHÔNG cần cookie/handshake (test bằng curl không cookie vẫn
   trả đủ dữ liệu):
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
import threading
import time

import requests

logger = logging.getLogger(__name__)

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

# Cache raw theo symbol, TTL đồng bộ với _FUNDAMENTALS_CACHE_TTL của
# fundamentals (30 phút): cùng 1 mã được phân tích lại trong nửa giờ thì
# valuation và growth (chạy song song trong fetch_fundamentals) tái dùng
# chung 1 lần fetch income statement thay vì bắn 2 request trùng nhau.
_CACHE_TTL_SEC = 30 * 60
_cache_lock = threading.Lock()
_ratio_cache: dict[str, tuple[float, list[dict]]] = {}
_income_cache: dict[str, tuple[float, list[dict]]] = {}
_details_cache: dict[str, tuple[float, dict]] = {}


def _get_json(path: str, params: dict | None = None) -> dict:
    """GET {BASE}/{path} và trả về JSON. path là phần sau /v1/company/,
    ví dụ "{symbol}/statistics-financial" hoặc "details" (endpoint này nằm
    trực tiếp dưới /company, không theo {symbol})."""
    response = requests.get(
        f"{_BASE_URL}/{path}",
        params=params,
        headers=_HEADERS,
        timeout=_REQUEST_TIMEOUT_SEC,
    )
    response.raise_for_status()
    return response.json()


def _cached(cache: dict, symbol: str, fetch):
    """Cache theo symbol với double-check lock: hit ngoài lock cho rẻ, miss
    thì vào lock fetch (2 luồng cùng miss chỉ fetch 1 lần)."""
    key = symbol.strip().upper()
    now = time.monotonic()
    hit = cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_SEC:
        return hit[1]
    with _cache_lock:
        hit = cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_SEC:
            return hit[1]
        rows = fetch()
        cache[key] = (time.monotonic(), rows)
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
    """Thông tin công ty + giá hiện tại (currentPrice, marketCap...). LƯU Ý
    khác 2 endpoint còn lại: details nằm ở /company/details với query param
    ?ticker= (KHÔNG phải /company/{symbol}/details - URL đó trả 404). Có thể
    raise (mạng/HTTP/JSON lỗi) - caller tự xử lý."""
    def _fetch() -> dict:
        data = _get_json("details", params={"ticker": symbol})
        return data.get("data") if isinstance(data, dict) else None

    return _cached(_details_cache, symbol, _fetch) or {}


def fetch_statistical_ratios(symbol: str) -> list[dict]:
    """Toàn bộ quý ratio của {symbol}, sort MỚI->CŨ (quý mới nhất ở [0]).
    Có thể raise (mạng/HTTP/JSON lỗi) - caller tự xử lý."""
    def _fetch() -> dict:
        data = _get_json(f"{symbol}/statistics-financial")
        rows = data.get("data") if isinstance(data, dict) else None
        return _sorted_desc(rows or [], "yearReport", "quarter")

    return _cached(_ratio_cache, symbol, _fetch)


def fetch_income_statement(symbol: str) -> list[dict]:
    """Các quý của báo cáo KQKD (INCOME_STATEMENT), sort MỚI->CŨ, trường ISA
    raw (isa3 = doanh thu thuần, isa20 = LN sau thuế, isa23 = EPS cơ bản).
    Có thể raise (mạng/HTTP/JSON lỗi) - caller tự xử lý."""
    def _fetch() -> list[dict]:
        data = _get_json(f"{symbol}/financial-statement", params={"section": "INCOME_STATEMENT"})
        payload = data.get("data") if isinstance(data, dict) else None
        rows = payload.get("quarters") if isinstance(payload, dict) else None
        return _sorted_desc(rows or [], "yearReport", "lengthReport")

    return _cached(_income_cache, symbol, _fetch)
