"""Kiểm tra tầng phát hiện chuỗi giá chưa điều chỉnh sau chia tách/cổ tức.

Mỗi test tương ứng một lỗi thật có thể làm sai khuyến nghị bằng tiền thật:
gap ngày GDKHQ bị coi là sạp sàn, mốc hỗ trợ cũ khác hệ quy chiếu bị dùng
làm điểm vào, và biến động trần/sàn thật bị báo động oan.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import analysis as stock_analysis
from stock import price_adjust


def _dates(count: int) -> list[str]:
    start = date(2026, 5, 4)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def _calm_series(bars: int = 40, start: float = 20000.0) -> list[float]:
    return [start + index * 100 for index in range(bars)]


def test_bonus_issue_gap_is_flagged_as_unadjusted():
    closes = _calm_series()
    closes.append(closes[-1] / 2)
    closes.append(closes[-1] + 100)
    gaps = price_adjust.detect_price_gaps(closes, _dates(len(closes)))
    assert len(gaps) == 1
    assert gaps[0].level == "certain"
    assert "1:1" in gaps[0].ratio_hint
    assert price_adjust.infer_is_adjusted(closes, _dates(len(closes))) is False


def test_limit_up_streak_is_not_flagged():
    closes = [10000.0]
    for _ in range(20):
        closes.append(round(closes[-1] * 1.069, 2))
    assert price_adjust.detect_price_gaps(closes) == []
    assert price_adjust.build_note("AAA", "dnse", []) == ""


def test_medium_gap_is_only_a_suspicion():
    closes = _calm_series()
    closes.append(round(closes[-1] * 0.86, 2))
    gaps = price_adjust.detect_price_gaps(closes, _dates(len(closes)))
    assert len(gaps) == 1
    assert gaps[0].level == "suspect"
    assert price_adjust.infer_is_adjusted(closes, _dates(len(closes))) is None
    note = price_adjust.build_note("AAA", "dnse", gaps)
    assert "NGHI VẤN" in note


def test_certain_note_names_every_distorted_indicator():
    closes = _calm_series()
    closes.append(closes[-1] / 2)
    dates = _dates(len(closes))
    note = price_adjust.build_note("FPT", "dnse", price_adjust.detect_price_gaps(closes, dates))
    for indicator in ("SMA50", "Donchian", "ATR14", "trend ~3 tháng"):
        assert indicator in note
    assert "rủi ro" in note
    assert dates[-1] in note


def test_reverse_split_gap_up_is_detected():
    closes = _calm_series()
    closes.append(closes[-1] * 2)
    gaps = price_adjust.detect_price_gaps(closes, _dates(len(closes)))
    assert len(gaps) == 1
    assert gaps[0].move_pct > 0
    assert gaps[0].ratio_hint == ""
    note = price_adjust.build_note("AAA", "vnstock-vci", gaps)
    assert "tăng" in note
    assert "vnstock-vci" in note


def test_missing_dates_and_invalid_bars_do_not_crash():
    closes = [0.0, 10000.0, 5000.0]
    gaps = price_adjust.detect_price_gaps(closes)
    assert len(gaps) == 1
    assert gaps[0].date == ""


def test_audit_series_reports_clean_data_silently():
    closes = _calm_series()
    audit = price_adjust.audit_series("AAA", "dnse", closes, _dates(len(closes)))
    assert audit.is_adjusted is None
    assert audit.gaps == []
    assert audit.note == ""


def test_audit_series_wires_flag_gaps_and_note_together():
    closes = _calm_series()
    closes.append(closes[-1] / 2)
    audit = price_adjust.audit_series("FPT", "dnse", closes, _dates(len(closes)))
    assert audit.is_adjusted is False
    assert audit.gaps
    assert audit.note.startswith("⚠️")


def test_stock_context_carries_the_adjustment_note():
    assert "adjustment_note" in stock_analysis.StockContext.__dataclass_fields__


def test_prompt_template_renders_the_adjustment_note():
    rendered = stock_analysis._STOCK_PROMPT_TEMPLATE.render(
        symbol="FPT",
        adjustment_note="CANH-BAO-DIEU-CHINH-GIA",
    )
    assert "CANH-BAO-DIEU-CHINH-GIA" in rendered


# ─── Ngưỡng gap theo biên độ của sàn (HOSE 7%, HNX 10%, UPCOM 15%) ──────────


def _limit_day_series(move: float, bars: int = 80) -> list[float]:
    closes = _calm_series(bars)
    closes.append(round(closes[-1] * (1 + move), 2))
    closes.extend(closes[-1] + 100 * i for i in range(1, 4))
    return closes


def test_suspect_threshold_follows_exchange_band():
    assert price_adjust.suspect_gap_pct("HOSE") == 9.1
    assert price_adjust.suspect_gap_pct("hsx") == 9.1
    assert price_adjust.suspect_gap_pct("HNX") == 13.0
    assert price_adjust.suspect_gap_pct("UPCOM") == 19.5
    # Chưa rõ sàn: giữ mức chung cũ.
    assert price_adjust.suspect_gap_pct(None) == price_adjust.SUSPECT_GAP_PCT == 12.0


def test_upcom_limit_up_day_is_not_an_unadjusted_gap():
    """Phiên trần UPCOM hợp lệ (+14,5%) trước đây vượt ngưỡng chung 12% nên bị coi là
    gap quyền chưa điều chỉnh -> policy đứng ngoài, confidence 0 suốt ~3 tháng."""
    closes = _limit_day_series(0.145)
    dates = _dates(len(closes))
    assert price_adjust.detect_price_gaps(closes, dates, exchange="UPCOM") == []
    audit = price_adjust.audit_series("ABC", "dnse", closes, dates, exchange="UPCOM")
    assert audit.gaps == [] and audit.note == ""
    # Chưa rõ sàn thì vẫn cảnh báo như cũ (không đoán sàn).
    unknown = price_adjust.detect_price_gaps(closes, dates)
    assert len(unknown) == 1 and unknown[0].level == "suspect"
    # Vượt biên độ UPCOM x 1,3 vẫn bị nghi; vượt 25% vẫn là chắc chắn.
    above_band = price_adjust.detect_price_gaps(_limit_day_series(0.21), exchange="UPCOM")
    assert [gap.level for gap in above_band] == ["suspect"]
    split = price_adjust.detect_price_gaps(_limit_day_series(-0.30), exchange="UPCOM")
    assert [gap.level for gap in split] == ["certain"]


def test_hose_gap_beyond_band_is_suspect_even_below_old_threshold():
    """HOSE biên độ 7%: phiên -10% không thể là giao dịch thường (trước đây lọt dưới 12%)."""
    closes = _limit_day_series(-0.10)
    assert len(price_adjust.detect_price_gaps(closes, exchange="HOSE")) == 1
    assert price_adjust.detect_price_gaps(closes, exchange="HNX") == []
    assert price_adjust.detect_price_gaps(closes) == []


@pytest.mark.asyncio
async def test_build_context_upcom_limit_day_keeps_policy_signal(monkeypatch):
    from stock import corporate_actions, providers

    n = 120
    closes = [30_000.0 + i * 20 for i in range(n)]
    closes[n - 3] = round(closes[n - 4] * 1.145, -1)  # phiên trần UPCOM 3 phiên trước
    closes[n - 2] = closes[n - 3] + 50
    closes[n - 1] = closes[n - 2] + 50
    dates = _dates(n)
    series = providers.OhlcvSeries(
        "UPX",
        closes,
        [c * 1.01 for c in closes],
        [c * 0.99 for c in closes],
        [1e6] * n,
        dates,
        "dnse",
    )
    vn_closes = [1_000.0 + i for i in range(n)]
    vnindex = providers.OhlcvSeries(
        "VNINDEX",
        vn_closes,
        [c + 5 for c in vn_closes],
        [c - 5 for c in vn_closes],
        [1e6] * n,
        dates,
        "dnse",
    )
    exchange = {"value": "UPCOM"}

    async def fake_ohlcv(symbol, days=90):
        return vnindex if symbol == "VNINDEX" else series

    async def none(*args, **kwargs):
        return None

    async def empty(*args, **kwargs):
        return []

    async def blank(*args, **kwargs):
        return ""

    async def no_events(symbol):
        return [], []

    async def fake_exchange(symbol):
        return exchange["value"]

    monkeypatch.setattr(providers, "fetch_ohlcv", fake_ohlcv)
    monkeypatch.setattr(providers, "fetch_quote", none)
    monkeypatch.setattr(providers, "fetch_news", empty)
    monkeypatch.setattr(providers, "fetch_symbol_exchange", fake_exchange)
    monkeypatch.setattr(stock_analysis, "_safe_sector_prompt", blank)
    monkeypatch.setattr(stock_analysis, "_safe_fundamentals_prompt", blank)
    monkeypatch.setattr(corporate_actions, "fetch_corporate_events_cached", no_events)

    ctx = await stock_analysis.build_context("UPX", is_holding=False)
    assert ctx.unadjusted_gap is False
    assert ctx.adjustment_note == ""
    assert ctx.decision.confidence > 0
    # Cùng chuỗi nhưng không rõ sàn: giữ hành vi thận trọng cũ (gate gap).
    exchange["value"] = None
    ctx = await stock_analysis.build_context("UPX", is_holding=False)
    assert ctx.unadjusted_gap is True and ctx.decision.confidence == 0.0
