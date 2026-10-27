"""Điều chỉnh chuỗi giá bằng dữ liệu corporate actions (cổ tức tiền mặt, cổ
tức bằng cổ phiếu, cổ phiếu thưởng, chia tách).

Bối cảnh: stock/price_adjust.py chỉ PHÁT HIỆN gap nghi GDKHQ (không sửa số).
Module này dùng tiếp bước đó: lấy lịch sự kiện quyền từ VCI (REST trực tiếp, nguồn
không chính thức, không SLA), và CHỈ khi dữ liệu sự kiện CÙNG THỜI ĐIỂM lẫn
ĐỘ LỚN gap khớp nhau mới điều chỉnh ngược chuỗi giá trước ngày GDKHQ.

Nguyên tắc an toàn (đọc trước khi sửa):
- Một sự kiện cổ tức tiền chỉ được dùng để điều chỉnh khi gap thực tế giảm
  đúng trong khoảng [0.4x, 1.7x] mức giảm lý thuyết dps/prev_close.
- Sự kiện cổ phiếu (thưởng/cổ tức cổ phiếu/chia tách, tỷ lệ r cổ phiếu mới
  trên 1 cổ phiếu cũ) chỉ được dùng khi giá sau GDKHQ khớp giá tham chiếu lý
  thuyết prev_close/(1+r) trong biên ±_STOCK_EVENT_MOVE_TOLERANCE (đủ chứa
  biến động giao dịch bình thường của chính phiên đó). Phát hành quyền mua
  có giá mua ưu đãi nên KHÔNG điều chỉnh theo công thức này - bỏ qua.
- Dữ liệu sai (sai ngày, sai tỷ lệ) thì gap sẽ không khớp và sự kiện bị BỎ -
  không bao giờ điều chỉnh theo dữ liệu không được giá xác nhận.
- Điều chỉnh kiểu back-adjust (nhân các bar TRƯỚC ngày GDKHQ với factor):
  giá hiện tại giữ nguyên hệ quy chiếu, chỉ lịch sử được hạ xuống. Với sự
  kiện cổ phiếu, khối lượng trước GDKHQ được nhân (1+r) cho cùng đơn vị cổ
  phiếu.
- Sau điều chỉnh, chạy lại price_adjust trên chuỗi mới; gap còn lại không giải
  thích được phải được nêu trong note (không được im lặng).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SEC = 15

# Bản địa hoá mức cổ tức từ nhiều quy ước ghi số khác nhau của nguồn:
# 0.10 = 10% mệnh giá (10.000 VND) -> 1.000 VND/cp; 10 = 10% mệnh giá -> 1.000;
# >= 50 được coi là VND/cp thật (5.000 VND/cp là mức cao thực tế).
_PAR_VALUE_VND = 10_000.0

# Biên lệch cho phép giữa giá sau GDKHQ và giá tham chiếu lý thuyết của sự
# kiện cổ phiếu: 10% đủ chứa biên độ trần/sàn HOSE (7%)/HNX (10%) của chính
# phiên GDKHQ, nhưng đủ chặt để tỷ lệ sai (vd 1:1 thay vì 1:4) bị loại.
_STOCK_EVENT_MOVE_TOLERANCE = 0.10
# Tỷ lệ cổ phiếu mới/cũ hợp lý: 1% (cổ tức cổ phiếu nhỏ) .. 10x (chia tách).
_MIN_STOCK_RATIO = 0.01
_MAX_STOCK_RATIO = 10.0

# Từ khoá nhận diện sự kiện cổ phiếu trong tiêu đề sự kiện VCI.
_STOCK_EVENT_KEYWORDS = (
    "cổ phiếu thưởng",
    "thưởng cổ phiếu",
    "cổ tức bằng cổ phiếu",
    "chia tách",
    "trả cổ tức bằng cổ phiếu",
    "phát hành cổ phiếu để trả cổ tức",
    "bonus",
    "stock dividend",
    "split",
)
# Phát hành quyền mua có giá ưu đãi -> công thức prev/(1+r) không đúng -> bỏ.
_RIGHTS_KEYWORDS = ("quyền mua", "giá phát hành", "phát hành thêm cho cổ đông hiện hữu", "rights")
_RATIO_PAIR_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)")
_RATIO_PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


@dataclass
class DividendEvent:
    ex_date: str  # "YYYY-MM-DD" (ngày GDKHQ), rỗng nếu nguồn không cho
    cash_value: float  # giá trị thô từ nguồn (quy ước chưa chuẩn hoá)


@dataclass
class StockEvent:
    """Sự kiện làm tăng số cổ phiếu không kèm dòng tiền: thưởng, cổ tức cổ
    phiếu, chia tách. ratio = số cổ phiếu MỚI nhận trên 1 cổ phiếu cũ."""

    ex_date: str
    ratio: float
    label: str = ""


@dataclass
class AdjustmentOutcome:
    closes: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    events_applied: int = 0
    unexplained_gaps: int = 0
    note: str = ""
    # Đúng các sự kiện đã được gap giá XÁC NHẬN và áp vào chuỗi - danh mục dùng
    # để quy đổi vị thế (stock/portfolio.py), không bao giờ dùng sự kiện chưa xác nhận.
    applied_stock_events: list[StockEvent] = field(default_factory=list)
    applied_dividends: list[DividendEvent] = field(default_factory=list)


def normalize_dps(value: float | None) -> float | None:
    """Chuẩn hoá giá trị cổ tức tiền về VND/cổ phần. Trả None nếu không hợp lệ."""
    if value is None or value <= 0:
        return None
    if value <= 5:
        return round(value * _PAR_VALUE_VND, 2)  # 0.10 -> 1.000 VND (tỷ lệ mệnh giá)
    if value <= 50:
        return round(value / 100 * _PAR_VALUE_VND, 2)  # 10 -> 1.000 VND (% mệnh giá)
    return round(value, 2)  # VND/cp thật


def _to_number(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_stock_ratio(raw) -> float | None:
    """Chuẩn hoá tỷ lệ sự kiện cổ phiếu về "số cổ phiếu mới / 1 cổ phiếu cũ".

    Quy ước VN: "tỷ lệ 100:25" / "4:1" = sở hữu 100 (4) cổ phiếu cũ nhận 25 (1)
    cổ phiếu mới -> 0.25; "25%" -> 0.25; số thực <= 10 coi là tỷ lệ trực tiếp
    nếu < 1 (0.25), số 1..100 coi là phần trăm (25 -> 0.25). Trả None nếu
    không suy ra được tỷ lệ hợp lý - KHÔNG đoán liều.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if value != value or value <= 0:  # NaN / không dương
            return None
        ratio = value if value < 1 else value / 100
    else:
        text = str(raw).strip().lower()
        if not text or text == "nan":
            return None
        pair = _RATIO_PAIR_RE.search(text)
        pct = _RATIO_PCT_RE.search(text)
        if pair:
            old, new = _to_number(pair.group(1)), _to_number(pair.group(2))
            if not old or not new or old <= 0 or new <= 0:
                return None
            ratio = new / old
        elif pct:
            value = _to_number(pct.group(1))
            if value is None or value <= 0:
                return None
            ratio = value / 100
        else:
            value = _to_number(text)
            if value is None:
                return None
            return parse_stock_ratio(value)
    if not (_MIN_STOCK_RATIO <= ratio <= _MAX_STOCK_RATIO):
        return None
    return round(ratio, 6)


