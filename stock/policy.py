"""Policy layer - nhận feature đã tính xong (từ stock_features.py) + data
quality (từ stock_validation.py), trả về Decision đã chốt hoàn toàn.

KHÔNG gọi mạng, KHÔNG tính lại chỉ báo ở đây - mọi input phải là feature có
sẵn. Đây là nơi DUY NHẤT quyết định action; Gemini ở stock_analysis.py chỉ
được diễn giải Decision đã chốt, không được đổi action/con số.

Áp dụng 4 gate theo Stock.md:
  Gate A - market regime (VNINDEX risk-on/risk-off)
  Gate B - symbol/data quality (stock_validation.DataQuality)
  Gate C - setup quality (breakout/pullback/mean_reversion/none, agreement)
  Gate D - risk/reward (chỉ áp dụng khi cân nhắc BUY mới)
"""
from dataclasses import dataclass, field

from stock import features as feat
from stock.validation import DataQuality

CONFIDENCE_BUY_MIN = 0.75
CONFIDENCE_WATCH_MIN = 0.55
MIN_RR_RATIO = 1.5
DISTRIBUTION_DAY_THRESHOLD = 4  # >= 4 ngày phân phối / 25 phiên -> ép risk_off (chuẩn O'Neil)
_NEAR_CEILING_PCT = 5.5  # sát trần HOSE (7%) - cảnh báo rủi ro T+2.5 khi mua đuổi
_NEAR_CEILING_STRENGTH_PENALTY = 0.15

# B2: sizing theo % NAV
RISK_PER_TRADE_PCT = 1.0   # rủi ro tối đa mỗi lệnh = 1% NAV
MAX_POSITION_PCT = 20.0    # trần tỷ trọng 1 mã
MIN_POSITION_PCT = 2.0


@dataclass
class TradePlan:
    entry_low: float
    entry_high: float          # vùng mua ±1%, không phải 1 điểm
    stop: float
    target1: float             # kháng cự gần nhất (từ KeyLevels), chốt 1/2 tại đây
    target2: float | None      # kháng cự mạnh kế tiếp, phần còn lại
    position_size_pct: float   # % NAV đề xuất, suy ngược từ khoảng cách stop
    plan_note: str             # quy tắc: chốt 1/2 tại T1, dời stop về hoà vốn


def build_trade_plan(
    price: float, stop: float, target_price: float | None, confidence: float,
    key_levels: feat.KeyLevels | None, liquidity: feat.Liquidity | None,
) -> TradePlan | None:
    """B2 - suy ngược tỷ trọng vị thế từ khoảng cách rủi ro (không bịa số
    NAV thật - đây là % NAV ĐỀ XUẤT dựa trên nguyên tắc rủi ro cố định mỗi
    lệnh, người dùng tự đối chiếu với NAV thật của mình)."""
    if price <= 0 or stop is None or stop >= price:
        return None
    risk_pct = (price - stop) / price * 100
    if risk_pct <= 0:
        return None

    size = RISK_PER_TRADE_PCT / risk_pct * 100
    size *= 0.6 + 0.4 * feat.clamp((confidence - CONFIDENCE_BUY_MIN) / 0.25, 0, 1)
    if liquidity and liquidity.is_thin:
        size *= 0.5
    size = round(feat.clamp(size, MIN_POSITION_PCT, MAX_POSITION_PCT), 1)

    entry_low = feat.round_price(price * 0.99)
    entry_high = feat.round_price(price * 1.01)

    resistances = key_levels.resistances if key_levels else []
    if resistances:
        target1 = resistances[0].price
        target2 = resistances[1].price if len(resistances) > 1 else None
    else:
        # không có kháng cự thật dùng được - fallback về target_price hiện
        # tại (đã tính ở _compute_stop_target) thay vì bịa hệ số nhân mới.
        target1 = target_price
        target2 = None

    if target1 is None:
        return None

    return TradePlan(
        entry_low=entry_low, entry_high=entry_high, stop=stop,
        target1=target1, target2=target2, position_size_pct=size,
        plan_note="Chốt 1/2 vị thế tại T1, dời stop về hoà vốn cho phần còn lại; T2 (nếu có) cho phần còn lại.",
    )

