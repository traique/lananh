import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import tavily_client  # noqa: E402


@pytest.mark.asyncio
async def test_search_results_boost_vietnamese_and_keeps_structured_results(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "query": "giá iphone",
                "answer": "Có nhiều nơi bán.",
                "results": [
                    {"title": "A", "url": "https://a.vn/p1", "content": "Giá A"},
                    {"title": "A2", "url": "https://a.vn/p2", "content": "Giá A2"},
                    {"title": "B", "url": "https://b.vn/p1", "content": "Giá B"},
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_api_key():
        return "tvly-test"

    async def fake_record(*args, **kwargs):
        return None

    monkeypatch.setattr(tavily_client, "_api_key", fake_api_key)
    monkeypatch.setattr(tavily_client, "_get_client", lambda: client)
    monkeypatch.setattr(tavily_client.db, "record_provider_call", fake_record)

    result = await tavily_client.search_results(
        "giá iphone", max_results=10, max_results_per_domain=1
    )

    assert captured["country"] == "vietnam"
    assert captured["language"] == "vi"
    assert captured["filter_by_language"] is False
    assert [item.url for item in result.results] == ["https://a.vn/p1", "https://b.vn/p1"]
    assert result.unique_domains == {"a.vn", "b.vn"}
    await client.aclose()


@pytest.mark.asyncio
async def test_search_compatibility_wrapper_formats_structured_response(monkeypatch):
    response = tavily_client.TavilySearchResponse(
        query="tin mới",
        answer="Tóm tắt",
        results=(
            tavily_client.TavilyResult("Bài A", "https://a.vn", "Nội dung A"),
        ),
    )

    async def fake_search_results(*args, **kwargs):
        return response

    monkeypatch.setattr(tavily_client, "search_results", fake_search_results)
    text = await tavily_client.search("tin mới")
    assert "Tóm tắt: Tóm tắt" in text
    assert "https://a.vn" in text
