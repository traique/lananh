"""Scenario test cho stock_policy.py - mô phỏng các tình huống thị trường
điển hình (uptrend đẹp, downtrend mạnh, breakout thất bại, dữ liệu xấu,
risk-off, tín hiệu mâu thuẫn, R:R kém, thanh khoản thấp...) để kiểm tra
Decision cuối cùng có hợp lý không, thay vì chỉ test từng hàm biệt lập.
"""

import math
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
    closes,
    *,
    quality=None,
    vnindex_bullish=True,
    session=None,
    liquidity=None,
    highs=None,
    lows=None,
    relative_strength=5.0,
    news_impact=0.0,
):
    price = closes[-1]
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    volumes = [500_000.0] * len(closes)
    quality = quality or _ok_quality(len(closes))

    stats = feat.calc_signal_stats(closes, volumes, price)
    enhanced = (
        feat.build_enhanced_indicators(closes, price, highs, lows) if len(closes) >= 20 else None
    )
    ma_alignment = feat.calc_ma_alignment(closes) if len(closes) >= 20 else None
    support_resistance = feat.calc_support_resistance(highs, lows, price, 30)
    liquidity = liquidity if liquidity is not None else feat.calc_liquidity(volumes)
    trend_score = (
        feat.calc_trend_score(ma_alignment, stats.rsi14, enhanced.macd.histogram)
        if ma_alignment and enhanced
        else None
    )

    vnindex_closes = _closes_uptrend(90) if vnindex_bullish else _closes_downtrend(90)
    vnindex_multi_tf = feat.calc_multi_timeframe(vnindex_closes)
    vnindex_adx = feat.calc_adx(
        vnindex_closes, [c * 1.01 for c in vnindex_closes], [c * 0.99 for c in vnindex_closes]
    )

    return pol.PolicyInputs(
        price=price,
        stats=stats,
        enhanced=enhanced,
        ma_alignment=ma_alignment,
        support_resistance=support_resistance,
        liquidity=liquidity,
        session=session,
        relative_strength=relative_strength,
        trend_score=trend_score,
        news_impact=news_impact,
        quality=quality,
        vnindex_multi_tf=vnindex_multi_tf,
        vnindex_adx=vnindex_adx,
    )


def _closes_pullback(n=120, base=40_000.0, slope=150.0, dip_bars=2, dip=0.01, wiggle=900.0):
    """Xu hướng tăng có nhịp lắc, 2 phiên cuối điều chỉnh nhẹ (setup pullback)."""
    closes = [base + i * slope + wiggle * math.sin(i / 3.0) for i in range(n)]
    for k in range(dip_bars):
        closes[n - dip_bars + k] = closes[n - dip_bars - 1] * (1 - dip * (k + 1))
    return closes


def _closes_breakout(n=120):
    """Đỉnh cũ ~60.000, tích luỹ quanh 51.000, phiên cuối vượt kênh Donchian."""
    closes = []
    for i in range(n):
        if i < 60:
            closes.append(45_000.0 + i * 250)
        elif i < 75:
            closes.append(60_000.0 - (i - 60) * 600)
        else:
            closes.append(51_000 + 800 * math.sin(i / 2.0))
    closes[-1] = 54_000.0
    return closes


def _with_resistance(inputs, factor):
    """Đặt kháng cự thật (đỉnh 30 phiên trước) ở price x factor; None = không có."""
    p = inputs.price
    sr = inputs.support_resistance
    level = round(p * factor) if factor else None
    inputs.support_resistance = feat.SupportResistance(
        sr.support,
        level,
        sr.dist_to_support,
        sr.dist_to_resistance,
        resistance_prior=level,
        support_prior=sr.support_prior,
    )
    return inputs


VOLUME_CONFIRM = feat.Liquidity(
    avg_volume_20=500_000,
    current_volume=800_000,
    liquidity_ratio_pct=160.0,
    volume_percentile=90.0,
    is_thin=False,
)


def _pullback_buy_inputs(exchange="HOSE", **kwargs):
    """Setup pullback đủ điều kiện BUY (kháng cự thật cách giá 12%)."""
    inputs = _with_resistance(
        _make_inputs(_closes_pullback(), relative_strength=8.0, **kwargs), 1.12
    )
    inputs.exchange = exchange
    return inputs