# Ngưỡng session risk (port từ session-risk guardrail cũ, giờ là 1 phần của
# Gate C thay vì hậu xử lý action sau khi đã quyết định).
_SELLOFF_PCT = -4.0
_NEAR_FLOOR_PCT = -6.0
_CLOSE_NEAR_LOW_PCT = 25.0
_DISTRIBUTION_CHANGE_PCT = -2.5
_DISTRIBUTION_VOLUME_RATIO = 150.0


@dataclass
class Scenario:
    name: str        # base | bull | bear
    trigger: str     # điều kiện kích hoạt bằng lời, CÓ CON SỐ cụ thể
    action: str


@dataclass
class Decision:
    action: str              # BUY / HOLD / SELL / WATCH / NO_TRADE
    confidence: float        # 0.0 -> 1.0, có nghĩa vận hành: xem CONFIDENCE_*_MIN
    setup_type: str          # breakout / pullback / mean_reversion / none
    reasons: list[str]
    risk_level: str          # low / medium / high
    stop_price: float | None
    target_price: float | None
    rr_ratio: float | None
    invalidation_reason: str | None
    market_regime: str       # risk_on / neutral / risk_off / unknown
    data_quality: str        # ok / degraded / bad
    trade_plan: TradePlan | None = None
    scenarios: list[Scenario] = field(default_factory=list)


@dataclass
class PolicyInputs:
    price: float
    stats: feat.SignalStats
    enhanced: feat.EnhancedIndicators | None
    ma_alignment: feat.MAAlignment | None
    support_resistance: feat.SupportResistance | None
    liquidity: feat.Liquidity | None
    session: feat.SessionMetrics | None
    relative_strength: float
    trend_score: int | None
    news_impact: float
    quality: DataQuality
    vnindex_multi_tf: feat.MultiTimeframe | None
    vnindex_adx: feat.ADXResult | None
    vnindex_distribution_days: int = 0
    key_levels: feat.KeyLevels | None = None
    # True nếu user đang thật sự giữ mã này (vd đã ghi trong danh mục nhớ
    # dài hạn). Mặc định False = đang cân nhắc mở vị thế MỚI. 2 trường hợp
    # cần quyết định khác nhau: SELL chỉ có ý nghĩa khi đang giữ hàng (không
    # thể bán cái không có); tín hiệu chưa đủ rõ khi đang giữ nên là HOLD
    # (giữ nguyên, theo dõi) chứ không phải NO_TRADE (đứng ngoài, coi như
    # không có gì) - 2 hành động này có ý nghĩa vận hành khác hẳn nhau với
    # người đang có vị thế.
    is_holding: bool = False


def classify_market_regime(
    vnindex_multi_tf: feat.MultiTimeframe | None, vnindex_adx: feat.ADXResult | None,
    distribution_days: int = 0,
) -> str:
    """Gate A - risk_on/neutral/risk_off dựa trên xu hướng VNINDEX.

    Bảo thủ theo hướng risk_off: chỉ cần MỘT trong các tín hiệu sau (trend 3
    khung thời gian bearish, ADX xác nhận trending xuống, hoặc >= 4 ngày phân
    phối/25 phiên theo chuẩn O'Neil) là đủ để coi thị trường chung xấu và hạn
    chế BUY mới - vì cái giá của một BUY sai trong thị trường xấu thường nặng
    hơn cái giá bỏ lỡ một BUY đúng. Distribution days được ưu tiên kiểm tra
    trước vì nó có thể phát hiện phân phối ngầm TRƯỚC KHI xu hướng giá kịp
    quay đầu rõ ràng (alignment vẫn có thể chưa "bearish" khi distribution
    days đã chạm ngưỡng)."""
    if distribution_days >= DISTRIBUTION_DAY_THRESHOLD:
        return "risk_off"
    if vnindex_multi_tf is None:
        return "unknown"
    if vnindex_multi_tf.alignment == "bearish":
        return "risk_off"
    if vnindex_adx is not None and vnindex_adx.available and vnindex_adx.trending and vnindex_adx.di_minus > vnindex_adx.di_plus:
        return "risk_off"
    if vnindex_multi_tf.alignment == "bullish":
        return "risk_on"
    return "neutral"


