"""Test stock_backtest.py bằng dữ liệu tổng hợp (không cần mạng thật) - kiểm
tra cơ chế mở/đóng lệnh (stop/target/timeout), không kiểm tra hiệu quả chiến
lược thật (cần dữ liệu VN thật cho việc đó, xem run_backtest)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import backtest as bt  # noqa: E402


def _uptrend_series(n=200, start=30_000.0, step=150.0):
    closes = [start + i * step for i in range(n)]
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    volumes = [800_000.0] * n
    dates = [f"2024-01-{(i % 28) + 1:02d}" for i in range(n)]
    return closes, highs, lows, volumes, dates


def test_khong_crash_tren_du_lieu_uptrend_dai():
    closes, highs, lows, volumes, dates = _uptrend_series()
    vn_closes, vn_highs, vn_lows, _, _ = _uptrend_series()
    result = bt.run_backtest_on_series("TEST", closes, highs, lows, volumes, dates, vn_closes, vn_highs, vn_lows)
    assert result.symbol == "TEST"
    assert result.total_days_evaluated > 0


def test_khong_bao_gio_mo_2_lenh_cung_luc():
    closes, highs, lows, volumes, dates = _uptrend_series()
    vn_closes, vn_highs, vn_lows, _, _ = _uptrend_series()
    result = bt.run_backtest_on_series("TEST", closes, highs, lows, volumes, dates, vn_closes, vn_highs, vn_lows)
    # entry_date của các lệnh phải tăng dần và không trùng lặp - vì 1 lệnh
    # phải đóng xong (target/stop/timeout) mới được mở lệnh tiếp theo.
    entry_dates = [t.entry_date for t in result.trades]
    assert len(entry_dates) == len(set(entry_dates)) or len(entry_dates) <= 1


def test_target_hit_cho_r_duong_stop_hit_cho_r_am():
    closes, highs, lows, volumes, dates = _uptrend_series()
    vn_closes, vn_highs, vn_lows, _, _ = _uptrend_series()
    result = bt.run_backtest_on_series("TEST", closes, highs, lows, volumes, dates, vn_closes, vn_highs, vn_lows)
    for t in result.trades:
        if t.outcome == "target_hit":
            assert t.r_multiple is not None and t.r_multiple > 0
        elif t.outcome == "stop_hit":
            assert t.r_multiple == -1.0


def test_win_rate_trong_khoang_0_100():
    closes, highs, lows, volumes, dates = _uptrend_series()
    vn_closes, vn_highs, vn_lows, _, _ = _uptrend_series()
    result = bt.run_backtest_on_series("TEST", closes, highs, lows, volumes, dates, vn_closes, vn_highs, vn_lows)
    if result.win_rate is not None:
        assert 0.0 <= result.win_rate <= 100.0


def test_format_summary_khong_crash_khi_khong_co_tin_hieu():
    empty_result = bt.BacktestResult(symbol="XYZ", trades=[], win_rate=None, avg_r=None, total_days_evaluated=50, buy_signals=0)
    text = bt.format_backtest_summary([empty_result])
    assert "XYZ" in text
    assert "0 tín hiệu BUY" in text
