"""Backtest TỐI THIỂU cho stock_policy.py.

Mục tiêu duy nhất: trả lời "policy hiện tại có ra quyết định BUY hợp lý
không" bằng win rate + expectancy theo R trên dữ liệu lịch sử thật, KHÔNG
phải xây một framework quant đầy đủ. Giới hạn có chủ đích (ghi rõ để không
ai hiểu nhầm đây là công cụ đã production-ready cho việc tối ưu tham số):

- Mô phỏng phí mua/bán, thuế bán và slippage bảo thủ.
- Không tính Sharpe/max drawdown - chỉ win rate & average R multiple.
- Chỉ đo tín hiệu BUY (WATCH/HOLD/SELL/NO_TRADE không có entry để đo lời/lỗ
  theo kiểu R-multiple; muốn đánh giá SELL cần logic khác, chưa làm ở đây).
- Mỗi mã tối đa 1 lệnh mở tại 1 thời điểm (đơn giản hoá, không mô phỏng
  nhồi lệnh/DCA).
- VNINDEX được căn theo NGÀY giao dịch (features.benchmark_end_indices): bar
  i của mã chỉ nhìn thấy các phiên VNINDEX có ngày <= ngày của bar i. Căn theo
  chỉ số mảng (cách cũ) cho look-ahead khi mã thiếu phiên (tạm ngừng giao dịch)
  và lệch hàng năm với mã niêm yết muộn. Chỉ khi thiếu ngày chuẩn YYYY-MM-DD
  mới rơi về căn theo chỉ số (dữ liệu tổng hợp trong test).
- Gate chất lượng dữ liệu đánh giá "độ cũ" theo NGÀY CỦA CỬA SỔ lịch sử
  (không theo ngày chạy backtest) - trước đây mọi cửa sổ quá khứ bị coi là
  dữ liệu cũ -> "degraded", confidence bị nhân 0,85: backtest đo một policy
  KHÁC policy đang chạy thật.
- Mô phỏng T+2.5 của VN: lệnh không thể thoát (dù đã chạm stop/target) trước
  SETTLEMENT_BARS=3 phiên sau entry, vì hàng chưa về tài khoản để bán. Khi
  chạm stop, r_multiple tính theo giá thoát THẬT (min(close, stop) - có thể
  gap qua sâu hơn stop trong lúc chờ hàng về), không hardcode -1.0.

Cần mạng thật (DNSE qua stock_providers) để chạy `run_backtest` - không chạy
được trong môi trường không có egress tới DNSE.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from stock import features as feat
from stock import policy
from stock import price_adjust
from stock import providers
from stock import validation

logger = logging.getLogger(__name__)

MIN_BARS_TO_START = 30
MAX_HOLD_DAYS = 20
SETTLEMENT_BARS = 3
TRADING_DAYS_PER_YEAR = 252
DEFAULT_BACKTEST_YEARS = 5
DEFAULT_BACKTEST_DAYS = TRADING_DAYS_PER_YEAR * DEFAULT_BACKTEST_YEARS
BUY_FEE_PCT = 0.15
SELL_FEE_PCT = 0.15
SELL_TAX_PCT = 0.10
SLIPPAGE_PCT = 0.10
OUT_OF_SAMPLE_RATIO = 0.30
# Dưới ngưỡng này win rate theo setup quá nhiễu để hiển thị cho người dùng/LLM
# (khoảng tin cậy 95% của 10 lệnh rộng ~±30 điểm %).
MIN_TRADES_FOR_STATS = 30
_WILSON_Z = 1.96
BACKTEST_ALLOW_RENDER_ENV = "STOCK_BACKTEST_ALLOW_ON_RENDER"
RENDER_MAX_SYMBOLS = 50
RENDER_MAX_DAYS = TRADING_DAYS_PER_YEAR * 3
RENDER_CONCURRENCY = 2
DEFAULT_CONCURRENCY = 4
BACKTEST_BATCH_SIZE = 50


def is_render_runtime(environ=None):
    env = os.environ if environ is None else environ
    return str(env.get("RENDER", "")).lower() in ("1", "true", "yes") or any(
        env.get(k) for k in ("RENDER_SERVICE_ID", "RENDER_INSTANCE_ID", "RENDER_EXTERNAL_URL")
    )


def assert_backtest_runtime_allowed(environ=None):
    env = os.environ if environ is None else environ
    if is_render_runtime(env) and str(env.get(BACKTEST_ALLOW_RENDER_ENV, "false")).lower() not in (
        "1",
        "true",
        "yes",
    ):
        raise RuntimeError(
            "Heavy backtest is disabled on Render web service. Run it in GitHub Actions/offline, then deploy backtest_stats.json."
        )


def backtest_runtime_limits(days, symbol_count, environ=None):
    env = os.environ if environ is None else environ
    if is_render_runtime(env):
        return min(days, RENDER_MAX_DAYS), min(symbol_count, RENDER_MAX_SYMBOLS), RENDER_CONCURRENCY
    requested = max(1, int(env.get("STOCK_BACKTEST_CONCURRENCY", DEFAULT_CONCURRENCY)))
    return days, symbol_count, min(requested, 8)


@dataclass(frozen=True)
class TradingCosts:
    buy_fee_pct: float = BUY_FEE_PCT
    sell_fee_pct: float = SELL_FEE_PCT
    sell_tax_pct: float = SELL_TAX_PCT
    slippage_pct: float = SLIPPAGE_PCT


DEFAULT_COSTS = TradingCosts()


def _entry_cost(price, c):
    return price * (1 + c.slippage_pct / 100) * (1 + c.buy_fee_pct / 100)


def _exit_proceeds(price, c):
    return price * (1 - c.slippage_pct / 100) * (1 - (c.sell_fee_pct + c.sell_tax_pct) / 100)


def _net_r(entry_raw, exit_raw, stop_raw, c):
    entry = _entry_cost(entry_raw, c)
    exit_value = _exit_proceeds(exit_raw, c)
    stop = _exit_proceeds(stop_raw, c)
    risk = entry - stop
    return (round((exit_value - entry) / risk, 2) if risk > 0 else None, entry, exit_value)


# B6: thống kê backtest theo setup_type, lưu tĩnh sau khi chạy run_backtest()
# thủ công/định kỳ - build_prompt() (stock_analysis.py) CHỈ ĐỌC file này, KHÔNG
# gọi backtest runtime (backtest chậm, cần mạng thật tới DNSE, không phù hợp
# chạy mỗi lần trả lời chat).
BACKTEST_STATS_PATH = Path(__file__).resolve().parent / "data" / "backtest_stats.json"


def wilson_interval(wins: int, n: int, z: float = _WILSON_Z) -> tuple[float, float] | None:
    """Khoảng tin cậy Wilson (mặc định 95%) cho tỷ lệ thắng, đơn vị %."""
    if n <= 0 or wins < 0 or wins > n:
        return None
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return round(max(0.0, center - half) * 100, 1), round(min(1.0, center + half) * 100, 1)


def save_setup_stats(
    results: list[BacktestResult] | None = None,
    *,
    by_setup: dict | None = None,
    path: Path = BACKTEST_STATS_PATH,
) -> None:
    """Gom trades của mọi BacktestResult theo setup_type rồi ghi ra JSON tĩnh.
    Gọi thủ công/định kỳ sau khi chạy run_backtest() trên tập mã đại diện -
    KHÔNG gọi từ đường dẫn phục vụ chat."""
    if by_setup is None:
        by_setup = {}
        if results:
            for r in results:
                for t in r.trades:
                    if t.r_multiple is None:
                        continue
                    bucket = by_setup.setdefault(t.setup_type, [])
                    bucket.append(t.r_multiple)
        summary = {}
        for setup, rs in by_setup.items():
            if not rs:
                continue
            wins = sum(1 for r in rs if r > 0)
            ci = wilson_interval(wins, len(rs))
            summary[setup] = {
                "win_rate": round(wins / len(rs) * 100, 1),
                "wins": wins,
                "avg_r": round(sum(rs) / len(rs), 2),
                "n": len(rs),
                "win_rate_ci95": list(ci) if ci else None,
            }
        by_setup = summary
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(by_setup, ensure_ascii=False, indent=2), encoding="utf-8")


def load_setup_stats(path: Path = BACKTEST_STATS_PATH) -> dict:
    """Đọc thống kê đã lưu - không raise nếu file chưa tồn tại/hỏng (chưa
    từng chạy backtest là trạng thái hợp lệ, không phải lỗi)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def format_setup_stats_line(setup_type: str, stats: dict | None = None) -> str | None:
    """1 dòng cho prompt - mục đích để LLM khiêm tốn khi win rate thấp, tự
    tin có track record khi cao. Trả None nếu chưa có đủ dữ liệu cho đúng
    setup_type đang xét (không lấy nhầm số của setup khác)."""
    stats = stats if stats is not None else load_setup_stats()
    entry = stats.get(setup_type)
    if not entry:
        return None
    try:
        n = int(entry.get("n", 0))
        win_rate = float(entry["win_rate"])
    except (KeyError, TypeError, ValueError):
        return None
    if n < MIN_TRADES_FOR_STATS:
        return None
    wins = entry.get("wins")
    if not isinstance(wins, int):
        wins = round(win_rate / 100 * n)
    ci = wilson_interval(wins, n)
    ci_text = f" (khoảng tin cậy 95%: {ci[0]}-{ci[1]}%)".replace(".", ",") if ci else ""
    return (
        f"Backtest lịch sử cho setup '{setup_type}': win rate {win_rate}%{ci_text}, "
        f"avg R {entry.get('avg_r')}, trên {n} lệnh đã đóng (xem giới hạn ở docstring stock/backtest.py - "
        f"không phải bảo đảm hiệu suất tương lai)."
    )


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str | None
    exit_price: float | None
    outcome: str  # target_hit | stop_hit | timeout | open
    r_multiple: float | None
    confidence: float
    setup_type: str


