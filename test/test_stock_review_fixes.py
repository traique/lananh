"""Regression test cho các lỗi của đợt review hệ thống phân tích cổ phiếu.

Mỗi nhóm test ứng với một lỗi đã xác minh: backtest coi mọi cửa sổ lịch sử là
dữ liệu cũ, gap cổ phiếu thưởng bị đọc thành breakdown, R:R vòng tròn khi
không có kháng cự thật, vùng mua/tỷ trọng tính ở giá giữa, SELL bảo chờ giá
xuống mới bán, nến dở dang trong phiên, VNINDEX căn theo chỉ số mảng...
"""

import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock import analysis as sa  # noqa: E402
from stock import backtest as bt  # noqa: E402
from stock import corporate_actions as ca  # noqa: E402
from stock import features as feat  # noqa: E402
from stock import policy as pol  # noqa: E402
from stock import price_adjust  # noqa: E402
from stock import providers  # noqa: E402
from stock import report_format as rf  # noqa: E402
from stock import trend_model  # noqa: E402
from stock import vci_direct  # noqa: E402
from stock.validation import DataQuality  # noqa: E402
from test_stock_policy import _closes_uptrend, _make_inputs  # noqa: E402

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _trading_dates(n: int, end: date) -> list[str]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


# ─── 2. Backtest: độ cũ dữ liệu theo ngày của cửa sổ ────────────────────────


def test_backtest_cua_so_lich_su_khong_bi_coi_la_du_lieu_cu():
    n = 80
    closes = [20_000.0 + i * 30 for i in range(n)]
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    dates = _trading_dates(n, date(2023, 6, 30))
    decision = bt._evaluate_day(
        "TEST", closes, highs, lows, [500_000.0] * n, dates, n - 1, closes, highs, lows, [1e6] * n
    )
    assert decision.data_quality == "ok"
    assert not any("dữ liệu cũ" in r for r in decision.reasons)


# ─── 19. Dữ liệu cũ chặn BUY ─────────────────────────────────────────────────


def test_du_lieu_stale_chan_buy(monkeypatch):
    monkeypatch.setattr(pol, "_compute_confidence", lambda *a, **k: 0.9)
    inputs = _make_inputs(_closes_uptrend(90), relative_strength=8.0)
    p = inputs.price
    inputs.support_resistance = feat.SupportResistance(
        round(p * 0.97),
        round(p * 1.12),
        0,
        0,
        resistance_prior=round(p * 1.12),
        support_prior=round(p * 0.97),
    )
    assert pol.evaluate_policy(inputs).action == "BUY"
    inputs.quality = DataQuality(
        status="degraded", reasons=["dữ liệu cũ"], bars_available=90, is_stale=True
    )
    decision = pol.evaluate_policy(inputs)
    assert decision.action != "BUY"
    assert decision.trade_plan is None
    assert any("cũ" in r for r in decision.reasons)


# ─── 4/5. Gate D: kháng cự thật, R:R tại giá khớp xấu nhất ──────────────────


def _forced_buy_inputs(monkeypatch, resistance_factor):
    monkeypatch.setattr(pol, "_compute_confidence", lambda *a, **k: 0.9)
    inputs = _make_inputs(_closes_uptrend(90), relative_strength=8.0)
    p = inputs.price
    resistance = round(p * resistance_factor) if resistance_factor else None
    inputs.support_resistance = feat.SupportResistance(
        round(p * 0.97),
        resistance,
        0,
        0,
        resistance_prior=resistance,
        support_prior=round(p * 0.97),
    )
    return inputs


def test_khong_co_khang_cu_that_thi_target_uoc_luong_khong_mo_buy(monkeypatch):
    decision = pol.evaluate_policy(_forced_buy_inputs(monkeypatch, None))
    assert decision.action == "WATCH"
    assert decision.trade_plan is None
    assert any("không có kháng cự thật" in r for r in decision.reasons)


def test_khang_cu_gan_hon_1r_duoc_ton_trong(monkeypatch):
    inputs = _forced_buy_inputs(monkeypatch, 1.02)
    decision = pol.evaluate_policy(inputs)
    assert decision.target_price == feat.round_order_price(round(inputs.price * 1.02))
    assert decision.action == "WATCH"
    assert decision.rr_ratio < pol.MIN_RR_RATIO


