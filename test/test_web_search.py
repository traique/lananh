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


def test_format_search_results_avoids_citation_markers_and_keeps_numeric_note():
    text = tavily_client.format_search_results(_response(["a.vn", "b.vn"]))
    assert "theo kết quả tìm kiếm" in text  # chỉ xuất hiện trong câu dặn KHÔNG viết vậy
    assert "KHÔNG chèn số thứ tự" in text
    assert "KHÔNG làm tròn" in text
    assert "1. R0" in text and "2. R1" in text


def test_should_deep_read_flags_price_and_news_queries():
    assert web_search.should_deep_read("giá vàng hôm nay bao nhiêu") is True
    assert web_search.should_deep_read("tin AI mới nhất") is True
    assert web_search.should_deep_read("kể chuyện cười đi") is False


def test_recency_params_detects_news_and_time_window():
    assert web_search._recency_params("tin tức AI hôm nay") == ("news", "day")
    assert web_search._recency_params("tin tức tuần này về AI") == ("news", "week")
    assert web_search._recency_params("giá iPhone 16 Pro") == (None, None)
    assert web_search._recency_params("tin tưởng vào bản thân") == (None, None)


@pytest.mark.asyncio
async def test_rewrite_and_expand_query_returns_original_without_history():
    queries = await web_search._rewrite_and_expand_query("còn giá thì sao?", "")
    assert queries == ["còn giá thì sao?"]


@pytest.mark.asyncio
async def test_rewrite_and_expand_query_parses_llm_lines(monkeypatch):
    class Response:
        text = "giá cổ phiếu VNM hôm nay\ngiá VNM mới nhất\ngiá VNM mới nhất\n"

    async def fake_ask(prompt, **kwargs):
        assert "còn giá thì sao?" in prompt
        return Response()

    monkeypatch.setattr(web_search.orchestrator, "ask", fake_ask)

    queries = await web_search._rewrite_and_expand_query(
        "còn giá thì sao?", "user: VNM dạo này thế nào\nmodel: đang tăng nhẹ"
    )
    # dedupe giữ thứ tự, tối đa 3
    assert queries == ["giá cổ phiếu VNM hôm nay", "giá VNM mới nhất"]


@pytest.mark.asyncio
async def test_rewrite_and_expand_query_falls_back_on_error(monkeypatch):
    async def fake_ask(prompt, **kwargs):
        raise RuntimeError("provider chain down")

    monkeypatch.setattr(web_search.orchestrator, "ask", fake_ask)

    queries = await web_search._rewrite_and_expand_query("còn giá thì sao?", "history")
    assert queries == ["còn giá thì sao?"]


def test_merge_tavily_responses_dedupes_by_url_and_merges_content():
    resp_a = tavily_client.TavilySearchResponse(
        query="q1",
        answer="",
        results=(
            tavily_client.TavilyResult(title="R0", url="https://a.vn/p0", content="phần 1"),
        ),
    )
    resp_b = tavily_client.TavilySearchResponse(
        query="q2",
        answer="tóm tắt b",
        results=(
            tavily_client.TavilyResult(title="R0", url="https://a.vn/p0", content="phần 2"),
            tavily_client.TavilyResult(title="R1", url="https://b.vn/p1", content="nội dung b"),
        ),
    )

    merged = web_search._merge_tavily_responses("q1", [resp_a, resp_b])

    assert merged.answer == "tóm tắt b"
    assert len(merged.results) == 2
    first = next(r for r in merged.results if r.url == "https://a.vn/p0")
    assert "phần 1" in first.content and "phần 2" in first.content


@pytest.mark.asyncio
async def test_search_web_runs_multi_query_in_parallel_and_merges(monkeypatch):
    seen_queries: list[str] = []

    async def fake_tavily(query, *args, **kwargs):
        seen_queries.append(query)
        domain = "a.vn" if query == "q1" else "b.vn"
        return _response([domain, domain + "2"])

    monkeypatch.setattr(tavily_client, "search_results", fake_tavily)

    result = await web_search.search_web("q1", extra_queries=["q2"])

    assert sorted(seen_queries) == ["q1", "q2"]
    assert result.provider == "tavily"
    assert result.tavily_quality_ok is True


@pytest.mark.asyncio
async def test_search_web_deep_read_appends_extracted_facts(monkeypatch):
    async def fake_tavily(*args, **kwargs):
        return _response(["a.vn", "b.vn"])

    async def fake_read_url(url):
        return f"Nội dung đầy đủ của {url} với nhiều số liệu."

    class ExtractResponse:
        def __init__(self, text):
            self.text = text

    ask_calls = {"n": 0}

    async def fake_ask(prompt, **kwargs):
        ask_calls["n"] += 1
        return ExtractResponse("- Giá tăng 12.3% trong phiên hôm nay")

    from services import web_reader

    monkeypatch.setattr(tavily_client, "search_results", fake_tavily)
    monkeypatch.setattr(web_reader, "read_url", fake_read_url)
    monkeypatch.setattr(web_search.orchestrator, "ask", fake_ask)

    result = await web_search.search_web("giá VNM", deep_read=True)

    assert ask_calls["n"] >= 1
    assert "Đọc sâu thêm nguồn" in result.text
    assert "12.3%" in result.text


@pytest.mark.asyncio
async def test_maybe_search_passes_user_id_into_rewrite(monkeypatch):
    captured = {}

    async def enabled():
        return True

    async def fake_history(user_id, turns=3):
        captured["user_id"] = user_id
        return "user: hỏi về VNM\nmodel: đang tăng"

    async def fake_rewrite(text, history_text):
        captured["history_text"] = history_text
        return ["giá cổ phiếu VNM hôm nay", "giá VNM mới nhất"]

    async def fake_search_web(primary, *, extra_queries=None, deep_read=False):
        captured["primary"] = primary
        captured["extra_queries"] = extra_queries
        return web_search.SearchGrounding("grounding", "tavily", True)

    monkeypatch.setattr(tavily_client, "get_enabled", enabled)
    monkeypatch.setattr(web_search, "_recent_history_text", fake_history)
    monkeypatch.setattr(web_search, "_rewrite_and_expand_query", fake_rewrite)
    monkeypatch.setattr(web_search, "search_web", fake_search_web)

    result = await web_search.maybe_search("còn giá thì sao?", user_id=42)

    assert result == "grounding"
    assert captured["user_id"] == 42
    assert captured["primary"] == "giá cổ phiếu VNM hôm nay"
    assert captured["extra_queries"] == ["giá VNM mới nhất"]
