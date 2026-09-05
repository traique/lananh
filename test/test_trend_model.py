"""Test stock/trend_model.py - feature vector và walk-forward eval bằng dữ liệu
tổng hợp có tín hiệu học được, không cần mạng."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import trend_model  # noqa: E402


def test_feature_vector_du_do_dai_va_khong_loi():
    rng = np.random.default_rng(7)
    n = 70
    closes = list(np.cumprod(1 + rng.normal(0, 0.01, n)) * 50_000)
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    volumes = list(rng.uniform(100_000, 500_000, n))
    x = trend_model.compute_feature_vector(closes, highs, lows, volumes, n - 1, closes, highs, lows, volumes)
    assert len(x) == len(trend_model.FEATURE_NAMES)
    # Không được có None/exception - chỉ float hoặc NaN (thiếu dữ liệu).
    assert all(isinstance(v, float) or v is None for v in x)


def test_feature_vector_chuoi_ngan_nan_khong_loi():
    closes = [50_000.0] * 10
    x = trend_model.compute_feature_vector(closes, [], [], [], len(closes) - 1, [], [], [], [])
    assert len(x) == len(trend_model.FEATURE_NAMES)
    assert all(isinstance(v, float) for v in x)


def test_walk_forward_hoc_duoc_tin_hieu_gia():
    """Dataset tổng hợp: feature 0 quyết định trực tiếp nhãn -> model phải học
    được (auc > 0.7) và top decile phải nhỉnh hơn baseline. Dùng lại
    walk_forward_eval trên Dataset tổng hợp, không fetch mạng."""
    rng = np.random.default_rng(42)
    n = 4000
    x = rng.normal(size=(n, 6))
    logit = 2.0 * x[:, 0] - 1.5 * x[:, 1]
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-logit))).astype(int)

    ds = trend_model.Dataset()
    dates = [f"2025-{1 + i // 300:02d}-{1 + i % 28:02d}" for i in range(n)]
    ds.x = x.tolist()
    ds.y = y.tolist()
    # fwd return gắn với xác suất thật để đo expectancy có ý nghĩa.
    ds.fwd_returns = (0.01 * (2 * y - 1) + rng.normal(0, 0.005, n)).tolist()
    ds.dates = dates
    ds.symbols = ["SYN"] * n

    model, metrics = trend_model.walk_forward_eval(ds, test_ratio=0.3, horizon=5)
    assert metrics["auc"] > 0.7
    assert 0.0 <= metrics["accuracy"] <= 1.0
    assert metrics["n_test"] > 50
    # Top decile phải cao hơn bottom decile khi nhãn có tín hiệu thật.
    assert metrics["top_decile_mean_fwd_ret_pct"] > metrics["bottom_decile_mean_fwd_ret_pct"]


def test_prob_up_khong_co_model_tra_none():
    # Trên máy không có model file (hoặc trỏ sang path khác) -> None, không raise.
    original = trend_model.MODEL_PATH
    try:
        trend_model.MODEL_PATH = Path("_khong_ton_tai_trend_model.joblib")
        trend_model._model_cache = None

        class _S:
            closes = [50_000.0] * 70
            highs = [50_500.0] * 70
            lows = [49_500.0] * 70
            volumes = [100_000.0] * 70

        class _V:
            closes = [1_500.0] * 70
            highs = [1_505.0] * 70
            lows = [1_495.0] * 70
            volumes = [1_000_000.0] * 70

        assert trend_model.prob_up_for_series(_S(), _V()) is None
    finally:
        trend_model.MODEL_PATH = original
        trend_model._model_cache = None
