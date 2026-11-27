"""Khoá đầu ra của stock/policy.py trên bộ kịch bản cố định (golden).

Đầu ra (action, confidence, stop/target, R:R, kế hoạch, kịch bản, lý do) của
từng kịch bản được chụp lại từ code TRƯỚC khi chuyển các ngưỡng/trọng số cứng
vào PolicyParams. Đổi tham số trong stock/policy_params.json hoặc tái cấu trúc
policy mà làm lệch bất kỳ con số nào ở đây là đổi hành vi - phải cố ý, không
được lọt vào như "refactor".

Bộ kịch bản tránh 2 chỗ đổi có chủ đích: ngưỡng sát trần/gần sàn theo sàn
HNX/UPCOM (chỉ dùng sàn HOSE/không rõ khi có phiên) và mốc tham chiếu SELL phụ
thuộc tin tức (kịch bản thoát hàng đều có news_impact = 0).

Tạo lại file golden (CHỈ khi cố ý đổi hành vi): ``python test/test_stock_policy_golden.py``.
"""

import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock import features as feat  # noqa: E402
from stock import policy as pol  # noqa: E402
from stock.validation import DataQuality  # noqa: E402
from test_stock_policy import (  # noqa: E402
    _breakout_inputs,
    _closes_downtrend,
    _closes_flat,
    _closes_pullback,
    _closes_uptrend,
    _make_inputs,
    _with_resistance,
)
from test_stock_policy import _pullback_buy_inputs as _pullback_buy  # noqa: E402

GOLDEN_PATH = Path(__file__).resolve().parent / "data" / "stock_policy_golden.json"

_THIN = feat.Liquidity(
    avg_volume_20=5_000,
    current_volume=4_000,
    liquidity_ratio_pct=80.0,
    volume_percentile=20.0,
    is_thin=True,
)


def _holding(inputs):
    inputs.is_holding = True
    return inputs


def _stale(inputs):
    inputs.quality = DataQuality(
        status="degraded", reasons=["dữ liệu cũ"], bars_available=120, is_stale=True
    )
    return inputs


def _gap(inputs, holding):
    inputs.unadjusted_gap = True
    inputs.is_holding = holding
    return inputs


def _scenarios():
    """(tên, hàm dựng PolicyInputs, tham số PolicyParams ghi đè)."""
    return [
        ("uptrend_riskon", lambda: _make_inputs(_closes_uptrend(90), relative_strength=8.0), {}),
        (
            "uptrend_riskon_holding",
            lambda: _holding(_make_inputs(_closes_uptrend(90), relative_strength=8.0)),
            {},
        ),
        (
            "downtrend_riskoff",
            lambda: _make_inputs(
                _closes_downtrend(90), vnindex_bullish=False, relative_strength=-8.0
            ),
            {},
        ),
        (
            "downtrend_riskoff_holding",
            lambda: _holding(
                _make_inputs(_closes_downtrend(90), vnindex_bullish=False, relative_strength=-8.0)
            ),
            {},
        ),
        ("flat", lambda: _make_inputs(_closes_flat(90), relative_strength=0.0), {}),
        (
            "flat_holding",
            lambda: _holding(_make_inputs(_closes_flat(90), relative_strength=0.0)),
            {},
        ),
        (
            "riskoff_uptrend",
            lambda: _make_inputs(
                _closes_uptrend(90), vnindex_bullish=False, relative_strength=10.0
            ),
            {},
        ),
        ("pullback_buy_unknown_exchange", lambda: _pullback_buy(None), {}),
        ("pullback_buy_hose", lambda: _pullback_buy("HOSE"), {}),
        ("pullback_buy_hnx_no_session", lambda: _pullback_buy("HNX"), {}),
        ("pullback_buy_holding", lambda: _holding(_pullback_buy()), {}),
        ("pullback_buy_news_pos", lambda: _pullback_buy(news_impact=1.0), {}),
        ("pullback_buy_news_neg", lambda: _pullback_buy(news_impact=-1.0), {}),
        (
            "pullback_close_resistance",
            lambda: _make_inputs(_closes_pullback(), relative_strength=8.0),
            {},
        ),
        (
            "pullback_no_resistance",
            lambda: _with_resistance(_make_inputs(_closes_pullback(), relative_strength=8.0), None),
            {},
        ),
        ("pullback_stale", lambda: _stale(_pullback_buy()), {}),
        ("pullback_stale_holding", lambda: _holding(_stale(_pullback_buy())), {}),
        ("pullback_thin", lambda: _pullback_buy(liquidity=_THIN), {}),
        ("pullback_rr_strict", lambda: _pullback_buy(), {"min_rr_ratio": 4.0}),
        (
            "session_near_floor",
            lambda: _pullback_buy(session=feat.SessionMetrics(-6.5, 30.0, 110.0)),
            {},
        ),
        (
            "session_selloff",
            lambda: _pullback_buy(session=feat.SessionMetrics(-4.5, 40.0, 100.0)),
            {},
        ),
        (
            "session_distribution",
            lambda: _pullback_buy(session=feat.SessionMetrics(-3.0, 20.0, 160.0)),
            {},
        ),
        (
            "session_selloff_distribution",
            lambda: _pullback_buy(session=feat.SessionMetrics(-4.2, 60.0, 170.0)),
            {},
        ),
        ("breakout_buy", lambda: _breakout_inputs(), {}),
        (
            "breakout_near_ceiling",
            lambda: _breakout_inputs(session=feat.SessionMetrics(6.0, 90.0, 160.0)),
            {},
        ),
        ("breakout_no_resistance", lambda: _breakout_inputs(factor=None), {}),
        (
            "breakout_measured_move",
            lambda: _breakout_inputs(factor=None),
            {"measured_move_enabled": True},
        ),
        (
            "thin_liquidity_uptrend",
            lambda: _make_inputs(_closes_uptrend(90), relative_strength=8.0, liquidity=_THIN),
            {},
        ),
        (
            "degraded_short",
            lambda: _make_inputs(
                _closes_uptrend(35),
                quality=DataQuality(status="degraded", reasons=["dữ liệu cũ"], bars_available=35),
            ),
            {},
        ),
        (
            "bad_quality",
            lambda: _make_inputs(
                _closes_uptrend(15),
                quality=DataQuality(status="bad", reasons=["chỉ có 10 phiên"], bars_available=10),
            ),
            {},
        ),
        ("unadjusted_gap", lambda: _gap(_pullback_buy(), False), {}),
        ("unadjusted_gap_holding", lambda: _gap(_pullback_buy(), True), {}),
    ]


