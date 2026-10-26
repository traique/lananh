"""Bản nháp chữ đang sinh (TELEGRAM_STREAM_DRAFTS): đọc SSE theo dòng chỉ khi lượt
chat bật drafting(), chữ cuối giống hệt đường cũ, bản nháp bỏ link/chữ CJK."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from ai import drafts, openai_compatible


def _sse(*pieces: str) -> bytes:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)
        for piece in pieces
    ]
    return ("\n\n".join(lines + ["data: [DONE]"]) + "\n\n").encode()


def _client(body: bytes, status: int = 200):
    def handler(request):
        return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _call(client):
    return await openai_compatible.post_chat_completion(
        client,
        base_url="https://gw.example",
        api_key="k",
        messages=[{"role": "user", "content": "hi"}],
        model="m",
        temperature=0.5,
        max_tokens=100,
        provider_label="Test",
    )


async def test_chi_phat_nhap_khi_bat_drafting():
    seen: list[str] = []
    async with _client(_sse("Dạ ", "em ", "đây")) as client:
        with drafts.sink_scope(seen.append):
            # Lượt gọi phụ (router tool, trích nhớ...): không phát nháp.
            assert await _call(client) == "Dạ em đây"
            assert seen == []
            with drafts.drafting():
                assert await _call(client) == "Dạ em đây"
    assert seen == ["Dạ ", "Dạ em ", "Dạ em đây"]


async def test_loi_http_khi_stream_van_thanh_loi_provider():
    async with _client(b'{"error": "quota"}', status=429) as client:
        with drafts.sink_scope(lambda text: None), drafts.drafting():
            with pytest.raises(openai_compatible.OpenAICompatibleError) as info:
                await _call(client)
    assert info.value.status_code == 429


async def test_ban_nhap_bo_link_va_chu_cjk():
    from handlers import turn_feedback

    assert turn_feedback.draft_text("**Xem** https://ke-xau.vn/?q=bi-mat nha") == "Xem … nha"
    assert turn_feedback.draft_text("答案是") is None


async def test_draft_streamer_gui_nhap_roi_dung():
    from handlers import turn_feedback

    calls = []

    async def do_api_request(endpoint, api_kwargs=None):
        calls.append((endpoint, api_kwargs))
        return True

    bot = SimpleNamespace(do_api_request=do_api_request)
    streamer = turn_feedback.DraftStreamer(bot, 42)
    streamer.start()
    await asyncio.sleep(0.01)
    streamer.push("Dạ em")
    await asyncio.sleep(0.05)
    await asyncio.wait_for(streamer.stop(), 1)
    assert calls[0] == (
        "sendMessageDraft",
        {"chat_id": 42, "draft_id": streamer.draft_id, "text": ""},
    )
    assert calls[1][1]["text"] == "Dạ em"
    count = len(calls)
    await asyncio.sleep(0.05)
    assert len(calls) == count  # đã dừng hẳn


async def test_run_chat_turn_chi_phat_nhap_cho_chat_thuong(monkeypatch):
    from services import channel_chat_service as ccs

    flags = []

    async def fake_chat(*args, **kwargs):
        flags.append(drafts.partial_sink() is not None)
        return SimpleNamespace(text="ok", used_fallback=False, searched=False)

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.telemetry, "start", nothing)
    monkeypatch.setattr(ccs.telemetry, "success", nothing)
    monkeypatch.setattr(ccs.conversation_log, "record_exchange", nothing)
    monkeypatch.setattr(ccs.conversation_log, "last_model_row", nothing)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", nothing)

    async def no_memory(user_id, text):
        return SimpleNamespace(context="", style_rules="")

    async def plan(text, user_id=None):
        return ccs.web_search.SearchPlan()

    monkeypatch.setattr(ccs.memory_service, "build_turn_memory", no_memory)
    monkeypatch.setattr(ccs.web_search, "plan_search", plan)
    with drafts.sink_scope(lambda text: None):
        await ccs.run_chat_turn(42, "chào em")
        await ccs.run_chat_turn(42, "giá FPT sao", grounding="GIÁ FPT 120")
    assert flags == [True, False]
