"""Test stock/personas.py và stock/analysis.py::analyze_persona.

Persona là tầng chat giải trí: detect intent bằng từ khóa, prompt BẮT BUỘC
cấm sinh số mới - test phải chặn việc prompt bị bỏ ràng buộc này.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import analysis as stock_analysis
from stock import personas as stock_personas  # noqa: E402
import messages  # noqa: E402


def test_detect_persona_nho_ten():
    assert stock_personas.detect_personas("theo buffett thì FPT đáng mua không") == ["buffett"]
    assert stock_personas.detect_personas("Minervini thấy MWG thế nào") == ["minervini"]


def test_detect_persona_khong_dau():
    assert stock_personas.detect_personas("doan vinh binh nhìn VNM được không") == ["duan"]


def test_detect_generic_tra_ve_ca_ba():
    assert stock_personas.detect_personas("3 huyền thoại nhìn HPG thế nào?") == ["buffett", "duan", "minervini"]


def test_detect_khong_phai_persona():
    assert stock_personas.detect_personas("FPT giá bao nhiêu") == []
    assert stock_personas.detect_personas("hôm nay ăn gì") == []


def test_build_prompt_co_day_du_rang_buoc_va_du_lieu():
    prompt = stock_personas.build_persona_prompt(
        "FPT", ["buffett"], "Giá 100.000 VND | RSI14 55", "buffett nghĩ gì về FPT",
    )
    assert "TUYỆT ĐỐI không đưa giá mua/giá mục tiêu/giá cắt lỗ" in prompt
    assert "Warren Buffett" in prompt
    assert "[DỮ LIỆU HỆ THỐNG CHO FPT]" in prompt
    assert "Giá 100.000 VND | RSI14 55" in prompt
    assert "buffett nghĩ gì về FPT" in prompt


def _fake_ctx():
    return SimpleNamespace(
        symbol="FPT",
        price=100_000.0,
        fetched_at_vn="12:00 07/09/2026",
        decision=SimpleNamespace(
            action="HOLD", target_price=None, stop_price=None, rr_ratio=None,
            confidence=6.0, setup_type="range", market_regime="sideways",
            risk_level="trung bình", reasons=["nến tay hai"], invalidation_reason=None,
        ),
        stats=SimpleNamespace(rsi14=None, trend_3m=None),
        last_bar_date=None,
        ml_prob_up=None,
        realtime_quote_line="",
        liquidity=None,
        adjustment_note=None,
        quality=SimpleNamespace(status="ok"),
    )


@pytest.mark.asyncio
async def test_analyze_persona_tra_loi_co_disclaimer(monkeypatch):
    async def fake_ctx(*args, **kwargs):
        return _fake_ctx()

    async def fake_holding(user_id, symbol):
        return False

    async def fake_fundamentals(symbol):
        return ""

    class FakeResponse:
        text = "**Warren Buffett** Doanh nghiệp này có lợi thế cạnh tranh rõ ràng."

    async def fake_ask(prompt):
        assert "TUYỆT ĐỐI không đưa giá mua" in prompt
        assert "[DỮ LIỆU HỆ THỐNG CHO FPT]" in prompt
        return FakeResponse()

    monkeypatch.setattr(stock_analysis, "build_context", fake_ctx)
    monkeypatch.setattr(stock_analysis, "_is_holding_symbol", fake_holding)
    monkeypatch.setattr(stock_analysis, "_safe_fundamentals_prompt", fake_fundamentals)
    from ai import orchestrator
    monkeypatch.setattr(orchestrator, "ask", fake_ask)

    result = await stock_analysis.analyze_persona("FPT", ["buffett"], "buffett nghĩ gì về FPT", user_id=1)

    assert "Warren Buffett" in result
    assert result.count("\n\n") >= 1 and result != FakeResponse.text  # đã nối disclaimer
    assert "tham khảo" in result.lower()


@pytest.mark.asyncio
async def test_analyze_persona_ctx_none_tra_loi_fetch_error(monkeypatch):
    async def fake_ctx(*args, **kwargs):
        return None

    async def fake_holding(user_id, symbol):
        return False

    monkeypatch.setattr(stock_analysis, "build_context", fake_ctx)
    monkeypatch.setattr(stock_analysis, "_is_holding_symbol", fake_holding)

    result = await stock_analysis.analyze_persona("FPT", ["buffett"], user_id=1)

    assert result == messages.STOCK_FETCH_ERROR.format(symbol="FPT")
