"""Train model thống kê dự đoán xác suất tăng 5 phiên (walk-forward).

CHỈ chạy offline/CI - bị chặn trên Render (assert_backtest_runtime_allowed,
giống backtest). Kết quả ghi vào:
- stock/data/trend_model.joblib   (model; deploy kèm nếu muốn bật runtime)
- stock/data/trend_model_stats.json (thống kê out-of-sample, luôn ghi)

Ví dụ:
    python scripts/train_trend_model.py --symbols FPT,VNM,VCB --days 750
    python scripts/train_trend_model.py --symbols 30 --days 1000
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock import trend_model  # noqa: E402
from stock.backtest import assert_backtest_runtime_allowed  # noqa: E402
from stock.providers import fetch_symbol_universe  # noqa: E402
from stock.sector import ALL_KNOWN_SYMBOLS  # noqa: E402

logger = logging.getLogger(__name__)


async def _resolve_symbols(arg: str) -> list[str]:
    if not arg or arg.lower() in ("auto", "universe"):
        try:
            symbols = await fetch_symbol_universe()
        except Exception:
            symbols = []
        if not symbols:
            symbols = sorted(ALL_KNOWN_SYMBOLS)
        # Lấy ~60 mã đầu cho 1 lượt train thực tế (mỗi mã 1 request DNSE).
        return symbols[:60]
    if arg.isdigit():
        n = int(arg)
        try:
            symbols = await fetch_symbol_universe()
        except Exception:
            symbols = []
        if not symbols:
            symbols = sorted(ALL_KNOWN_SYMBOLS)
        return symbols[:n]
    return [s.strip().upper() for s in arg.split(",") if s.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="30", help="Danh sách mã phẩy, hoặc số N mã đầu, hoặc 'auto'")
    parser.add_argument("--days", type=int, default=750, help="Số phiên lịch sử mỗi mã (mặc định 750 ~ 3 năm)")
    parser.add_argument("--horizon", type=int, default=5, help="Số phiên dự đoán tới (mặc định 5)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    assert_backtest_runtime_allowed()
    symbols = asyncio.run(_resolve_symbols(args.symbols))
    logger.info("Train trend model: %d mã, %d ngày, horizon %d", len(symbols), args.days, args.horizon)
    stats = trend_model.train_and_save(symbols, args.days, horizon=args.horizon)
    print("\n=== TREND MODEL OUT-OF-SAMPLE ===")
    for key in ("auc", "accuracy", "n_train", "n_test", "split_date", "test_start_date",
                "baseline_mean_fwd_ret_pct", "top_decile_mean_fwd_ret_pct",
                "bottom_decile_mean_fwd_ret_pct", "class_balance_pct_up", "model_saved"):
        print(f"{key}: {stats.get(key)}")
    print("\nĐọc kết quả: model chỉ có giá trị nếu top_decile_mean_fwd_ret_pct rõ ràng")
    print("cao hơn baseline_mean_fwd_ret_pct trên out-of-sample. Nếu không - KHÔNG deploy model.")


if __name__ == "__main__":
    main()