def _breakout_inputs(factor=1.12, session=None, exchange="HOSE"):
    """Breakout Donchian có volume xác nhận; factor=None: không có kháng cự thật."""
    inputs = _make_inputs(
        _closes_breakout(), relative_strength=8.0, liquidity=VOLUME_CONFIRM, session=session
    )
    inputs.exchange = exchange
    return _with_resistance(inputs, factor)


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
    session = feat.SessionMetrics(
        daily_change_pct=-5.0, close_position_pct=10.0, volume_ratio_pct=200.0
    )
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
        avg_volume_20=5_000,
        current_volume=4_000,
        liquidity_ratio_pct=80.0,
        volume_percentile=20.0,
        is_thin=True,
    )
    normal_liquidity = feat.Liquidity(
        avg_volume_20=500_000,
        current_volume=500_000,
        liquidity_ratio_pct=100.0,
        volume_percentile=50.0,
        is_thin=False,
    )
    inputs_thin = _make_inputs(
        closes, vnindex_bullish=True, liquidity=thin_liquidity, relative_strength=8.0
    )
    inputs_normal = _make_inputs(
        closes, vnindex_bullish=True, liquidity=normal_liquidity, relative_strength=8.0
    )
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
    liquidity = feat.Liquidity(
        avg_volume_20=1_000_000,
        current_volume=1_000_000,
        liquidity_ratio_pct=100.0,
        volume_percentile=50.0,
        is_thin=False,
    )
    plan = pol.build_trade_plan(
        price=50_000,
        stop=49_000,
        target_price=52_000,
        confidence=0.9,
        key_levels=None,
        liquidity=liquidity,
    )
    assert plan is not None
    assert pol.MIN_POSITION_PCT <= plan.position_size_pct <= pol.MAX_POSITION_PCT


def test_build_trade_plan_thanh_khoan_mong_giam_size():
    thin = feat.Liquidity(
        avg_volume_20=50_000,
        current_volume=50_000,
        liquidity_ratio_pct=100.0,
        volume_percentile=50.0,
        is_thin=True,
    )
    normal = feat.Liquidity(
        avg_volume_20=1_000_000,
        current_volume=1_000_000,
        liquidity_ratio_pct=100.0,
        volume_percentile=50.0,
        is_thin=False,
    )
    plan_thin = pol.build_trade_plan(
        price=50_000,
        stop=49_000,
        target_price=52_000,
        confidence=0.9,
        key_levels=None,
        liquidity=thin,
    )
    plan_normal = pol.build_trade_plan(
        price=50_000,
        stop=49_000,
        target_price=52_000,
        confidence=0.9,
        key_levels=None,
        liquidity=normal,
    )
    assert plan_thin is not None and plan_normal is not None
    assert plan_thin.position_size_pct <= plan_normal.position_size_pct


def test_build_trade_plan_thieu_resistance_fallback_target_price():
    plan = pol.build_trade_plan(
        price=50_000,
        stop=49_000,
        target_price=52_000,
        confidence=0.9,
        key_levels=feat.KeyLevels([], []),
        liquidity=None,
    )
    assert plan is not None
    assert plan.target1 == 52_000
    assert plan.target2 is None


def test_build_trade_plan_co_resistance_dung_key_levels():
    # T1 luôn trùng target_price đã qua Gate D (1 nguồn số duy nhất cho
    # Decision và TradePlan); T2 = kháng cự thật kế tiếp TRÊN T1.
    levels = feat.KeyLevels(
        supports=[],
        resistances=[
            feat.PriceLevel(price=52_000, touches=3, kind="resistance", strength=0.8),
            feat.PriceLevel(price=55_000, touches=2, kind="resistance", strength=0.5),
        ],
    )
    plan = pol.build_trade_plan(
        price=50_000,
        stop=49_000,
        target_price=51_000,
        confidence=0.9,
        key_levels=levels,
        liquidity=None,
    )
    assert plan is not None
    assert plan.target1 == 51_000
    assert plan.target2 == 52_000


def test_build_trade_plan_stop_khong_hop_le_tra_none():
    assert (
        pol.build_trade_plan(
            price=50_000,
            stop=50_000,
            target_price=52_000,
            confidence=0.9,
            key_levels=None,
            liquidity=None,
        )
        is None
    )
    assert (
        pol.build_trade_plan(
            price=50_000,
            stop=None,
            target_price=52_000,
            confidence=0.9,
            key_levels=None,
            liquidity=None,
        )
        is None
    )


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
    levels = feat.find_key_levels(
        highs, lows, closes, lookback=len(highs), pivot_window=3, cluster_pct=1.5
    )
    assert isinstance(levels, feat.KeyLevels)
    for lv in levels.resistances:
        assert lv.touches >= 1
        assert 0.0 <= lv.strength <= 1.0