def _to_date_str(raw) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    return text[:10]


def _date_key(date_str: str):
    """Parse "YYYY-MM-DD" (hoặc tiền tố ISO của ISO-datetime) thành date; trả
    None nếu hỏng - _find_ex_index bỏ qua ngày không parse được."""
    text = _to_date_str(date_str)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


# Ngày GDKHQ ghi ở nguồn rơi vào cuối tuần/ngày lễ (hoặc nguồn ghi ngày chốt
# quyền = phiên kế tiếp): chấp nhận phiên đầu tiên SAU ngày đó trong vòng
# chừng này ngày lịch (thứ Sáu -> thứ Hai = 3, thêm 1 ngày lễ = 4).
_EX_DATE_FORWARD_TOLERANCE_DAYS = 4


def _find_ex_index(dates: list[str], ex_date: str) -> int | None:
    """Tìm index của ngày GDKHQ trong chuỗi ngày.

    Khớp đúng ngày -> lấy luôn. Không có phiên đúng ngày (GDKHQ rơi vào ngày
    không giao dịch, hoặc nguồn ghi ngày chốt quyền): lấy phiên đầu tiên sau đó
    trong _EX_DATE_FORWARD_TOLERANCE_DAYS ngày lịch, hoặc phiên liền trước nếu
    chỉ lệch 1 ngày. Trước đây chỉ cho ±1 ngày LỊCH: GDKHQ thứ Hai nhưng nguồn
    ghi thứ Sáu (3 ngày) là bỏ sự kiện -> cổ tức không bao giờ được điều chỉnh."""
    if not ex_date or not dates:
        return None
    target = _date_key(ex_date)
    if target is None:
        return None
    parsed_dates = [(i, _date_key(d)) for i, d in enumerate(dates)]
    for i, parsed in parsed_dates:
        if parsed == target:
            return i
    after = [
        (i, parsed)
        for i, parsed in parsed_dates
        if parsed is not None and 0 < (parsed - target).days <= _EX_DATE_FORWARD_TOLERANCE_DAYS
    ]
    if after:
        return min(after, key=lambda item: item[1])[0]
    before = [
        (i, parsed)
        for i, parsed in parsed_dates
        if parsed is not None and (target - parsed).days == 1
    ]
    return before[0][0] if before else None