def test_buy_rr_va_ty_trong_tinh_tai_can_tren_vung_mua(monkeypatch):
    decision = pol.evaluate_policy(_forced_buy_inputs(monkeypatch, 1.12))
    assert decision.action == "BUY"
    plan = decision.trade_plan
    assert plan is not None
    assert plan.entry_low > plan.stop
    worst_rr = round((plan.target1 - plan.entry_high) / (plan.entry_high - plan.stop), 2)
    assert decision.rr_ratio == worst_rr >= pol.MIN_RR_RATIO
    loss_pct_nav = plan.position_size_pct * (plan.entry_high - plan.stop) / plan.entry_high
    assert loss_pct_nav <= pol.RISK_PER_TRADE_PCT + 1e-6


def test_entry_zone_luon_cao_hon_stop_khi_stop_sat_gia():
    zone = pol.entry_zone(50_000, 49_500, "HOSE")
    assert zone is not None
    low, high = zone
    assert low > 49_500 and high >= 50_000


# ─── 6. SELL không bảo chờ giá xuống mới bán ────────────────────────────────


def _ctx_for(decision, price=50_000.0):
    return SimpleNamespace(
        symbol="FPT",
        price=price,
        fetched_at_vn="10:00 ngày 12/10/2026",
        decision=decision,
        stats=SimpleNamespace(rsi14=40.0, trend_3m=-10.0),
        last_bar_date="2026-10-09",
        ml_prob_up=None,
        realtime_quote_line=None,
        liquidity=None,
        adjustment_note="",
        quality=SimpleNamespace(status="ok"),
        relative_strength=-5.0,
        ma_alignment=None,
        support_resistance=None,
        intraday_bar_excluded=False,
        unadjusted_gap=False,
        realtime_price=None,
    )


def test_sell_khong_noi_chot_loi_cat_lo_quanh_muc_duoi_gia():
    decision = pol.Decision(
        action="SELL",
        confidence=0.8,
        setup_type="breakdown",
        reasons=["breakdown"],
        risk_level="high",
        stop_price=51_500,
        target_price=45_000,
        rr_ratio=3.3,
        invalidation_reason=None,
        market_regime="risk_off",
        data_quality="ok",
    )
    text = sa._fallback_text(_ctx_for(decision), fallback_note=False)
    assert "chốt/cắt lỗ quanh" not in text
    assert "không phải mức chờ để bán" in text
    rendered = sa._STOCK_PROMPT_TEMPLATE.render(
        symbol="FPT", action="SELL", price="50.000", target_price="45.000", stop_price="51.500"
    )
    assert "vùng cân nhắc chốt lời/cắt lỗ quanh" not in rendered
    assert "KHÔNG phải chờ giá giảm thêm mới bán" in rendered


# ─── 3. Gap cổ phiếu thưởng chưa điều chỉnh ─────────────────────────────────


def test_policy_gap_chua_dieu_chinh_khong_ra_tin_hieu_dinh_huong():
    inputs = _make_inputs(_closes_uptrend(90), relative_strength=8.0)
    inputs.unadjusted_gap = True
    inputs.is_holding = True
    decision = pol.evaluate_policy(inputs)
    assert decision.action == "HOLD"
    assert decision.setup_type == "none"
    assert decision.stop_price is None and decision.target_price is None
    inputs.is_holding = False
    assert pol.evaluate_policy(inputs).action == "NO_TRADE"


def test_last_gap_in_window_chi_xet_cua_so_policy():
    closes = [20_000.0] * 100 + [16_000.0] * 10
    gaps = price_adjust.detect_price_gaps(closes)
    assert price_adjust.last_gap_in_window(gaps, len(closes)).index == 100
    old = [20_000.0] * 10 + [16_000.0] * 100
    assert price_adjust.last_gap_in_window(price_adjust.detect_price_gaps(old), len(old)) is None


