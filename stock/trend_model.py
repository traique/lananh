"""Model thống kê walk-forward dự đoán xác suất đóng cửa TĂNG sau H phiên.

Mục tiêu (rất có giới hạn - đọc trước khi tin output): học từ chính features
của stock/features.py + bối cảnh VNINDEX một mapping thống kê tới nhãn "giá
đóng cửa sau H phiên cao hơn hiện tại" - KHÔNG phải "dự đoán giá" và KHÔNG
được dùng như gate định lượng của stock/policy.py (gate vẫn là hợp đồng
"số do code chốt"). Model chỉ bổ sung MỘT thông tin tham khảo vào prompt với
độ tin cậy out-of-sample được ghi rõ.

Nguyên tắc chống leakage:
- Feature tại bar i chỉ được dùng dữ liệu tới i (giống backtest._evaluate_day).
- Nhãn dùng close[i+H] - close[i]: split train/test theo NGÀY (không theo
  row, vì các mã có ngày giao dịch trùng nhau), giữa train và test có
  embargo H phiên giao dịch để nhãn của bar cuối train không "nhìn thấy" test.
- Đánh giá là MỘT lần split theo ngày (single_split_eval), KHÔNG phải
  walk-forward nhiều fold - tên walk_forward_eval chỉ giữ làm alias tương thích.
- VNINDEX căn theo NGÀY (features.benchmark_end_indices) cả lúc train lẫn
  runtime; căn theo chỉ số mảng cho look-ahead/lệch năm với mã thiếu phiên.
- Không tuning tham số trên test: hyperparameter cố định, chỉ chạy 1 lần.
- Model + thống kê out-of-sample lưu vào stock/data/ giống backtest_stats.json;
  runtime CHỈ ĐỌC, không train trong đường dẫn chat, và CHỈ dùng output khi
  file stats qua được model_quality_gate (xem bên dưới).

Chạy train: `python -m scripts.train_trend_model` hoặc scripts/train_trend_model.py
(CHỈ offline/CI - bị chặn trên Render bởi assert_backtest_runtime_allowed).
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from stock import features as feat

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).resolve().parent / "data" / "trend_model.joblib"
STATS_PATH = Path(__file__).resolve().parent / "data" / "trend_model_stats.json"

DEFAULT_HORIZON = 5
DEFAULT_MIN_HISTORY = 60

# Gate chất lượng runtime: model chỉ được đưa vào prompt/báo cáo khi thống kê
# out-of-sample cho thấy nó hơn hẳn đoán mò. Stats 09/2026 (8 mã, accuracy
# 55,8% < 57,3% của "luôn đoán giảm") KHÔNG qua gate này.
MIN_GATE_AUC = 0.55
MIN_GATE_ACCURACY_EDGE = 0.01  # accuracy phải hơn baseline lớp đa số >= 1 điểm %
MIN_GATE_SYMBOLS = 30
MIN_GATE_TEST_ROWS = 2000
SPLIT_METHOD = "single_date_split_embargo"
# Hyperparameter cố định: KHÔNG tune trên test set. HistGradientBoosting xử
# lý NaN sẵn có (feature thiếu dữ liệu -> nan) nên không phải bịa số trung tính.
_TREE_PARAMS = dict(
    max_iter=200,
    learning_rate=0.06,
    max_leaf_nodes=15,
    min_samples_leaf=50,
    l2_regularization=1.0,
    random_state=42,
)

FEATURE_NAMES = [
    "rsi14",
    "macd_hist_pct",
    "macd_cross",
    "sma20_bias_pct",
    "sma50_bias_pct",
    "sma200_bias_pct",
    "ma_alignment",
    "bb_pct_b",
    "bb_width",
    "trend_1w",
    "trend_1m",
    "trend_3m",
    "trend_1y",
    "adx",
    "di_diff",
    "atr_pct",
    "donchian_state",
    "distribution_days_25",
    "liq_ratio_pct",
    "volume_percentile",
    "daily_change_pct",
    "close_position_pct",
    "volume_ratio_pct",
    "relative_strength_65",
    "trend_score",
    "signal_agreement",
    "vn_alignment",
    "vn_trend_3m",
    "vn_adx",
    "vn_distribution_days",
]

_ALIGN_CODE = {"bullish": 1.0, "bearish": -1.0, "mixed": 0.0, "unknown": 0.0}
_DONCHIAN_CODE = {"breakout_up": 1.0, "breakout_down": -1.0, "inside": 0.0, "unknown": 0.0}
_MACD_CROSS_CODE = {"bullish": 1.0, "bearish": -1.0, "none": 0.0}


def compute_feature_vector(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    i: int,
    vn_closes: list[float],
    vn_highs: list[float],
    vn_lows: list[float],
    vn_volumes: list[float],
    vn_end: int | None = None,
) -> list[float]:
    """Feature vector tại bar i (dữ liệu tới i). Giá trị thiếu -> NaN, KHÔNG
    bịa trung tính (HistGradientBoosting xử lý NaN native).

    vn_end: số phiên VNINDEX có ngày <= ngày bar i (căn theo NGÀY, xem
    features.benchmark_end_indices). None -> căn theo chỉ số (dữ liệu tổng hợp)."""
    w_c = closes[: i + 1]
    w_h = highs[: i + 1] if highs else []
    w_l = lows[: i + 1] if lows else []
    w_v = volumes[: i + 1] if volumes else []
    price = w_c[-1]
    nan = math.nan

    enh = feat.build_enhanced_indicators(w_c, price, w_h or None, w_l or None)
    stats = feat.calc_signal_stats(w_c, w_v, price)
    ma = feat.calc_ma_alignment(w_c) if len(w_c) >= 20 else None
    liq = feat.calc_liquidity(w_v)
    session = feat.calc_session_metrics(w_c, w_h or [], w_l or [], w_v)
    agreement = feat.calc_signal_agreement(enh)

    trend_score = None
    if ma and ma.alignment != "unknown":
        trend_score = feat.calc_trend_score(ma, stats.rsi14, enh.macd.histogram)

    vi_end = min(i + 1, len(vn_closes)) if vn_end is None else min(vn_end, len(vn_closes))
    wv_c = vn_closes[:vi_end]
    wv_h = vn_highs[:vi_end] if vn_highs else []
    wv_l = vn_lows[:vi_end] if vn_lows else []
    wv_v = vn_volumes[:vi_end] if vn_volumes else []
    vn_multi = feat.calc_multi_timeframe(wv_c) if wv_c else None
    vn_adx = feat.calc_adx(wv_c, wv_h, wv_l) if wv_c else None
    vn_dist = feat.calc_distribution_days(wv_c, wv_v)

    def bias(ma_value: float | None) -> float:
        return round((price - ma_value) / ma_value * 100, 3) if ma_value and price > 0 else nan

    macd_available = len(w_c) >= 35
    return [
        stats.rsi14 if stats.rsi14 is not None else nan,
        round(enh.macd.histogram / price * 100, 3) if macd_available and price > 0 else nan,
        _MACD_CROSS_CODE.get(enh.macd.crossover, 0.0) if macd_available else nan,
        bias(enh.sma20),
        bias(enh.sma50),
        bias(enh.sma200),
        _ALIGN_CODE.get(ma.alignment, 0.0) if ma else nan,
        float(enh.bollinger.pct_b) if enh.bollinger.available else nan,
        float(enh.bollinger.width) if enh.bollinger.available else nan,
        float(enh.multi_tf.trend_1w),
        float(enh.multi_tf.trend_1m),
        float(enh.multi_tf.trend_3m),
        float(enh.multi_tf.trend_1y) if enh.multi_tf.bars_used_1y >= 250 else nan,
        float(enh.adx.adx) if enh.adx.available else nan,
        float(enh.adx.di_plus - enh.adx.di_minus) if enh.adx.available else nan,
        float(enh.atr_pct) if enh.atr_pct is not None else nan,
        _DONCHIAN_CODE.get(enh.donchian.state, 0.0),
        float(feat.calc_distribution_days(w_c, w_v)),
        float(liq.liquidity_ratio_pct) if liq else nan,
        float(liq.volume_percentile) if liq else nan,
        float(session.daily_change_pct) if session else nan,
        float(session.close_position_pct) if session else nan,
        float(session.volume_ratio_pct) if session else nan,
        round(feat.calc_relative_strength(w_c, wv_c, lookback=65), 3),
        float(trend_score) if trend_score is not None else nan,
        float(agreement),
        _ALIGN_CODE.get(vn_multi.alignment, 0.0) if vn_multi else nan,
        float(vn_multi.trend_3m) if vn_multi else nan,
        float(vn_adx.adx) if vn_adx and vn_adx.available else nan,
        float(vn_dist),
    ]


@dataclass
class Dataset:
    x: list = field(default_factory=list)
    y: list = field(default_factory=list)
    fwd_returns: list = field(default_factory=list)
    dates: list = field(default_factory=list)
    symbols: list = field(default_factory=list)


def avg_traded_value(closes: list[float], volumes: list[float], bars: int = 60) -> float:
    """Giá trị giao dịch bình quân (VND/phiên) của `bars` phiên gần nhất."""
    pairs = list(zip(closes, volumes, strict=False))[-bars:]
    if not pairs:
        return 0.0
    return sum(c * v for c, v in pairs) / len(pairs)


async def build_dataset(
    symbols: list[str],
    days: int,
    *,
    horizon: int = DEFAULT_HORIZON,
    min_history: int = DEFAULT_MIN_HISTORY,
    min_avg_traded_value: float = 0.0,
) -> Dataset:
    """Fetch OHLCV + VNINDEX rồi build dataset (label = fwd return > 0).
    Rows sort theo (ngày, mã) để split train/test theo NGÀY, không theo row.
    Mã có giá trị giao dịch bình quân < min_avg_traded_value bị loại (mã
    thanh khoản mỏng làm nhiễu nhãn và không đại diện cho mã được hỏi)."""
    from stock import providers

    ds = Dataset()
    vnindex = await providers.fetch_ohlcv("VNINDEX", days=days)
    for sym in symbols:
        series = await providers.fetch_ohlcv(sym, days=days)
        n = len(series.closes)
        if n < min_history + horizon + 1:
            logger.warning("Bỏ qua %s: chỉ có %d bar (< %d)", sym, n, min_history + horizon + 1)
            continue
        if (
            min_avg_traded_value
            and avg_traded_value(series.closes, series.volumes) < min_avg_traded_value
        ):
            logger.warning(
                "Bỏ qua %s: thanh khoản dưới ngưỡng %.0f VND/phiên", sym, min_avg_traded_value
            )
            continue
        vn_ends = feat.benchmark_end_indices(series.dates, vnindex.dates, n, len(vnindex.closes))
        for i in range(min_history, n - horizon):
            x = compute_feature_vector(
                series.closes,
                series.highs,
                series.lows,
                series.volumes,
                i,
                vnindex.closes,
                vnindex.highs,
                vnindex.lows,
                vnindex.volumes,
                vn_end=vn_ends[i],
            )
            fwd = series.closes[i + horizon] / series.closes[i] - 1
            ds.x.append(x)
            ds.y.append(1 if fwd > 0 else 0)
            ds.fwd_returns.append(fwd)
            ds.dates.append(series.dates[i] if i < len(series.dates) else "")
            ds.symbols.append(sym)
        logger.info("Dataset %s: tổng cộng %d rows", sym, len(ds.x))

    order = sorted(range(len(ds.x)), key=lambda k: (ds.dates[k], ds.symbols[k]))
    ds.x = [ds.x[k] for k in order]
    ds.y = [ds.y[k] for k in order]
    ds.fwd_returns = [ds.fwd_returns[k] for k in order]
    ds.dates = [ds.dates[k] for k in order]
    ds.symbols = [ds.symbols[k] for k in order]
    return ds


def single_split_eval(
    ds: Dataset, *, test_ratio: float = 0.30, horizon: int = DEFAULT_HORIZON
) -> tuple[object, dict]:
    """MỘT lần train/test split theo NGÀY (không phải walk-forward nhiều fold),
    giữa train và test bỏ trống `horizon` phiên giao dịch (embargo). Trả
    (model, metrics dict). Chỉ tính: AUC, accuracy@0.5 (kèm baseline lớp đa
    số), mean fwd return toàn test (baseline "mua đại") và top/bottom decile."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import accuracy_score, roc_auc_score

    n = len(ds.x)
    if n < 500:
        raise RuntimeError(f"Dataset quá nhỏ ({n} rows) - không đánh giá model được.")

    unique_dates = sorted({d for d in ds.dates if d})
    if len(unique_dates) <= horizon + 2:
        raise RuntimeError("Không đủ ngày giao dịch để split walk-forward theo ngày.")
    split_date_idx = max(
        0, min(len(unique_dates) - 1, int(len(unique_dates) * (1 - test_ratio)) - 1)
    )
    test_date_idx = split_date_idx + horizon + 1
    if test_date_idx >= len(unique_dates):
        raise RuntimeError("Không đủ dữ liệu test sau embargo - tăng `days` hoặc thêm mã.")

    split_date = unique_dates[split_date_idx]
    test_start_date = unique_dates[test_date_idx]
    split = next((idx for idx, d in enumerate(ds.dates) if d > split_date), n)
    test_start = next((idx for idx, d in enumerate(ds.dates) if d >= test_start_date), n)
    if split <= 0 or test_start >= n - 50:
        raise RuntimeError("Không đủ dữ liệu test sau embargo - tăng `days` hoặc thêm mã.")

    model = HistGradientBoostingClassifier(**_TREE_PARAMS)
    model.fit(ds.x[:split], ds.y[:split])
    prob = model.predict_proba(ds.x[test_start:])[:, 1]
    y_test = ds.y[test_start:]
    fwd_test = ds.fwd_returns[test_start:]

    auc = float(roc_auc_score(y_test, prob))
    accuracy = float(accuracy_score(y_test, [1 if p >= 0.5 else 0 for p in prob]))

    # Top-decile theo prob: nếu model có thông tin thật thì mean fwd return
    # của decile này phải rõ ràng hơn baseline (mua ngẫu nhiên/toàn bộ).
    ranked = sorted(zip(prob, fwd_test, strict=False), key=lambda t: -t[0])
    top_n = max(1, len(ranked) // 10)
    top_mean = sum(f for _, f in ranked[:top_n]) / top_n * 100
    baseline_mean = sum(fwd_test) / len(fwd_test) * 100
    bottom_mean = sum(f for _, f in ranked[-top_n:]) / top_n * 100

    up_share = sum(y_test) / len(y_test)
    embargo_dates = test_date_idx - split_date_idx - 1
    if embargo_dates < horizon:
        raise RuntimeError("Embargo giữa train và test ngắn hơn horizon - split không hợp lệ.")
    metrics = {
        "auc": round(auc, 4),
        "accuracy": round(accuracy, 4),
        "majority_baseline_accuracy": round(max(up_share, 1 - up_share), 4),
        "n_train": split,
        "n_test": n - test_start,
        "split_method": SPLIT_METHOD,
        "split_date": split_date,
        "test_start_date": test_start_date,
        "embargo_trading_dates": embargo_dates,
        "baseline_mean_fwd_ret_pct": round(baseline_mean, 3),
        "top_decile_mean_fwd_ret_pct": round(top_mean, 3),
        "bottom_decile_mean_fwd_ret_pct": round(bottom_mean, 3),
        "class_balance_pct_up": round(sum(y_test) / len(y_test) * 100, 1),
    }
    return model, metrics


def train_and_save(
    symbols: list[str],
    days: int,
    *,
    horizon: int = DEFAULT_HORIZON,
    min_avg_traded_value: float = 0.0,
) -> dict:
    """Train + ghi model và stats. CHỈ chạy offline/CI (bị chặn trên Render)."""
    from stock.backtest import assert_backtest_runtime_allowed

    assert_backtest_runtime_allowed()

    async def _collect():
        return await build_dataset(
            symbols, days, horizon=horizon, min_avg_traded_value=min_avg_traded_value
        )

    import asyncio

    ds = asyncio.run(_collect())
    model, metrics = single_split_eval(ds, horizon=horizon)
    try:
        import joblib

        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, MODEL_PATH)
    except ImportError:
        logger.warning("joblib/sklearn không có - chỉ lưu stats, không lưu model.")
        metrics["model_saved"] = False
    else:
        metrics["model_saved"] = True

    stats = {
        **metrics,
        "horizon": horizon,
        "n_features": len(FEATURE_NAMES),
        "trained_at_vn": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "n_symbols": len(set(ds.symbols)),
        "note": "AUC/accuracy là out-of-sample theo ngày, embargo H phiên; KHÔNG bảo đảm hiệu suất tương lai.",
    }
    usable, why = model_quality_gate(stats)
    stats["runtime_gate_passed"] = usable
    stats["runtime_gate_reason"] = why
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Đã lưu trend model stats: %s", stats)
    return stats