@dataclass
class _SessionFlags:
    is_selloff: bool
    is_near_floor_like: bool
    is_close_near_low: bool
    is_distribution: bool
    hard_no_buy: bool
    reasons: list[str]


def _evaluate_session(session: feat.SessionMetrics | None, enhanced: feat.EnhancedIndicators | None) -> _SessionFlags:
    """Nhận diện phiên rủi ro cao để chặn BUY sai. Với cổ phiếu Việt Nam,
    volume bùng nổ trong phiên giảm mạnh/đóng sát đáy thường là phân phối
    hoặc force-sell, không phải tín hiệu gom hàng - khác với giả định "volume
    tăng luôn tích cực" của bản heuristic cũ."""
    reasons: list[str] = []
    if session is None:
        return _SessionFlags(False, False, False, False, False, reasons)

    is_selloff = session.daily_change_pct <= _SELLOFF_PCT
    is_near_floor_like = session.daily_change_pct <= _NEAR_FLOOR_PCT
    is_close_near_low = session.close_position_pct <= _CLOSE_NEAR_LOW_PCT and session.daily_change_pct < 0
    is_distribution = session.daily_change_pct <= _DISTRIBUTION_CHANGE_PCT and session.volume_ratio_pct >= _DISTRIBUTION_VOLUME_RATIO

    if is_near_floor_like:
        reasons.append(f"giảm rất mạnh {session.daily_change_pct:.2f}% trong phiên (gần sàn/đạp mạnh)")
    elif is_selloff:
        reasons.append(f"giảm mạnh {session.daily_change_pct:.2f}% trong phiên")
    if is_distribution:
        reasons.append(f"volume {session.volume_ratio_pct:.1f}% so với TB20 trong phiên giảm — dấu hiệu phân phối")
    if is_close_near_low:
        reasons.append(f"đóng cửa sát đáy phiên (vị trí close {session.close_position_pct:.1f}% biên độ ngày)")

    below_both_sma = bool(enhanced and not enhanced.cross.above_sma20 and not enhanced.cross.above_sma50)
    macd_bad = bool(enhanced and (enhanced.macd.crossover == "bearish" or enhanced.macd.histogram < 0))

    hard_no_buy = (
        is_near_floor_like
        or (is_selloff and is_distribution)
        or (is_distribution and is_close_near_low)
        or (below_both_sma and macd_bad)
    )
    if below_both_sma and macd_bad and not hard_no_buy:
        reasons.append("giá dưới cả SMA20/SMA50 và MACD bearish")

    return _SessionFlags(is_selloff, is_near_floor_like, is_close_near_low, is_distribution, hard_no_buy, reasons)


@dataclass
class _Bias:
    direction: str  # bullish / bearish / conflict / flat
    bull_votes: int
    bear_votes: int


def _classify_bias(inputs: PolicyInputs) -> _Bias:
    ma = inputs.ma_alignment
    enh = inputs.enhanced
    agreement = feat.calc_signal_agreement(enh) if enh is not None else 0.0

    bull_votes = sum([
        inputs.stats.trend_3m > 3,
        bool(ma and ma.alignment == "bullish"),
        inputs.relative_strength > 3,
        agreement > 0.3,
    ])
    bear_votes = sum([
        inputs.stats.trend_3m < -3,
        bool(ma and ma.alignment == "bearish"),
        inputs.relative_strength < -3,
        agreement < -0.3,
    ])

    if bull_votes >= 2 and bear_votes >= 2:
        return _Bias("conflict", bull_votes, bear_votes)
    if bull_votes > bear_votes and bull_votes >= 1:
        return _Bias("bullish", bull_votes, bear_votes)
    if bear_votes > bull_votes and bear_votes >= 1:
        return _Bias("bearish", bull_votes, bear_votes)
    return _Bias("flat", bull_votes, bear_votes)


