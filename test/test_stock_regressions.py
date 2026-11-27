import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import debate
from stock import providers
from stock import sector
from stock.schemas import DebateCase


@pytest.mark.asyncio
async def test_dnse_schema_list_tra_empty_series_de_failover(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return []

    class Client:
        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(providers, "get_http_client", lambda: Client())
    series = await providers._fetch_ohlcv_dnse("FPT", days=90)
    assert series.closes == []


@pytest.mark.asyncio
async def test_sector_khong_bia_relative_performance_khi_thieu_vnindex(monkeypatch):
    async def fake_fetch(symbol, days=90):
        closes = [100.0 + i for i in range(90)]
        return providers.OhlcvSeries(
            symbol=symbol,
            closes=closes,
            highs=[v + 1 for v in closes],
            lows=[v - 1 for v in closes],
            volumes=[100_000.0] * len(closes),
            dates=[str(i) for i in range(len(closes))],
        )

    monkeypatch.setattr(sector.providers, "fetch_ohlcv", fake_fetch)
    perf = await sector._analyze_sector("technology", sector.SECTOR_MAP["technology"], None)
    assert perf is not None
    assert perf.vs_vnindex_1m is None
    ctx = sector.SectorContext([perf], [], [], "Dòng tiền chưa rõ")
    text = sector.build_sector_prompt_section(ctx, "FPT")
    assert "chưa có dữ liệu VNINDEX để so sánh" in text
    assert "outperform VNINDEX" not in text
    assert "underperform VNINDEX" not in text


def _debate_ctx(news=()):
    decision = SimpleNamespace(
        action="WATCH",
        confidence=0.6,
        setup_type="pullback",
        market_regime="neutral",
        risk_level="medium",
        reasons=["test"],
        stop_price=None,
        target_price=None,
    )
    return SimpleNamespace(
        symbol="FPT",
        price=100_000.0,
        decision=decision,
        indicator_summary="",
        sector_prompt="",
        fundamentals_prompt="",
        news=list(news),
    )


@pytest.mark.asyncio
async def test_debate_bull_bear_mot_luot_bear_phan_bien_bull(monkeypatch):
    """Bull + bear gộp 1 lượt JSON (trước đây 2 lượt tuần tự + 1 lượt Manager)."""
    calls = []

    async def fake_structured(schema, prompt, *, step_name, pacer=None):
        calls.append((schema, step_name, prompt))
        return schema.model_validate(
            {"bull": {"thesis": "tăng", "points": []}, "bear": {"thesis": "rủi ro", "points": []}}
        )

    monkeypatch.setattr(debate, "ask_structured", fake_structured)
    news, case = await debate.run_debate(_debate_ctx())
    assert news is None  # không có tin -> không gọi LLM bước tin
    assert [(schema, name) for schema, name, _ in calls] == [(DebateCase, "debate")]
    prompt = calls[0][2]
    assert "bull:" in prompt and "bear:" in prompt and "phản biện" in prompt
    assert "Action hệ thống (đã chốt, không được đổi): WATCH" in prompt
    assert case.bull.thesis == "tăng" and case.bear.thesis == "rủi ro"
    assert not hasattr(debate, "run_manager_step")


@pytest.mark.asyncio
async def test_debate_toi_da_2_luot_json_khi_co_tin(monkeypatch):
    steps = []

    async def fake_structured(schema, prompt, *, step_name, pacer=None):
        steps.append(step_name)
        return None

    monkeypatch.setattr(debate, "ask_structured", fake_structured)
    headline = providers.NewsHeadline("FPT lãi lớn", "CafeF", "", "", 0.5, True)
    assert await debate.run_debate(_debate_ctx([headline])) == (None, None)
    assert steps == ["news", "debate"]


def test_prompt_tong_hop_kiem_vai_research_manager_nhung_giu_action():
    from stock import analysis

    case = DebateCase.model_validate(
        {
            "bull": {"thesis": "LUAN-DIEM-TANG", "points": ["y1"]},
            "bear": {"thesis": "PHAN-BIEN-GIAM", "points": ["y2"]},
        }
    )
    with_debate = analysis._STOCK_PROMPT_TEMPLATE.render(
        symbol="FPT", action="WATCH", bull_case=case.bull, bear_case=case.bear
    )
    assert "LUAN-DIEM-TANG" in with_debate and "PHAN-BIEN-GIAM" in with_debate
    assert "vai Research Manager" in with_debate
    assert "mức thuyết phục" in with_debate and "vẫn giữ nguyên action WATCH" in with_debate
    assert "Confidence định tính của Manager" not in with_debate
    without = analysis._STOCK_PROMPT_TEMPLATE.render(symbol="FPT", action="WATCH")
    assert "Không có tranh luận nội bộ lần này" in without
    assert "dùng thẳng action hệ thống rule-based WATCH" in without
    # Bản tham khảo (thành viên) không có tranh luận lẫn đánh giá action.
    reference = analysis._STOCK_PROMPT_TEMPLATE.render(
        symbol="FPT", action="WATCH", bull_case=case.bull, reference_only=True
    )
    assert "LUAN-DIEM-TANG" not in reference and "Research Manager" not in reference
