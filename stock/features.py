"""Feature layer: chỉ tính toán chỉ báo/tín hiệu trên OHLCV, KHÔNG kết luận
BUY/SELL/NO_TRADE. Mọi ngưỡng diễn giải (setup đẹp/xấu, regime tốt/xấu...)
thuộc về stock_policy.py.

Giữ nguyên các hàm chỉ báo thuần toán từng có ở phiên bản cũ hơn (đã có unit
test tính tay), bổ sung ATR/Donchian breakout/volume percentile/signal
agreement theo yêu cầu Stock.md.
"""
from dataclasses import dataclass, field


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def round_price(v: float) -> float:
    return round(v / 10) * 10


# ─── RSI (Wilder) ────────────────────────────────────────────────────────────

def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    """Trả None khi chưa đủ dữ liệu - KHÔNG bịa giá trị 50 (trung tính) trông
    như một chỉ báo thật, vì nó sẽ chảy vào scoring như thể có dữ liệu."""
    if len(closes) < period + 1:
        return None
    avg_gain = avg_loss = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            avg_gain += diff
        else:
            avg_loss += abs(diff)
    avg_gain /= period
    avg_loss /= period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff >= 0 else 0
        loss = abs(diff) if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


# ─── Momentum (hồi quy tuyến tính) ───────────────────────────────────────────