def test_parse_stock_ratio_cac_quy_uoc():
    assert ca.parse_stock_ratio("Thưởng cổ phiếu tỷ lệ 100:25") == 0.25
    assert ca.parse_stock_ratio("4:1") == 0.25
    assert ca.parse_stock_ratio("25%") == 0.25
    assert ca.parse_stock_ratio(0.2) == 0.2
    assert ca.parse_stock_ratio(20) == 0.2
    assert ca.parse_stock_ratio("không rõ") is None
    assert ca.parse_stock_ratio(0) is None


def test_su_kien_co_phieu_khop_gap_duoc_dieu_chinh_nguoc():
    closes = [50_000.0] * 20 + [40_000.0] * 6  # thưởng 25% -> tham chiếu 40.000
    dates = [(date(2026, 3, 2) + timedelta(days=i)).isoformat() for i in range(len(closes))]
    outcome = ca.apply_corporate_adjustment(
        closes,
        [c + 500 for c in closes],
        [c - 500 for c in closes],
        dates,
        [],
        [ca.StockEvent(ex_date=dates[20], ratio=0.25)],
        volumes=[1_000.0] * len(closes),
    )
    assert outcome.events_applied == 1
    assert outcome.closes[19] == 40_000.0
    assert outcome.volumes[19] == 1_250.0
    assert outcome.unexplained_gaps == 0


def test_su_kien_co_phieu_sai_ty_le_bi_bo():
    closes = [50_000.0] * 20 + [40_000.0] * 6
    dates = [(date(2026, 3, 2) + timedelta(days=i)).isoformat() for i in range(len(closes))]
    outcome = ca.apply_corporate_adjustment(
        closes,
        closes,
        closes,
        dates,
        [],
        [ca.StockEvent(ex_date=dates[20], ratio=1.0)],
    )
    assert outcome.events_applied == 0


def test_parse_events_frame_tach_co_tuc_tien_va_co_phieu():
    import pandas as pd

    df = pd.DataFrame(
        [
            {
                "event_code": "DIV",
                "exright_date": "2026-03-10",
                "value_per_share": 1000.0,
                "event_title_vi": "Trả cổ tức tiền",
            },
            {
                "event_code": "ISS",
                "exright_date": "2026-04-10",
                "value_per_share": None,
                "event_title_vi": "Thưởng cổ phiếu tỷ lệ 100:20",
            },
            {
                "event_code": "ISS",
                "exright_date": "2026-05-10",
                "value_per_share": None,
                "event_title_vi": "Phát hành quyền mua tỷ lệ 10:1",
            },
            {
                "event_code": "AGME",
                "exright_date": "2026-06-10",
                "value_per_share": None,
                "event_title_vi": "Đại hội",
            },
        ]
    )
    dividends, stock_events = ca._parse_events_frame(df)
    assert [(d.ex_date, d.cash_value) for d in dividends] == [("2026-03-10", 1000.0)]
    assert [(e.ex_date, e.ratio) for e in stock_events] == [("2026-04-10", 0.2)]


# ─── 7. Nến dở dang trong phiên ─────────────────────────────────────────────


def test_nen_hom_nay_trong_gio_giao_dich_bi_loai():
    series = providers.OhlcvSeries(
        "FPT",
        [1.0, 2.0, 3.0],
        [1.0, 2.0, 3.0],
        [1.0, 2.0, 3.0],
        [1, 2, 3],
        ["2026-10-08", "2026-10-09", "2026-10-12"],
        "dnse",
    )
    trimmed, dropped = sa._drop_unfinished_bar(series, datetime(2026, 10, 12, 10, 0, tzinfo=VN_TZ))
    assert dropped and trimmed.closes == [1.0, 2.0] and trimmed.dates[-1] == "2026-10-09"
    assert series.closes == [1.0, 2.0, 3.0]  # không mutate object trong cache
    _, dropped_after_close = sa._drop_unfinished_bar(
        series, datetime(2026, 10, 12, 15, 30, tzinfo=VN_TZ)
    )
    assert dropped_after_close is False


# ─── 8. VNINDEX căn theo ngày ────────────────────────────────────────────────


def test_benchmark_end_indices_theo_ngay_khong_look_ahead():
    vn_dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    sym_dates = ["2026-01-05", "2026-01-08", "2026-01-09"]  # mã nghỉ 2 phiên
    assert feat.benchmark_end_indices(sym_dates, vn_dates, 3, 5) == [1, 4, 5]
    assert feat.benchmark_end_indices(["0", "1"], ["0", "1"], 2, 2) == [
        1,
        2,
    ]  # không có ngày -> tương thích cũ


