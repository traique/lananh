"""Train model thống kê dự đoán xác suất tăng 5 phiên (walk-forward).

CHỈ chạy offline/CI - bị chặn trên Render (assert_backtest_runtime_allowed,
giống backtest). Kết quả ghi vào:
- stock/data/trend_model.joblib   (model; deploy kèm nếu muốn bật runtime)
- stock/data/trend_model_stats.json (thống kê out-of-sample, luôn ghi)

Chọn mã: KHÔNG lấy N mã đầu theo bảng chữ cái (lệch hẳn về mã nhỏ bắt đầu bằng
"A"). Với --symbols N/auto: lấy mẫu ngẫu nhiên CÓ SEED từ universe (tái lập
được bằng --seed), rồi build_dataset loại mã có giá trị giao dịch bình quân
dưới --min-value (mặc định 5 tỷ VND/phiên). Runtime chỉ dùng model khi stats
qua trend_model.model_quality_gate (>= 30 mã, AUC >= 0,55, accuracy hơn baseline
lớp đa số...) - nên lấy mẫu dư (mặc định 80) để còn đủ mã sau lọc thanh khoản.

Ví dụ:
    python scripts/train_trend_model.py --symbols FPT,VNM,VCB --days 750
    python scripts/train_trend_model.py --symbols 80 --days 1000 --seed 7
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock import trend_model  # noqa: E402
from stock.backtest import assert_backtest_runtime_allowed  # noqa: E402
from stock.providers import fetch_symbol_universe  # noqa: E402
from stock.sector import ALL_KNOWN_SYMBOLS  # noqa: E402

logger = logging.getLogger(__name__)


DEFAULT_SAMPLE_SIZE = 80
DEFAULT_SEED = 42
DEFAULT_MIN_AVG_VALUE = 5_000_000_000  # 5 tỷ VND/phiên


def sample_symbols(universe: list[str], n: int, seed: int) -> list[str]:
    """Mẫu ngẫu nhiên có seed (tái lập được), không phụ thuộc thứ tự universe."""
    pool = sorted(set(universe))
    if n >= len(pool):
        return pool
    return sorted(random.Random(seed).sample(pool, n))


async def _resolve_symbols(arg: str, seed: int = DEFAULT_SEED) -> list[str]:
    if not arg or arg.lower() in ("auto", "universe") or arg.isdigit():
        n = int(arg) if arg and arg.isdigit() else DEFAULT_SAMPLE_SIZE
        try:
            symbols = await fetch_symbol_universe()
        except Exception:
            symbols = []
        if not symbols:
            symbols = sorted(ALL_KNOWN_SYMBOLS)
        return sample_symbols(symbols, n, seed)
    return [s.strip().upper() for s in arg.split(",") if s.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        default=str(DEFAULT_SAMPLE_SIZE),
        help="Danh sách mã phẩy, hoặc số N mã lấy mẫu ngẫu nhiên có seed, hoặc 'auto'",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="Seed lấy mẫu mã (tái lập được)"
    )
    parser.add_argument(
        "--min-value",
        type=float,
        default=DEFAULT_MIN_AVG_VALUE,
        help="Giá trị GD bình quân tối thiểu (VND/phiên)",
    )
    parser.add_argument(
        "--days", type=int, default=750, help="Số phiên lịch sử mỗi mã (mặc định 750 ~ 3 năm)"
    )
    parser.add_argument("--horizon", type=int, default=5, help="Số phiên dự đoán tới (mặc định 5)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    assert_backtest_runtime_allowed()
    symbols = asyncio.run(_resolve_symbols(args.symbols, args.seed))
    logger.info(
        "Train trend model: %d mã, %d ngày, horizon %d", len(symbols), args.days, args.horizon
    )
    stats = trend_model.train_and_save(
        symbols, args.days, horizon=args.horizon, min_avg_traded_value=args.min_value
    )
    print("\n=== TREND MODEL OUT-OF-SAMPLE ===")
    for key in (
        "auc",
        "accuracy",
        "majority_baseline_accuracy",
        "n_symbols",
        "n_train",
        "n_test",
        "split_date",
        "test_start_date",
        "embargo_trading_dates",
        "baseline_mean_fwd_ret_pct",
        "top_decile_mean_fwd_ret_pct",
        "bottom_decile_mean_fwd_ret_pct",
        "class_balance_pct_up",
        "model_saved",
        "runtime_gate_passed",
        "runtime_gate_reason",
    ):
        print(f"{key}: {stats.get(key)}")
    print("\nĐọc kết quả: runtime chỉ dùng model khi runtime_gate_passed = True")
    print("(trend_model.model_quality_gate). Nếu không - KHÔNG deploy model.")


if __name__ == "__main__":
    main()
