"""Kiểm tra tầng phát hiện chuỗi giá chưa điều chỉnh sau chia tách/cổ tức.

Mỗi test tương ứng một lỗi thật có thể làm sai khuyến nghị bằng tiền thật:
gap ngày GDKHQ bị coi là sạp sàn, mốc hỗ trợ cũ khác hệ quy chiếu bị dùng
làm điểm vào, và biến động trần/sàn thật bị báo động oan.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

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