# Alias tương thích tên cũ - thực chất là MỘT lần split, xem single_split_eval.
walk_forward_eval = single_split_eval


def model_quality_gate(stats: dict) -> tuple[bool, str]:
    """Model có đủ tốt để đưa vào runtime không - (passed, lý do).

    Tất cả điều kiện phải đạt: accuracy hơn baseline lớp đa số, AUC đủ xa
    0,5, đủ số mã và số dòng test, và stats do code split có embargo hiện
    tại sinh ra (có split_method + embargo_trading_dates >= horizon)."""
    if not stats:
        return False, "chưa có thống kê out-of-sample"
    try:
        auc = float(stats["auc"])
        accuracy = float(stats["accuracy"])
        n_symbols = int(stats.get("n_symbols", 0))
        n_test = int(stats.get("n_test", 0))
        horizon = int(stats.get("horizon", DEFAULT_HORIZON))
    except (KeyError, TypeError, ValueError):
        return False, "thống kê thiếu trường bắt buộc"
    baseline = stats.get("majority_baseline_accuracy")
    if baseline is None and stats.get("class_balance_pct_up") is not None:
        up = float(stats["class_balance_pct_up"]) / 100
        baseline = max(up, 1 - up)
    if baseline is None:
        return False, "thiếu baseline lớp đa số để so accuracy"
    if (
        stats.get("split_method") != SPLIT_METHOD
        or int(stats.get("embargo_trading_dates", -1)) < horizon
    ):
        return False, "thống kê không do split theo ngày có embargo hiện tại sinh ra"
    if accuracy < float(baseline) + MIN_GATE_ACCURACY_EDGE:
        return False, f"accuracy {accuracy:.4f} không hơn baseline lớp đa số {float(baseline):.4f}"
    if auc < MIN_GATE_AUC:
        return False, f"AUC {auc:.4f} < {MIN_GATE_AUC}"
    if n_symbols < MIN_GATE_SYMBOLS:
        return False, f"chỉ {n_symbols} mã (< {MIN_GATE_SYMBOLS})"
    if n_test < MIN_GATE_TEST_ROWS:
        return False, f"chỉ {n_test} dòng test (< {MIN_GATE_TEST_ROWS})"
    return True, "đạt"