def test_moc_tham_chieu_sell_khong_doi_theo_tin_tuc():
    """Cùng chuỗi giá -> cùng mốc tham chiếu SELL: news_impact (cảm xúc theo từ khoá
    tiêu đề Google News) không được làm lệch mốc dự phóng của nhánh thoát hàng."""
    closes = _closes_downtrend(90)
    price = closes[-1]
    volumes = [500_000.0] * len(closes)
    stats = feat.calc_signal_stats(closes, volumes, price)
    enhanced = feat.build_enhanced_indicators(
        closes, price, [c * 1.01 for c in closes], [c * 0.99 for c in closes]
    )
    # Không truyền S/R để test trực tiếp fallback reward multiplier.
    _, target_negative, _, _ = pol._compute_stop_target(
        price, enhanced, stats, -1.0, "exit", None, "HOSE"
    )
    _, target_neutral, _, _ = pol._compute_stop_target(
        price, enhanced, stats, 0.0, "exit", None, "HOSE"
    )
    _, target_positive, _, _ = pol._compute_stop_target(
        price, enhanced, stats, 1.0, "exit", None, "HOSE"
    )
    assert target_negative == target_neutral == target_positive < price


def test_trade_plan_entry_price_luon_dung_tick_hose():
    plan = pol.build_trade_plan(
        price=20_030,
        stop=19_000,
        target_price=22_000,
        confidence=0.9,
        key_levels=None,
        liquidity=None,
        exchange="HOSE",
    )
    assert plan is not None
    assert plan.entry_low % 50 == 0
    assert plan.entry_high % 50 == 0


# ─── PolicyParams config-driven + measured-move cho breakout đỉnh mới ────────


def test_policy_params_load_tu_json_va_apply(tmp_path):
    import json

    path = tmp_path / "p.json"
    path.write_text(json.dumps({"min_rr_ratio": 2.0, "la_key": 1}), encoding="utf-8")
    params = pol.load_params(path)
    assert params.min_rr_ratio == 2.0
    assert params.confidence_buy_min == pol.PolicyParams().confidence_buy_min
    original = pol.PARAMS
    try:
        pol.apply_params(params)
        assert pol.MIN_RR_RATIO == 2.0
    finally:
        pol.apply_params(original)
    assert pol.MIN_RR_RATIO == original.min_rr_ratio
    # File hỏng -> mặc định, không raise.
    path.write_text("{rác", encoding="utf-8")
    assert pol.load_params(path) == pol.PolicyParams()
    assert pol.load_params(tmp_path / "khong-co.json") == pol.PolicyParams()


def test_policy_params_json_trong_repo_khop_mac_dinh():
    """stock/policy_params.json commit trong repo phải là bộ mặc định đang chạy."""
    assert pol.load_params() == pol.PolicyParams()


def test_breakout_dinh_moi_dung_measured_move_thay_vi_watch():
    # Kênh Donchian 20 phiên trước: 100.000-110.000 (rộng 10.000); phiên cuối
    # breakout lên 112.000, không có đỉnh lịch sử nào cao hơn -> trước đây
    # không có kháng cự thật => WATCH. Nay target measured-move 120.000.
    closes = [100_000.0 + (i % 5) * 2_500.0 for i in range(60)] + [112_000.0]
    highs = [c + 500 for c in closes[:-1]] + [112_500.0]
    lows = [c - 500 for c in closes]
    enhanced = feat.build_enhanced_indicators(closes, closes[-1], highs, lows)
    assert enhanced.donchian.state == "breakout_up"
    stats = feat.calc_signal_stats(closes, [600_000.0] * len(closes), closes[-1])
    sr = feat.calc_support_resistance(highs, lows, closes[-1], 30)
    # Mặc định TẮT (backtest cho kỳ vọng âm) -> projected như cũ, không mở BUY.
    _, _, _, basis_off = pol._compute_stop_target(
        closes[-1], enhanced, stats, 0.0, "buy", sr, "HOSE", None
    )
    assert "+projected" in basis_off
    # Bật qua params -> target measured-move.
    original = pol.PARAMS
    try:
        pol.apply_params(pol.PolicyParams(measured_move_enabled=True))
        stop, target, rr, basis = pol._compute_stop_target(
            closes[-1], enhanced, stats, 0.0, "buy", sr, "HOSE", None
        )
        assert "+measured_move" in basis
        assert target is not None and target >= enhanced.donchian.upper + 9_000
        assert rr is not None and rr > 0
    finally:
        pol.apply_params(original)


