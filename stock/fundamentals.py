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
- `_fetch_foreign_history_sync` (khối ngoại NHIỀU phiên) và `_fetch_events_sync`
  (lịch sự kiện KQKD/ĐHCĐ/cổ tức): ĐÃ XÁC MINH bằng cách đọc mã nguồn
  vnstock đã cài (môi trường viết/sửa code này không có mạng ra ngoài tới
  API thật của VCI/TCBS, nên không gọi thử end-to-end được, nhưng việc đọc
  source đã đủ để xác nhận nguyên nhân, xem docstring từng hàm để biết chi
  tiết):
    - `_fetch_events_sync`: source="TCBS" (bản cũ) LUÔN lỗi vì TCBS đã bị gỡ
      khỏi StockComponents.SUPPORTED_SOURCES - đã sửa sang source="VCI" (có
      method events() thật). Vẫn chưa chắc chắn 100% vì chưa gọi mạng thật
      để xem tên cột (title/date) thực tế trả về có khớp `_find_col_any` bên
      dưới không - nếu vẫn trả "chưa có dữ liệu" sau khi deploy, hãy chạy
      thử trên máy có mạng:
          from vnstock import Vnstock
          df = Vnstock().stock(symbol="FPT", source="VCI").company.events()
          print(df.columns.tolist())
      rồi bổ sung tên cột thực tế vào `_find_col_any(...)` trong
      `_fetch_events_sync`.
    - `_fetch_foreign_history_sync`: hiện KHÔNG có endpoint nào hoạt động ở
      cả facade cũ lẫn `vnstock.api.trading.foreign_trade` (chỉ là stub rỗng
      chưa có provider implement) - đây là giới hạn thật của thư viện, không
      phải thiếu sót có thể tự sửa ở tầng bot lúc này.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from stock import features as feat
from stock import fundamental_profiles

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
class ForeignFlowTrend:
    """Khối ngoại nhiều phiên (mặc định 10 phiên) - THỬ NGHIỆM, xem ghi chú đầu file."""
    days: int
    net_total: float | None = None
    buy_days: int | None = None
    sell_days: int | None = None
    streak: int = 0  # dương = số phiên mua ròng liên tục gần nhất, âm = bán ròng liên tục


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
    try:
        from vnstock import Vnstock
    except ImportError:
        logger.warning("Chưa cài thư viện vnstock (pip install vnstock).")
        return None

    try:
        stock = Vnstock().stock(symbol=symbol, source="VCI")
    except Exception:
        logger.warning("vnstock: không khởi tạo được cho %s", symbol, exc_info=True)
        return None

    df = None
    for kwargs in ({"period": "quarter"}, {}):
        try:
            df = stock.finance.ratio(**kwargs)
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
        from vnstock import Vnstock
    except ImportError:
        return None

    try:
        stock = Vnstock().stock(symbol=symbol, source="VCI")
        df = stock.finance.income_statement(period="quarter")
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
    try:
        from vnstock import Vnstock
    except ImportError:
        return None

    try:
        stock = Vnstock().stock(symbol=symbol, source="VCI")
        board = stock.trading.price_board(symbols_list=[symbol])
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
    room_a = _val("foreign", "room")
    room_b = _val("room")
    room = room_a if room_a is not None else room_b
    net = None
    if buy is not None and sell is not None:
        net = round(buy - sell, 2)

    if buy is None and sell is None and room is None:
        return None
    return ForeignFlowReal(foreign_buy_vol=buy, foreign_sell_vol=sell, foreign_net_vol=net, foreign_room_pct=room)