def _parse_events_frame(df) -> tuple[list[DividendEvent], list[StockEvent]]:
    """Tách danh sách sự kiện của VCI thành cổ tức tiền và sự kiện cổ phiếu.

    Nhận list dict (stock/vci_direct.fetch_event_history - đường chính, không cần
    pandas) hoặc DataFrame events() của vnstock (giữ tương thích). Không phụ thuộc
    tên cột cứng: tìm cột theo từ khoá. event_code DIV = cổ tức tiền, ISS = phát
    hành (thưởng/cổ tức cổ phiếu/chia tách/quyền mua - phân loại tiếp bằng tiêu đề)."""
    if df is None:
        return [], []
    if isinstance(df, list):
        rows = [r for r in df if isinstance(r, dict)]
        columns: list = list(dict.fromkeys(k for r in rows for k in r))
    else:
        if getattr(df, "empty", True):
            return [], []
        rows = df.to_dict("records")
        columns = list(df.columns)
    if not rows:
        return [], []
    cols = {str(c).strip().lower(): c for c in columns}

    def pick(*keywords: str):
        for kw in keywords:
            for low, orig in cols.items():
                if kw in low:
                    return orig
        return None

    code_col = pick("event_code", "event_list_code")
    ex_col = pick("exright", "ex_date", "gdkhq")
    cash_col = pick("value_per_share", "cash", "dividend", "tiền mặt", "cổ tức tiền")
    ratio_col = pick("ratio", "exrate", "tỷ lệ", "ty_le")
    title_col = pick("event_title", "title", "event_name", "nội dung")

    dividends: list[DividendEvent] = []
    stock_events: list[StockEvent] = []
    for row in rows:
        code = ""
        if code_col is not None and row.get(code_col) is not None:
            code = str(row.get(code_col)).strip().upper()
        title = (
            str(row.get(title_col)).strip()
            if title_col is not None and row.get(title_col) is not None
            else ""
        )
        lowered_title = title.lower()
        ex_date = _to_date_str(row.get(ex_col)) if ex_col is not None else ""

        is_stock_event = code == "ISS" or any(kw in lowered_title for kw in _STOCK_EVENT_KEYWORDS)
        if code in ("", "DIV") and not is_stock_event:
            if cash_col is None:
                continue
            if code == "" and ex_col is None:
                continue
            raw_value = row.get(cash_col)
            if raw_value is None:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            dividends.append(DividendEvent(ex_date=ex_date, cash_value=value))
            continue
        if not is_stock_event:
            continue
        if any(kw in lowered_title for kw in _RIGHTS_KEYWORDS):
            continue
        ratio = parse_stock_ratio(row.get(ratio_col)) if ratio_col is not None else None
        if ratio is None and title:
            ratio = parse_stock_ratio(title)
        if ratio is None or not ex_date:
            continue
        stock_events.append(StockEvent(ex_date=ex_date, ratio=ratio, label=title[:80]))
    return dividends, stock_events


async def fetch_corporate_events(symbol: str) -> tuple[list[DividendEvent], list[StockEvent]]:
    """Lịch cổ tức tiền + sự kiện cổ phiếu qua REST /v1/events của VCI
    (stock/vci_direct.py, không nạp vnstock/pandas). Không bao giờ raise - lỗi chỉ
    trả ([], []) (khi đó module này vô hiệu, hệ thống quay về hành vi chỉ cảnh báo
    của price_adjust)."""
    from stock import vci_direct
    from stock.providers import get_vnstock_semaphore

    def _sync() -> tuple[list[DividendEvent], list[StockEvent]]:
        try:
            rows = vci_direct.fetch_event_history(symbol)
        except Exception:
            logger.warning("VCI: lịch sự kiện quyền lỗi cho %s", symbol, exc_info=True)
            return [], []
        return _parse_events_frame(rows)

    try:
        async with get_vnstock_semaphore():
            return await asyncio.wait_for(asyncio.to_thread(_sync), timeout=_FETCH_TIMEOUT_SEC)
    except Exception:
        logger.warning("fetch_corporate_events lỗi cho %s", symbol, exc_info=True)
        return [], []