def test_build_trade_plan_size_mult_giam_ty_trong():
    full = pol.build_trade_plan(100_000.0, 95_000.0, 115_000.0, 0.9, None, None, "HOSE")
    reduced = pol.build_trade_plan(
        100_000.0, 95_000.0, 115_000.0, 0.9, None, None, "HOSE", size_mult=0.6
    )
    assert full is not None and reduced is not None
    assert reduced.position_size_pct < full.position_size_pct


# ─── Ngưỡng sát trần/gần sàn theo biên độ của sàn (HOSE 7%, HNX 10%, UPCOM 15%) ─


def test_band_scale_giu_nguyen_muc_hose_va_quy_doi_hnx_upcom():
    assert pol._band_scale("HOSE") == 1.0 and pol._band_scale(None) == 1.0
    assert pol._band_scale("hsx") == 1.0
    assert pol._band_scale("HNX") == 10.0 / 7.0
    assert pol._band_scale("UPCOM") == 15.0 / 7.0


def test_gan_san_tinh_theo_bien_do_cua_san():
    drop = feat.SessionMetrics(
        daily_change_pct=-6.5, close_position_pct=40.0, volume_ratio_pct=100.0
    )
    # HOSE (sàn -7%) và chưa rõ sàn: -6,5% là gần sàn -> chặn mua mới.
    assert pol._evaluate_session(drop, None, "HOSE").is_near_floor_like
    assert pol._evaluate_session(drop, None, None).hard_no_buy
    # HNX (sàn -10%, ngưỡng ~-8,57%) / UPCOM (sàn -15%, ~-12,86%): chỉ là phiên giảm mạnh.
    for exchange in ("HNX", "UPCOM"):
        flags = pol._evaluate_session(drop, None, exchange)
        assert not flags.is_near_floor_like and not flags.hard_no_buy
        assert flags.is_selloff
    deep = feat.SessionMetrics(
        daily_change_pct=-9.0, close_position_pct=40.0, volume_ratio_pct=100.0
    )
    assert pol._evaluate_session(deep, None, "HNX").is_near_floor_like
    assert not pol._evaluate_session(deep, None, "UPCOM").is_near_floor_like


def test_phien_giam_manh_upcom_khong_bi_chan_mua_nhu_gan_san_hose():
    drop = feat.SessionMetrics(
        daily_change_pct=-6.5, close_position_pct=40.0, volume_ratio_pct=100.0
    )
    hose = pol.evaluate_policy(_pullback_buy_inputs("HOSE", session=drop))
    upcom = pol.evaluate_policy(_pullback_buy_inputs("UPCOM", session=drop))
    assert hose.action == "WATCH" and any("gần sàn" in r for r in hose.reasons)
    assert upcom.action == "BUY" and not any("gần sàn" in r for r in upcom.reasons)


def test_sat_tran_tinh_theo_bien_do_cua_san():
    up6 = feat.SessionMetrics(daily_change_pct=6.0, close_position_pct=90.0, volume_ratio_pct=160.0)
    hose = pol.evaluate_policy(_breakout_inputs(session=up6, exchange="HOSE"))
    hnx = pol.evaluate_policy(_breakout_inputs(session=up6, exchange="HNX"))
    plain = pol.evaluate_policy(_breakout_inputs(session=None, exchange="HNX"))
    # +6% sát trần HOSE (5,5%) -> phạt strength; với HNX (~7,86%) thì chưa.
    assert any("sát trần" in r for r in hose.reasons)
    assert not any("sát trần" in r for r in hnx.reasons)
    assert hose.confidence < hnx.confidence == plain.confidence
    up12 = feat.SessionMetrics(
        daily_change_pct=12.0, close_position_pct=90.0, volume_ratio_pct=160.0
    )
    upcom = pol.evaluate_policy(_breakout_inputs(session=up12, exchange="UPCOM"))
    assert any("sát trần" in r for r in upcom.reasons)


# ─── Gate B: gap quyền chưa điều chỉnh, dữ liệu cũ; mốc SELL khi đang giữ ────