def _detect_setup(inputs: PolicyInputs, bias: _Bias, session_flags: _SessionFlags) -> tuple[str, float, list[str]]:
    """Gate C - chỉ công nhận setup 'sạch' nếu đa số điều kiện của setup đó
    khớp. Trả (setup_type, setup_strength 0..1, reasons)."""
    enh = inputs.enhanced
    ma = inputs.ma_alignment
    rsi = inputs.stats.rsi14
    reasons: list[str] = []

    if enh is None or ma is None:
        return "none", 0.3, ["chưa đủ dữ liệu để xác định setup rõ ràng"]

    volume_confirm = bool(inputs.liquidity and inputs.liquidity.liquidity_ratio_pct >= 120)

    if enh.donchian.state == "breakout_up" and not session_flags.hard_no_buy:
        strength = 0.6
        if volume_confirm:
            strength += 0.2
            reasons.append("breakout khỏi kênh Donchian 20 phiên có volume xác nhận")
        else:
            reasons.append("breakout khỏi kênh Donchian 20 phiên nhưng volume chưa xác nhận rõ")
        if bias.direction == "bullish":
            strength += 0.1
        if inputs.session is not None and inputs.session.daily_change_pct > _NEAR_CEILING_PCT:
            strength -= _NEAR_CEILING_STRENGTH_PENALTY
            reasons.append("mua đuổi phiên tăng sát trần: hàng về T+2.5, nếu breakout fail sẽ không kịp thoát")
        return "breakout", feat.clamp(strength, 0.0, 1.0), reasons

    if enh.donchian.state == "breakout_down":
        reasons.append("breakdown xuống dưới kênh Donchian 20 phiên")
        return "breakdown", 0.6 if bias.direction == "bearish" else 0.4, reasons

    if (
        ma.alignment in ("bullish",) and bias.direction != "bearish"
        and rsi is not None and rsi < 68
        and inputs.stats.trend_3m > 0
    ):
        strength = 0.55
        if inputs.relative_strength > 0:
            strength += 0.1
            reasons.append("outperform VNINDEX, MA alignment bullish - setup pullback-to-trend")
        else:
            reasons.append("MA alignment bullish, chưa rõ outperform VNINDEX")
        return "pullback", min(strength, 1.0), reasons

    if enh.bollinger.available and (enh.bollinger.pct_b < 10 or (rsi is not None and rsi < 30)):
        if inputs.relative_strength > -10 and not session_flags.is_near_floor_like:
            reasons.append("giá về sát dải Bollinger dưới / RSI vùng quá bán - mean reversion có kiểm soát")
            return "mean_reversion", 0.5, reasons
        reasons.append("giá quá bán nhưng relative strength quá yếu / gần sàn - không coi là mean reversion an toàn")
        return "none", 0.3, reasons

    reasons.append("không khớp mẫu setup rõ ràng nào (breakout/pullback/mean-reversion)")
    return "none", 0.35, reasons