async def fetch_dividends(symbol: str) -> list[DividendEvent]:
    """Giữ tương thích: chỉ phần cổ tức tiền của fetch_corporate_events."""
    dividends, _ = await fetch_corporate_events(symbol)
    return dividends


def _scale_before(
    outcome: AdjustmentOutcome, index: int, price_factor: float, volume_factor: float = 1.0
) -> None:
    for j in range(index):
        outcome.closes[j] = round(outcome.closes[j] * price_factor, 2)
        if j < len(outcome.highs):
            outcome.highs[j] = round(outcome.highs[j] * price_factor, 2)
        if j < len(outcome.lows):
            outcome.lows[j] = round(outcome.lows[j] * price_factor, 2)
        if volume_factor != 1.0 and j < len(outcome.volumes):
            outcome.volumes[j] = outcome.volumes[j] * volume_factor


def apply_corporate_adjustment(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    dates: list[str],
    dividends: list[DividendEvent],
    stock_events: list[StockEvent] | None = None,
    volumes: list[float] | None = None,
) -> AdjustmentOutcome:
    """Điều chỉnh ngược chuỗi giá cho các sự kiện được gap XÁC NHẬN."""
    from stock import price_adjust

    outcome = AdjustmentOutcome(
        closes=list(closes),
        highs=list(highs),
        lows=list(lows),
        volumes=list(volumes or []),
    )
    if not closes or (not dividends and not stock_events):
        return outcome

    applied: list[str] = []
    # Sự kiện cổ phiếu trước (gap lớn), cổ tức tiền sau - kiểm tra khớp của
    # mỗi sự kiện dùng giá tại chính ngày GDKHQ nên thứ tự không đổi factor.
    for ev in stock_events or []:
        i = _find_ex_index(dates, ev.ex_date)
        if i is None or i < 1 or i >= len(outcome.closes):
            continue
        prev_close = outcome.closes[i - 1]
        close_i = outcome.closes[i]
        if prev_close <= 0 or close_i <= 0:
            continue
        reference = prev_close / (1 + ev.ratio)
        implied_move = close_i / reference - 1
        if abs(implied_move) > _STOCK_EVENT_MOVE_TOLERANCE:
            logger.debug(
                "Sự kiện cổ phiếu %s tỷ lệ %s không khớp gap (%.2f%% so với tham chiếu) - bỏ qua",
                ev.ex_date,
                ev.ratio,
                implied_move * 100,
            )
            continue
        _scale_before(outcome, i, 1 / (1 + ev.ratio), 1 + ev.ratio)
        outcome.applied_stock_events.append(ev)
        applied.append(f"{ev.ex_date} (cổ phiếu, tỷ lệ {ev.ratio * 100:.4g}%)".replace(".", ","))

    for ev in dividends:
        dps = normalize_dps(ev.cash_value)
        if dps is None:
            continue
        i = _find_ex_index(dates, ev.ex_date)
        if i is None or i < 1 or i >= len(outcome.closes):
            continue
        prev_close = outcome.closes[i - 1]
        close_i = outcome.closes[i]
        if prev_close <= 0 or close_i <= 0 or dps >= prev_close:
            continue
        actual_drop_pct = (prev_close - close_i) / prev_close * 100
        expected_drop_pct = dps / prev_close * 100
        if actual_drop_pct <= 0:
            continue  # gap phải là giảm; gap tăng không phải cổ tức tiền
        if not (expected_drop_pct * 0.4 <= actual_drop_pct <= expected_drop_pct * 1.7):
            logger.debug(
                "Cổ tức %s ngày %s (%s VND/cp) không khớp gap %.2f%% (kỳ vọng %.2f%%) - bỏ qua",
                ev.cash_value,
                ev.ex_date,
                dps,
                actual_drop_pct,
                expected_drop_pct,
            )
            continue
        _scale_before(outcome, i, (prev_close - dps) / prev_close)
        outcome.applied_dividends.append(ev)
        applied.append(f"{ev.ex_date} ({dps:,.0f} VND/cp)".replace(",", "."))

    # Chạy lại kiểm tra trên chuỗi ĐÃ điều chỉnh: gap còn lại là gap chưa
    # giải thích được (sự kiện nguồn không có/sai tỷ lệ).
    remaining = price_adjust.detect_price_gaps(outcome.closes, dates)
    outcome.events_applied = len(applied)
    outcome.unexplained_gaps = len(remaining)
    if applied:
        detail = "; ".join(applied[:3])
        note = (
            f"ℹ️ Chuỗi giá ĐÃ điều chỉnh theo dữ liệu quyền cho {len(applied)} sự kiện GDKHQ "
            f"({detail}) - SMA/trend/ATR trên chuỗi này so sánh được qua các mốc quyền."
        )
        if remaining:
            note += (
                f" Vẫn còn {len(remaining)} gap vượt biên độ không giải thích được bằng dữ liệu quyền - "
                "các mốc giá TRƯỚC gap đó vẫn không cùng hệ quy chiếu, KHÔNG dùng làm hỗ trợ/kháng cự."
            )
        outcome.note = note
    return outcome