@dataclass
class BacktestResult:
    symbol: str
    trades: list[Trade] = field(default_factory=list)
    win_rate: float | None = None
    avg_r: float | None = None
    total_days_evaluated: int = 0
    buy_signals: int = 0
    sample: str = "full"


@dataclass
class OutOfSampleResult:
    split_index: int
    train: BacktestResult
    test: BacktestResult


def _window_now(w_dates) -> datetime | None:
    """Mốc "hiện tại" của cửa sổ lịch sử = ngày của bar cuối cửa sổ, để gate độ
    cũ dữ liệu đánh giá như lúc phiên đó vừa đóng cửa. None nếu ngày không
    đúng định dạng (validate_ohlcv tự đánh dấu dữ liệu xấu trường hợp đó)."""
    if not w_dates:
        return None
    try:
        return datetime.strptime(str(w_dates[-1])[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _evaluate_day(
    symbol: str,
    closes,
    highs,
    lows,
    volumes,
    dates,
    i: int,
    vnindex_closes,
    vnindex_highs,
    vnindex_lows,
    vnindex_volumes,
    vn_end: int | None = None,
) -> policy.Decision:
    """Chạy đúng luồng validate -> feature -> policy như stock_analysis.py,
    nhưng CHỈ trên dữ liệu tính tới ngày i (window) - không cho policy nhìn
    thấy tương lai (điều kiện bắt buộc để backtest có ý nghĩa).

    vn_end: số phiên VNINDEX có ngày <= ngày bar i (xem
    features.benchmark_end_indices); None -> căn theo chỉ số (tương thích cũ)."""
    w_closes, w_highs, w_lows, w_volumes = (
        closes[: i + 1],
        highs[: i + 1],
        lows[: i + 1],
        volumes[: i + 1],
    )
    w_dates = dates[: i + 1] if dates else []
    price = w_closes[-1]

    quality = validation.validate_ohlcv(
        w_closes, w_highs, w_lows, w_volumes, w_dates, now=_window_now(w_dates)
    )
    stats = feat.calc_signal_stats(w_closes, w_volumes, price)

    enhanced = None
    if quality.usable and len(w_closes) >= 20:
        enhanced = feat.build_enhanced_indicators(w_closes, price, w_highs, w_lows)
    ma_alignment = feat.calc_ma_alignment(w_closes) if len(w_closes) >= 20 else None
    # Cùng quy tắc với runtime: gap quyền chưa điều chỉnh trong cửa sổ chỉ
    # báo -> policy không ra tín hiệu định hướng (không có lệnh để đo).
    recent = w_closes[-(price_adjust.POLICY_LOOKBACK_BARS + 1) :]
    gap = price_adjust.last_gap_in_window(price_adjust.detect_price_gaps(recent), len(recent))
    support_resistance = feat.calc_support_resistance(w_highs, w_lows, price, 30)
    key_levels = feat.find_key_levels(w_highs, w_lows, w_closes)
    liquidity = feat.calc_liquidity(w_volumes)
    session = feat.calc_session_metrics(w_closes, w_highs, w_lows, w_volumes)
    trend_score = (
        feat.calc_trend_score(ma_alignment, stats.rsi14, enhanced.macd.histogram)
        if ma_alignment and ma_alignment.alignment != "unknown" and enhanced
        else None
    )

    vi_end = min(i + 1, len(vnindex_closes)) if vn_end is None else min(vn_end, len(vnindex_closes))
    w_vn_closes = vnindex_closes[:vi_end]
    w_vn_highs = vnindex_highs[:vi_end]
    w_vn_lows = vnindex_lows[:vi_end]
    w_vn_volumes = vnindex_volumes[:vi_end] if vnindex_volumes else []
    vnindex_multi_tf = feat.calc_multi_timeframe(w_vn_closes) if w_vn_closes else None
    vnindex_adx = feat.calc_adx(w_vn_closes, w_vn_highs, w_vn_lows) if w_vn_closes else None
    vnindex_distribution_days = feat.calc_distribution_days(w_vn_closes, w_vn_volumes)

    relative_strength = feat.calc_relative_strength(w_closes, w_vn_closes, lookback=65)

    inputs = policy.PolicyInputs(
        price=price,
        stats=stats,
        enhanced=enhanced,
        ma_alignment=ma_alignment,
        support_resistance=support_resistance,
        liquidity=liquidity,
        session=session,
        relative_strength=relative_strength,
        trend_score=trend_score,
        news_impact=0.0,
        quality=quality,
        vnindex_multi_tf=vnindex_multi_tf,
        vnindex_adx=vnindex_adx,
        vnindex_distribution_days=vnindex_distribution_days,
        key_levels=key_levels,
        unadjusted_gap=gap is not None,
    )
    return policy.evaluate_policy(inputs)


def run_backtest_on_series(
    symbol: str,
    closes,
    highs,
    lows,
    volumes,
    dates,
    vnindex_closes,
    vnindex_highs,
    vnindex_lows,
    vnindex_volumes=None,
    *,
    min_bars: int = MIN_BARS_TO_START,
    max_hold_days: int = MAX_HOLD_DAYS,
    evaluation_start_idx: int | None = None,
    costs: TradingCosts = DEFAULT_COSTS,
    sample: str = "full",
    vnindex_dates=None,
) -> BacktestResult:
    """Walk-forward thuần Python trên chuỗi đã có sẵn (không tự fetch mạng) -
    tách riêng khỏi run_backtest() để test được bằng dữ liệu tổng hợp, không
    cần mạng thật."""
    n = len(closes)
    vn_ends = feat.benchmark_end_indices(dates, vnindex_dates, n, len(vnindex_closes))
    trades: list[Trade] = []
    buy_signals = 0
    total_days_evaluated = 0
    open_trade: dict | None = None
    pending: dict | None = None  # tín hiệu BUY vừa phát hiện, chờ vào lệnh ở phiên kế tiếp

    for i in range(max(min_bars, evaluation_start_idx or min_bars), n):
        date_i = dates[i] if i < len(dates) and dates else str(i)

        if pending is not None:
            # Tín hiệu phát hiện ở phiên (i-1) dựa trên close của chính phiên
            # đó - không thể vào lệnh NGAY tại đúng mức giá vừa dùng để ra
            # tín hiệu. Nếu phiên kế tiếp gap qua stop/target thì kế hoạch cũ
            # đã mất hiệu lực: bỏ lệnh thay vì tạo trade có target nằm dưới
            # entry (có thể sinh target_hit nhưng R âm).
            entry_raw = closes[i]
            if entry_raw <= pending["stop"] or entry_raw >= pending["target"]:
                pending = None
                continue
            open_trade = {
                "entry_idx": i,
                "entry_raw": entry_raw,
                "stop": pending["stop"],
                "target": pending["target"],
                "confidence": pending["confidence"],
                "setup_type": pending["setup_type"],
                "date": date_i,
            }
            pending = None
            continue  # phiên vào lệnh không xét luôn stop/target cùng lúc

        if open_trade is not None:
            held_days = i - open_trade["entry_idx"]
            if held_days < SETTLEMENT_BARS:
                # hàng chưa về (T+2.5 ở VN) - không thể bán trước phiên thứ 3
                # sau entry dù giá đã chạm stop/target trong lúc chờ.
                continue
            hit_stop = lows[i] <= open_trade["stop"]
            hit_target = highs[i] >= open_trade["target"]
            if hit_stop:
                # cả 2 chạm cùng phiên: không biết thứ tự thật trong ngày,
                # bảo thủ giả định stop chạm trước (kỷ luật rủi ro > kỳ vọng
                # lời). exit_price = min(closes[i], stop) vì (a) entry là close
                # phiên KẾ TIẾP tín hiệu, khác giá phiên tín hiệu dùng để tính
                # stop; (b) sau khi thêm T+2.5, lỗ thật có thể sâu hơn stop nếu
                # giá gap qua trong lúc chờ hàng về - risk tính từ entry thật.
                exit_raw = min(closes[i], open_trade["stop"])
                r, entry_value, exit_value = _net_r(
                    open_trade["entry_raw"], exit_raw, open_trade["stop"], costs
                )
                trades.append(
                    Trade(
                        symbol,
                        open_trade["date"],
                        entry_value,
                        date_i,
                        exit_value,
                        "stop_hit",
                        r,
                        open_trade["confidence"],
                        open_trade["setup_type"],
                    )
                )
                open_trade = None
            elif hit_target:
                r, entry_value, exit_value = _net_r(
                    open_trade["entry_raw"], open_trade["target"], open_trade["stop"], costs
                )
                trades.append(
                    Trade(
                        symbol,
                        open_trade["date"],
                        entry_value,
                        date_i,
                        exit_value,
                        "target_hit",
                        r,
                        open_trade["confidence"],
                        open_trade["setup_type"],
                    )
                )
                open_trade = None
            elif held_days >= max_hold_days:
                r, entry_value, exit_value = _net_r(
                    open_trade["entry_raw"], closes[i], open_trade["stop"], costs
                )
                trades.append(
                    Trade(
                        symbol,
                        open_trade["date"],
                        entry_value,
                        date_i,
                        exit_value,
                        "timeout",
                        r,
                        open_trade["confidence"],
                        open_trade["setup_type"],
                    )
                )
                open_trade = None
            continue  # không xét tín hiệu mới khi đang có lệnh mở

        d = _evaluate_day(
            symbol,
            closes,
            highs,
            lows,
            volumes,
            dates,
            i,
            vnindex_closes,
            vnindex_highs,
            vnindex_lows,
            vnindex_volumes or [],
            vn_end=vn_ends[i],
        )
        total_days_evaluated += 1
        if d.action == "BUY" and d.stop_price is not None and d.target_price is not None:
            buy_signals += 1
            pending = {
                "stop": d.stop_price,
                "target": d.target_price,
                "confidence": d.confidence,
                "setup_type": d.setup_type,
            }

    if open_trade is not None:
        trades.append(
            Trade(
                symbol,
                open_trade["date"],
                _entry_cost(open_trade["entry_raw"], costs),
                None,
                None,
                "open",
                None,
                open_trade["confidence"],
                open_trade["setup_type"],
            )
        )

    closed = [t for t in trades if t.r_multiple is not None]
    win_rate = (
        round(sum(1 for t in closed if t.r_multiple > 0) / len(closed) * 100, 1) if closed else None
    )
    avg_r = round(sum(t.r_multiple for t in closed) / len(closed), 2) if closed else None

    return BacktestResult(
        symbol=symbol,
        trades=trades,
        win_rate=win_rate,
        avg_r=avg_r,
        total_days_evaluated=total_days_evaluated,
        buy_signals=buy_signals,
        sample=sample,
    )


def run_out_of_sample_on_series(
    symbol,
    closes,
    highs,
    lows,
    volumes,
    dates,
    vnindex_closes,
    vnindex_highs,
    vnindex_lows,
    vnindex_volumes=None,
    *,
    test_ratio=OUT_OF_SAMPLE_RATIO,
    costs=DEFAULT_COSTS,
    vnindex_dates=None,
):
    """Tách phần đầu (train) và phần cuối (out_of_sample) theo thời gian.

    LƯU Ý: policy KHÔNG có tham số nào được fit trên phần train - "train" ở
    đây chỉ là báo cáo hiệu suất giai đoạn đầu để so với giai đoạn sau, không
    phải huấn luyện. VNINDEX luôn truyền ĐỦ chuỗi, việc chặn tương lai do căn
    theo ngày trong run_backtest_on_series đảm nhiệm."""
    split = max(MIN_BARS_TO_START + 1, min(len(closes) - 1, int(len(closes) * (1 - test_ratio))))
    if vnindex_dates:
        train = run_backtest_on_series(
            symbol,
            closes[:split],
            highs[:split],
            lows[:split],
            volumes[:split],
            dates[:split],
            vnindex_closes,
            vnindex_highs,
            vnindex_lows,
            vnindex_volumes or [],
            costs=costs,
            sample="train",
            vnindex_dates=vnindex_dates,
        )
    else:
        train = run_backtest_on_series(
            symbol,
            closes[:split],
            highs[:split],
            lows[:split],
            volumes[:split],
            dates[:split],
            vnindex_closes[:split],
            vnindex_highs[:split],
            vnindex_lows[:split],
            (vnindex_volumes or [])[:split],
            costs=costs,
            sample="train",
        )
    test = run_backtest_on_series(
        symbol,
        closes,
        highs,
        lows,
        volumes,
        dates,
        vnindex_closes,
        vnindex_highs,
        vnindex_lows,
        vnindex_volumes or [],
        evaluation_start_idx=split,
        costs=costs,
        sample="out_of_sample",
        vnindex_dates=vnindex_dates,
    )
    return OutOfSampleResult(split, train, test)


async def run_backtest(
    symbols: list[str] | None = None, days: int = DEFAULT_BACKTEST_DAYS
) -> list[BacktestResult]:
    """Run offline research only. Render is denied by default to protect the web service.

    Even with the explicit Render override, work is capped at 50 symbols,
    three years and concurrency two. All runtimes submit bounded batches so
    a full universe does not create thousands of in-memory tasks at once.
    """
    import asyncio

    assert_backtest_runtime_allowed()
    symbols = list(symbols or await providers.fetch_symbol_universe())
    days, max_symbols, concurrency = backtest_runtime_limits(days, len(symbols))
    symbols = symbols[:max_symbols]
    vnindex_series = await providers.fetch_ohlcv("VNINDEX", days=days)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(sym):
        async with semaphore:
            series = await providers.fetch_ohlcv(sym, days=days)
        if len(series.closes) < MIN_BARS_TO_START + 5:
            return None
        return run_out_of_sample_on_series(
            sym,
            series.closes,
            series.highs,
            series.lows,
            series.volumes,
            series.dates,
            vnindex_series.closes,
            vnindex_series.highs,
            vnindex_series.lows,
            vnindex_series.volumes,
            vnindex_dates=vnindex_series.dates,
        ).test

    results = []
    for start in range(0, len(symbols), BACKTEST_BATCH_SIZE):
        batch = await asyncio.gather(
            *(one(sym) for sym in symbols[start : start + BACKTEST_BATCH_SIZE])
        )
        results.extend(result for result in batch if result is not None)
    return results


def format_backtest_summary(results: list[BacktestResult]) -> str:
    lines = ["=== BACKTEST SUMMARY (chỉ tín hiệu BUY, xem giới hạn trong docstring module) ==="]
    for r in results:
        if r.buy_signals == 0:
            lines.append(f"{r.symbol}: 0 tín hiệu BUY / {r.total_days_evaluated} phiên xét")
            continue
        lines.append(
            f"{r.symbol}: {r.buy_signals} BUY / {r.total_days_evaluated} phiên xét | "
            f"win rate {r.win_rate}% | avg R {r.avg_r}"
        )
    return "\n".join(lines)


async def refresh_setup_stats(
    symbols: list[str] | None = None, days: int = DEFAULT_BACKTEST_DAYS
) -> dict:
    """Chạy backtest trên tập mã đại diện rồi ghi thống kê per-setup_type ra
    BACKTEST_STATS_PATH - gọi THỦ CÔNG/ĐỊNH KỲ (vd cron riêng), KHÔNG nằm
    trên đường dẫn phục vụ chat (build_prompt chỉ đọc file này qua
    load_setup_stats/format_setup_stats_line)."""
    results = await run_backtest(symbols, days=days)
    save_setup_stats(results)
    stats = load_setup_stats()
    logger.info("Đã lưu backtest_stats.json: %s", stats)
    return stats


if __name__ == "__main__":
    import asyncio

    asyncio.run(refresh_setup_stats())