def _fetch_foreign_history_sync(symbol: str, days: int = 10) -> ForeignFlowTrend | None:
    """1 phiên đơn lẻ dễ nhiễu; broker thường nhìn chuỗi 5-10 phiên để xác
    nhận dòng tiền có bền không.

    ĐÃ XÁC MINH (kiểm tra mã nguồn vnstock đã cài, không cần gọi mạng thật):
    hiện KHÔNG có endpoint nào hoạt động cho lịch sử khối ngoại nhiều phiên
    trong bản vnstock đang pin (xem requirements.txt):
    - Lớp Trading cũ của VCI (vnstock/explorer/vci/trading.py) chỉ có đúng 1
      method công khai là price_board() (bảng giá realtime, không có cột
      mua/bán khối ngoại nhiều phiên) - không khớp bất kỳ candidate nào
      dưới đây.
    - Lớp Trading mới (vnstock/api/trading.py) CÓ khai báo method
      foreign_trade(), nhưng đây chỉ là stub rỗng (@dynamic_method, thân hàm
      `pass`) - không có provider nào (kể cả VCI) thực sự implement nó ở
      bản đang cài.
    Giữ nguyên hành vi trả None (không raise, không chặn phần phân tích còn
    lại) - đây là giới hạn thật của thư viện vnstock hiện tại, không phải lỗi
    có thể tự sửa ở tầng bot. Cần theo dõi khi vnstock cập nhật thêm provider
    cho foreign_trade(), hoặc tìm nguồn dữ liệu khác cho tính năng này."""
    try:
        from vnstock import Vnstock
    except ImportError:
        return None

    try:
        stock = Vnstock().stock(symbol=symbol, source="VCI")
    except Exception:
        return None

    df = None
    candidates = []
    trading = getattr(stock, "trading", None)
    if trading is not None:
        for name in ("foreign_trade", "foreign_trading", "foreign_trade_data"):
            fn = getattr(trading, name, None)
            if callable(fn):
                candidates.append(fn)
    for fn in candidates:
        try:
            result = fn()
            if result is not None and not result.empty:
                df = result
                break
        except Exception:
            continue

    if df is None or df.empty:
        logger.info(
            "vnstock: không lấy được lịch sử khối ngoại nhiều phiên cho %s "
            "(API trading.foreign_trade*() có thể chưa tồn tại/đã đổi tên ở "
            "version vnstock đang cài - xem ghi chú đầu file stock_fundamentals.py)",
            symbol,
        )
        return None

    flat_cols = _flatten_columns(df.columns)
    buy_idx = _find_col(flat_cols, "buy")
    sell_idx = _find_col(flat_cols, "sell")
    if buy_idx is None or sell_idx is None:
        return None

    window = df.iloc[:days]
    daily_net: list[float] = []
    for _, r in window.iterrows():
        b = _to_float(r.iloc[buy_idx])
        s = _to_float(r.iloc[sell_idx])
        if b is not None and s is not None:
            daily_net.append(round(b - s, 2))
    if not daily_net:
        return None

    buy_days = sum(1 for v in daily_net if v > 0)
    sell_days = sum(1 for v in daily_net if v < 0)
    # streak: số phiên liên tục cùng chiều tính từ phiên gần nhất (daily_net[0]).
    streak = 0
    for v in daily_net:
        if v == 0:
            break
        direction = 1 if v > 0 else -1
        if streak == 0:
            streak = direction
        elif (direction > 0) == (streak > 0):
            streak += direction
        else:
            break

    return ForeignFlowTrend(
        days=len(daily_net), net_total=round(sum(daily_net), 2),
        buy_days=buy_days, sell_days=sell_days, streak=streak,
    )


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
    """
    try:
        from vnstock import Vnstock
    except ImportError:
        return None

    try:
        stock = Vnstock().stock(symbol=symbol, source="VCI")
    except Exception:
        return None

    company = getattr(stock, "company", None)
    if company is None:
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
            v=await asyncio.wait_for(asyncio.to_thread(_fetch_valuation_sync,peer),timeout=_FETCH_TIMEOUT_SEC); value=getattr(v,profile.benchmark_metric,None) if v else None
            return value if value is not None and 0<value<500 else None
        except Exception: return None
    values=[v for v in await asyncio.gather(*(load(p) for p in peers)) if v is not None]
    return SectorBenchmark(profile.benchmark_metric,round(sum(values)/len(values),2) if values else None,len(values),meta["label"])


@dataclass
class FundamentalsBundle:
    valuation: Valuation | None = None
    foreign: ForeignFlowReal | None = None
    foreign_trend: ForeignFlowTrend | None = None
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
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=_FETCH_TIMEOUT_SEC)
        except Exception:
            logger.warning("stock_fundamentals lỗi cho %s (%s)", symbol, fn.__name__, exc_info=True)
            return None

    valuation, foreign, foreign_trend, growth, events, benchmark = await asyncio.gather(
        _safe(_fetch_valuation_sync, symbol),
        _safe(_fetch_foreign_sync, symbol),
        _safe(_fetch_foreign_history_sync, symbol),
        _safe(_fetch_growth_sync, symbol),
        _safe(_fetch_events_sync, symbol),
        fetch_sector_benchmark(symbol),
    )
    profile=fundamental_profiles.get_profile(symbol)
    sector_pe_avg=benchmark.average if benchmark and benchmark.metric=='pe' else None
    sector_pe_sample=benchmark.sample if sector_pe_avg is not None else 0
    sector_pe_label=benchmark.label if sector_pe_avg is not None else None
    return FundamentalsBundle(
        valuation=valuation, foreign=foreign, foreign_trend=foreign_trend,
        growth=growth, events=events, sector_pe_avg=sector_pe_avg,
        sector_pe_sample=sector_pe_sample, sector_pe_label=sector_pe_label, sector_profile=profile, sector_benchmark=benchmark,
    )


def _fmt(v: float | None, suffix: str = "") -> str:
    return f"{v:g}{suffix}" if v is not None else "chưa có dữ liệu"


def build_fundamentals_prompt_section(
    valuation: Valuation | None,
    foreign: ForeignFlowReal | None,
    symbol: str,
    foreign_trend: ForeignFlowTrend | None = None,
    growth: GrowthTrend | None = None,
    events: list[UpcomingEvent] | None = None,
    sector_pe_avg: float | None = None,
    sector_pe_sample: int = 0,
    sector_pe_label: str | None = None,
    sector_profile: fundamental_profiles.FundamentalProfile | None = None,
    sector_benchmark: SectorBenchmark | None = None,
) -> str:
    if not any([valuation, foreign, foreign_trend, growth, events, sector_pe_avg]):
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
    if foreign_trend and foreign_trend.net_total is not None:
        streak_note = ""
        if foreign_trend.streak > 1:
            streak_note = f" — đang MUA RÒNG liên tục {foreign_trend.streak} phiên"
        elif foreign_trend.streak < -1:
            streak_note = f" — đang BÁN RÒNG liên tục {abs(foreign_trend.streak)} phiên"
        lines.append(
            f"Khối ngoại {foreign_trend.days} phiên gần nhất — Tổng ròng: {_fmt(foreign_trend.net_total)} "
            f"({foreign_trend.buy_days} phiên mua ròng / {foreign_trend.sell_days} phiên bán ròng)"
            f"{streak_note}. (1 phiên đơn lẻ dễ nhiễu — nhìn chuỗi này để biết dòng tiền có bền không.)"
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
