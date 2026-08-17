"""Scenario test cho stock_policy.py - mô phỏng các tình huống thị trường
điển hình (uptrend đẹp, downtrend mạnh, breakout thất bại, dữ liệu xấu,
risk-off, tín hiệu mâu thuẫn, R:R kém, thanh khoản thấp...) để kiểm tra
Decision cuối cùng có hợp lý không, thay vì chỉ test từng hàm biệt lập.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import features as feat  # noqa: E402
from stock import policy as pol  # noqa: E402
from stock.validation import DataQuality  # noqa: E402


def _ok_quality(n=60):
    return DataQuality(status="ok", reasons=[], bars_available=n)


def _bad_quality():
    return DataQuality(status="bad", reasons=["chỉ có 10 phiên"], bars_available=10)


def _degraded_quality():
    return DataQuality(status="degraded", reasons=["dữ liệu cũ"], bars_available=35)


def _closes_uptrend(n=90, start=50_000.0, step=250.0):
    return [start + i * step for i in range(n)]


def _closes_downtrend(n=90, start=80_000.0, step=250.0):
    return [start - i * step for i in range(n)]


def _closes_flat(n=90, price=30_000.0):
    return [price] * n


def _make_inputs(
    closes, *, quality=None, vnindex_bullish=True, session=None, liquidity=None,
    highs=None, lows=None, relative_strength=5.0, news_impact=0.0,
):
    price = closes[-1]
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    volumes = [500_000.0] * len(closes)
    quality = quality or _ok_quality(len(closes))

    stats = feat.calc_signal_stats(closes, volumes, price)
    enhanced = feat.build_enhanced_indicators(closes, price, highs, lows) if len(closes) >= 20 else None
    ma_alignment = feat.calc_ma_alignment(closes) if len(closes) >= 20 else None
    support_resistance = feat.calc_support_resistance(highs, lows, price, 30)
    liquidity = liquidity if liquidity is not None else feat.calc_liquidity(volumes)
    trend_score = (
        feat.calc_trend_score(ma_alignment, stats.rsi14, enhanced.macd.histogram)
        if ma_alignment and enhanced else None
    )

    vnindex_closes = _closes_uptrend(90) if vnindex_bullish else _closes_downtrend(90)
    vnindex_multi_tf = feat.calc_multi_timeframe(vnindex_closes)
    vnindex_adx = feat.calc_adx(vnindex_closes, [c * 1.01 for c in vnindex_closes], [c * 0.99 for c in vnindex_closes])

    return pol.PolicyInputs(
        price=price, stats=stats, enhanced=enhanced, ma_alignment=ma_alignment,
        support_resistance=support_resistance, liquidity=liquidity, session=session,
        relative_strength=relative_strength, trend_score=trend_score, news_impact=news_impact,
        quality=quality, vnindex_multi_tf=vnindex_multi_tf, vnindex_adx=vnindex_adx,
    )


# ─── Gate B: data quality ────────────────────────────────────────────────────

def test_du_lieu_bad_luon_no_trade():
    inputs = _make_inputs(_closes_uptrend(15), quality=_bad_quality())
    d = pol.evaluate_policy(inputs)
    assert d.action == "NO_TRADE"
    assert d.confidence == 0.0
    assert d.stop_price is None and d.target_price is None


def test_gia_khong_hop_le_no_trade():
    inputs = _make_inputs(_closes_uptrend(60), quality=_ok_quality())
    inputs.price = 0.0
    d = pol.evaluate_policy(inputs)
    assert d.action == "NO_TRADE"


# ─── Uptrend rõ ràng, thị trường risk-on -> nên nghiêng về BUY/WATCH ────────

def test_uptrend_manh_risk_on_khong_ra_sell():
    closes = _closes_uptrend(90)
    inputs = _make_inputs(closes, vnindex_bullish=True, relative_strength=8.0)
    d = pol.evaluate_policy(inputs)
    assert d.action in ("BUY", "WATCH")
    assert d.market_regime == "risk_on"
    if d.action == "BUY":
        assert d.stop_price is not None and d.target_price is not None
        assert d.stop_price < inputs.price < d.target_price
        assert d.rr_ratio is not None and d.rr_ratio >= pol.MIN_RR_RATIO


# ─── Downtrend mạnh -> không được ra BUY ────────────────────────────────────

def test_downtrend_manh_khong_bao_gio_buy():
    closes = _closes_downtrend(90)
    inputs = _make_inputs(closes, vnindex_bullish=False, relative_strength=-8.0)
    d = pol.evaluate_policy(inputs)
    assert d.action != "BUY"
    assert d.market_regime == "risk_off"


# ─── Giá đi ngang, không tín hiệu -> HOLD hoặc NO_TRADE, không BUY/SELL ─────

def test_gia_di_ngang_khong_buy_khong_sell():
    closes = _closes_flat(90)
    inputs = _make_inputs(closes, vnindex_bullish=True, relative_strength=0.0)
    d = pol.evaluate_policy(inputs)
    assert d.action in ("HOLD", "NO_TRADE", "WATCH")
    assert d.action not in ("BUY", "SELL")


# ─── Gate A: risk-off chặn BUY mới dù setup mã riêng đẹp ────────────────────

def test_risk_off_chan_buy_du_ma_rieng_uptrend():
    closes = _closes_uptrend(90)
    inputs = _make_inputs(closes, vnindex_bullish=False, relative_strength=10.0)
    d = pol.evaluate_policy(inputs)
    assert d.action != "BUY"
    assert d.market_regime == "risk_off"


# ─── Gate C: phiên phân phối mạnh (giảm sâu + volume lớn) chặn BUY ──────────

def test_phien_phan_phoi_chan_buy():
    closes = _closes_uptrend(90)
    session = feat.SessionMetrics(daily_change_pct=-5.0, close_position_pct=10.0, volume_ratio_pct=200.0)
    inputs = _make_inputs(closes, vnindex_bullish=True, session=session, relative_strength=8.0)
    d = pol.evaluate_policy(inputs)
    assert d.action != "BUY"
    assert any("phân phối" in r or "giảm" in r for r in d.reasons)


# ─── Gate D: R:R kém -> hạ xuống WATCH dù setup được công nhận ──────────────

def test_rr_ratio_luon_dat_toi_thieu_khi_buy():
    # Chạy nhiều biến thể uptrend, bất kỳ khi nào action=BUY thì rr phải đạt ngưỡng.
    for step in (50.0, 100.0, 250.0, 500.0, 1000.0):
        closes = _closes_uptrend(90, step=step)
        inputs = _make_inputs(closes, vnindex_bullish=True, relative_strength=8.0)
        d = pol.evaluate_policy(inputs)
        if d.action == "BUY":
            assert d.rr_ratio >= pol.MIN_RR_RATIO


# ─── Thanh khoản quá thấp -> giảm confidence, không được BUY tự tin tuyệt đối ─

def test_thanh_khoan_thap_giam_confidence():
    closes = _closes_uptrend(90)
    thin_liquidity = feat.Liquidity(
        avg_volume_20=5_000, current_volume=4_000, liquidity_ratio_pct=80.0,
        volume_percentile=20.0, is_thin=True,
    )
    normal_liquidity = feat.Liquidity(
        avg_volume_20=500_000, current_volume=500_000, liquidity_ratio_pct=100.0,
        volume_percentile=50.0, is_thin=False,
    )
    inputs_thin = _make_inputs(closes, vnindex_bullish=True, liquidity=thin_liquidity, relative_strength=8.0)
    inputs_normal = _make_inputs(closes, vnindex_bullish=True, liquidity=normal_liquidity, relative_strength=8.0)
    d_thin = pol.evaluate_policy(inputs_thin)
    d_normal = pol.evaluate_policy(inputs_normal)
    assert d_thin.confidence <= d_normal.confidence


# ─── NO_TRADE luôn không có stop/target/rr (không được để lộ số "vô nghĩa") ──

def test_no_trade_khong_co_stop_target():
    inputs = _make_inputs(_closes_uptrend(15), quality=_bad_quality())
    d = pol.evaluate_policy(inputs)
    assert d.action == "NO_TRADE"
    assert d.stop_price is None
    assert d.target_price is None
    assert d.rr_ratio is None


# ─── Mọi Decision phải có confidence trong [0,1] và action hợp lệ ───────────

def test_moi_scenario_confidence_hop_le():
    scenarios = [
        _make_inputs(_closes_uptrend(90), vnindex_bullish=True, relative_strength=8.0),
        _make_inputs(_closes_downtrend(90), vnindex_bullish=False, relative_strength=-8.0),
        _make_inputs(_closes_flat(90), vnindex_bullish=True, relative_strength=0.0),
        _make_inputs(_closes_uptrend(15), quality=_bad_quality()),
        _make_inputs(_closes_uptrend(35), quality=_degraded_quality()),
    ]
    for inputs in scenarios:
        d = pol.evaluate_policy(inputs)
        assert 0.0 <= d.confidence <= 1.0
        assert d.action in ("BUY", "HOLD", "SELL", "WATCH", "NO_TRADE")


# ─── B2: build_trade_plan ────────────────────────────────────────────────────

def test_build_trade_plan_sizing_clamp_trong_khoang():
    liquidity = feat.Liquidity(avg_volume_20=1_000_000, current_volume=1_000_000, liquidity_ratio_pct=100.0, volume_percentile=50.0, is_thin=False)
    plan = pol.build_trade_plan(price=50_000, stop=49_000, target_price=52_000, confidence=0.9, key_levels=None, liquidity=liquidity)
    assert plan is not None
    assert pol.MIN_POSITION_PCT <= plan.position_size_pct <= pol.MAX_POSITION_PCT


def test_build_trade_plan_thanh_khoan_mong_giam_size():
    thin = feat.Liquidity(avg_volume_20=50_000, current_volume=50_000, liquidity_ratio_pct=100.0, volume_percentile=50.0, is_thin=True)
    normal = feat.Liquidity(avg_volume_20=1_000_000, current_volume=1_000_000, liquidity_ratio_pct=100.0, volume_percentile=50.0, is_thin=False)
    plan_thin = pol.build_trade_plan(price=50_000, stop=49_000, target_price=52_000, confidence=0.9, key_levels=None, liquidity=thin)
    plan_normal = pol.build_trade_plan(price=50_000, stop=49_000, target_price=52_000, confidence=0.9, key_levels=None, liquidity=normal)
    assert plan_thin is not None and plan_normal is not None
    assert plan_thin.position_size_pct <= plan_normal.position_size_pct


def test_build_trade_plan_thieu_resistance_fallback_target_price():
    plan = pol.build_trade_plan(price=50_000, stop=49_000, target_price=52_000, confidence=0.9, key_levels=feat.KeyLevels([], []), liquidity=None)
    assert plan is not None
    assert plan.target1 == 52_000
    assert plan.target2 is None


def test_build_trade_plan_co_resistance_dung_key_levels():
    levels = feat.KeyLevels(
        supports=[],
        resistances=[
            feat.PriceLevel(price=52_000, touches=3, kind="resistance", strength=0.8),
            feat.PriceLevel(price=55_000, touches=2, kind="resistance", strength=0.5),
        ],
    )
    plan = pol.build_trade_plan(price=50_000, stop=49_000, target_price=51_000, confidence=0.9, key_levels=levels, liquidity=None)
    assert plan is not None
    assert plan.target1 == 52_000
    assert plan.target2 == 55_000


def test_build_trade_plan_stop_khong_hop_le_tra_none():
    assert pol.build_trade_plan(price=50_000, stop=50_000, target_price=52_000, confidence=0.9, key_levels=None, liquidity=None) is None
    assert pol.build_trade_plan(price=50_000, stop=None, target_price=52_000, confidence=0.9, key_levels=None, liquidity=None) is None


# ─── B1: find_key_levels ─────────────────────────────────────────────────────

def test_find_key_levels_khong_du_du_lieu_tra_rong():
    levels = feat.find_key_levels([100.0] * 5, [90.0] * 5, [95.0] * 5, pivot_window=3)
    assert levels.supports == []
    assert levels.resistances == []


def test_find_key_levels_khong_dung_bar_cuoi_lam_pivot():
    # Đỉnh giả ở bar CUỐI CÙNG không được công nhận là pivot (chưa xác nhận).
    highs = [100.0] * 30 + [999.0]
    lows = [95.0] * 31
    closes = [98.0] * 31
    levels = feat.find_key_levels(highs, lows, closes, lookback=31, pivot_window=3)
    assert all(lv.price != 999.0 for lv in levels.resistances)


def test_find_key_levels_clustering_gom_pivot_gan_nhau():
    lows = [90.0] * 40
    highs = [100.0, 100.0, 100.0, 101.0, 102.0, 103.0, 104.0, 103.0, 102.0, 101.0]
    highs += [95.0, 95.5, 96.0, 100.5, 101.0, 102.0, 102.5, 101.5, 100.5, 99.0]
    highs += [98.0] * 20
    closes = [92.0] * len(highs)
    levels = feat.find_key_levels(highs, lows, closes, lookback=len(highs), pivot_window=3, cluster_pct=1.5)
    assert isinstance(levels, feat.KeyLevels)
    for lv in levels.resistances:
        assert lv.touches >= 1
        assert 0.0 <= lv.strength <= 1.0
