"""Unit test cho stock_features.py bằng chuỗi giá đã biết trước kết quả.

Các test dưới đây dùng dữ liệu nhỏ, tính tay được (hoặc dùng công thức thống
kê chuẩn độc lập với module - vd statistics.pstdev cho Bollinger) để so sánh
với kết quả thực tế của hàm, thay vì mock/patch. Với ADX (đệ quy Wilder khó
tính tay chính xác trên dữ liệu đủ dài), test kiểm tra tính chất định hướng
(uptrend -> +DI > -DI, trending=True) thay vì so khớp số thập phân tuyệt đối.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import features as ind  # noqa: E402


# ─── RSI (Wilder) ────────────────────────────────────────────────────────────

def test_rsi_khong_du_du_lieu_tra_none():
    assert ind.calc_rsi([10, 12, 11], period=14) is None


def test_rsi_tang_lien_tuc_bang_100():
    # Toàn bộ là gain -> avg_loss=0 -> RSI=100 theo đúng nhánh avg_loss==0.
    closes = [10, 11, 12, 13, 14]
    assert ind.calc_rsi(closes, period=2) == 100.0


def test_rsi_giam_lien_tuc_bang_0():
    # Toàn bộ là loss -> avg_gain=0 -> rs=0 -> RSI=100-100/(1+0)=0.
    closes = [14, 13, 12, 11, 10]
    assert ind.calc_rsi(closes, period=2) == 0.0


def test_rsi_gia_tri_tinh_tay():
    # closes=[10,12,11,13], period=2:
    #   avg_gain seed=(2+0)/2=1.0, avg_loss seed=(0+1)/2=0.5 (diff1=+2, diff2=-1)
    #   i=3: diff=+2 -> avg_gain=(1.0*1+2)/2=1.5, avg_loss=(0.5*1+0)/2=0.25
    #   rs=1.5/0.25=6.0 -> RSI=100-100/7=85.714285714285...
    closes = [10, 12, 11, 13]
    rsi = ind.calc_rsi(closes, period=2)
    assert rsi == pytest.approx(85.71428571428571)


# ─── SMA / EMA ───────────────────────────────────────────────────────────────

def test_sma_tinh_tay():
    assert ind.calc_sma([1, 2, 3, 4, 5], period=3) == 4.0


def test_ema_tinh_tay():
    # alpha=2/(3+1)=0.5, seed=sum([1,2,3])/3=2.0
    # i=3: 4*0.5+2*0.5=3.0 | i=4: 5*0.5+3*0.5=4.0
    assert ind.calc_ema([1, 2, 3, 4, 5], period=3) == 4.0


# ─── MACD(12,26,9) ───────────────────────────────────────────────────────────

def test_macd_thieu_du_lieu_tra_mac_dinh():
    result = ind.calc_macd([100.0] * 20)
    assert result == ind.MACDResult()
    assert result.crossover == "none"


def test_macd_gia_khong_doi_bang_0():
    # Giá hằng số -> EMA12 = EMA26 = giá đó với mọi kỳ -> macd/signal/hist đều 0.
    result = ind.calc_macd([100.0] * 40)
    assert result.macd_line == 0.0
    assert result.signal_line == 0.0
    assert result.histogram == 0.0
    assert result.crossover == "none"


# ─── Bollinger Bands(20, 2σ) ─────────────────────────────────────────────────

def test_bollinger_thieu_du_lieu_unavailable():
    result = ind.calc_bollinger([100.0] * 19, price=100.0)
    assert result.available is False


def test_bollinger_gia_hang_so():
    # 20 phiên cùng giá 100 -> std=0 -> upper=lower=middle=100, width=0, squeeze=True.
    result = ind.calc_bollinger([100.0] * 20, price=100.0)
    assert result.available is True
    assert (result.upper, result.middle, result.lower) == (100, 100, 100)
    assert result.width == 0.0
    assert result.pct_b == 50.0  # nhánh đặc biệt khi upper==lower
    assert result.squeeze is True


def test_bollinger_gia_tri_tinh_doc_lap_bang_statistics_pstdev():
    # closes = 91..110 (20 số nguyên liên tiếp). Kỳ vọng tính độc lập bằng
    # statistics.pstdev (không gọi lại hàm đang test):
    #   mean=100.5, pstd=5.766281297335398
    #   upper_raw=112.0325..., lower_raw=88.9674... -> round() -> 112 / 89
    #   middle round(100.5)=100 (Python round-half-to-even)
    #   width=22.95%, pct_b=91.2%, squeeze=False (width>5)
    closes = list(range(91, 111))
    result = ind.calc_bollinger(closes, price=closes[-1])
    assert result.available is True
    assert (result.upper, result.middle, result.lower) == (112, 100, 89)
    assert result.width == 22.95
    assert result.pct_b == 91.2
    assert result.squeeze is False


# ─── ADX(14) ─────────────────────────────────────────────────────────────────

def test_adx_khong_co_high_low_that_unavailable():
    closes = [100.0 + i for i in range(40)]
    result = ind.calc_adx(closes, highs=None, lows=None)
    assert result.available is False


def test_adx_khong_du_du_lieu_unavailable():
    closes = [100.0 + i for i in range(10)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    result = ind.calc_adx(closes, highs, lows)
    assert result.available is False


def test_adx_xu_huong_tang_di_plus_lon_hon_di_minus():
    # Uptrend đều, biên độ ngày hẹp -> lực tăng (+DI) phải lớn hơn lực giảm (-DI)
    # và ADX phản ánh xu hướng mạnh (trending=True).
    closes = [100.0 + i * 2 for i in range(40)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    result = ind.calc_adx(closes, highs, lows)
    assert result.available is True
    assert result.di_plus > result.di_minus
    assert result.trending is True
    assert 0.0 <= result.adx <= 100.0


def test_adx_xu_huong_giam_di_minus_lon_hon_di_plus():
    closes = [200.0 - i * 2 for i in range(40)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    result = ind.calc_adx(closes, highs, lows)
    assert result.available is True
    assert result.di_minus > result.di_plus
    assert result.trending is True


# ─── ATR(14) ─────────────────────────────────────────────────────────────────

def test_atr_khong_co_high_low_that_tra_none():
    closes = [100.0 + i for i in range(20)]
    assert ind.calc_atr(closes, None, None) is None


def test_atr_khong_du_du_lieu_tra_none():
    closes = [100.0 + i for i in range(10)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    assert ind.calc_atr(closes, highs, lows, period=14) is None


def test_atr_bien_do_hang_ngay_2_thi_atr_xap_xi_2():
    # high-low = 2 mọi phiên, giá tăng đều nên true range = high-low = 2 luôn
    # (không có gap so với close hôm trước) -> ATR hội tụ về 2.
    closes = [100.0 + i for i in range(30)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    atr = ind.calc_atr(closes, highs, lows, period=14)
    assert atr == pytest.approx(2.0, abs=0.01)


# ─── Donchian breakout ────────────────────────────────────────────────────────

def test_donchian_khong_du_du_lieu_unknown():
    result = ind.calc_donchian_breakout([1, 2], [1, 2], [1, 2], period=20)
    assert result.state == "unknown"


def test_donchian_breakout_up():
    # 20 phiên đầu đi ngang 100-110, phiên cuối vọt lên 200 -> breakout_up.
    highs = [110.0] * 20 + [200.0]
    lows = [100.0] * 20 + [190.0]
    closes = [105.0] * 20 + [200.0]
    result = ind.calc_donchian_breakout(highs, lows, closes, period=20)
    assert result.state == "breakout_up"
    assert result.upper == 110


def test_donchian_inside():
    highs = [110.0] * 21
    lows = [100.0] * 21
    closes = [105.0] * 21
    result = ind.calc_donchian_breakout(highs, lows, closes, period=20)
    assert result.state == "inside"


# ─── Signal agreement ─────────────────────────────────────────────────────────

def test_signal_agreement_khong_co_tin_hieu_bang_0():
    enh = ind.EnhancedIndicators(
        macd=ind.MACDResult(), bollinger=ind.BollingerResult(0, 0, 0, 0, 50, False, False),
        multi_tf=ind.MultiTimeframe(0, 0, 0, "mixed"),
        cross=ind.CrossSignal(False, False, False, False),
        adx=ind.ADXResult(0, 0, 0, False, available=False),
        donchian=ind.DonchianState(None, None, "unknown"),
        sma20=0, sma50=0, ema9=0, atr14=None, atr_pct=None,
    )
    # cross: above_sma20=False, above_sma50=False -> "toàn bộ dưới MA" -> vote -1
    assert ind.calc_signal_agreement(enh) == -1.0


def test_signal_agreement_dong_thuan_tang_bang_1():
    enh = ind.EnhancedIndicators(
        macd=ind.MACDResult(1, 0.5, 0.5, "bullish"),
        bollinger=ind.BollingerResult(0, 0, 0, 0, 50, False, False),
        multi_tf=ind.MultiTimeframe(1, 1, 1, "bullish"),
        cross=ind.CrossSignal(True, False, True, True),
        adx=ind.ADXResult(30, 25, 10, True, available=True),
        donchian=ind.DonchianState(100, 90, "breakout_up"),
        sma20=0, sma50=0, ema9=0, atr14=None, atr_pct=None,
    )
    assert ind.calc_signal_agreement(enh) == 1.0