def _compute_confidence(
    inputs: PolicyInputs, bias: _Bias, setup_strength: float, regime: str,
) -> float:
    """Confidence có nghĩa vận hành (xem CONFIDENCE_*_MIN), không phải số
    trang trí. Blend setup quality + đồng thuận tín hiệu + trend score,
    trừ điểm khi regime xấu hoặc dữ liệu kém tin cậy."""
    enh = inputs.enhanced
    agreement = feat.calc_signal_agreement(enh) if enh is not None else 0.0
    # agreement (-1..1) và trend_score (0..100) đều được ĐỊNH NGHĨA để đo mức
    # độ ĐỒNG THUẬN TĂNG (xem calc_signal_agreement/calc_trend_score) - nếu
    # dùng thẳng khi bias bearish, 1 setup giảm giá càng rõ ràng lại càng bị
    # cho điểm THẤP (vì agreement/trend_score càng âm/càng thấp), khiến SELL
    # gần như không bao giờ đạt ngưỡng CONFIDENCE_BUY_MIN dù tín hiệu giảm rất
    # mạnh. Bias bearish -> đảo (1 - x) để đo đúng mức đồng thuận GIẢM.
    if bias.direction == "bearish":
        agreement_component = (1 - agreement) / 2  # -1..1 -> 0..1, càng bearish càng cao
        trend_component = 1 - (inputs.trend_score / 100) if inputs.trend_score is not None else 0.5
    else:
        agreement_component = (agreement + 1) / 2  # -1..1 -> 0..1, càng bullish càng cao
        trend_component = (inputs.trend_score / 100) if inputs.trend_score is not None else 0.5

    confidence = (
        setup_strength * 0.40
        + agreement_component * 0.30
        + trend_component * 0.30
    )

    if regime == "risk_off" and bias.direction == "bullish":
        confidence -= 0.15
    elif regime == "risk_on" and bias.direction == "bullish":
        confidence += 0.05
    elif regime == "risk_off" and bias.direction == "bearish":
        confidence += 0.05

    if inputs.quality.status == "degraded":
        confidence *= 0.85

    if inputs.liquidity and inputs.liquidity.is_thin:
        confidence *= 0.85

    return round(feat.clamp(confidence, 0.0, 1.0), 2)


def _compute_stop_target(
    price: float, enhanced: feat.EnhancedIndicators | None, stats: feat.SignalStats, news_impact: float,
    direction: str, support_resistance: feat.SupportResistance | None,
) -> tuple[float | None, float | None, float | None, str]:
    """Gate D input - stop/target ưu tiên ATR thật; chỉ rơi về % biến động
    lịch sử khi không có H/L thật để tính ATR (feature không bịa số, nhưng
    policy vẫn cần MỘT con số để vận hành - nên fallback ở đây, có nêu rõ
    trong invalidation_reason là dùng phương án nào).

    stop/target ưu tiên đặt tại support/resistance THẬT (30 phiên gần nhất)
    khi mốc đó nằm trong biên độ rủi ro hợp lý (ATR-based risk_amount) -
    trước đây cả stop và target đều tự tính từ risk_amount x hệ số nên
    rr_ratio luôn trùng khớp reward_mult một cách hình thức, không phản ánh
    khoảng cách thật tới vùng giá có ý nghĩa kỹ thuật. Chỉ khi không có S/R
    dùng được mới fallback về khoảng cách theo ATR/% biến động như cũ.

    direction: "buy" (đang cân nhắc mua mới) | "exit" (SELL - tham khảo
    chốt lời/cắt lỗ nếu đang giữ) | "watch" (chưa đủ rõ để đề xuất vùng giá).
    """
    if price <= 0 or direction == "watch":
        return None, None, None, "watch"

    atr = enhanced.atr14 if enhanced is not None else None
    if atr is not None and atr > 0:
        risk_amount = feat.clamp(atr * 1.5, price * 0.01, price * 0.10)
        basis = "atr"
    else:
        risk_pct = feat.clamp(stats.volatility, 3, 8)
        risk_amount = price * risk_pct / 100
        basis = "volatility_pct"

    rsi = stats.rsi14
    rsi_adj = 0.0 if rsi is None else (-0.3 if rsi > 70 else (0.3 if rsi < 30 else 0.0))
    news_boost = feat.clamp(news_impact, -1, 1)
    support = support_resistance.support if support_resistance else None
    resistance = support_resistance.resistance if support_resistance else None

    if direction == "buy":
        # reward_mult chỉ dùng làm fallback khi không có resistance thật
        # dùng được - vẫn cần trend_3m >= 0 vì hệ số này giả định đà tăng
        # còn tiếp diễn.
        reward_mult = feat.clamp(1.5 + news_boost * 0.5 + rsi_adj, 0.8, 2.5) if stats.trend_3m >= 0 else 1.0

        if support is not None and 0 < support < price and (price - support) <= risk_amount * 2:
            stop = feat.round_price(support * 0.99)  # dưới support 1 chút, tránh bị quét nhiễu đúng vùng hỗ trợ
            basis = f"{basis}+support"
        else:
            stop = feat.round_price(price - risk_amount)
        if stop >= price:
            # cổ phiếu thị giá nhỏ: round_price (bước 10) có thể kéo stop
            # ngang bằng giá, ép lùi thêm 1 bước để risk luôn dương.
            stop -= 10
        risk = price - stop
        if risk <= 0:
            return None, None, None, basis

        if resistance is not None and resistance > price and (resistance - price) >= risk:
            target = feat.round_price(resistance)
            basis = f"{basis}+resistance"
        else:
            target = feat.round_price(price + risk * reward_mult)

        rr = round((target - price) / risk, 2)
        return stop, target, rr, basis

    # exit reference cho SELL: invalidation ở TRÊN giá (nếu giá vượt lên thì
    # tín hiệu SELL bị vô hiệu), target ở DƯỚI giá (mốc tham khảo nếu giảm
    # tiếp). trend_3m <= 0 (thay vì >= 0 như buy) vì đây là hệ số cho đà GIẢM
    # - trước đây dùng chung điều kiện >= 0 nên với setup bearish (trend_3m
    # gần như luôn âm) reward_mult bị khoá cứng ở 1.0, không phản ứng theo
    # news/RSI như phía buy.
    reward_mult = feat.clamp(1.5 + news_boost * 0.5 + rsi_adj, 0.8, 2.5) if stats.trend_3m <= 0 else 1.0

    if resistance is not None and resistance > price and (resistance - price) <= risk_amount * 2:
        invalidation = feat.round_price(resistance * 1.01)
        basis = f"{basis}+resistance"
    else:
        invalidation = feat.round_price(price + risk_amount)
    if invalidation <= price:
        # tương tự nhánh buy: cổ phiếu thị giá nhỏ có thể bị round_price kéo
        # invalidation về trùng giá, ép lên 1 bước để risk luôn dương.
        invalidation += 10
    risk = invalidation - price
    if risk <= 0:
        return None, None, None, basis

    if support is not None and support < price and (price - support) >= risk:
        target = feat.round_price(support)
        basis = f"{basis}+support"
    else:
        target = feat.round_price(price - risk * reward_mult)

    rr = round((price - target) / risk, 2)
    return invalidation, target, rr, basis


