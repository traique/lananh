"""Test stock/corporate_actions.py - điều chỉnh giá bằng dữ liệu cổ tức tiền.

Dữ liệu tổng hợp tính tay được: mã giá 50.000, ngày GDKHQ cổ tức 10% mệnh giá
(1.000 VND/cp) -> gap giảm 2% (dưới ngưỡng suspect của price_adjust nên phải
test riêng logic khớp), và các case gap KHÔNG khớp cổ tức phải bị bỏ qua.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import corporate_actions as ca  # noqa: E402


def _dates(n, start=(2025, 3, 1)):
    """n ngày liên tiếp bắt đầu từ start (chỉ để khớp ngày GDKHQ ±1)."""
    from datetime import date, timedelta

    d0 = date(*start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def test_normalize_dps_cac_quy_uoc():
    assert ca.normalize_dps(0.10) == 1000.0      # 10% mệnh giá dạng thập phân
    assert ca.normalize_dps(10) == 1000.0        # 10% mệnh giá dạng phần trăm
    assert ca.normalize_dps(1500) == 1500.0      # VND/cp thật
    assert ca.normalize_dps(0) is None
    assert ca.normalize_dps(None) is None
    assert ca.normalize_dps(-5) is None


def test_adjust_giam_dung_gap_khop_cotuc():
    # Giá 50.000 -> GDKHQ cổ tức 1.000 VND (2%) -> close 49.000 phiên kế.
    # Gap 2% không vượt SUSPECT_GAP_PCT nhưng logic điều chỉnh vẫn hoạt động
    # khi có dữ liệu cổ tức khớp.
    closes = [50_000.0] * 20 + [49_000.0] + [49_000.0] * 5
    highs = [c + 500 for c in closes]
    lows = [c - 500 for c in closes]
    dates = _dates(len(closes))
    ex_idx = 20
    events = [ca.DividendEvent(ex_date=dates[ex_idx], cash_value=1000)]

    outcome = ca.apply_dividend_adjustment(closes, highs, lows, dates, events)
    assert outcome.events_applied == 1
    # Bar trước GDKHQ nhân factor (50000-1000)/50000 = 0.98
    assert outcome.closes[ex_idx - 1] == 49_000.0
    assert outcome.closes[ex_idx] == 49_000.0  # giá hiện tại giữ nguyên
    # Chuỗi sau điều chỉnh là phẳng - không còn gap.
    assert outcome.unexplained_gaps == 0
    assert "ĐÃ điều chỉnh" in outcome.note


def test_adjust_gap_khong_khop_cotuc_bi_bo_qua():
    # Cổ tức 1.000 VND (~2%) nhưng gap giảm 15% -> không khớp [0.4x, 1.7x],
    # KHÔNG được điều chỉnh (đây là case chia tách/cổ phiếu thưởng khác).
    closes = [50_000.0] * 20 + [42_500.0] + [42_500.0] * 5
    highs = [c + 500 for c in closes]
    lows = [c - 500 for c in closes]
    dates = _dates(len(closes))
    events = [ca.DividendEvent(ex_date=dates[20], cash_value=1000)]

    outcome = ca.apply_dividend_adjustment(closes, highs, lows, dates, events)
    assert outcome.events_applied == 0
    assert outcome.closes[19] == 50_000.0
    assert outcome.note == ""


def test_adjust_sai_ngay_khong_dieu_chinh():
    closes = [50_000.0] * 20 + [49_000.0] + [49_000.0] * 5
    dates = _dates(len(closes))
    # ex_date cách xa mọi phiên trong chuỗi -> không tìm thấy index ±1 ngày.
    events = [ca.DividendEvent(ex_date="2026-01-01", cash_value=1000)]
    outcome = ca.apply_dividend_adjustment(closes, [c + 500 for c in closes], [c - 500 for c in closes], dates, events)
    assert outcome.events_applied == 0


def test_adjust_gap_tang_khong_dung_lam_cotuc():
    closes = [50_000.0] * 20 + [56_000.0] + [56_000.0] * 5
    dates = _dates(len(closes))
    events = [ca.DividendEvent(ex_date=dates[20], cash_value=1000)]
    outcome = ca.apply_dividend_adjustment(closes, [c + 500 for c in closes], [c - 500 for c in closes], dates, events)
    assert outcome.events_applied == 0