def test_backtest_truyen_vn_end_theo_ngay(monkeypatch):
    seen = []

    def fake_evaluate(*args, vn_end=None, **kwargs):
        seen.append((args[6], vn_end))
        return pol.Decision(
            "NO_TRADE", 0.0, "none", [], "low", None, None, None, None, "neutral", "ok"
        )

    monkeypatch.setattr(bt, "_evaluate_day", fake_evaluate)
    vn_dates = _trading_dates(60, date(2026, 6, 30))
    sym_dates = vn_dates[:30] + vn_dates[40:]  # mã tạm ngừng 10 phiên
    n = len(sym_dates)
    closes = [10_000.0] * n
    bt.run_backtest_on_series(
        "TEST",
        closes,
        closes,
        closes,
        [1.0] * n,
        sym_dates,
        [1_000.0] * 60,
        [1_000.0] * 60,
        [1_000.0] * 60,
        [1.0] * 60,
        min_bars=30,
        vnindex_dates=vn_dates,
    )
    for i, vn_end in seen:
        assert vn_dates[vn_end - 1] == sym_dates[i]


def test_trend_model_runtime_can_vnindex_theo_ngay(monkeypatch):
    captured = {}

    def fake_vector(*args, vn_end=None, **kwargs):
        captured["vn_end"] = vn_end
        return [0.0] * len(trend_model.FEATURE_NAMES)

    class FakeModel:
        def predict_proba(self, rows):
            return [[0.4, 0.6]]

    monkeypatch.setattr(trend_model, "compute_feature_vector", fake_vector)
    monkeypatch.setattr(trend_model, "_load_model", lambda: (FakeModel(), {}))
    vn_dates = _trading_dates(260, date(2026, 6, 30))
    symbol = SimpleNamespace(
        closes=[1.0] * 100,
        highs=[1.0] * 100,
        lows=[1.0] * 100,
        volumes=[1.0] * 100,
        dates=vn_dates[-100:],
    )
    vn = SimpleNamespace(
        closes=[1.0] * 260, highs=[1.0] * 260, lows=[1.0] * 260, volumes=[1.0] * 260, dates=vn_dates
    )
    assert trend_model.prob_up_for_series(symbol, vn) == 0.6
    assert captured["vn_end"] == 260  # cách cũ lấy nhầm 100 phiên ĐẦU chuỗi VNINDEX


# ─── 17. Gate chất lượng trend model ────────────────────────────────────────


def test_stats_hien_tai_khong_qua_gate():
    import json

    stats = json.loads(trend_model.STATS_PATH.read_text(encoding="utf-8"))
    passed, reason = trend_model.model_quality_gate(stats)
    assert passed is False and reason


def test_stats_tot_qua_gate():
    stats = {
        "auc": 0.6,
        "accuracy": 0.6,
        "majority_baseline_accuracy": 0.55,
        "n_symbols": 40,
        "n_test": 5000,
        "horizon": 5,
        "split_method": trend_model.SPLIT_METHOD,
        "embargo_trading_dates": 5,
    }
    assert trend_model.model_quality_gate(stats) == (True, "đạt")
    assert trend_model.model_quality_gate({**stats, "accuracy": 0.55})[0] is False
    assert trend_model.model_quality_gate({**stats, "n_symbols": 8})[0] is False


def test_model_khong_duoc_load_khi_stats_truot_gate(monkeypatch, tmp_path):
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(
        '{"auc": 0.61, "accuracy": 0.55, "class_balance_pct_up": 42.7, "n_symbols": 8}',
        encoding="utf-8",
    )
    model_path = tmp_path / "model.joblib"
    model_path.write_text("không phải model", encoding="utf-8")
    monkeypatch.setattr(trend_model, "STATS_PATH", stats_path)
    monkeypatch.setattr(trend_model, "MODEL_PATH", model_path)
    monkeypatch.setattr(trend_model, "_model_cache", None)
    assert trend_model._load_model() is None