def _risk_level(inputs: PolicyInputs, session_flags: _SessionFlags) -> str:
    atr_pct = inputs.enhanced.atr_pct if inputs.enhanced else None
    if atr_pct is not None:
        base = "high" if atr_pct > 6 else ("medium" if atr_pct > 3 else "low")
    else:
        base = "high" if inputs.stats.volatility > 15 else ("medium" if inputs.stats.volatility > 8 else "low")
    if session_flags.hard_no_buy or inputs.quality.status == "degraded":
        return "high" if base != "low" else "medium"
    return base


def _build_scenarios(plan: TradePlan) -> list[Scenario]:
    """B3 - 3 kịch bản deterministic dựa hoàn toàn trên số của TradePlan đã
    chốt, KHÔNG suy diễn thêm số mới ngoài stop/target1/target2 sẵn có."""
    base = Scenario(
        name="base",
        trigger=f"Giá giữ trên vùng stop {plan.stop:,.0f}, tích lũy đi ngang trong vùng mua {plan.entry_low:,.0f}-{plan.entry_high:,.0f}".replace(",", "."),
        action=f"Nắm giữ theo kế hoạch, chờ chạm T1 {plan.target1:,.0f} để chốt 1/2 vị thế".replace(",", "."),
    )
    if plan.target2 is not None:
        bull_action = f"Giữ phần còn lại nhắm T2 {plan.target2:,.0f}, dời stop về hoà vốn (entry ~{plan.entry_low:,.0f})".replace(",", ".")
    else:
        bull_action = f"Không có T2 rõ ràng - trail stop theo MA10 cho phần còn lại thay vì chốt cứng"
    bull = Scenario(
        name="bull",
        trigger=f"Đóng cửa vượt T1 {plan.target1:,.0f} kèm volume > 130% trung bình 20 phiên".replace(",", "."),
        action=bull_action,
    )
    bear = Scenario(
        name="bear",
        trigger=f"Đóng cửa dưới stop {plan.stop:,.0f}".replace(",", "."),
        action="Cắt toàn bộ vị thế theo stop đã định, không bình quân giá xuống",
    )
    return [base, bull, bear]