_model_cache: tuple[float, object, dict] | None = None  # (loaded_at, model, stats)
_MODEL_RELOAD_SEC = 6 * 3600


def _load_model() -> tuple[object, dict] | None:
    """Load model + stats (cache trong process). Không raise - thiếu file,
    thiếu sklearn hay file hỏng đều trả None để feature tự vô hiệu."""
    global _model_cache
    if _model_cache and time.monotonic() - _model_cache[0] < _MODEL_RELOAD_SEC:
        model, stats = _model_cache[1], _model_cache[2]
        return (model, stats) if model is not None else None
    model = None
    try:
        stats = json.loads(STATS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stats = {}
    usable, why = model_quality_gate(stats)
    if not usable:
        # Không qua gate -> KHÔNG load model (cũng không import sklearn/joblib,
        # tiết kiệm RAM trên Render 512MB).
        logger.info("Trend model bị tắt: %s", why)
    elif MODEL_PATH.exists():
        try:
            import joblib

            model = joblib.load(MODEL_PATH)
        except Exception:
            logger.warning(
                "Không load được trend model %s - tắt tính năng.", MODEL_PATH, exc_info=True
            )
    _model_cache = (time.monotonic(), model, stats or {})
    return (model, stats) if model is not None else None


def prob_up_for_series(
    symbol_series, vnindex_series, *, min_history: int = DEFAULT_MIN_HISTORY
) -> float | None:
    """Xác suất tăng sau H phiên cho trạng thái bar CUỐI của chuỗi (runtime).
    Trả None khi model không có / dữ liệu quá ngắn."""
    loaded = _load_model()
    if loaded is None:
        return None
    model, _stats = loaded
    closes = list(symbol_series.closes)
    if len(closes) < min_history:
        return None
    try:
        vn_closes = list(vnindex_series.closes)
        sym_dates = list(getattr(symbol_series, "dates", []) or [])
        vn_dates = list(getattr(vnindex_series, "dates", []) or [])
        # Căn theo NGÀY: VNINDEX tới đúng ngày bar cuối của mã. Thiếu ngày thì
        # cả hai chuỗi cùng kết thúc ở phiên gần nhất -> lấy đủ VNINDEX (cách
        # cũ min(i+1, len) lấy nhầm đoạn VNINDEX từ đầu chuỗi khi mã ít bar).
        vn_end = feat.benchmark_end_indices(sym_dates, vn_dates, len(closes), len(vn_closes))[-1]
        if not (feat._looks_iso_dates(sym_dates) and feat._looks_iso_dates(vn_dates)):
            vn_end = len(vn_closes)
        x = compute_feature_vector(
            closes,
            list(symbol_series.highs),
            list(symbol_series.lows),
            list(symbol_series.volumes),
            len(closes) - 1,
            vn_closes,
            list(vnindex_series.highs),
            list(vnindex_series.lows),
            list(vnindex_series.volumes),
            vn_end=vn_end,
        )
        prob = model.predict_proba([x])[0][1]
        return round(float(prob), 3)
    except Exception:
        logger.warning("predict trend model lỗi", exc_info=True)
        return None


def model_stats() -> dict:
    """Thống kê out-of-sample đã lưu (rỗng nếu chưa train)."""
    _load_model()
    return dict(_model_cache[2]) if _model_cache else {}
