"""Định giá cơ bản (P/E, P/B, EPS, ROE, D/E...) + dòng tiền khối ngoại +
tăng trưởng theo quý + lịch sự kiện, dựa trên `vnstock`.

Nguồn (đã thay đổi 08/09/2026 - đọc kỹ trước khi sửa):
- `_fetch_valuation_sync` (định giá + EPS) và `_fetch_growth_sync` (tăng
  trưởng DT/LN quý) KHÔNG còn đi qua vnstock mà gọi thẳng REST VCI qua
  `stock.vci_direct`. Nguyên nhân: endpoint GraphQL mà vnstock<=3.5.1 dùng
  bị VCI tắt (KeyError 'data' cho mọi mã, log production 08/09/2026), còn
  các method Finance công khai trên vnstock 4.0.7 cắt dữ liệu còn 4 quý
  2018 sai do .head(4) trên danh sách cũ->mới - chi tiết đầy đủ trong
  docstring stock/vci_direct.py.
- `_fetch_events_sync`, `_fetch_company_news_sync`, `_fetch_foreign_sync`
  vẫn qua vnstock (Company.events/news, Trading.price_board) - các method
  này đã được kiểm tra chạy tốt trên vnstock 4.0.7.

⚠️ QUAN TRỌNG - đọc trước khi tin tưởng module này:
- VCI/vnstock là nguồn bên thứ 3, API không tài liệu hoá chính thức ->
  KHÔNG có SLA, có thể lỗi hoặc đổi cấu trúc dữ liệu bất kỳ lúc nào mà
  không báo trước.
- Các hàm qua vnstock match tên cột theo TỪ KHOÁ (substring) thay vì tên
  cột cứng, để bớt nhạy cảm với thay đổi nhỏ giữa các phiên bản - nhưng
  KHÔNG đảm bảo luôn đúng 100%. Nếu không tìm thấy cột phù hợp, trả về None
  cho trường đó thay vì đoán liều. Với vci_direct, tên trường raw (pe, pb,
  isa20...) được gọi thẳng - đã cố định theo mapping metrics của VCI.
- Giấy phép vnstock: dành cho cá nhân/phi thương mại - phù hợp bot 1 user
  này, KHÔNG dùng cho mục đích thương mại nếu chưa xin phép tác giả.
- Gọi nguồn ngoài là thao tác ĐỒNG BỘ (blocking, dùng requests) -> luôn
  chạy qua asyncio.to_thread() để không chặn event loop, và luôn có timeout.

🧪 GHI CHÚ ĐỘ TIN CẬY:
- `_fetch_valuation_sync`: P/E, P/B, ROE (đã *100), D/E, current ratio lấy
  trực tiếp từ raw; EPS = currentPrice/pe (isa23 của VCI sai lệch với một
  số mã - xem _fetch_eps_sync); dividend_yield của VCI đang trả 0.0 cả với
  mã có trả cổ tức -> coi 0 là "chưa có dữ liệu", đừng kết luận "không trả
  cổ tức" từ trường này.
- `_fetch_growth_sync`: isa3 (doanh thu thuần) NULL với mã ngân hàng (mã
  loại NH chỉ populate isa16 trở đi) -> với ngân hàng, phần doanh thu sẽ
  "chưa có dữ liệu", phần lợi nhuận (isa20) vẫn hoạt động.
- `_fetch_events_sync` (lịch sự kiện): đã xác minh end-to-end trên vnstock
  4.0.7 (Company.events()), ưu tiên event_title_* trước event_name_* (tên
  loại chung chung: "Sự kiện khác", "Đại hội Đồng Cổ đông"...).
- Khối ngoại NHIỀU phiên (lịch sử mua/bán ròng theo chuỗi ngày) ĐÃ BỊ BỎ
  KHỎI module này: cả facade cũ (`vnstock/explorer/vci/trading.py`, chỉ có
  đúng 1 method công khai là price_board()) lẫn API mới
  (`vnstock.api.trading.foreign_trade()`) đều không có provider nào implement
  thật (chỉ là stub `pass`). Chỉ còn `_fetch_foreign_sync` (khối ngoại
  PHIÊN GẦN NHẤT, qua price_board() - có hoạt động thật) là nguồn khối ngoại
  duy nhất trong bot này.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from stock import features as feat
from stock import fundamental_profiles, vci_direct
from stock.providers import NewsHeadline, ensure_vnstock_api_key, get_vnstock_semaphore, sentiment_score

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SEC = 15
_PE_HISTORY_QUARTERS = 20  # ~5 năm dữ liệu quý, dùng để tính percentile P/E


@dataclass
class Valuation:
    pe: float | None = None
    pb: float | None = None
    eps: float | None = None
    roe: float | None = None
    dividend_yield: float | None = None
    debt_equity: float | None = None
    current_ratio: float | None = None
    pe_percentile: float | None = None  # 0-100: P/E hiện tại đang cao/thấp hơn bao nhiêu % lịch sử
    pe_history_quarters: int = 0  # số quý dữ liệu thực tế dùng để tính percentile (độ tin cậy)


@dataclass
class ForeignFlowReal:
    foreign_buy_vol: float | None = None
    foreign_sell_vol: float | None = None
    foreign_net_vol: float | None = None
    foreign_room_pct: float | None = None


@dataclass
class GrowthTrend:
    revenue_qoq_pct: float | None = None
    revenue_yoy_pct: float | None = None
    profit_qoq_pct: float | None = None
    profit_yoy_pct: float | None = None
    quarters_available: int = 0


@dataclass
class UpcomingEvent:
    """THỬ NGHIỆM - xem ghi chú đầu file."""
    title: str
    date: str | None = None


def _to_float(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _flatten_columns(columns) -> list[str]:
    flat = []
    for col in columns:
        if isinstance(col, tuple):
            flat.append("_".join(str(c) for c in col if c).strip().lower())
        else:
            flat.append(str(col).strip().lower())
    return flat


def _find_col(flat_columns: list[str], *keywords: str) -> int | None:
    """Trả về index cột đầu tiên chứa TẤT CẢ keyword (không phân biệt hoa/thường)."""
    for i, col in enumerate(flat_columns):
        if all(kw in col for kw in keywords):
            return i
    return None


def _find_col_any(flat_columns: list[str], *keyword_groups: tuple[str, ...]) -> int | None:
    """Thử lần lượt từng nhóm keyword (mỗi nhóm là 1 tuple AND-keywords), trả
    về index đầu tiên khớp. Dùng khi vnstock có thể đặt tên cột theo tiếng
    Việt HOẶC tiếng Anh tuỳ version/lang."""
    for group in keyword_groups:
        idx = _find_col(flat_columns, *group)
        if idx is not None:
            return idx
    return None


def _percentile_rank(current: float, history: list[float]) -> float:
    return feat._percentile_rank(current, history)


def _fetch_valuation_sync(symbol: str) -> Valuation | None:
    """Định giá qua REST VCI thẳng (stock.vci_direct) - KHÔNG qua vnstock.

    Nền tảng (xem docstring stock/vci_direct.py): endpoint GraphQL mà
    vnstock 3.5.1 dùng đã bị VCI tắt (KeyError: 'data' cho mọi mã), còn
    vnstock 4.0.7 thì Finance.ratio() công khai bị cắt còn 4 kỳ bằng
    .head(4) theo thứ tự CŨ->MỚI - trả 2018-Q1..Q4 thay vì các quý gần
    nhất. Gọi thẳng statistics-financial raw lấy đủ 41+ quý, tự parse.
    """
    try:
        rows = vci_direct.fetch_statistical_ratios(symbol)
    except Exception:
        logger.warning("vci_direct: statistics-financial lỗi cho %s", symbol, exc_info=True)
        return None
    if not rows:
        return None

    row = rows[0]

    def _val(*keys: str) -> float | None:
        for k in keys:
            if k in row:
                return _to_float(row[k])
        return None

    pe = _val("pe")
    pb = _val("pb")
    # roe (và dividendYield) từ VCI là phân số 0-1 -> nhân 100 cho khớp quy
    # ước % mà build_fundamentals_prompt_section/percentile lịch sử hiển thị.
    roe = _val("roe")
    if roe is not None:
        roe = roe * 100
    # dividendYield của VCI đang trả 0.0 cả với các mã TRẢ CỔ TỨC (VCB, HPG -
    # xác minh 08/09/2026) => trường này không đáng tin: coi 0 là "không có
    # dữ liệu" thay vì khẳng định mã không trả cổ tức.
    dividend_yield = _val("dividendYield")
    if dividend_yield is not None:
        dividend_yield = dividend_yield * 100
        if dividend_yield <= 0 or dividend_yield > 40:
            # >40%: nhiều khả năng cột lấy được là dividend per share (VND)
            # chứ không phải % - không tin cậy để hiển thị như tỷ suất.
            dividend_yield = None
    debt_equity = _val("debtToEquity", "debtPerEquity")
    current_ratio = _val("currentRatio")
    eps = _fetch_eps_sync(symbol, pe)

    # Percentile P/E so với chính nó trong lịch sử: tối đa _PE_HISTORY_QUARTERS
    # quý gần nhất có P/E dương (quý lỗ có pe âm/null bị loại khỏi history).
    pe_percentile = None
    pe_quarters = 0
    if pe is not None:
        history = []
        for r in rows[:_PE_HISTORY_QUARTERS]:
            f = _to_float(r.get("pe"))
            if f is not None and f > 0:
                history.append(f)
        pe_quarters = len(history)
        if pe_quarters >= 4:  # dưới 1 năm dữ liệu thì percentile không có nhiều ý nghĩa
            pe_percentile = _percentile_rank(pe, history)

    return Valuation(
        pe=pe, pb=pb, eps=eps, roe=roe, dividend_yield=dividend_yield,
        debt_equity=debt_equity, current_ratio=current_ratio,
        pe_percentile=pe_percentile, pe_history_quarters=pe_quarters,
    )


def _fetch_eps_sync(symbol: str, pe: float | None) -> float | None:
    """EPS TTM (VND/cp) suy ra từ giá hiện tại / P/E của chính VCI.

    Vì sao không dùng dữ liệu có sẵn:
    - statistics-financial raw KHÔNG có trường eps.
    - isa23 (EPS cơ bản theo KQKD) sai lệch nghiêm trọng với một số mã: CII
      báo 12 (TTM thật ~170), CTD báo 721 (TTM thật ~5200), trong khi VCB/HPG
      thì khớp - đã đối chiếu 08/09/2026, không đáng tin.
    - Tự tính TTM từ isa20 (LN sau thuế từng quý) cũng không được: các hàng
      quý của VCI không thống nhất rời rạc/lũy kế (CII rời rạc, VCB có quý
      đột biến gấp run-rate ~1.6 lần) -> tổng 4 quý không khớp P/E mà chính
      VCI báo.
    EPS = currentPrice / pe là công thức định nghĩa P/E đảo lại, bảo đảm
    nội thống nhất: pe * eps == giá tại thời điểm VCI tính pe.
    """
    if not pe or pe <= 0:
        return None
    try:
        details = vci_direct.fetch_details(symbol)
    except Exception:
        return None
    price = _to_float(details.get("currentPrice"))
    if not price or price <= 0:
        return None
    return round(price / pe, 1)


def _fetch_growth_sync(symbol: str) -> GrowthTrend | None:
    """Tăng trưởng DT/LN theo quý từ KQKD VCI (qua stock.vci_direct).

    Mã cột ISA cố định theo chuẩn VCI (xem docstring stock/vci_direct.py):
    isa3 = doanh thu thuần, isa20 = lãi/lỗ thuần sau thuế. Các quý được
    vci_direct sort MỚI->CŨ nên vals[0] luôn là quý gần nhất.
    """
    try:
        income = vci_direct.fetch_income_statement(symbol)
    except Exception:
        logger.warning("vci_direct: income_statement lỗi cho %s", symbol, exc_info=True)
        return None
    # QoQ cần 2 quý, YoY cần 5 quý (quý [0] so với quý cùng vị trí năm trước
    # ở [4]) - ít hơn thì growth không đủ ý nghĩa, trả None như cũ.
    if len(income) < 2:
        return None

    def _vals(code: str) -> list[float | None]:
        return [_to_float(r.get(code)) for r in income]

    def _growth(vals: list[float | None]) -> tuple[float | None, float | None]:
        qoq = yoy = None
        if len(vals) >= 2 and vals[0] is not None and vals[1]:
            qoq = round((vals[0] - vals[1]) / abs(vals[1]) * 100, 1)
        if len(vals) >= 5 and vals[0] is not None and vals[4]:
            yoy = round((vals[0] - vals[4]) / abs(vals[4]) * 100, 1)
        return qoq, yoy

    rev_qoq, rev_yoy = _growth(_vals("isa3"))
    profit_qoq, profit_yoy = _growth(_vals("isa20"))
    if rev_qoq is None and rev_yoy is None and profit_qoq is None and profit_yoy is None:
        return None

    return GrowthTrend(
        revenue_qoq_pct=rev_qoq, revenue_yoy_pct=rev_yoy,
        profit_qoq_pct=profit_qoq, profit_yoy_pct=profit_yoy,
        quarters_available=len(income),
    )


def _fetch_foreign_sync(symbol: str) -> ForeignFlowReal | None:
    """Dùng thẳng vnstock.explorer.vci.Trading thay vì facade Vnstock().stock().

    Trading không eager-fetch gì ở __init__ (khác Company/Finance) - đây là
    chỗ tiết kiệm nhiều nhất trong 5 hàm fetch: dùng facade sẽ tốn thêm 2
    lần fetch Company + khởi tạo Finance thừa dù chỉ cần price_board()."""
    try:
        ensure_vnstock_api_key()
        from vnstock.explorer.vci import Trading
    except ImportError:
        return None

    try:
        trading = Trading(symbol=symbol, show_log=False)
        board = trading.price_board(symbols_list=[symbol])
    except Exception:
        logger.warning("vnstock: price_board lỗi cho %s", symbol, exc_info=True)
        return None
    if board is None or board.empty:
        return None

    flat_cols = _flatten_columns(board.columns)
    row = board.iloc[0]

    def _val(*keywords: str) -> float | None:
        idx = _find_col(flat_cols, *keywords)
        return _to_float(row.iloc[idx]) if idx is not None else None

    buy_a = _val("foreign", "buy", "vol")
    buy_b = _val("foreign", "buy")
    buy = buy_a if buy_a is not None else buy_b
    sell_a = _val("foreign", "sell", "vol")
    sell_b = _val("foreign", "sell")
    sell = sell_a if sell_a is not None else sell_b
    # "room" không kèm "pct/ratio/%/tỷ lệ" rất dễ khớp nhầm cột room CÒN LẠI
    # THEO SỐ CỔ PHIẾU (raw, có thể hàng chục/trăm triệu) thay vì tỷ lệ % -
    # đã quan sát thấy giá trị garbage kiểu 1.37e+08% trong production do lỗi
    # này. Ưu tiên cột rõ ràng là %, chỉ fallback về "room" trần khi không có
    # cột nào khớp, và luôn chặn giá trị ngoài khoảng 0-100 (không phải %).
    room_idx = _find_col_any(
        flat_cols,
        ("room", "pct"), ("room", "ratio"), ("room", "%"), ("room", "tỷ lệ"),
    )
    room = _to_float(row.iloc[room_idx]) if room_idx is not None else None
    if room is None:
        room_fallback = _val("room")
        room = room_fallback if room_fallback is not None and 0 <= room_fallback <= 100 else None
    elif not (0 <= room <= 100):
        room = None
    net = None
    if buy is not None and sell is not None:
        net = round(buy - sell, 2)

    if buy is None and sell is None and room is None:
        return None
    return ForeignFlowReal(foreign_buy_vol=buy, foreign_sell_vol=sell, foreign_net_vol=net, foreign_room_pct=room)


def _fetch_events_sync(symbol: str, limit: int = 3) -> list[UpcomingEvent] | None:
    """Lịch KQKD/ĐHCĐ/chia cổ tức/phát hành thêm - thứ hay gây bất ngờ giá.

    ĐÃ XÁC MINH (kiểm tra mã nguồn vnstock đã cài, không cần gọi mạng thật):
    - source="TCBS" (dùng ở bản trước) LUÔN LỖI vì TCBS không còn nằm trong
      StockComponents.SUPPORTED_SOURCES (chỉ còn KBS/VCI/MSN/FMP) - mọi lệnh
      gọi trước đây rơi thẳng vào except Exception -> None, không phải do
      thiếu mạng lúc viết code như ghi chú cũ, mà do source đã bị gỡ khỏi
      vnstock. Đổi sang source="VCI": lớp Company của VCI có sẵn method
      events() thật (vnstock/explorer/vci/company.py), khớp đúng 1 trong các
      tên hàm candidate bên dưới.
    - Dùng thẳng vnstock.explorer.vci.Company thay vì facade Vnstock().stock()
      (facade eager-init thừa cả Finance/Quote/Trading dù ở đây chỉ cần
      Company).
    """
    try:
        ensure_vnstock_api_key()
        from vnstock.explorer.vci import Company
    except ImportError:
        return None

    try:
        company = Company(symbol=symbol, show_log=False)
    except Exception:
        return None

    df = None
    for name in ("events", "event"):
        fn = getattr(company, name, None)
        if callable(fn):
            try:
                result = fn()
                if result is not None and not result.empty:
                    df = result
                    break
            except Exception:
                continue

    if df is None or df.empty:
        logger.info(
            "vnstock: không lấy được lịch sự kiện cho %s (API company.events() "
            "có thể chưa tồn tại/đã đổi tên - xem ghi chú đầu file stock_fundamentals.py)",
            symbol,
        )
        return None

    flat_cols = _flatten_columns(df.columns)
    # Ưu tiên event_title_* (vd "CII - Thực hiện quyền mua trái phiếu...")
    # trước event_name_* (chỉ là tên loại chung: "Sự kiện khác", "Đại hội...")
    title_idx = _find_col_any(flat_cols, ("event", "title"), ("event", "name"), ("event",), ("title",), ("nội dung",))
    date_idx = _find_col_any(flat_cols, ("date",), ("ngày",))
    if title_idx is None:
        return None

    out: list[UpcomingEvent] = []
    for _, r in df.head(limit).iterrows():
        title_val = r.iloc[title_idx]
        if title_val is None:
            continue
        title = str(title_val).strip()
        if not title or title.lower() == "nan":
            continue
        date_val = str(r.iloc[date_idx]).strip() if date_idx is not None else None
        out.append(UpcomingEvent(title=title, date=date_val))
    return out or None


def _fetch_company_news_sync(symbol: str, limit: int = 5) -> list[NewsHeadline] | None:
    """Tin công ty CHÍNH CHỦ từ VCI (company.news()) - đã được VCI gắn đúng
    organ_code của {symbol}, nên KHÔNG cần kiểm tra lại mã có xuất hiện
    trong tiêu đề như tin cào từ Google News (rfmt.title_mentions_symbol) -
    luôn đánh dấu confirmed=True. Đây là nguồn BỔ SUNG cho
    providers.fetch_news(), không thay thế (tin VCI có thể ít/chậm hơn báo
    chí, nhưng độ chính xác gắn đúng mã cao hơn). Dùng thẳng
    vnstock.explorer.vci.Company thay vì facade Vnstock().stock() (facade
    eager-init thừa cả Finance/Quote/Trading dù ở đây chỉ cần Company)."""
    try:
        ensure_vnstock_api_key()
        from vnstock.explorer.vci import Company
    except ImportError:
        return None

    try:
        company = Company(symbol=symbol, show_log=False)
    except Exception:
        return None

    news_fn = getattr(company, "news", None)
    if not callable(news_fn):
        return None
    try:
        df = news_fn()
    except Exception:
        logger.info("vnstock: không lấy được tin công ty (company.news()) cho %s", symbol)
        return None
    if df is None or df.empty:
        return None

    flat_cols = _flatten_columns(df.columns)
    title_idx = _find_col_any(flat_cols, ("news", "title"), ("title",), ("tiêu đề",))
    date_idx = _find_col_any(flat_cols, ("public", "date"), ("date",), ("ngày",))
    if title_idx is None:
        return None

    out: list[NewsHeadline] = []
    for _, r in df.head(limit).iterrows():
        title_val = r.iloc[title_idx]
        if title_val is None:
            continue
        title = str(title_val).strip()
        if not title or title.lower() == "nan":
            continue
        date_val = str(r.iloc[date_idx]).strip() if date_idx is not None else ""
        out.append(NewsHeadline(
            title=title, source="VCI", pub_date=date_val, url="",
            sentiment=sentiment_score(title), confirmed=True,
        ))
    return out or None


async def fetch_company_news(symbol: str, limit: int = 5) -> list[NewsHeadline]:
    """Wrapper async cho _fetch_company_news_sync, qua semaphore VCI dùng
    chung + timeout, không bao giờ raise ra ngoài (giống pattern _safe() ở
    fetch_fundamentals)."""
    try:
        async with get_vnstock_semaphore():
            result = await asyncio.wait_for(
                asyncio.to_thread(_fetch_company_news_sync, symbol, limit),
                timeout=_FETCH_TIMEOUT_SEC,
            )
        return result or []
    except Exception:
        logger.warning("fetch_company_news lỗi cho %s", symbol, exc_info=True)
        return []


@dataclass
class SectorBenchmark:
    metric: str
    average: float | None
    sample: int
    label: str | None
async def fetch_sector_benchmark(symbol, sample_size=8):
    from stock import sector
    profile=fundamental_profiles.get_profile(symbol); keys=sector.get_symbol_sectors(symbol)
    if not keys: return SectorBenchmark(profile.benchmark_metric,None,0,None)
    meta=sector.SECTOR_MAP[keys[0]]; peers=[p for p in meta["symbols"] if p!=symbol.upper()][:sample_size]
    async def load(peer):
        try:
            async with get_vnstock_semaphore():
                v=await asyncio.wait_for(asyncio.to_thread(_fetch_valuation_sync,peer),timeout=_FETCH_TIMEOUT_SEC)
            value=getattr(v,profile.benchmark_metric,None) if v else None
            return value if value is not None and 0<value<500 else None
        except Exception: return None
    values=[v for v in await asyncio.gather(*(load(p) for p in peers)) if v is not None]
    return SectorBenchmark(profile.benchmark_metric,round(sum(values)/len(values),2) if values else None,len(values),meta["label"])


@dataclass
class FundamentalsBundle:
    valuation: Valuation | None = None
    foreign: ForeignFlowReal | None = None
    growth: GrowthTrend | None = None
    events: list[UpcomingEvent] | None = None
    sector_pe_avg: float | None = None
    sector_pe_sample: int = 0
    sector_pe_label: str | None = None
    sector_profile: fundamental_profiles.FundamentalProfile | None = None
    sector_benchmark: SectorBenchmark | None = None

# Cache bundle fundamentals theo symbol. BCTC/định giá đổi theo quý, khối ngoại
# đổi theo phiên nên 30 phút là an toàn, trong khi mỗi lượt phân tích bắn
# ~12-13 request VCI (4 cho mã + 8 peer ngành) - với API free 20 req/phút, 2
# lượt phân tích liên tiếp không cache là đủ bị rate-limit và toàn bundle rỗng.
_FUNDAMENTALS_CACHE_TTL = 30 * 60
_fundamentals_cache: dict[str, tuple[float, "FundamentalsBundle"]] = {}


def _evict_fundamentals_cache(now: float) -> None:
    expired = [k for k, (ts, _) in _fundamentals_cache.items() if now - ts >= _FUNDAMENTALS_CACHE_TTL]
    for k in expired:
        _fundamentals_cache.pop(k, None)


async def fetch_fundamentals(symbol: str) -> FundamentalsBundle:
    """Lấy song song toàn bộ dữ liệu cơ bản. Không bao giờ raise ra ngoài.

    Có cache 30 phút theo symbol (xem _FUNDAMENTALS_CACHE_TTL) - cùng 1 mã được
    phân tích lại trong nửa giờ thì tái dùng bundle, không bắn lại request VCI.
    """
    sym = symbol.strip().upper()
    cached = _fundamentals_cache.get(sym)
    if cached:
        ts, bundle = cached
        if time.monotonic() - ts < _FUNDAMENTALS_CACHE_TTL:
            return bundle

    async def _safe(fn, *args):
        try:
            async with get_vnstock_semaphore():
                return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=_FETCH_TIMEOUT_SEC)
        except Exception:
            logger.warning("stock_fundamentals lỗi cho %s (%s)", symbol, fn.__name__, exc_info=True)
            return None

    valuation, foreign, growth, events, benchmark = await asyncio.gather(
        _safe(_fetch_valuation_sync, symbol),
        _safe(_fetch_foreign_sync, symbol),
        _safe(_fetch_growth_sync, symbol),
        _safe(_fetch_events_sync, symbol),
        fetch_sector_benchmark(symbol),
    )
    profile=fundamental_profiles.get_profile(symbol)
    sector_pe_avg=benchmark.average if benchmark and benchmark.metric=='pe' else None
    sector_pe_sample=benchmark.sample if sector_pe_avg is not None else 0
    sector_pe_label=benchmark.label if sector_pe_avg is not None else None
    bundle = FundamentalsBundle(
        valuation=valuation, foreign=foreign,
        growth=growth, events=events, sector_pe_avg=sector_pe_avg,
        sector_pe_sample=sector_pe_sample, sector_pe_label=sector_pe_label, sector_profile=profile, sector_benchmark=benchmark,
    )
    now = time.monotonic()
    _evict_fundamentals_cache(now)
    _fundamentals_cache[sym] = (now, bundle)
    return bundle


def _fmt(v: float | None, suffix: str = "") -> str:
    return f"{v:g}{suffix}" if v is not None else "chưa có dữ liệu"


def build_fundamentals_prompt_section(
    valuation: Valuation | None,
    foreign: ForeignFlowReal | None,
    symbol: str,
    growth: GrowthTrend | None = None,
    events: list[UpcomingEvent] | None = None,
    sector_pe_avg: float | None = None,
    sector_pe_sample: int = 0,
    sector_pe_label: str | None = None,
    sector_profile: fundamental_profiles.FundamentalProfile | None = None,
    sector_benchmark: SectorBenchmark | None = None,
) -> str:
    if not any([valuation, foreign, growth, events, sector_pe_avg,
                sector_benchmark is not None and sector_benchmark.average is not None]):
        return ""
    profile=sector_profile or fundamental_profiles.get_profile(symbol)
    lines=[f"[ĐỊNH GIÁ & DÒNG TIỀN THẬT — {symbol}, nguồn công khai VCI/TCBS qua vnstock]"]
    lines.append(f"Chuẩn hóa ngành {profile.label}: ưu tiên {', '.join(profile.priority_metrics)}. {profile.note}".strip())
    if valuation:
        lines.append(
            f"P/E: {_fmt(valuation.pe)} | P/B: {_fmt(valuation.pb)} | "
            f"EPS: {_fmt(valuation.eps)} VND | ROE: {_fmt(valuation.roe, '%')} | "
            f"Tỷ suất cổ tức: {_fmt(valuation.dividend_yield, '%')}"
        )
        if (valuation.debt_equity is not None or valuation.current_ratio is not None) and not ({'debt_equity','current_ratio'} <= set(profile.suppress_metrics)):
            lines.append(
                f"Rủi ro tài chính — Nợ/Vốn chủ (D/E): {_fmt(valuation.debt_equity)} | "
                f"Thanh khoản hiện hành (current ratio): {_fmt(valuation.current_ratio)}"
            )
        if valuation.pe_percentile is not None:
            lines.append(
                f"P/E hiện tại đang ở percentile {valuation.pe_percentile}% so với chính nó "
                f"trong {valuation.pe_history_quarters} quý gần nhất (percentile càng cao = P/E "
                f"đang càng đắt so với lịch sử của chính mã này, KHÔNG phải so ngành)."
            )
        if sector_benchmark and sector_benchmark.average is not None:
            current=getattr(valuation,sector_benchmark.metric,None)
            if current is not None:
                diff=round((current-sector_benchmark.average)/sector_benchmark.average*100,1) if sector_benchmark.average else None
                metric="P/B" if sector_benchmark.metric=="pb" else "P/E"; relation=f"; mã {'CAO' if diff>0 else 'THẤP'} hơn {abs(diff)}%" if diff is not None else ""
                lines.append(f"So ngành {sector_benchmark.label or ''}: {metric} trung bình {sector_benchmark.average} từ {sector_benchmark.sample} mã hợp lệ{relation}.")
        elif valuation.pe is not None and sector_pe_avg is not None:
            diff_pct = round((valuation.pe - sector_pe_avg) / sector_pe_avg * 100, 1) if sector_pe_avg else None
            cheap_or_expensive = ""
            if diff_pct is not None:
                cheap_or_expensive = f", tức {'CAO' if diff_pct > 0 else 'THẤP'} hơn {abs(diff_pct)}%"
            lines.append(
                f"So ngành {sector_pe_label or ''}: P/E trung bình {sector_pe_avg} "
                f"(ước lượng nhanh từ {sector_pe_sample} mã tiêu biểu cùng ngành, không phải toàn "
                f"ngành){cheap_or_expensive}."
            )
    if growth and (growth.revenue_qoq_pct is not None or growth.profit_qoq_pct is not None):
        def _g(v):
            return _fmt(v, "%") if v is None else (f"+{v}%" if v >= 0 else f"{v}%")
        lines.append(
            f"Tăng trưởng theo quý ({growth.quarters_available} quý dữ liệu) — "
            f"Doanh thu QoQ: {_g(growth.revenue_qoq_pct)}, YoY: {_g(growth.revenue_yoy_pct)} | "
            f"LN sau thuế QoQ: {_g(growth.profit_qoq_pct)}, YoY: {_g(growth.profit_yoy_pct)}"
        )
    elif valuation is None and sector_benchmark and sector_benchmark.average is not None:
        # Benchmark ngành có số nhưng valuation của chính mã rỗng (trước đây
        # cả section bị skip dù trung bình ngành dùng được - case phổ biến
        # nhất là ngành ngân hàng so P/B). Vẫn in benchmark, ghi rõ là chưa
        # so được trực tiếp.
        metric = "P/B" if sector_benchmark.metric == "pb" else "P/E"
        lines.append(
            f"So ngành {sector_benchmark.label or ''}: {metric} trung bình "
            f"{sector_benchmark.average} từ {sector_benchmark.sample} mã hợp lệ "
            f"(chưa lấy được định giá của chính mã này để so trực tiếp)."
        )
    if foreign:
        lines.append(
            f"Khối ngoại phiên gần nhất — Mua: {_fmt(foreign.foreign_buy_vol)} | "
            f"Bán: {_fmt(foreign.foreign_sell_vol)} | "
            f"Ròng: {_fmt(foreign.foreign_net_vol)} | "
            f"Room ngoại còn lại: {_fmt(foreign.foreign_room_pct, '%')}"
        )
    if events:
        lines.append("Sự kiện sắp tới: " + "; ".join(
            f"{e.title}" + (f" ({e.date})" if e.date else "") for e in events
        ))
    lines.append(
        "(Lưu ý: dữ liệu lấy qua thư viện bên thứ 3 không chính thức, có thể thiếu/trễ - "
        "nếu số liệu quan trọng cho quyết định lớn, đối chiếu thêm trên app công ty chứng khoán.)"
    )
    return "\n".join(lines)
