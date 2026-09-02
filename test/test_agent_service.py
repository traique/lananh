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
from core import database as db  # noqa: E402


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
    yield


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
    # _call_tool() tra fn thực thi từ _TOOLS (đóng gói lúc import module) -
    # phải patch đúng entry trong dict này, patch tên hàm module-level không
    # đủ vì tuple trong _TOOLS đã giữ tham chiếu hàm gốc từ trước.
    desc, schema, _old_fn = agent_service._TOOLS["xem_thong_ke"]
    monkeypatch.setitem(agent_service._TOOLS, "xem_thong_ke", (desc, schema, fake_tool_xem_thong_ke))

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
            return router9_client.openai_compatible.Response('{"tool": "xem_thong_ke", "args": {"so_gio": 24}}')
        # Lượt tổng hợp câu trả lời cuối.
        assert "10 lượt gọi" in prompt
        return router9_client.openai_compatible.Response("Trong 24h qua có 10 lượt gọi.")

    async def fake_tool(so_gio: int = 168) -> str:
        assert so_gio == 24
        return "10 lượt gọi"

    monkeypatch.setattr(router9_client, "generate_with_tools", fake_native_fail)
    monkeypatch.setattr(router9_client, "generate", fake_generate)
    desc, schema, _old = agent_service._TOOLS["xem_thong_ke"]
    monkeypatch.setitem(agent_service._TOOLS, "xem_thong_ke", (desc, schema, fake_tool))

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
    result = await agent_service._call_tool("doc_link", {"url": "https://vnexpress.net/bai-viet"})

    assert result == "Nội dung bài báo đã đọc."


@pytest.mark.asyncio
async def test_tool_doc_link_bao_loi_ssrf_khong_crash_agent(monkeypatch):
    assert "doc_link" in agent_service._TOOLS
    # Không patch gì - dùng thẳng normalize_public_http_url() thật để xác
    # nhận URL nội bộ bị chặn và _call_tool() trả text lỗi (không raise, để
    # model tự xử lý) thay vì làm sập cả agent loop.
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
    result = await agent_service._call_tool(
        "xem_rss", {"url": "https://vnexpress.net/rss/kinh-doanh.rss", "so_muc": 3}
    )

    assert "Tin A" in result