def _snapshot(decision: pol.Decision) -> dict:
    # Vòng JSON: so sánh đúng như khi đọc từ file golden (tuple -> list...).
    return json.loads(json.dumps(dataclasses.asdict(decision), ensure_ascii=False))


def _run_all() -> dict:
    original = pol.PARAMS
    out = {}
    try:
        for name, build, overrides in _scenarios():
            pol.apply_params(dataclasses.replace(original, **overrides))
            out[name] = _snapshot(pol.evaluate_policy(build()))
    finally:
        pol.apply_params(original)
    return out


def test_policy_khop_golden_tren_bo_kich_ban_co_dinh():
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    current = _run_all()
    assert sorted(current) == sorted(golden)
    for name in golden:
        assert current[name] == golden[name], name


def test_bo_kich_ban_golden_phu_du_cac_nhanh_chinh():
    """Golden phải chạm đủ các nhánh quyết định, không chỉ toàn NO_TRADE."""
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    actions = {snap["action"] for snap in golden.values()}
    assert {"BUY", "HOLD", "WATCH", "SELL", "NO_TRADE"} <= actions
    assert any(snap["trade_plan"] for snap in golden.values())
    setups = {snap["setup_type"] for snap in golden.values()}
    assert {"pullback", "breakout", "mean_reversion", "none"} <= setups


def test_nguong_moi_trong_policy_params_thuc_su_duoc_dung():
    """Các ngưỡng/trọng số vừa chuyển vào PolicyParams phải đổi được đầu ra (không
    còn hằng số cứng nào đè lên)."""
    original = pol.PARAMS
    near_floor = feat.SessionMetrics(-6.5, 30.0, 110.0)
    try:
        assert pol.evaluate_policy(_pullback_buy(session=near_floor)).action == "WATCH"
        pol.apply_params(dataclasses.replace(original, near_floor_pct=-7.0))
        assert pol.evaluate_policy(_pullback_buy(session=near_floor)).action == "BUY"

        pol.apply_params(dataclasses.replace(original, confidence_weight_setup=0.0))
        assert pol.evaluate_policy(_pullback_buy()).confidence < 0.85

        # Mốc vô hiệu SELL/thoát hàng = kháng cự x resistance_invalidation_mult.
        pol.apply_params(original)

        def flat_holding():
            return _holding(_make_inputs(_closes_flat(90), relative_strength=0.0))

        base_stop = pol.evaluate_policy(flat_holding()).stop_price
        assert base_stop is not None
        pol.apply_params(dataclasses.replace(original, resistance_invalidation_mult=1.03))
        assert pol.evaluate_policy(flat_holding()).stop_price > base_stop
    finally:
        pol.apply_params(original)


def test_policy_params_json_cho_cung_dau_ra_voi_mac_dinh():
    """stock/policy_params.json (file chạy thật) và PolicyParams() cho cùng đầu ra."""
    original = pol.PARAMS
    try:
        pol.apply_params(pol.load_params())
        from_file = _run_all()
        pol.apply_params(pol.PolicyParams())
        defaults = _run_all()
    finally:
        pol.apply_params(original)
    assert from_file == defaults


if __name__ == "__main__":
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(
        json.dumps(_run_all(), ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Đã ghi {GOLDEN_PATH}")