def test_gate_gap_chua_dieu_chinh_de_len_ca_setup_buy_dep():
    """Setup BUY đủ điều kiện nhưng chuỗi còn gap quyền trong cửa sổ chỉ báo: không
    ra tín hiệu định hướng nào - NO_TRADE (chưa giữ) / HOLD (đang giữ), confidence 0,
    không stop/target/R:R/kế hoạch."""
    assert pol.evaluate_policy(_pullback_buy_inputs()).action == "BUY"
    for holding, expected in ((False, "NO_TRADE"), (True, "HOLD")):
        inputs = _pullback_buy_inputs()
        inputs.unadjusted_gap = True
        inputs.is_holding = holding
        d = pol.evaluate_policy(inputs)
        assert d.action == expected
        assert d.confidence == 0.0 and d.setup_type == "none" and d.risk_level == "high"
        assert d.stop_price is None and d.target_price is None and d.rr_ratio is None
        assert d.trade_plan is None and d.scenarios == []
        assert d.market_regime == "risk_on"  # regime vẫn được đánh giá
        assert any("gap quyền chưa điều chỉnh" in r for r in d.reasons)
        assert "điều chỉnh theo sự kiện quyền" in d.invalidation_reason


def test_gate_gap_cung_chan_sell_khi_dang_giu():
    inputs = _make_inputs(_closes_downtrend(90), vnindex_bullish=False, relative_strength=-8.0)
    inputs.is_holding = True
    assert pol.evaluate_policy(inputs).action == "SELL"
    inputs.unadjusted_gap = True
    d = pol.evaluate_policy(inputs)
    assert d.action == "HOLD" and d.setup_type == "none" and d.stop_price is None


def test_gate_du_lieu_cu_chan_mo_mua_moi_nhung_giu_nguyen_so_lieu():
    """is_stale (chất lượng vẫn ok): cùng confidence/setup nhưng không mở BUY, không
    kế hoạch/tỷ trọng; người đang giữ nhận HOLD."""
    fresh = pol.evaluate_policy(_pullback_buy_inputs())
    assert fresh.action == "BUY" and fresh.trade_plan is not None
    for holding, expected in ((False, "WATCH"), (True, "HOLD")):
        inputs = _pullback_buy_inputs()
        inputs.quality = DataQuality(status="ok", bars_available=120, is_stale=True)
        inputs.is_holding = holding
        d = pol.evaluate_policy(inputs)
        assert d.action == expected
        assert d.confidence == fresh.confidence and d.setup_type == fresh.setup_type
        assert d.trade_plan is None and d.stop_price is None and d.rr_ratio is None
        assert any("dữ liệu giá đã cũ" in r for r in d.reasons)


def test_sell_khi_dang_giu_moc_vo_hieu_tren_gia_moc_ho_tro_duoi_gia():
    inputs = _make_inputs(_closes_downtrend(90), vnindex_bullish=False, relative_strength=-8.0)
    inputs.is_holding = True
    inputs.exchange = "HOSE"
    d = pol.evaluate_policy(inputs)
    assert d.action == "SELL"
    price = inputs.price
    assert d.stop_price > price > d.target_price
    assert d.stop_price % 100 == 0 and d.target_price % 100 == 0  # bước giá HOSE >= 50.000
    assert d.rr_ratio == round((price - d.target_price) / (d.stop_price - price), 2)
    assert d.trade_plan is None and d.scenarios == []
    assert d.invalidation_reason == (
        f"nếu giá vượt lên trên {d.stop_price:,.0f} thì tín hiệu SELL coi như vô hiệu, "
        "cần đánh giá lại"
    ).replace(",", ".")
    # Không giữ hàng: cùng tín hiệu giảm chỉ là "đừng mua mới", không có số SELL.
    inputs.is_holding = False
    d = pol.evaluate_policy(inputs)
    assert d.action == "NO_TRADE" and d.stop_price is None and d.target_price is None


def test_watch_thoat_hang_khi_dang_giu_dung_khang_cu_va_ho_tro_that():
    inputs = _make_inputs(_closes_flat(90), relative_strength=0.0)
    inputs.is_holding = True
    d = pol.evaluate_policy(inputs)
    assert d.action == "WATCH"
    # Kháng cự 30.300 (cách giá 1%) -> mốc vô hiệu = 30.300 x 1,01 làm tròn 100đ;
    # hỗ trợ thật 29.700 là mốc tham chiếu phía dưới.
    assert d.stop_price == 30_600 and d.target_price == 29_700
    assert d.rr_ratio == round((30_000 - 29_700) / (30_600 - 30_000), 2)
