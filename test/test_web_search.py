import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import tavily_client  # noqa: E402
from services import web_search  # noqa: E402


def _response(domains: list[str]) -> tavily_client.TavilySearchResponse:
    return tavily_client.TavilySearchResponse(
        query="q",
        answer="",
        results=tuple(
            tavily_client.TavilyResult(
                title=f"R{i}", url=f"https://{domain}/p{i}", content=f"content {i}"
            )
            for i, domain in enumerate(domains)
        ),
    )


def test_search_intent_gate_rejects_small_talk_and_accepts_lookup():
    assert web_search.looks_like_search_question("ừm") is False
    assert web_search.looks_like_search_question("tin AI mới nhất hôm nay") is True


def test_quality_gate_requires_source_diversity():
    assert web_search.is_tavily_quality_sufficient(_response(["a.vn", "b.vn"])) is True
    assert web_search.is_tavily_quality_sufficient(_response(["a.vn", "a.vn"])) is False


@pytest.mark.asyncio
async def test_good_tavily_result_does_not_call_fallback(monkeypatch):
    async def fake_tavily(*args, **kwargs):
        return _response(["a.vn", "b.vn", "c.vn"])

    async def should_not_call(*args, **kwargs):
        raise AssertionError("fallback must not run")

    monkeypatch.setattr(tavily_client, "search_results", fake_tavily)
    monkeypatch.setattr(web_search.orchestrator, "ask", should_not_call)

    result = await web_search.search_web("tin mới")
    assert result.provider == "tavily"
    assert result.tavily_quality_ok is True


@pytest.mark.asyncio
async def test_weak_tavily_result_uses_grounded_fallback(monkeypatch):
    async def fake_tavily(*args, **kwargs):
        return _response(["a.vn", "a.vn"])

    class Response:
        text = "Nguồn: https://b.vn - dữ liệu mới"

    captured = {}

    async def fake_ask(prompt, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(tavily_client, "search_results", fake_tavily)
    monkeypatch.setattr(web_search.orchestrator, "ask", fake_ask)

    result = await web_search.search_web("tin mới")
    assert result.provider == "fallback"
    assert captured["providers_override"] == ["groq", "api1", "api2"]
    assert captured["require_real_search"] is True
    assert "https://b.vn" in result.text

@pytest.mark.asyncio
async def test_maybe_search_respects_toggle_and_intent_gate(monkeypatch):
    calls = {"search": 0}

    async def enabled():
        return True

    async def fake_search(*args, **kwargs):
        calls["search"] += 1
        return web_search.SearchGrounding("grounding", "tavily", True)

    monkeypatch.setattr(tavily_client, "get_enabled", enabled)
    monkeypatch.setattr(web_search, "search_web", fake_search)

    assert await web_search.maybe_search("ừm") == ""
    assert calls["search"] == 0
    assert await web_search.maybe_search("tin AI mới nhất hôm nay") == "grounding"
    assert calls["search"] == 1