def apply_dividend_adjustment(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    dates: list[str],
    events: list[DividendEvent],
) -> AdjustmentOutcome:
    """Giữ tương thích: chỉ điều chỉnh cổ tức tiền."""
    return apply_corporate_adjustment(closes, highs, lows, dates, events)


# Cache lịch sự kiện quyền theo mã (TTL 24h, trần số mã) - lịch quyền đổi theo
# ngày, không theo phút; mỗi lượt phân tích không tốn thêm request VCI.
_EVENTS_CACHE_TTL_SEC = 24 * 3600
_EVENTS_CACHE_MAX = 512
_events_cache: dict[str, tuple[float, tuple[list[DividendEvent], list[StockEvent]]]] = {}
# Chỉ fetch lịch quyền cho chuỗi đủ dài để điều chỉnh có ý nghĩa (chuỗi 5-20
# bar cho báo giá nhanh không cần).
MIN_BARS_FOR_EVENT_LOOKUP = 60


async def fetch_corporate_events_cached(
    symbol: str,
) -> tuple[list[DividendEvent], list[StockEvent]]:
    import time

    key = symbol.strip().upper()
    now = time.monotonic()
    hit = _events_cache.get(key)
    if hit is not None and now - hit[0] < _EVENTS_CACHE_TTL_SEC:
        return hit[1]
    result = await fetch_corporate_events(key)
    # Không cache kết quả rỗng do lỗi? fetch_corporate_events không phân biệt
    # được "không có sự kiện" với "lỗi nguồn" - cache ngắn hơn (1h) cho rỗng.
    ttl_offset = 0.0 if (result[0] or result[1]) else _EVENTS_CACHE_TTL_SEC - 3600
    _events_cache[key] = (now - ttl_offset, result)
    while len(_events_cache) > _EVENTS_CACHE_MAX:
        _events_cache.pop(next(iter(_events_cache)))
    return result


async def adjust_with_corporate_actions(
    symbol: str, closes, highs, lows, dates, volumes=None
) -> AdjustmentOutcome:
    """Fetch lịch sự kiện quyền (cache 24h) rồi điều chỉnh những sự kiện được
    gap giá xác nhận.

    Trước đây chỉ fetch khi price_adjust thấy gap >= 12%: cổ tức tiền 3-6%
    (phổ biến nhất) không bao giờ tạo gap đó nên không bao giờ được điều
    chỉnh, SMA50/ATR/trend 3 tháng méo âm thầm. Giờ fetch cho mọi chuỗi đủ
    dài; apply_corporate_adjustment vẫn chỉ áp sự kiện khi gap thực tế khớp
    độ lớn lý thuyết (an toàn với dữ liệu quyền sai)."""
    unchanged = AdjustmentOutcome(
        closes=list(closes), highs=list(highs), lows=list(lows), volumes=list(volumes or [])
    )
    if not closes or len(closes) < MIN_BARS_FOR_EVENT_LOOKUP:
        return unchanged
    dividends, stock_events = await fetch_corporate_events_cached(symbol)
    if not dividends and not stock_events:
        return unchanged
    return apply_corporate_adjustment(closes, highs, lows, dates, dividends, stock_events, volumes)


async def adjust_with_dividends(symbol: str, closes, highs, lows, dates) -> AdjustmentOutcome:
    """Giữ tương thích tên cũ - giờ dùng cả sự kiện cổ phiếu."""
    return await adjust_with_corporate_actions(symbol, closes, highs, lows, dates)
