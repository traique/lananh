"""Định giá cơ bản (P/E, P/B, EPS, ROE, D/E...) + dòng tiền khối ngoại +
tăng trưởng theo quý + lịch sự kiện, dựa trên `vnstock`.

Nguồn: thư viện `vnstock` (mã nguồn mở, MIỄN PHÍ, KHÔNG cần đăng ký/API key) -
gom dữ liệu công khai từ VCI/TCBS.

⚠️ QUAN TRỌNG - đọc trước khi tin tưởng module này:
- vnstock là công cụ của bên thứ 3, dựa trên API công khai không tài liệu hoá
  chính thức của VCI/TCBS -> KHÔNG có SLA, có thể lỗi hoặc đổi cấu trúc dữ
  liệu bất kỳ lúc nào mà không báo trước.
- Toàn bộ hàm ở đây match tên cột theo TỪ KHOÁ (substring) thay vì tên cột
  cứng, để bớt nhạy cảm với thay đổi nhỏ giữa các phiên bản vnstock - nhưng
  KHÔNG đảm bảo luôn đúng 100%. Nếu không tìm thấy cột phù hợp, trả về None
  cho trường đó thay vì đoán liều.
- Giấy phép vnstock: dành cho cá nhân/phi thương mại - phù hợp bot 1 user
  này, KHÔNG dùng cho mục đích thương mại nếu chưa xin phép tác giả.
- Gọi vnstock là thao tác ĐỒNG BỘ (blocking, dùng requests) -> luôn chạy qua
  asyncio.to_thread() để không chặn event loop, và luôn có timeout.

🧪 GHI CHÚ ĐỘ TIN CẬY (đọc trước khi deploy):
- `_fetch_valuation_sync` (P/E, P/B, EPS, ROE, D/E, current ratio, percentile
  P/E lịch sử) và `_fetch_growth_sync` (tăng trưởng DT/LN quý) dùng
  `stock.finance.ratio()` / `stock.finance.income_statement()` - đây là 2 hàm
  đã được dùng ổn định trong bản gốc, rủi ro thấp, chỉ thêm cột/nhiều dòng
  hơn so với trước. Lưu ý: `Vnstock().stock(...)` (facade dùng chung cho cả
  2 hàm này lẫn 2 hàm bên dưới) đã bị vnstock đánh dấu DEPRECATED kể từ
  31/08/2025 (tự in cảnh báo mỗi lần gọi, khuyến nghị chuyển sang
  `vnstock.api.*`) - vẫn chạy được ở bản đang pin, nhưng có thể bị gỡ hẳn ở
  bản vnstock sau này.
- `_fetch_events_sync` (lịch sự kiện KQKD/ĐHCĐ/cổ tức): ĐÃ XÁC MINH bằng cách
  đọc mã nguồn vnstock đã cài (môi trường viết/sửa code này không có mạng ra
  ngoài tới API thật của VCI/TCBS, nên không gọi thử end-to-end được, nhưng
  việc đọc source đã đủ để xác nhận nguyên nhân): source="TCBS" (bản cũ) LUÔN
  lỗi vì TCBS đã bị gỡ khỏi StockComponents.SUPPORTED_SOURCES - đã sửa sang
  source="VCI" (có method events() thật). Vẫn chưa chắc chắn 100% vì chưa gọi
  mạng thật để xem tên cột (title/date) thực tế trả về có khớp
  `_find_col_any` bên dưới không - nếu vẫn trả "chưa có dữ liệu" sau khi
  deploy, hãy chạy thử trên máy có mạng:
      from vnstock import Vnstock
      df = Vnstock().stock(symbol="FPT", source="VCI").company.events()
      print(df.columns.tolist())
  rồi bổ sung tên cột thực tế vào `_find_col_any(...)` trong
  `_fetch_events_sync`.
- Khối ngoại NHIỀU phiên (lịch sử mua/bán ròng theo chuỗi ngày) ĐÃ BỊ BỎ
  KHỎI module này: cả facade cũ (`vnstock/explorer/vci/trading.py`, chỉ có
  đúng 1 method công khai là price_board()) lẫn API mới
  (`vnstock.api.trading.foreign_trade()`) đều không có provider nào implement
  thật (chỉ là stub `pass`) - đã xác minh lại trên vnstock 3.5.1, không phải
  giới hạn riêng của bản đang pin. Chỉ còn `_fetch_foreign_sync` (khối ngoại
  PHIÊN GẦN NHẤT, qua price_board() - có hoạt động thật) là nguồn khối ngoại
  duy nhất trong bot này.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from stock import features as feat
from stock import fundamental_profiles
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


_RATIO_COL_DENYLIST = ("period", "type", "length")


def _find_ratio_col(flat_columns: list[str], primary: str, fallback: str) -> int | None:
    """Ưu tiên match tên cột đầy đủ (vd "p/e"); fallback substring ngắn (vd
    "pe") chỉ được chấp nhận khi tên cột không chứa từ trong denylist -
    tránh khớp nhầm các cột như "period"/"period_length"/"type" chứa "pe"
    như một substring tình cờ."""
    idx = _find_col(flat_columns, primary)
    if idx is not None:
        return idx
    for i, col in enumerate(flat_columns):
        if fallback in col and not any(bad in col for bad in _RATIO_COL_DENYLIST):
            return i
    return None


def _percentile_rank(current: float, history: list[float]) -> float:
    return feat._percentile_rank(current, history)


def _fetch_valuation_sync(symbol: str) -> Valuation | None:
    """Dùng thẳng vnstock.explorer.vci.Finance thay vì facade Vnstock().stock().

    ĐÃ XÁC MINH bằng traceback thật từ production (ReadTimeoutError tới
    trading.vietcap.com.vn): facade Vnstock().stock(symbol, source="VCI")
    khi khởi tạo sẽ eager-fetch CẢ Company LẪN Finance LẪN Quote/Trading
    (StockComponents._initialize_components), dù ở đây chỉ cần
    finance.ratio(). Tệ hơn, Finance.__init__ tự nó CŨNG gọi thêm 1 lần
    Company(...)._fetch_data() riêng (để lấy mã ngành ICB4) - tức dùng
    facade tốn tới 2 lần fetch Company không cần thiết trước khi chạm được
    tới dữ liệu ratio() thật sự muốn lấy. Gọi Finance(symbol) trực tiếp vẫn
    còn 1 lần fetch Company (không tránh được, nằm sâu trong thư viện), nhưng
    bớt được lần thứ 2 - giảm ~50% số request/khả năng timeout cho hàm này.
    """
    try:
        ensure_vnstock_api_key()
        from vnstock.explorer.vci import Finance
    except ImportError:
        logger.warning("Chưa cài thư viện vnstock (pip install vnstock).")
        return None

    try:
        finance = Finance(symbol=symbol, show_log=False)
    except Exception:
        logger.warning("vnstock: không khởi tạo được Finance cho %s", symbol, exc_info=True)
        return None

    df = None
    for kwargs in ({"period": "quarter"}, {}):
        try:
            df = finance.ratio(**kwargs)
            if df is not None and not df.empty:
                break
        except Exception:
            continue
    if df is None or df.empty:
        return None

    flat_cols = _flatten_columns(df.columns)

    # Đảm bảo quý gần nhất luôn ở iloc[0]: không giả định df đã sắp xếp sẵn,
    # sort tường minh theo năm (và quý nếu có) giảm dần.
    year_idx = _find_col(flat_cols, "year")
    quarter_idx = _find_col_any(flat_cols, ("quarter",), ("length",))
    if year_idx is not None:
        sort_cols = [df.columns[year_idx]]
        if quarter_idx is not None:
            sort_cols.append(df.columns[quarter_idx])
        df = df.sort_values(by=sort_cols, ascending=False).reset_index(drop=True)

    row = df.iloc[0]

    def _val(*keywords: str) -> float | None:
        idx = _find_col(flat_cols, *keywords)
        return _to_float(row.iloc[idx]) if idx is not None else None

    # "pe"/"pb" có thể trùng khớp nhầm vào các cột khác chứa chữ "pe"/"pb" (vd
    # "period", "period_length", "type") - dùng _find_ratio_col với denylist
    # thay vì substring "or" đơn thuần (vốn cũng nuốt luôn giá trị 0.0 hợp lệ).
    pe_idx = _find_ratio_col(flat_cols, "p/e", "pe")
    pe = _to_float(row.iloc[pe_idx]) if pe_idx is not None else None
    pb_idx = _find_ratio_col(flat_cols, "p/b", "pb")
    pb = _to_float(row.iloc[pb_idx]) if pb_idx is not None else None
    eps = _val("eps")
    roe = _val("roe")
    # Ưu tiên cột vừa chứa "dividend" vừa chứa "yield"/"suất" (đúng là tỷ suất
    # %) trước khi fallback về substring "dividend" đơn thuần (có thể là DPS
    # theo VND tuỳ version vnstock - xem sanity check bên dưới, C2).
    dividend_yield_a = _val("dividend", "yield")
    dividend_yield_b = _val("dividend", "suất")
    dividend_yield = dividend_yield_a if dividend_yield_a is not None else dividend_yield_b
    if dividend_yield is None:
        dividend_yield = _val("dividend")
    if dividend_yield is not None and dividend_yield > 40:
        # không tỷ suất cổ tức thật nào ở VN vượt mức này -> nhiều khả năng
        # cột lấy được là dividend per share (VND) chứ không phải %, không
        # tin cậy để hiển thị như tỷ suất.
        dividend_yield = None
    # D/E và current ratio: tên cột có thể tiếng Việt ("nợ"/"vốn chủ", "thanh
    # toán hiện hành") hoặc tiếng Anh ("debt"/"equity", "current ratio") tuỳ
    # version/lang của vnstock - thử cả 2.
    debt_equity_idx = _find_col_any(
        flat_cols,
        ("nợ", "vốn chủ"),
        ("debt", "equity"),
        ("nợ/vcsh",),
    )
    debt_equity = _to_float(row.iloc[debt_equity_idx]) if debt_equity_idx is not None else None
    current_ratio_idx = _find_col_any(
        flat_cols,
        ("thanh toán", "hiện"),
        ("current", "ratio"),
    )
    current_ratio = _to_float(row.iloc[current_ratio_idx]) if current_ratio_idx is not None else None

    # Percentile P/E so với chính nó trong lịch sử: lấy toàn bộ cột P/E qua
    # nhiều quý (giả định df sắp xếp mới nhất -> cũ dần, giống hàng iloc[0]
    # ở trên đã lấy làm "hiện tại").
    pe_percentile = None
    pe_quarters = 0
    if pe_idx is not None and pe is not None:
        history = []
        for v in df.iloc[:_PE_HISTORY_QUARTERS, pe_idx]:
            f = _to_float(v)
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


def _fetch_growth_sync(symbol: str) -> GrowthTrend | None:
    try:
        ensure_vnstock_api_key()
        from vnstock.explorer.vci import Finance
    except ImportError:
        return None

    try:
        finance = Finance(symbol=symbol, show_log=False)
        df = finance.income_statement(period="quarter")
    except Exception:
        logger.warning("vnstock: income_statement lỗi cho %s", symbol, exc_info=True)
        return None
    if df is None or df.empty or len(df) < 2:
        return None

    flat_cols = _flatten_columns(df.columns)
    rev_idx = _find_col_any(flat_cols, ("doanh thu",), ("revenue",), ("net sale",))
    profit_idx = _find_col_any(
        flat_cols,
        ("lợi nhuận sau thuế",),
        ("lợi nhuận", "cổ đông"),
        ("net profit",),
        ("profit", "after"),
    )
    if rev_idx is None and profit_idx is None:
        return None

    def _growth(idx: int | None) -> tuple[float | None, float | None]:
        if idx is None:
            return None, None
        vals = [_to_float(v) for v in df.iloc[:, idx]]
        qoq = yoy = None
        # vals[0] = quý gần nhất (giả định df mới nhất -> cũ dần, giống ratio()).
        if len(vals) >= 2 and vals[0] is not None and vals[1]:
            qoq = round((vals[0] - vals[1]) / abs(vals[1]) * 100, 1)
        if len(vals) >= 5 and vals[0] is not None and vals[4]:
            yoy = round((vals[0] - vals[4]) / abs(vals[4]) * 100, 1)
        return qoq, yoy

    rev_qoq, rev_yoy = _growth(rev_idx)
    profit_qoq, profit_yoy = _growth(profit_idx)
    if rev_qoq is None and rev_yoy is None and profit_qoq is None and profit_yoy is None:
        return None

    return GrowthTrend(
        revenue_qoq_pct=rev_qoq, revenue_yoy_pct=rev_yoy,
        profit_qoq_pct=profit_qoq, profit_yoy_pct=profit_yoy,
        quarters_available=len(df),
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
    title_idx = _find_col_any(flat_cols, ("event", "name"), ("event",), ("title",), ("nội dung",))
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


async def fetch_sector_pe_average(symbol: str, sample_size: int = 4) -> tuple[float | None, int, str | None]:
    """So P/E hiện tại với trung bình MỘT MẪU NHỎ mã cùng ngành, tái dùng
    nhóm ngành có sẵn trong stock_sector.py - đây KHÔNG phải trung bình toàn
    ngành chính xác qua screener (sẽ cần gọi rất nhiều request, chậm và dễ bị
    giới hạn), chỉ là ước lượng nhanh từ vài mã tiêu biểu. Trả về
    (avg_pe, số mã lấy được dữ liệu, tên ngành) - số mã lấy được thấp thì độ
    tin cậy của trung bình cũng thấp, cần nêu rõ khi hiển thị.
    """
    try:
        from stock import sector
    except ImportError:
        return None, 0, None

    sector_keys = sector.get_symbol_sectors(symbol)
    if not sector_keys:
        return None, 0, None
    meta = sector.SECTOR_MAP[sector_keys[0]]
    peers = [s for s in meta["symbols"] if s != symbol.upper()][:sample_size]
    if not peers:
        return None, 0, meta["label"]

    async def _safe_pe(sym: str) -> float | None:
        try:
            async with get_vnstock_semaphore():
                val = await asyncio.wait_for(asyncio.to_thread(_fetch_valuation_sync, sym), timeout=_FETCH_TIMEOUT_SEC)
            return val.pe if val and val.pe and val.pe > 0 else None
        except Exception:
            return None

    results = await asyncio.gather(*[_safe_pe(p) for p in peers])
    valid = [r for r in results if r is not None]
    if not valid:
        return None, 0, meta["label"]
    return round(sum(valid) / len(valid), 1), len(valid), meta["label"]


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

async def fetch_fundamentals(symbol: str) -> FundamentalsBundle:
    """Lấy song song toàn bộ dữ liệu cơ bản. Không bao giờ raise ra ngoài.

    Lưu ý: sector_pe_avg gọi thêm vài request cho mã cùng ngành (xem
    fetch_sector_pe_average) -> tổng thời gian chờ tăng thêm so với bản gốc,
    nhưng vẫn chạy song song với các phần khác nên không cộng dồn tuần tự.
    """
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
    return FundamentalsBundle(
        valuation=valuation, foreign=foreign,
        growth=growth, events=events, sector_pe_avg=sector_pe_avg,
        sector_pe_sample=sector_pe_sample, sector_pe_label=sector_pe_label, sector_profile=profile, sector_benchmark=benchmark,
    )


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
    if not any([valuation, foreign, growth, events, sector_pe_avg]):
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
