"""Điều chỉnh chuỗi giá bằng dữ liệu corporate actions (cổ tức tiền mặt).

Bối cảnh: stock/price_adjust.py chỉ PHÁT HIỆN gap nghi GDKHQ (không sửa số),
vì không có nguồn corporate action. Module này dùng tiếp bước đó: lấy lịch
cổ tức tiền mặt từ vnstock/VCI (nguồn không chính thức, không SLA), và CHỈ
khi dữ liệu cổ tức CÙNG THỜI ĐIỂM lẫn ĐỘ LỚN gap khớp nhau mới điều chỉnh
ngược chuỗi giá trước ngày GDKHQ.

Nguyên tắc an toàn (đọc trước khi sửa):
- Một sự kiện cổ tức chỉ được dùng để điều chỉnh khi gap thực tế giảm đúng
  trong khoảng [0.4x, 1.7x] mức giảm lý thuyết dps/prev_close. Nếu dữ liệu
  cổ tức sai (sai ngày, sai số tiền, nhầm tỷ lệ cổ phiếu thưởng) thì gap sẽ
  không khớp và sự kiện bị BỎ - không bao giờ điều chỉnh theo dữ liệu không
  được giá xác nhận.
- Điều chỉnh kiểu back-adjust (nhân các bar TRƯỚC ngày GDKHQ với factor
  (prev_close - dps)/prev_close): giá hiện tại giữ nguyên hệ quy chiếu, chỉ
  lịch sử được hạ xuống - đúng thứ indicator cần (SMA200/Donchian/trend).
- Sau điều chỉnh, chạy lại price_adjust trên chuỗi mới; gap còn lại không giải
  thích được phải được nêu trong note (không được im lặng).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SEC = 15

# Bản địa hoá mức cổ tức từ nhiều quy ước ghi số khác nhau của nguồn:
# 0.10 = 10% mệnh giá (10.000 VND) -> 1.000 VND/cp; 10 = 10% mệnh giá -> 1.000;
# >= 50 được coi là VND/cp thật (5.000 VND/cp là mức cao thực tế).
_PAR_VALUE_VND = 10_000.0


@dataclass
class DividendEvent:
    ex_date: str  # "YYYY-MM-DD" (ngày GDKHQ), rỗng nếu nguồn không cho
    cash_value: float  # giá trị thô từ nguồn (quy ước chưa chuẩn hoá)


@dataclass
class AdjustmentOutcome:
    closes: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    events_applied: int = 0
    unexplained_gaps: int = 0
    note: str = ""


def normalize_dps(value: float | None) -> float | None:
    """Chuẩn hoá giá trị cổ tức tiền về VND/cổ phần. Trả None nếu không hợp lệ."""
    if value is None or value <= 0:
        return None
    if value <= 5:
        return round(value * _PAR_VALUE_VND, 2)  # 0.10 -> 1.000 VND (tỷ lệ mệnh giá)
    if value <= 50:
        return round(value / 100 * _PAR_VALUE_VND, 2)  # 10 -> 1.000 VND (% mệnh giá)
    return round(value, 2)  # VND/cp thật


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


def _find_ex_index(dates: list[str], ex_date: str) -> int | None:
    """Tìm index của ngày GDKHQ trong chuỗi ngày (cho phép lệch ±1 ngày lịch
    vì nguồn có thể ghi ngày chốt quyền thay vì ngày GDKHQ)."""
    if not ex_date or not dates:
        return None
    target = _date_key(ex_date)
    if target is None:
        return None
    best = None
    best_dist = None
    for i, d in enumerate(dates):
        parsed = _date_key(d)
        if parsed is None:
            continue
        dist = abs((parsed - target).days)
        if best_dist is None or dist < best_dist:
            best, best_dist = i, dist
    # ±1 ngày là đủ: GDKHQ lệch 1 ngày so với ghi nhận của nguồn vẫn cho phép;
    # lệch xa hơn nghĩa là không phải cùng sự kiện.
    return best if best_dist is not None and best_dist <= 1 else None


async def fetch_dividends(symbol: str) -> list[DividendEvent]:
    """Lịch cổ tức tiền mặt qua vnstock/VCI. Không bao giờ raise - lỗi chỉ
    trả [] (khi đó module này hoàn toàn vô hiệu, hệ thống quay về hành vi
    price_adjust cũ)."""
    from stock.providers import ensure_vnstock_api_key, get_vnstock_semaphore

    try:
        ensure_vnstock_api_key()
        from vnstock.explorer.vci import Company
    except ImportError:
        return []

    def _sync() -> list[DividendEvent]:
        try:
            df = Company(symbol=symbol, show_log=False).dividends()
        except Exception:
            logger.warning("vnstock: dividends lỗi cho %s", symbol, exc_info=True)
            return []
        if df is None or df.empty:
            return []
        cols = {str(c).strip().lower(): c for c in df.columns}

        def pick(*keywords: str):
            for kw in keywords:
                for low, orig in cols.items():
                    if kw in low:
                        return orig
            return None

        ex_col = pick("ex", "gdkhq")
        cash_col = pick("cash", "dividend", "tiền mặt", "cổ tức tiền")
        if cash_col is None:
            return []
        events: list[DividendEvent] = []
        for _, row in df.iterrows():
            try:
                value = float(row[cash_col])
            except (TypeError, ValueError, KeyError):
                continue
            ex_date = _to_date_str(row[ex_col]) if ex_col is not None else ""
            events.append(DividendEvent(ex_date=ex_date, cash_value=value))
        return events

    try:
        async with get_vnstock_semaphore():
            return await asyncio.wait_for(asyncio.to_thread(_sync), timeout=_FETCH_TIMEOUT_SEC)
    except Exception:
        logger.warning("fetch_dividends lỗi cho %s", symbol, exc_info=True)
        return []


def apply_dividend_adjustment(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    dates: list[str],
    events: list[DividendEvent],
) -> AdjustmentOutcome:
    """Điều chỉnh ngược chuỗi giá cho các sự kiện cổ tức được gap XÁC NHẬN."""
    from stock import price_adjust

    outcome = AdjustmentOutcome(closes=list(closes), highs=list(highs), lows=list(lows))
    if not closes or not events:
        return outcome

    applied: list[str] = []
    for ev in events:
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
                ev.cash_value, ev.ex_date, dps, actual_drop_pct, expected_drop_pct,
            )
            continue
        factor = (prev_close - dps) / prev_close
        for j in range(i):
            outcome.closes[j] = round(outcome.closes[j] * factor, 2)
            if j < len(outcome.highs):
                outcome.highs[j] = round(outcome.highs[j] * factor, 2)
            if j < len(outcome.lows):
                outcome.lows[j] = round(outcome.lows[j] * factor, 2)
        applied.append(f"{ev.ex_date} ({dps:,.0f} VND/cp)".replace(",", "."))

    # Chạy lại kiểm tra trên chuỗi ĐÃ điều chỉnh: gap còn lại là gap chưa
    # giải thích được (sự kiện khác nguồn không có, hoặc cổ phiếu thưởng).
    remaining = price_adjust.detect_price_gaps(outcome.closes, dates)
    outcome.events_applied = len(applied)
    outcome.unexplained_gaps = len(remaining)
    if applied:
        detail = "; ".join(applied[:3])
        note = (
            f"ℹ️ Chuỗi giá ĐÃ điều chỉnh cổ tức tiền mặt cho {len(applied)} sự kiện GDKHQ "
            f"({detail}) bằng dữ liệu corporate actions - SMA/trend/ATR trên chuỗi này "
            "so sánh được qua các mốc quyền."
        )
        if remaining:
            note += (
                f" Vẫn còn {len(remaining)} gap vượt biên độ không giải thích được bằng cổ tức - "
                "các mốc giá TRƯỚC gap đó vẫn không cùng hệ quy chiếu, KHÔNG dùng làm hỗ trợ/kháng cự."
            )
        outcome.note = note
    return outcome


async def adjust_with_dividends(symbol: str, closes, highs, lows, dates) -> AdjustmentOutcome:
    """Fetch cổ tức rồi điều chỉnh nếu khớp. Chuỗi không gap thì không tốn
    request nào (điều chỉnh chỉ có ý nghĩa khi có dấu hiệu chưa điều chỉnh)."""
    from stock import price_adjust

    if not closes or not price_adjust.detect_price_gaps(closes, dates):
        return AdjustmentOutcome(closes=list(closes), highs=list(highs), lows=list(lows))
    events = await fetch_dividends(symbol)
    if not events:
        return AdjustmentOutcome(closes=list(closes), highs=list(highs), lows=list(lows))
    return apply_dividend_adjustment(closes, highs, lows, dates, events)