def test_single_split_eval_ghi_embargo_va_baseline():
    import numpy as np

    rng = np.random.default_rng(1)
    rows = []
    for day in range(150):
        for sym in range(6):
            value = float(rng.normal())
            rows.append((f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}", f"S{sym}", value))
    ds = trend_model.Dataset(
        x=[[v] for _, _, v in rows],
        y=[int(v > 0) for _, _, v in rows],
        fwd_returns=[0.01 if v > 0 else -0.01 for _, _, v in rows],
        dates=[d for d, _, _ in rows],
        symbols=[s for _, s, _ in rows],
    )
    _, metrics = trend_model.single_split_eval(ds, horizon=5)
    assert metrics["split_method"] == trend_model.SPLIT_METHOD
    assert metrics["embargo_trading_dates"] == 5
    assert 0.5 <= metrics["majority_baseline_accuracy"] <= 1.0


def test_train_script_lay_mau_co_seed_khong_theo_alphabet():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "train_trend_model",
        Path(__file__).resolve().parent.parent / "scripts" / "train_trend_model.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    universe = [f"S{i:03d}" for i in range(200)]
    sample = module.sample_symbols(universe, 30, seed=7)
    assert sample == module.sample_symbols(list(reversed(universe)), 30, seed=7)
    assert sample != sorted(universe)[:30]


# ─── 18. Thống kê backtest: >= 30 lệnh + Wilson CI ──────────────────────────


def test_backtest_stats_duoi_30_lenh_khong_hien():
    assert (
        bt.format_setup_stats_line(
            "breakout", {"breakout": {"win_rate": 60.0, "avg_r": 0.5, "n": 12}}
        )
        is None
    )


def test_backtest_stats_co_khoang_tin_cay_wilson():
    line = bt.format_setup_stats_line(
        "breakout", {"breakout": {"win_rate": 50.0, "wins": 20, "avg_r": 0.2, "n": 40}}
    )
    assert line is not None and "khoảng tin cậy 95%" in line
    low, high = bt.wilson_interval(20, 40)
    assert low == pytest.approx(35.2, abs=0.1) and high == pytest.approx(64.8, abs=0.1)


# ─── 9. vci_direct không giữ lock chung khi gọi mạng; exchange map ──────────


def test_vci_direct_khong_chan_ma_khac_khi_mot_ma_dang_fetch(monkeypatch):
    monkeypatch.setattr(vci_direct, "_ratio_cache", {})
    release = threading.Event()
    started = threading.Event()
    results = {}

    def slow_fetch():
        started.set()
        release.wait(5)
        return ["A"]

    t1 = threading.Thread(
        target=lambda: results.setdefault(
            "A", vci_direct._cached(vci_direct._ratio_cache, "AAA", slow_fetch)
        )
    )
    t1.start()
    assert started.wait(2)
    t0 = time.monotonic()
    results["B"] = vci_direct._cached(vci_direct._ratio_cache, "BBB", lambda: ["B"])
    assert time.monotonic() - t0 < 1.0
    release.set()
    t1.join(5)
    assert results == {"A": ["A"], "B": ["B"]}


@pytest.mark.asyncio
async def test_exchange_map_loi_chi_cache_ngan(monkeypatch):
    responses = [{}, {"FPT": "HOSE"}]
    monkeypatch.setattr(providers, "_fetch_exchange_map_sync", lambda: responses.pop(0))
    monkeypatch.setattr(providers, "_exchange_map_cache", None)
    assert await providers.fetch_symbol_exchange("FPT") is None
    ts, mapping = providers._exchange_map_cache
    providers._exchange_map_cache = (ts - providers._EXCHANGE_MAP_NEGATIVE_TTL - 1, mapping)
    assert await providers.fetch_symbol_exchange("FPT") == "HOSE"


# ─── 10. Disclaimer không xoá nội dung phân tích ────────────────────────────


def test_cum_tu_long_khong_lam_mat_noi_dung_phan_tich():
    text = (
        "Kết luận: MUA FPT, anh cân nhắc kỹ trước khi vào lệnh.\n\n"
        "Kế hoạch: vùng mua 100.000, stop 95.000.\n\n"
        "Đây là khuyến nghị đầu tư mạnh nhất tuần: tỷ trọng 20% NAV."
    )
    out = rf.ensure_disclaimer(rf.clean_analysis_output(text))
    assert "Kế hoạch: vùng mua 100.000" in out
    assert "tỷ trọng 20% NAV" in out
    assert out.endswith(rf.STANDARD_DISCLAIMER) and out.count(rf.STANDARD_DISCLAIMER) == 1


# ─── 13. Hợp đồng policy được kiểm trong code ───────────────────────────────


def test_find_action_conflicts_bo_qua_cho_phu_dinh():
    assert rf.find_action_conflicts("Kết luận: MUA ngay", {"WATCH"}) == ["BUY:MUA"]
    assert rf.find_action_conflicts("Hiện KHÔNG MUA đuổi, chưa nên mua", {"WATCH"}) == []
    assert rf.find_action_conflicts("Tín hiệu THEO DÕI", {"WATCH"}) == []
    assert rf.find_plan_language("Vùng mua 25.000, stop 24.000")


def test_find_unknown_prices():
    prompt = "Giá 25.000 VND, kháng cự 27.500 VND, KL 1.234.567"
    assert rf.find_unknown_prices("Kháng cự 27.500, KL 1.234.567", prompt) == []
    assert rf.find_unknown_prices("Target 30.000", prompt) == [30000]


def _decision(action="WATCH"):
    return pol.Decision(
        action, 0.6, "pullback", ["lý do"], "medium", None, None, None, None, "neutral", "ok"
    )


def test_enforce_conflict_thay_bang_ban_so_lieu():
    ctx = _ctx_for(_decision("WATCH"))
    out = sa._enforce_policy_contract(
        "Kết luận: MUA FPT ngay.", ctx, "prompt", full_access=True, rule_report_sent=False
    )
    assert "MUA FPT ngay" not in out
    assert "Tín hiệu hệ thống" in out and sa._CONFLICT_DROPPED_NOTE in out


def test_enforce_so_la_dat_canh_bao_truoc_dien_giai():
    ctx = _ctx_for(_decision("WATCH"))
    out = sa._enforce_policy_contract(
        "Mốc 61.000 đáng chú ý.", ctx, "Giá 50.000", full_access=True, rule_report_sent=True
    )
    assert out.startswith(sa._UNKNOWN_PRICE_NOTE) and out.endswith("Mốc 61.000 đáng chú ý.")


def test_enforce_ban_tham_khao_cam_ngon_ngu_ke_hoach():
    ctx = _ctx_for(_decision("BUY"))
    out = sa._enforce_policy_contract(
        "Vùng mua đẹp quanh giá hiện tại.", ctx, "", full_access=False, rule_report_sent=False
    )
    assert "Vùng mua đẹp" not in out and "Tín hiệu hệ thống" not in out
    assert "không phải tín hiệu giao dịch" in out


# ─── 15/16. Giọng tham khảo + thành viên chỉ xem bản tham khảo ──────────────


def test_template_khong_con_giong_tu_van_khach_hang():
    full = sa._STOCK_PROMPT_TEMPLATE.render(symbol="FPT", action="BUY", reference_only=False)
    ref = sa._STOCK_PROMPT_TEMPLATE.render(
        symbol="FPT",
        action="BUY",
        reference_only=True,
        trade_plan={"entry_low": "1"},
        reasons_text="LYDO",
    )
    for rendered in (full, ref):
        assert "TIỀN THẬT CỦA KHÁCH" not in rendered
        assert "CHUYÊN VIÊN PHÂN TÍCH của công ty chứng khoán" not in rendered
        assert "THÔNG TIN THAM KHẢO" in rendered
    assert "Action hệ thống" in full
    assert (
        "Action hệ thống" not in ref
        and "LYDO" not in ref
        and "Kế hoạch hành động định lượng" not in ref
    )


def test_persona_prompt_ban_tham_khao_cam_khuyen_nghi():
    from stock import personas

    prompt = personas.build_persona_prompt("FPT", ["buffett"], "dữ liệu", reference_only=True)
    assert "không nói nhân vật sẽ mua/bán/giữ" in prompt
    assert "không đồng ý với tín hiệu hệ thống" not in prompt
    full = personas.build_persona_prompt("FPT", ["buffett"], "dữ liệu")
    assert "không nói nhân vật sẽ mua/bán/giữ" not in full


@pytest.mark.asyncio
async def test_channel_thanh_vien_nhan_ban_tham_khao_va_ban_so_lieu_truoc(monkeypatch):
    from services import channel_chat_service as ccs

    calls = []

    async def fake_find(text, limit=3):
        return ["FPT"]

    async def fake_analyze(symbol, *, user_text, user_id, on_rule_report, full_access):
        calls.append(full_access)
        await on_rule_report("BẢN SỐ LIỆU")
        return "DIỄN GIẢI"

    monkeypatch.setattr(ccs.stock_analysis, "find_valid_symbols", fake_find)
    monkeypatch.setattr(ccs.stock_analysis, "analyze_symbol", fake_analyze)
    result, _ = await ccs._handle_stock(5, "phân tích FPT", is_admin=False)
    assert result.messages == ["BẢN SỐ LIỆU", "DIỄN GIẢI"]
    await ccs._handle_stock(5, "phân tích FPT", is_admin=True)
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_channel_thanh_vien_danh_muc_khong_co_tin_hieu(monkeypatch):
    from services import channel_chat_service as ccs

    seen = {}

    async def fake_find(text, limit=3):
        return ["FPT", "VNM"]

    async def fake_portfolio(symbols, text, *, user_id, full_access):
        seen["full_access"] = full_access
        return "ok"

    monkeypatch.setattr(ccs.stock_analysis, "find_valid_symbols", fake_find)
    monkeypatch.setattr(ccs.stock_analysis, "analyze_portfolio", fake_portfolio)
    await ccs._handle_stock(5, "cơ cấu danh mục FPT VNM", is_admin=False)
    assert seen["full_access"] is False


def test_cache_tach_ban_day_du_va_ban_tham_khao():
    sa._cache.clear()
    sa._cache_set("FPT", False, "FULL", True)
    assert sa._cache_get("FPT", False, False) is None
    assert sa._cache_get("FPT", False, True) == "FULL"
    sa._cache.clear()


# ─── 14. Danh mục: lãi/lỗ tính trong code, prompt không bắt LLM quyết ───────


@pytest.mark.asyncio
async def test_portfolio_lai_lo_tinh_trong_code_prompt_khong_bat_quyet(monkeypatch):
    from stock import portfolio

    decision = _decision("SELL")
    decision.stop_price = 52_000
    ctx = _ctx_for(decision)
    ctx.support_resistance = None

    async def fake_holding(user_id, symbol):
        return True

    async def fake_ctx(symbol, **kwargs):
        return SimpleNamespace(**{**ctx.__dict__, "symbol": symbol})

    async def fake_get_holding(user_id, symbol):
        return portfolio.Holding(user_id, symbol, 100, 40_000)

    prompts = []

    async def fake_ask(prompt):
        prompts.append(prompt)
        return SimpleNamespace(text="Hai mã có diễn biến khác nhau.", used_fallback=False)

    monkeypatch.setattr(sa, "_is_holding_symbol", fake_holding)
    monkeypatch.setattr(sa, "build_context", fake_ctx)
    monkeypatch.setattr(portfolio, "get_holding", fake_get_holding)
    from ai import orchestrator

    monkeypatch.setattr(orchestrator, "ask", fake_ask)

    out = await sa.analyze_portfolio(["FPT", "VNM"], "cơ cấu danh mục", user_id=1, full_access=True)
    assert "+1.000.000 VND (+25,00%)" in out  # (50.000 - 40.000) x 100
    assert "BẰNG SỐ" not in prompts[0] and "broker" not in prompts[0]
    assert "không tự quyết định giữ/cắt" in prompts[0]
    member = await sa.analyze_portfolio(
        ["FPT", "VNM"], "cơ cấu danh mục", user_id=1, full_access=False
    )
    assert "tín hiệu hệ thống" not in member.lower()


@pytest.mark.asyncio
async def test_build_context_gap_thuong_chua_dieu_chinh(monkeypatch):
    """Gap -20% (thưởng 1:4) 3 phiên trước, nguồn quyền không có dữ liệu:
    quality phải degraded, policy không ra breakdown/SELL, hỗ trợ/kháng cự
    chỉ lấy từ các phiên SAU gap (không lấy đỉnh cũ chưa chia thưởng)."""
    from stock import fundamentals

    n = 120
    closes = [30_000.0 + i * 20 for i in range(n)]
    for i in range(n - 3, n):
        closes[i] = round(closes[i] * 0.8, -1)
    dates = _trading_dates(n, date.today() - timedelta(days=1))
    sym = providers.OhlcvSeries(
        "ABC",
        closes,
        [c * 1.01 for c in closes],
        [c * 0.99 for c in closes],
        [1e6] * n,
        dates,
        "dnse",
    )
    vn_closes = [1_000.0 + i for i in range(n)]
    vn = providers.OhlcvSeries(
        "VNINDEX",
        vn_closes,
        [c + 5 for c in vn_closes],
        [c - 5 for c in vn_closes],
        [1e6] * n,
        dates,
        "dnse",
    )

    async def fake_ohlcv(symbol, days=90):
        return vn if symbol == "VNINDEX" else sym

    async def none(*args, **kwargs):
        return None

    async def empty(*args, **kwargs):
        return []

    async def blank(*args, **kwargs):
        return ""

    async def no_events(symbol):
        return [], []

    monkeypatch.setattr(providers, "fetch_ohlcv", fake_ohlcv)
    monkeypatch.setattr(providers, "fetch_quote", none)
    monkeypatch.setattr(providers, "fetch_news", empty)
    monkeypatch.setattr(providers, "fetch_symbol_exchange", none)
    monkeypatch.setattr(fundamentals, "fetch_company_news", empty)
    monkeypatch.setattr(sa, "_safe_sector_prompt", blank)
    monkeypatch.setattr(sa, "_safe_fundamentals_prompt", blank)
    monkeypatch.setattr(ca, "fetch_corporate_events", no_events)

    ctx = await sa.build_context("ABC", is_holding=True)
    assert ctx.unadjusted_gap is True
    assert ctx.quality.status == "degraded"
    assert ctx.decision.action == "HOLD" and ctx.decision.setup_type != "breakdown"
    assert ctx.support_resistance.resistance < closes[n - 4]  # không lấy đỉnh trước gap
    assert sym.is_adjusted is None  # object trong cache không bị mutate


def test_cau_dieu_kien_trong_ke_hoach_khong_bi_coi_la_doi_action():
    assert (
        rf.find_action_conflicts("Nếu thủng 48.500 thì nên bán để quản trị rủi ro.", {"BUY"}) == []
    )
    assert rf.find_action_conflicts("Nếu đóng cửa dưới stop thì nên cắt lỗ.", {"BUY"}) == []
    assert rf.find_action_conflicts("Giá đã lên. Nên bán ngay.", {"BUY"}) == ["SELL:Nên bán"]
    # Bản tham khảo: câu điều kiện có khuyến nghị vẫn bị cấm.
    assert rf.find_plan_language("Nếu giá về 25.000 thì nên mua")
    assert rf.find_plan_language("Tỷ trọng doanh thu mảng phần mềm tăng") == []


def test_target_uoc_luong_khong_lot_ra_decision(monkeypatch):
    inputs = _forced_buy_inputs(monkeypatch, None)
    inputs.is_holding = True
    decision = pol.evaluate_policy(inputs)
    assert decision.action == "HOLD"
    assert (
        decision.target_price is None and decision.rr_ratio is None and decision.trade_plan is None
    )


def test_price_numbers_hieu_gia_viet_tat_nghin_dong():
    import stock.report_format as rf

    assert rf.price_numbers("mua quanh 25,5 chốt 27.5k") == {25500, 27500}
    assert rf.price_numbers("target 30k") == {30000}
    # Không nhầm phần trăm/số nhỏ/khối lượng
    assert rf.price_numbers("tăng 2,5% và 7.3% - RSI 55,5") == set()
    assert rf.find_unknown_prices("vùng mua 25,5", "giá hiện tại 25.500") == []
    assert rf.find_unknown_prices("vùng mua 26,2", "giá hiện tại 25.500") == [26200]
