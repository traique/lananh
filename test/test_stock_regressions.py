import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import debate
from stock import providers
from stock import sector
from stock.schemas import FinalDecision


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
            symbol=symbol, closes=closes,
            highs=[v + 1 for v in closes], lows=[v - 1 for v in closes],
            volumes=[100_000.0] * len(closes), dates=[str(i) for i in range(len(closes))],
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


@pytest.mark.asyncio
async def test_manager_khong_duoc_doi_action_policy(monkeypatch):
    async def fake_ask(*args, **kwargs):
        return FinalDecision(action="BUY", confidence=0.8, reasoning="Tôi muốn đổi action")

    monkeypatch.setattr(debate, "ask_structured", fake_ask)
    monkeypatch.setattr(debate.backtest, "format_setup_stats_line", lambda setup: "")
    decision = SimpleNamespace(
        action="WATCH", confidence=0.6, setup_type="pullback", market_regime="neutral", reasons=["test"]
    )
    ctx = SimpleNamespace(symbol="FPT", decision=decision)
    result = await debate.run_manager_step(ctx, None, None, None)
    assert result is not None
    assert result.action == "WATCH"
    assert result.confidence == 0.8