def calc_momentum_slope(closes: list[float], period: int = 10) -> float:
    s = closes[-period:]
    n = len(s)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = sum(s) / n
    num = sum((i - x_mean) * (s[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return (num / den / s[-1]) * 100 if den and s[-1] else 0.0


# ─── SMA / EMA ───────────────────────────────────────────────────────────────

def calc_sma(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    sl = closes[-period:]
    return sum(sl) / period


def _ema_series(closes: list[float], period: int) -> list[float]:
    if not closes:
        return []
    alpha = 2 / (period + 1)
    if len(closes) < period:
        seed = sum(closes) / len(closes)
        return [seed] * len(closes)
    result = [0.0] * len(closes)
    ema = sum(closes[:period]) / period
    for i in range(period):
        result[i] = ema
    for i in range(period, len(closes)):
        ema = closes[i] * alpha + ema * (1 - alpha)
        result[i] = ema
    return result


def calc_ema(closes: list[float], period: int) -> float:
    series = _ema_series(closes, period)
    return series[-1] if series else 0.0


# ─── MACD(12,26,9) ───────────────────────────────────────────────────────────

@dataclass
class MACDResult:
    macd_line: float = 0.0
    signal_line: float = 0.0
    histogram: float = 0.0
    crossover: str = "none"  # bullish | bearish | none


def calc_macd(closes: list[float]) -> MACDResult:
    # Cần >= 26 phiên để có MACD line, và thêm 9 phiên nữa để signal_line là
    # EMA9 THẬT trên chuỗi MACD (không rơi vào nhánh seed trung bình phẳng
    # của _ema_series khi valid_macd quá ngắn) - dưới 35 phiên, histogram bị
    # méo nhưng vẫn "available" như tín hiệu thật, chảy sai vào
    # calc_signal_agreement (C1).
    if len(closes) < 35:
        return MACDResult()
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_series = [a - b for a, b in zip(ema12, ema26)]
    valid_macd = macd_series[26:]
    signal_series = _ema_series(valid_macd, 9)

    macd_line = macd_series[-1] if macd_series else 0.0
    signal_line = signal_series[-1] if signal_series else 0.0
    histogram = macd_line - signal_line

    crossover = "none"
    if len(signal_series) >= 2:
        prev_hist = macd_series[-2] - signal_series[-2]
        if prev_hist < 0 and histogram > 0:
            crossover = "bullish"
        if prev_hist > 0 and histogram < 0:
            crossover = "bearish"

    return MACDResult(round(macd_line, 2), round(signal_line, 2), round(histogram, 2), crossover)


# ─── Bollinger Bands(20, 2σ) ─────────────────────────────────────────────────

@dataclass
class BollingerResult:
    upper: float
    middle: float
    lower: float
    width: float
    pct_b: float
    squeeze: bool
    available: bool = True


def calc_bollinger(closes: list[float], price: float) -> BollingerResult:
    """Khi lịch sử ngắn hơn period, KHÔNG bịa dải theo %price - trả về
    available=False để tầng policy biết đây không phải dữ liệu thật."""
    period = 20
    if len(closes) < period:
        return BollingerResult(price, price, price, 0.0, 50.0, False, available=False)
    sl = closes[-period:]
    middle = sum(sl) / period
    variance = sum((v - middle) ** 2 for v in sl) / period
    std_dev = variance ** 0.5
    upper = middle + 2 * std_dev
    lower = middle - 2 * std_dev
    width = ((upper - lower) / middle) * 100 if middle > 0 else 5.0
    pct_b = ((price - lower) / (upper - lower)) * 100 if upper != lower else 50.0
    return BollingerResult(
        round(upper), round(middle), round(lower), round(width, 2),
        round(clamp(pct_b, 0, 100), 1), width < 5, available=True,
    )


# ─── Multi-timeframe trend ───────────────────────────────────────────────────

@dataclass
class MultiTimeframe:
    trend_1w: float
    trend_1m: float
    trend_3m: float
    alignment: str  # bullish | bearish | mixed
    bars_used_3m: int = 0  # số phiên thật sự dùng để tính trend_3m (< 65 nếu dữ liệu ngắn)


def calc_multi_timeframe(closes: list[float]) -> MultiTimeframe:
    def pct(n: int) -> float:
        if len(closes) < n + 1:
            return 0.0
        past = closes[-1 - n]
        curr = closes[-1]
        return ((curr - past) / past) * 100 if past > 0 else 0.0

    bars_3m = min(len(closes) - 1, 65) if closes else 0
    t1w, t1m, t3m = pct(5), pct(22), pct(bars_3m)
    vals = [t1w, t1m, t3m]
    if all(v > 0 for v in vals):
        alignment = "bullish"
    elif all(v < 0 for v in vals):
        alignment = "bearish"
    else:
        alignment = "mixed"
    return MultiTimeframe(round(t1w, 2), round(t1m, 2), round(t3m, 2), alignment, bars_used_3m=max(bars_3m, 0))


# ─── SMA cross (golden/death) ────────────────────────────────────────────────

@dataclass
class CrossSignal:
    golden_cross: bool
    death_cross: bool
    above_sma20: bool
    above_sma50: bool


def calc_cross_signal(closes: list[float]) -> CrossSignal:
    price = closes[-1] if closes else 0.0
    if len(closes) < 52:
        return CrossSignal(False, False, False, False)
    sma20 = calc_sma(closes, 20)
    sma50 = calc_sma(closes, 50)
    window = 3
    golden = death = False
    for i in range(len(closes) - window, len(closes) - 1):
        s20p = calc_sma(closes[: i + 1], 20)
        s50p = calc_sma(closes[: i + 1], 50)
        s20c = calc_sma(closes[: i + 2], 20)
        s50c = calc_sma(closes[: i + 2], 50)
        if s20p <= s50p and s20c > s50c:
            golden = True
        if s20p >= s50p and s20c < s50c:
            death = True
    return CrossSignal(golden, death, price > sma20, price > sma50)


# ─── ADX(14) ──────────────────────────────────────────────────────────────────

@dataclass
class ADXResult:
    adx: float
    di_plus: float
    di_minus: float
    trending: bool
    available: bool = True


def _wilder_true_range_series(
    closes: list[float], highs: list[float], lows: list[float]
) -> tuple[list[float], list[float], list[float]]:
    trs, dm_plus, dm_minus = [], [], []
    for i in range(1, len(closes)):
        high, low, prev_close = highs[i], lows[i], closes[i - 1]
        prev_high, prev_low = highs[i - 1], lows[i - 1]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        up_move = high - prev_high
        down_move = prev_low - low
        dm_plus.append(up_move if (up_move > down_move and up_move > 0) else 0)
        dm_minus.append(down_move if (down_move > up_move and down_move > 0) else 0)
    return trs, dm_plus, dm_minus


def _has_real_hl(closes: list[float], highs: list[float] | None, lows: list[float] | None) -> bool:
    """stock_providers.py fallback khi thiếu H/L thật của 1 phiên là gán
    high = low = close cho ĐÚNG phiên đó - chỉ check độ dài như cũ sẽ để lọt
    toàn bộ chuỗi H/L giả (mọi phiên high == low) vào ADX/ATR, trông như chỉ
    báo thật trong khi thực chất tính trên số bịa. Một vài phiên đứng giá
    thật sự có thể có high == low, nhưng nếu ĐA SỐ phiên đều vậy thì gần như
    chắc chắn là H/L giả toàn chuỗi."""
    if not highs or not lows or len(highs) != len(closes) or len(lows) != len(closes):
        return False
    bars_with_real_range = sum(1 for h, l in zip(highs, lows) if h > l)
    return bars_with_real_range >= len(closes) * 0.5


def calc_adx(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None, period: int = 14) -> ADXResult:
    """Chỉ tính ADX khi có high/low THẬT (không tự tổng hợp H/L từ close - số
    bịa trông như chỉ báo thật sẽ chảy vào scoring một cách âm thầm). Nếu
    thiếu dữ liệu (không đủ phiên hoặc không có H/L thật) -> available=False,
    và tầng policy phải loại ADX khỏi mọi gate dựa vào nó."""
    has_real = _has_real_hl(closes, highs, lows)
    if len(closes) < period * 2 or not has_real:
        return ADXResult(0.0, 0.0, 0.0, False, available=False)

    trs, dm_plus, dm_minus = _wilder_true_range_series(closes, highs, lows)

    def smooth(arr: list[float]) -> list[float]:
        res = [0.0] * len(arr)
        if len(arr) < period:
            return res
        res[period - 1] = sum(arr[:period])
        for i in range(period, len(arr)):
            res[i] = res[i - 1] - res[i - 1] / period + arr[i]
        return res

    atr = smooth(trs)
    sdm_plus = smooth(dm_plus)
    sdm_minus = smooth(dm_minus)

    di_plus_series = [(v / atr[i] * 100) if atr[i] > 0 else 0 for i, v in enumerate(sdm_plus)]
    di_minus_series = [(v / atr[i] * 100) if atr[i] > 0 else 0 for i, v in enumerate(sdm_minus)]
    dx_series = []
    for i, v in enumerate(di_plus_series):
        s = v + di_minus_series[i]
        dx_series.append((abs(v - di_minus_series[i]) / s * 100) if s > 0 else 0)

    valid_dx = dx_series[period - 1:]
    if len(valid_dx) < period:
        return ADXResult(0.0, 0.0, 0.0, False, available=False)
    adx = sum(valid_dx[-period:]) / period
    di_plus = di_plus_series[-1] if di_plus_series else 0.0
    di_minus = di_minus_series[-1] if di_minus_series else 0.0

    return ADXResult(round(adx, 1), round(di_plus, 1), round(di_minus, 1), adx > 25, available=True)


# ─── ATR(14) - dùng cho stop/target theo biến động thật, không phải %price ──

def calc_atr(closes: list[float], highs: list[float] | None, lows: list[float] | None, period: int = 14) -> float | None:
    """Average True Range (Wilder). Trả None nếu thiếu H/L thật hoặc chưa đủ
    dữ liệu - KHÔNG suy ra ATR xấp xỉ từ %price vì nó che giấu việc thiếu dữ
    liệu thật, giống lý do calc_adx từ chối bịa H/L."""
    if not _has_real_hl(closes, highs, lows) or len(closes) < period + 1:
        return None
    trs, _, _ = _wilder_true_range_series(closes, highs, lows)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return round(atr, 2)


# ─── Donchian breakout state ─────────────────────────────────────────────────

@dataclass
class DonchianState:
    upper: float | None
    lower: float | None
    state: str  # breakout_up | breakout_down | inside | unknown


def calc_donchian_breakout(highs: list[float], lows: list[float], closes: list[float], period: int = 20) -> DonchianState:
    """So giá đóng cửa hiện tại với kênh Donchian ĐƯỢC TÍNH TRƯỚC phiên hiện
    tại (loại bar cuối ra khỏi kênh) - nếu tính cả bar cuối, breakout sẽ
    không bao giờ xảy ra vì bar cuối luôn nằm trong chính kênh của nó."""
    if len(highs) < period + 1 or len(lows) < period + 1 or not closes:
        return DonchianState(None, None, "unknown")
    prior_highs = highs[-period - 1:-1]
    prior_lows = lows[-period - 1:-1]
    upper, lower = max(prior_highs), min(prior_lows)
    price = closes[-1]
    if price > upper:
        state = "breakout_up"
    elif price < lower:
        state = "breakout_down"
    else:
        state = "inside"
    return DonchianState(round(upper), round(lower), state)


# ─── Support/Resistance, Bias MA, MA alignment, trend score ─────────────────

@dataclass
class SupportResistance:
    support: float | None
    resistance: float | None
    dist_to_support: float
    dist_to_resistance: float


def calc_support_resistance(highs: list[float], lows: list[float], price: float, lookback: int = 30) -> SupportResistance:
    length = min(len(highs), len(lows), lookback)
    if length == 0:
        return SupportResistance(None, None, 0.0, 0.0)
    recent_highs = highs[-length:]
    recent_lows = lows[-length:]
    resistance = max(recent_highs)
    support = min(recent_lows)
    dist_support = round((price - support) / support * 100, 1) if support > 0 else 0.0
    dist_resistance = round((price - resistance) / resistance * 100, 1) if resistance > 0 else 0.0
    return SupportResistance(round(support), round(resistance), dist_support, dist_resistance)


# ─── B1: S/R theo swing pivot + clustering (thay calc_support_resistance thô
#     max/min - giữ hàm cũ làm fallback khi không tìm được pivot nào) ───────

@dataclass
class PriceLevel:
    price: float
    touches: int        # số lần giá test vùng này
    kind: str            # support | resistance
    strength: float      # 0..1: kết hợp touches + độ gần giá hiện tại


@dataclass
class KeyLevels:
    supports: list[PriceLevel] = field(default_factory=list)     # sort gần giá nhất trước
    resistances: list[PriceLevel] = field(default_factory=list)  # sort gần giá nhất trước


def _find_swing_pivots(highs: list[float], lows: list[float], window: int) -> tuple[list[float], list[float]]:
    """Swing high tại i khi highs[i] == max(highs[i-w:i+w+1]); swing low
    tương tự. KHÔNG dùng bar cuối cùng (chưa xác nhận - cần w phiên sau đó
    để biết đó thực sự là đỉnh/đáy cục bộ)."""
    n = len(highs)
    swing_highs, swing_lows = [], []
    # range dừng trước bar cuối window để không xét các bar chưa đủ w phiên
    # xác nhận phía sau (bao gồm luôn bar cuối cùng của toàn chuỗi).
    for i in range(window, n - window):
        h_window = highs[i - window: i + window + 1]
        l_window = lows[i - window: i + window + 1]
        if highs[i] == max(h_window):
            swing_highs.append(highs[i])
        if lows[i] == min(l_window):
            swing_lows.append(lows[i])
    return swing_highs, swing_lows


def _cluster_pivots(pivots: list[float], cluster_pct: float) -> list[tuple[float, int]]:
    """Gom cụm các pivot cách nhau <= cluster_pct%: giá cụm = trung bình có
    trọng số theo số lần test, touches cộng dồn. Trả list (giá_cụm, touches)."""
    if not pivots:
        return []
    clusters: list[list[float]] = []
    for p in sorted(pivots):
        placed = False
        for cluster in clusters:
            cluster_avg = sum(cluster) / len(cluster)
            if cluster_avg > 0 and abs(p - cluster_avg) / cluster_avg * 100 <= cluster_pct:
                cluster.append(p)
                placed = True
                break
        if not placed:
            clusters.append([p])
    return [(sum(c) / len(c), len(c)) for c in clusters]


def find_key_levels(
    highs: list[float], lows: list[float], closes: list[float],
    lookback: int = 60, pivot_window: int = 3, cluster_pct: float = 1.5,
) -> KeyLevels:
    min_bars = pivot_window * 2 + 2
    if len(highs) < min_bars or len(lows) < min_bars or not closes:
        return KeyLevels([], [])

    length = min(len(highs), len(lows), lookback)
    w_highs = highs[-length:]
    w_lows = lows[-length:]
    price = closes[-1]

    swing_highs, swing_lows = _find_swing_pivots(w_highs, w_lows, pivot_window)
    high_clusters = _cluster_pivots(swing_highs, cluster_pct)
    low_clusters = _cluster_pivots(swing_lows, cluster_pct)

    def _to_level(cluster_price: float, touches: int, kind: str) -> PriceLevel:
        proximity = clamp(1 - abs(price - cluster_price) / price / 0.15, 0, 1) if price > 0 else 0.0
        strength = clamp(touches / 4, 0, 1) * 0.6 + proximity * 0.4
        return PriceLevel(round_price(cluster_price), touches, kind, round(strength, 2))

    # supports: chỉ giữ mốc DƯỚI giá hiện tại; resistances: chỉ giữ mốc TRÊN.
    supports = [_to_level(p, t, "support") for p, t in low_clusters if p < price]
    resistances = [_to_level(p, t, "resistance") for p, t in high_clusters if p > price]

    supports.sort(key=lambda lv: price - lv.price)
    resistances.sort(key=lambda lv: lv.price - price)

    return KeyLevels(supports=supports, resistances=resistances)


@dataclass
class BiasMA:
    bias: float
    status: str  # nguy_hiem | canh_giac | an_toan | chiet_khau | qua_ban


def calc_bias_ma(price: float, ma: float) -> BiasMA:
    if ma <= 0:
        return BiasMA(0.0, "an_toan")
    bias = round((price - ma) / ma * 100, 2)
    if bias > 8:
        status = "nguy_hiem"
    elif bias > 5:
        status = "canh_giac"
    elif bias < -8:
        status = "qua_ban"
    elif bias < -5:
        status = "chiet_khau"
    else:
        status = "an_toan"
    return BiasMA(bias, status)


def calc_distance_pct(price: float, level: float) -> float | None:
    """Khoảng cách % từ giá tới 1 mốc bất kỳ (MA20/MA50/support...) - dùng
    chung cho mọi mốc thay vì viết lại công thức từng nơi."""
    if level <= 0:
        return None
    return round((price - level) / level * 100, 2)


@dataclass
class MAAlignment:
    ma5: float
    ma10: float
    ma20: float
    alignment: str  # bullish | bearish | mixed | unknown
    is_bullish: bool | None


def calc_ma_alignment(closes: list[float]) -> MAAlignment:
    if len(closes) < 20:
        return MAAlignment(0, 0, 0, "unknown", None)
    ma5, ma10, ma20 = calc_sma(closes, 5), calc_sma(closes, 10), calc_sma(closes, 20)
    if ma5 > ma10 > ma20:
        alignment, is_bullish = "bullish", True
    elif ma5 < ma10 < ma20:
        alignment, is_bullish = "bearish", False
    else:
        alignment, is_bullish = "mixed", None
    return MAAlignment(round(ma5), round(ma10), round(ma20), alignment, is_bullish)


def calc_trend_score(ma_align: MAAlignment, rsi14: float | None, macd_histogram: float) -> int:
    """Điểm mô tả 0-100 (càng cao càng thiên tăng) - đây là FEATURE mô tả
    trạng thái trend, không phải quyết định action; tầng policy tự diễn giải
    ngưỡng nào là 'đủ tốt' cho từng gate."""
    score = 50
    if ma_align.alignment == "bullish":
        score += 20
    elif ma_align.alignment == "bearish":
        score -= 20
    elif ma_align.ma5 > ma_align.ma10 or ma_align.ma10 > ma_align.ma20:
        score += 5
    else:
        score -= 5

    if rsi14 is not None:
        if rsi14 > 70:
            score -= 8
        elif rsi14 < 30:
            score += 8
        elif rsi14 > 55:
            score += 5
        elif rsi14 < 45:
            score -= 5

    score += 5 if macd_histogram > 0 else -5
    return int(max(0, min(100, round(score))))


# ─── Thanh khoản: volume hiện tại vs trung bình 20 phiên + percentile ───────

@dataclass
class Liquidity:
    avg_volume_20: float
    current_volume: float
    liquidity_ratio_pct: float  # current vs avg20, 100 = bằng trung bình
    volume_percentile: float  # 0-100, percentile của volume hiện tại trong lịch sử gần đây
    is_thin: bool  # thanh khoản quá thấp - khuyến nghị mua/bán gần như vô nghĩa vì khó vào/ra


def _percentile_rank(current: float, history: list[float]) -> float:
    if not history:
        return 50.0
    below_or_equal = sum(1 for h in history if h <= current)
    return round(below_or_equal / len(history) * 100, 1)


def calc_distribution_days(closes: list[float], volumes: list[float], lookback: int = 25) -> int:
    """Đếm 'ngày phân phối' chuẩn O'Neil trong `lookback` phiên gần nhất: 1
    phiên giảm > 0.2% kèm volume cao hơn phiên liền trước = 1 ngày phân phối
    - tín hiệu tổ chức lớn đang bán ra dù giá chưa xác nhận downtrend rõ.
    Đây là FEATURE thuần đếm số liệu; ngưỡng bao nhiêu ngày thì coi là xấu
    thuộc về policy (Gate A)."""
    n = min(len(closes), len(volumes))
    if n < 2:
        return 0
    c, v = closes[-n:], volumes[-n:]
    window_start = max(1, len(c) - lookback)
    count = 0
    for i in range(window_start, len(c)):
        if c[i - 1] <= 0:
            continue
        change_pct = (c[i] - c[i - 1]) / c[i - 1] * 100
        if change_pct <= -0.2 and v[i] > v[i - 1]:
            count += 1
    return count


def calc_liquidity(volumes: list[float], min_avg_volume: float = 100_000, percentile_lookback: int = 60) -> Liquidity | None:
    """So khối lượng khớp phiên gần nhất với trung bình 20 phiên + percentile
    trong `percentile_lookback` phiên gần nhất.

    Đây là cảnh báo broker luôn nêu đầu tiên: mã thanh khoản thấp thì
    "mua/bán" gần như vô nghĩa vì không đủ đối ứng để vào/ra ở khối lượng
    đáng kể. `min_avg_volume` là ngưỡng tuyệt đối (cổ phiếu/phiên) để gắn cờ
    is_thin - 100k là ước lượng thô cho midcap/penny, có thể chỉnh theo khẩu vị.
    """
    if not volumes:
        return None
    window = volumes[-20:] if len(volumes) >= 20 else volumes
    if not window:
        return None
    avg20 = sum(window) / len(window)
    current = volumes[-1]
    ratio = (current / avg20) * 100 if avg20 > 0 else 0.0
    history = volumes[-percentile_lookback - 1:-1] if len(volumes) > 1 else []
    percentile = _percentile_rank(current, history)
    return Liquidity(
        avg_volume_20=round(avg20),
        current_volume=round(current),
        liquidity_ratio_pct=round(ratio, 1),
        volume_percentile=percentile,
        is_thin=avg20 < min_avg_volume,
    )


# ─── Signal stats tổng hợp (trend/momentum/volume/volatility) ──────────────

@dataclass
class SignalStats:
    trend_3m: float
    volatility: float
    momentum: float
    volume_trend: float
    rsi14: float | None


def calc_signal_stats(closes: list[float], volumes: list[float], price: float) -> SignalStats:
    if not closes or price <= 0:
        return SignalStats(0, 2, 0, 0, None)

    first, last = closes[0], closes[-1]
    trend_3m = ((last - first) / first) * 100 if first else 0
    momentum = calc_momentum_slope(closes)
    rsi14 = calc_rsi(closes)

    volume_trend = 0.0
    if len(volumes) >= 5:
        avg_vol = sum(volumes) / len(volumes)
        recent_vol = sum(volumes[-5:]) / 5
        volume_trend = ((recent_vol - avg_vol) / avg_vol) * 100 if avg_vol > 0 else 0

    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        volatility = (max(variance, 0) ** 0.5) * 100 * (5 ** 0.5)
    else:
        volatility = 2.0

    return SignalStats(
        round(trend_3m, 2), round(volatility, 2), round(momentum, 2), round(volume_trend, 2),
        round(rsi14, 1) if rsi14 is not None else None,
    )


# ─── Session metrics thuần (không phán xét is_selloff/hard_no_buy - tầng
#     policy tự áp ngưỡng để giữ feature layer chỉ tính toán) ────────────────

@dataclass
class SessionMetrics:
    daily_change_pct: float
    close_position_pct: float  # 0 = đóng sát đáy phiên, 100 = đóng sát đỉnh phiên
    volume_ratio_pct: float  # volume phiên gần nhất vs TB20 phiên trước đó


def calc_session_metrics(closes: list[float], highs: list[float], lows: list[float], volumes: list[float]) -> SessionMetrics | None:
    if len(closes) < 2:
        return None
    price, prev_close = closes[-1], closes[-2]
    daily_change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0

    high = highs[-1] if highs else price
    low = lows[-1] if lows else price
    close_position_pct = (price - low) / (high - low) * 100 if high > low else 50.0

    volume_ratio_pct = 100.0
    if volumes:
        prior = volumes[-21:-1] if len(volumes) >= 21 else volumes[:-1]
        if prior:
            avg20 = sum(prior) / len(prior)
            volume_ratio_pct = volumes[-1] / avg20 * 100 if avg20 > 0 else 100.0

    return SessionMetrics(
        daily_change_pct=round(daily_change_pct, 2),
        close_position_pct=round(close_position_pct, 1),
        volume_ratio_pct=round(volume_ratio_pct, 1),
    )


# ─── Enhanced indicators bundle + signal agreement + summary text ──────────

@dataclass
class EnhancedIndicators:
    macd: MACDResult
    bollinger: BollingerResult
    multi_tf: MultiTimeframe
    cross: CrossSignal
    adx: ADXResult
    donchian: DonchianState
    sma20: float
    sma50: float
    ema9: float
    atr14: float | None
    atr_pct: float | None  # ATR như % giá - dùng để so sánh biến động giữa các mã


def build_enhanced_indicators(closes: list[float], price: float, highs: list[float] | None = None, lows: list[float] | None = None) -> EnhancedIndicators:
    atr14 = calc_atr(closes, highs, lows)
    atr_pct = round(atr14 / price * 100, 2) if atr14 is not None and price > 0 else None
    return EnhancedIndicators(
        macd=calc_macd(closes),
        bollinger=calc_bollinger(closes, price),
        multi_tf=calc_multi_timeframe(closes),
        cross=calc_cross_signal(closes),
        adx=calc_adx(closes, highs, lows),
        donchian=calc_donchian_breakout(highs or [], lows or [], closes),
        sma20=round(calc_sma(closes, 20)),
        sma50=round(calc_sma(closes, 50)),
        ema9=round(calc_ema(closes, 9)),
        atr14=atr14,
        atr_pct=atr_pct,
    )


def calc_signal_agreement(ind: EnhancedIndicators) -> float:
    """Điểm đồng thuận -1..1 giữa các tín hiệu kỹ thuật hiện có (chỉ đếm
    tín hiệu THẬT SỰ available, không đếm None/unavailable như trung tính
    ẩn). +1 = mọi tín hiệu đồng thuận tăng, -1 = mọi tín hiệu đồng thuận
    giảm, gần 0 = tín hiệu mâu thuẫn (feature quan trọng cho Gate C: setup
    mâu thuẫn mạnh không nên vào lệnh dù điểm tổng có vẻ ổn)."""
    votes: list[float] = []

    if ind.macd.crossover == "bullish":
        votes.append(1.0)
    elif ind.macd.crossover == "bearish":
        votes.append(-1.0)
    elif ind.macd.histogram != 0:
        votes.append(1.0 if ind.macd.histogram > 0 else -1.0)

    if ind.multi_tf.alignment == "bullish":
        votes.append(1.0)
    elif ind.multi_tf.alignment == "bearish":
        votes.append(-1.0)

    if ind.cross.golden_cross:
        votes.append(1.0)
    if ind.cross.death_cross:
        votes.append(-1.0)
    if not ind.cross.golden_cross and not ind.cross.death_cross:
        votes.append(1.0 if (ind.cross.above_sma20 and ind.cross.above_sma50) else (-1.0 if not ind.cross.above_sma20 and not ind.cross.above_sma50 else 0.0))

    if ind.adx.available and ind.adx.trending:
        votes.append(1.0 if ind.adx.di_plus > ind.adx.di_minus else -1.0)

    if ind.donchian.state == "breakout_up":
        votes.append(1.0)
    elif ind.donchian.state == "breakout_down":
        votes.append(-1.0)

    if not votes:
        return 0.0
    return round(sum(votes) / len(votes), 2)


def build_indicator_summary(ind: EnhancedIndicators, symbol: str) -> str:
    tf, macd, bb, cross, adx = ind.multi_tf, ind.macd, ind.bollinger, ind.cross, ind.adx
    lines = []
    lines.append(
        f"Multi-TF [{symbol}]: 1W {'+' if tf.trend_1w > 0 else ''}{tf.trend_1w}% | "
        f"1M {'+' if tf.trend_1m > 0 else ''}{tf.trend_1m}% | "
        f"3M {'+' if tf.trend_3m > 0 else ''}{tf.trend_3m}% -> {tf.alignment.upper()}"
    )
    cross_note = f" ⚡ {macd.crossover.upper()} CROSSOVER" if macd.crossover != "none" else ""
    lines.append(f"MACD: line {'+' if macd.macd_line > 0 else ''}{macd.macd_line} | hist {'+' if macd.histogram > 0 else ''}{macd.histogram}{cross_note}")
    if bb.available:
        bb_note = " 🔥 SQUEEZE (sắp breakout)" if bb.squeeze else ""
        lines.append(f"BB: %B={bb.pct_b}% | width={bb.width}%{bb_note}")
    else:
        lines.append("BB: chưa đủ dữ liệu (cần tối thiểu 20 phiên)")
    cross_parts = []
    if cross.golden_cross:
        cross_parts.append("⭐ GOLDEN CROSS")
    if cross.death_cross:
        cross_parts.append("💀 DEATH CROSS")
    cross_parts.append("above SMA20" if cross.above_sma20 else "below SMA20")
    cross_parts.append("above SMA50" if cross.above_sma50 else "below SMA50")
    lines.append("SMA: " + " | ".join(cross_parts))
    if adx.available:
        lines.append(f"ADX: {adx.adx} ({'xu hướng mạnh' if adx.trending else 'sideway'}) | +DI {adx.di_plus} vs -DI {adx.di_minus}")
    else:
        lines.append("ADX: chưa đủ dữ liệu H/L thật")
    if ind.donchian.state != "unknown":
        donchian_note = {
            "breakout_up": "🚀 BREAKOUT lên trên kênh 20 phiên",
            "breakout_down": "⚠️ BREAKDOWN xuống dưới kênh 20 phiên",
            "inside": "trong kênh 20 phiên (chưa breakout)",
        }[ind.donchian.state]
        lines.append(f"Donchian(20): {donchian_note}")
    if ind.atr14 is not None:
        lines.append(f"ATR(14): {ind.atr14} ({ind.atr_pct}% giá)")
    return "\n".join(lines)