def evaluate_policy(inputs: PolicyInputs) -> Decision:
    quality = inputs.quality

    if not quality.usable or inputs.price <= 0:
        return Decision(
            action="NO_TRADE", confidence=0.0, setup_type="none",
            reasons=quality.reasons or ["dữ liệu không đủ tin cậy để ra quyết định"],
            risk_level="high", stop_price=None, target_price=None, rr_ratio=None,
            invalidation_reason="dữ liệu chưa đủ tin cậy - chờ dữ liệu tốt hơn",
            market_regime="unknown", data_quality=quality.status,
        )

    regime = classify_market_regime(inputs.vnindex_multi_tf, inputs.vnindex_adx, inputs.vnindex_distribution_days)
    session_flags = _evaluate_session(inputs.session, inputs.enhanced)
    bias = _classify_bias(inputs)
    setup_type, setup_strength, setup_reasons = _detect_setup(inputs, bias, session_flags)
    confidence = _compute_confidence(inputs, bias, setup_strength, regime)
    risk_level = _risk_level(inputs, session_flags)

    reasons: list[str] = []
    if quality.status == "degraded":
        reasons.extend(quality.reasons)
    if inputs.vnindex_distribution_days >= DISTRIBUTION_DAY_THRESHOLD:
        reasons.append(f"VNINDEX có {inputs.vnindex_distribution_days} ngày phân phối trong 25 phiên gần nhất - dấu hiệu tổ chức lớn bán ra, ép thị trường chung về risk-off")
    reasons.extend(setup_reasons)
    reasons.extend(session_flags.reasons)

    action = "NO_TRADE"
    direction = "watch"
    invalidation_reason = None
    holding = inputs.is_holding

    if bias.direction == "bullish":
        can_buy = (
            confidence >= CONFIDENCE_BUY_MIN
            and regime != "risk_off"
            and not session_flags.hard_no_buy
        )
        if can_buy:
            direction = "buy"  # BUY (mới) hay HOLD (đang giữ) chốt sau Gate D bên dưới
        elif confidence >= CONFIDENCE_WATCH_MIN:
            action = "HOLD" if holding else "WATCH"
            if regime == "risk_off":
                reasons.append("VNINDEX đang risk-off - hạn chế mở mua mới dù setup mã riêng còn ổn")
            if session_flags.hard_no_buy:
                reasons.append("phiên gần nhất có rủi ro phân phối/breakdown - chưa mở mua mới")
        else:
            action = "HOLD" if holding else "NO_TRADE"
            reasons.append("confidence chưa đạt ngưỡng để hành động, tín hiệu tăng còn yếu")
    elif bias.direction == "bearish":
        # SELL chỉ có ý nghĩa khi đang thật sự giữ mã - không giữ thì
        # "SELL" là khuyến nghị vô nghĩa (không có gì để bán); trường hợp đó
        # tín hiệu giảm chỉ có tác dụng "đừng mua mới" (NO_TRADE).
        if confidence >= CONFIDENCE_BUY_MIN:
            if holding:
                action, direction = "SELL", "exit"
            else:
                action = "NO_TRADE"
                reasons.append("tín hiệu giảm rõ - không phải cơ hội mua mới")
        elif confidence >= CONFIDENCE_WATCH_MIN:
            if holding:
                action, direction = "WATCH", "exit"
                reasons.append("tín hiệu giảm đang hình thành - theo dõi sát, chưa đủ mạnh để cắt ngay")
            else:
                action = "NO_TRADE"
                reasons.append("tín hiệu giảm chưa đủ rõ để kết luận - không mua mới")
        else:
            action = "HOLD" if holding else "NO_TRADE"
            reasons.append("confidence chưa đủ cao để khẳng định tín hiệu giảm")
    elif bias.direction == "conflict":
        reasons.append("tín hiệu tăng/giảm mâu thuẫn nhau - chưa đủ rõ ràng để hành động")
        if confidence >= CONFIDENCE_WATCH_MIN:
            action = "HOLD" if holding else "WATCH"
        else:
            action = "HOLD" if holding else "NO_TRADE"
    else:
        action = "HOLD" if holding else "NO_TRADE"
        reasons.append(
            "chưa có tín hiệu rõ ràng theo hướng nào - giữ nguyên vị thế, theo dõi thêm" if holding
            else "chưa có tín hiệu rõ ràng theo hướng nào - chưa có cơ sở để mở vị thế mới"
        )

    stop_price = target_price = rr_ratio = None
    if direction == "buy":
        stop_price, target_price, rr_ratio, basis = _compute_stop_target(
            inputs.price, inputs.enhanced, inputs.stats, inputs.news_impact, "buy", inputs.support_resistance,
        )
        if stop_price is None or rr_ratio is None:
            action = "HOLD" if holding else "WATCH"
            reasons.append("không tính được stop/target hợp lệ - không đề xuất vùng giá")
            invalidation_reason = None
        elif rr_ratio < MIN_RR_RATIO:
            action = "HOLD" if holding else "WATCH"
            reasons.append(
                f"risk/reward {rr_ratio} dưới ngưỡng tối thiểu {MIN_RR_RATIO} - "
                f"chưa {'mở thêm' if holding else 'vào mới'} dù setup ổn"
            )
            invalidation_reason = f"chờ R:R cải thiện (hiện {rr_ratio}, cần >= {MIN_RR_RATIO})"
        else:
            action = "HOLD" if holding else "BUY"
            invalidation_reason = (
                f"nếu giá đóng cửa dưới {stop_price:,.0f} nên cân nhắc cắt lỗ phần đang giữ" if holding
                else f"nếu giá đóng cửa dưới {stop_price:,.0f} coi như setup thất bại, cần cắt lỗ"
            ).replace(",", ".")
    elif direction == "exit":
        stop_price, target_price, rr_ratio, basis = _compute_stop_target(
            inputs.price, inputs.enhanced, inputs.stats, inputs.news_impact, "exit", inputs.support_resistance,
        )
        if stop_price is None or rr_ratio is None:
            action = "HOLD" if holding else "WATCH"
            reasons.append("không tính được stop/target hợp lệ - không đề xuất vùng giá")
            invalidation_reason = None
        else:
            invalidation_reason = f"nếu giá vượt lên trên {stop_price:,.0f} thì tín hiệu SELL coi như vô hiệu, cần đánh giá lại".replace(",", ".")
    else:
        invalidation_reason = None

    if action == "NO_TRADE":
        stop_price = target_price = rr_ratio = None
        if not reasons:
            reasons.append("edge không đủ rõ để ra quyết định - ưu tiên đứng ngoài")

    trade_plan: TradePlan | None = None
    scenarios: list[Scenario] = []
    if direction == "buy" and action in ("BUY", "HOLD") and stop_price is not None:
        trade_plan = build_trade_plan(inputs.price, stop_price, target_price, confidence, inputs.key_levels, inputs.liquidity)
        if trade_plan is not None:
            scenarios = _build_scenarios(trade_plan)

    return Decision(
        action=action,
        confidence=confidence,
        setup_type=setup_type,
        reasons=reasons,
        risk_level=risk_level,
        stop_price=stop_price,
        target_price=target_price,
        rr_ratio=rr_ratio,
        invalidation_reason=invalidation_reason,
        market_regime=regime,
        data_quality=quality.status,
        trade_plan=trade_plan,
        scenarios=scenarios,
    )
