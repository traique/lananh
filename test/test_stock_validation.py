"""Unit test cho stock_validation.py (Gate B - chất lượng dữ liệu)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import validation as val  # noqa: E402


def _dates(n: int, end: datetime) -> list[str]:
    return [(end - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d") for i in range(n)]


def test_khong_co_du_lieu_la_bad():
    q = val.validate_ohlcv([], [], [], [], [])
    assert q.status == "bad"
    assert q.usable is False


def test_qua_it_phien_la_bad():
    closes = [10.0] * 15
    q = val.validate_ohlcv(closes, closes, closes, closes, [])
    assert q.status == "bad"
    assert q.bars_available == 15


def test_du_lieu_stale_la_degraded():
    now = datetime(2026, 7, 15)
    n = 40
    closes = [10.0 + i * 0.01 for i in range(n)]
    dates = _dates(n, now - timedelta(days=10))  # phiên gần nhất cách 10 ngày
    q = val.validate_ohlcv(closes, closes, closes, closes, dates, now=now)
    assert q.status == "degraded"
    assert q.is_stale is True
    assert q.usable is True


def test_outlier_bien_dong_bat_thuong_la_degraded():
    n = 40
    closes = [10.0] * 20 + [50.0] + [50.0] * 19  # nhảy vọt >35% giữa 2 phiên
    highs = closes
    lows = closes
    volumes = [1000.0] * n
    q = val.validate_ohlcv(closes, highs, lows, volumes, [])
    assert q.status == "degraded"
    assert q.has_outlier is True


def test_ngay_trung_lap_la_bad():
    now = datetime(2026, 7, 15)
    n = 30
    closes = [10.0 + i * 0.01 for i in range(n)]
    dates = _dates(n, now)
    dates[-1] = dates[-2]  # trùng ngày
    q = val.validate_ohlcv(closes, closes, closes, closes, dates, now=now)
    assert q.status == "bad"
    assert q.has_duplicate_dates is True


def test_du_lieu_sach_du_phien_la_ok():
    now = datetime(2026, 7, 15)
    n = 40
    closes = [10.0 + i * 0.05 for i in range(n)]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]
    volumes = [100_000.0] * n
    dates = _dates(n, now)
    q = val.validate_ohlcv(closes, highs, lows, volumes, dates, now=now)
    assert q.status == "ok"
    assert q.usable is True
    assert q.reasons == []


def test_it_phien_hon_khuyen_nghi_nhung_tren_san_la_degraded():
    now = datetime(2026, 7, 15)
    n = 25  # >= MIN_BARS_HARD_FLOOR(20) nhưng < DEFAULT_MIN_BARS(30)
    closes = [10.0 + i * 0.05 for i in range(n)]
    dates = _dates(n, now)
    q = val.validate_ohlcv(closes, closes, closes, closes, dates, now=now)
    assert q.status == "degraded"
    assert q.usable is True


def test_length_mismatch_fails_closed():
 q=val.validate_ohlcv([10.0]*30,[11.0]*29,[9.0]*30,[100.0]*30,[])
 assert q.status=="bad" and q.has_length_mismatch
def test_nan_and_negative_volume_fail_closed():
 c=[10.0]*30;c[5]=float("nan");v=[100.0]*30;v[7]=-1
 q=val.validate_ohlcv(c,[11.0]*30,[9.0]*30,v,[])
 assert q.status=="bad" and q.has_invalid_numbers
def test_invalid_ohlc_relationship_fails_closed():
 q=val.validate_ohlcv([12.0]*30,[11.0]*30,[9.0]*30,[100.0]*30,[])
 assert q.status=="bad" and q.has_ohlc_violation
