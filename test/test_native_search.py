"""Provider TỰ tra web khi Tavily tắt/lỗi/mỏng (chế độ "native"), các chế độ
search_mode (off/auto/native/forced), Groq browser_search thay compound-mini đã
khai tử, và mô tả chế độ tra web ở /status, /tavily."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from ai import groq_client, openai_compatible, openrouter_client, orchestrator, router9_client
from ai.provider_state import provider_state
from core import config, database as db, untrusted
from services import search_status, web_search


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    store: dict[str, str] = {}

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    async def no_history(*args):
        return [("user", "lượt trước"), ("model", "em trả lời lượt trước")]

    async def no_record(*args, **kwargs):
        return None

    async def preferred():
        return None

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(db, "get_session_messages", no_history)
    monkeypatch.setattr(orchestrator, "_record_provider_call", no_record)
    monkeypatch.setattr(router9_client, "get_preferred_model_name", preferred)
    from ai import provider_overrides

    monkeypatch.setattr(provider_overrides, "_api_key_cache", {})
    monkeypatch.setattr(provider_overrides, "_enabled_cache", {})
    monkeypatch.setattr(provider_overrides, "_model_cache", {})
    provider_state.active_provider = "router9"
    provider_state.router9_dead_since = None
    provider_state.router9_enabled = True
    provider_state.api_exhausted_until = {
        "groq": 0.0,
        "openrouter": 0.0,
        "api1": 0.0,
        "api2": 0.0,
        "router9": 0.0,
    }
    provider_state._loaded = True
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "groq", "openrouter", "api1", "api2"])
    monkeypatch.setattr(config, "ROUTER9_API_KEY", "r9")
    monkeypatch.setattr(config, "GROQ_API_KEY", "g")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "o")
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", "k1")
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", "k2")
    monkeypatch.setattr(config, "GROQ_WEB_SEARCH", True)
    monkeypatch.setattr(config, "GROQ_REALTIME_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setattr(config, "OPENROUTER_WEB_SEARCH", False)
    monkeypatch.setattr(config, "ROUTER9_WEB_SEARCH", False)
    monkeypatch.setattr(
        config,
        "load_chat_skill",
        lambda force_reload=False, *, include_owner_profile=True: "PERSONA",
    )
    yield


class _Calls:
    """Ghi lại provider nào bị gọi theo kiểu nào (generate thường / tự tra web)."""

    def __init__(self, monkeypatch):
        self.log: list[tuple[str, dict]] = []
        self.fail: set[str] = set()
        mp = monkeypatch

        def plain(name):
            async def _call(prompt, **kwargs):
                self.log.append((name, {"prompt": prompt, **kwargs}))
                if name in self.fail:
                    raise RuntimeError(f"{name} lỗi")
                return openai_compatible.Response(f"{name}-ok")

            return _call

        def search(name, error=openai_compatible.NoSearchEvidenceError):
            async def _call(prompt, **kwargs):
                self.log.append((name, {"prompt": prompt, **kwargs}))
                if name in self.fail:
                    raise error(f"{name} không tra được")
                return openai_compatible.Response(f"{name}-searched", searched=True)

            return _call

        async def official(idx, prompt, **kwargs):
            name = f"api{idx}"
            self.log.append((name, {"prompt": prompt, **kwargs}))
            if kwargs.get("require_search_evidence") and name in self.fail:
                raise openai_compatible.NoSearchEvidenceError("không gọi Google Search")
            return SimpleNamespace(
                text=f"{name}-ok",
                used_fallback=True,
                searched=bool(kwargs.get("enable_search")),
            )

        mp.setattr(router9_client, "generate", plain("router9"))
        mp.setattr(router9_client, "generate_search", search("router9:search"))
        mp.setattr(groq_client, "generate", plain("groq"))
        mp.setattr(groq_client, "generate_realtime", search("groq:search"))
        mp.setattr(openrouter_client, "generate", plain("openrouter"))
        mp.setattr(openrouter_client, "generate_search", search("openrouter:search"))
        mp.setattr(orchestrator.official_client, "generate", official)

    def names(self):
        return [name for name, _ in self.log]


# ─── Chế độ native: chỉ provider tự tra web được ─────────────────────────────


@pytest.mark.asyncio
async def test_native_chi_thu_provider_tra_web_duoc_va_giu_persona_lich_su(monkeypatch):
    calls = _Calls(monkeypatch)
    response = await orchestrator.chat(1, "tin AI mới nhất hôm nay?", search_mode="native")

    assert response.text == "groq:search-searched" and response.searched
    # router9 (đứng đầu PROVIDER_ORDER) và openrouter không tự tra web được -> bị bỏ qua.
    assert calls.names() == ["groq:search"]
    kwargs = calls.log[0][1]
    assert kwargs["system_instruction"] == "PERSONA"
    assert kwargs["history"] == [("user", "lượt trước"), ("model", "em trả lời lượt trước")]
    assert kwargs["prompt"].startswith("[Thời điểm hiện tại:")
    assert "dùng công cụ tìm kiếm web" in kwargs["prompt"]
    assert kwargs["temperature"] == orchestrator._GROUNDED_TEMPERATURE


@pytest.mark.asyncio
async def test_native_groq_khong_chung_minh_da_tra_thi_sang_gemini_bat_buoc_co_grounding(
    monkeypatch,
):
    calls = _Calls(monkeypatch)
    calls.fail.add("groq:search")
    response = await orchestrator.chat(1, "giá iPhone 17 bao nhiêu?", search_mode="native")

    assert response.text == "api1-ok" and response.searched
    assert calls.names() == ["groq:search", "api1"]
    api_kwargs = calls.log[1][1]
    assert api_kwargs["enable_search"] is True
    assert api_kwargs["require_search_evidence"] is True
    assert api_kwargs["system_instruction"] == "PERSONA"
    assert provider_state.router9_dead_since is None


@pytest.mark.asyncio
async def test_native_ton_trong_cooldown_va_provider_bat_web_search(monkeypatch):
    calls = _Calls(monkeypatch)
    provider_state.api_exhausted_until["groq"] = time.time() + 600
    monkeypatch.setattr(config, "OPENROUTER_WEB_SEARCH", True)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "openrouter", "groq", "api1"])

    response = await orchestrator.chat(1, "tin mới?", search_mode="native")

    assert response.text == "openrouter:search-searched"
    assert calls.names() == ["openrouter:search"]


@pytest.mark.asyncio
async def test_native_khong_ai_tra_duoc_thi_chuoi_thuong_kem_ghi_chu_chua_tra_duoc(monkeypatch):
    calls = _Calls(monkeypatch)
    calls.fail.update({"groq:search", "api1", "api2"})

    response = await orchestrator.chat(1, "tỷ lệ lạm phát mới nhất?", search_mode="native")

    assert response.text == "router9-ok"
    assert not getattr(response, "searched", False)
    assert calls.names() == ["groq:search", "api1", "api2", "router9"]
    fallback_prompt = calls.log[-1][1]["prompt"]
    assert "KHÔNG tra được" in fallback_prompt and "TUYỆT ĐỐI không nêu số liệu" in fallback_prompt


@pytest.mark.asyncio
async def test_native_chua_provider_nao_tra_web_duoc_thi_ghi_chu_va_gemini_khong_search(
    monkeypatch,
):
    calls = _Calls(monkeypatch)
    monkeypatch.setattr(config, "GROQ_WEB_SEARCH", False)
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", None)
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", None)
    calls.fail.add("router9")

    response = await orchestrator.chat(1, "tin mới?", search_mode="native")

    assert response.text == "groq-ok"
    assert calls.names() == ["router9", "router9", "groq"]
    assert "KHÔNG tra được" in calls.log[-1][1]["prompt"]


@pytest.mark.asyncio
async def test_forced_giu_thu_tu_tra_cuu_chu_bot_chon_va_chi_thi_cam_bia(monkeypatch):
    calls = _Calls(monkeypatch)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["api2", "router9", "api1", "groq"])
    calls.fail.add("groq:search")

    response = await orchestrator.chat(1, "giá vàng SJC hôm nay", require_real_search=True)

    assert response.text == "api1-ok"
    assert calls.names() == ["groq:search", "api1"], "Groq -> api1 -> api2 bất kể PROVIDER_ORDER"
    assert orchestrator._FORCED_SEARCH_DIRECTIVE in calls.log[1][1]["prompt"]


@pytest.mark.asyncio
async def test_router9_khong_chung_minh_da_tra_web_chi_la_loi_theo_request(monkeypatch):
    calls = _Calls(monkeypatch)
    monkeypatch.setattr(config, "ROUTER9_WEB_SEARCH", True)

    async def router9_no_evidence(prompt, **kwargs):
        calls.log.append(("router9:search", kwargs))
        evidence = openai_compatible.NoSearchEvidenceError("không có url_citation")
        raise router9_client.Router9Error(str(evidence)) from evidence

    monkeypatch.setattr(router9_client, "generate_search", router9_no_evidence)
    response = await orchestrator.chat(1, "tin mới?", search_mode="native")

    assert response.text == "groq:search-searched"
    assert calls.names() == ["router9:search", "groq:search"]
    assert provider_state.router9_dead_since is None, "không tra được web != 9Router chết"


# ─── Các chế độ khác ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "gemini_search"),
    [("off", False), ("auto", True)],
)
async def test_off_va_auto_dieu_khien_google_search_cua_gemini(monkeypatch, mode, gemini_search):
    calls = _Calls(monkeypatch)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["api1"])
    await orchestrator.chat(1, "chào em", search_mode=mode)
    kwargs = calls.log[0][1]
    assert kwargs["enable_search"] is gemini_search
    assert kwargs["require_search_evidence"] is False


@pytest.mark.asyncio
async def test_rag_khong_bao_gio_bat_google_search(monkeypatch, tmp_path):
    from services import rag_service

    calls = _Calls(monkeypatch)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["api1"])
    match = SimpleNamespace(file="a.md", heading="H", text="nội dung", score=1.0)
    monkeypatch.setattr(rag_service, "_format_citations", lambda matches: "【a.md › H】 nội dung")
    await rag_service._ask_llm(1, "câu hỏi", [match])
    assert calls.log[0][1]["enable_search"] is False


@pytest.mark.asyncio
async def test_nhiet_do_thap_khi_co_grounding_cao_khi_chat_tu_do(monkeypatch):
    calls = _Calls(monkeypatch)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9"])
    await orchestrator.chat(1, "chào em")
    await orchestrator.chat(1, "thời tiết sao?", grounding="[Kết quả tool] 30 độ")
    assert calls.log[0][1]["temperature"] == orchestrator._FREE_CHAT_TEMPERATURE
    assert calls.log[1][1]["temperature"] == orchestrator._GROUNDED_TEMPERATURE


@pytest.mark.asyncio
async def test_grounding_co_du_lieu_ngoai_thi_persona_kem_luat_an_toan(monkeypatch):
    calls = _Calls(monkeypatch)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9"])
    fenced = untrusted.wrap_untrusted("kết quả web", "Tavily")
    await orchestrator.chat(1, "hỏi", grounding=fenced, search_mode="off")
    assert calls.log[0][1]["system_instruction"].endswith(untrusted.SYSTEM_RULE)


def test_strip_citations_go_trich_dan_browser_search():
    response = SimpleNamespace(text="Giá vàng 80 triệu【2†L6-L10】. Nguồn SJC [1].")
    assert orchestrator._strip_citations(response).text == "Giá vàng 80 triệu. Nguồn SJC."


# ─── Groq browser_search (thay groq/compound-mini đã khai tử) ────────────────


def test_model_compound_da_khai_tu_tu_doi_ve_gpt_oss():
    assert config._normalize_groq_realtime_model("groq/compound-mini") == "openai/gpt-oss-120b"
    assert config._normalize_groq_realtime_model("") == "openai/gpt-oss-120b"
    assert config._normalize_groq_realtime_model("openai/gpt-oss-20b") == "openai/gpt-oss-20b"


def _install_groq_transport(monkeypatch, body: dict):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(groq_client._pool, "get_client", lambda: client)
    return captured


@pytest.mark.asyncio
async def test_groq_browser_search_gui_tool_va_persona_lich_su(monkeypatch):
    body = {
        "choices": [
            {
                "message": {
                    "content": "Dạ giá vàng hôm nay 80 triệu【1†L2-L4】 anh nhé.",
                    "executed_tools": [{"type": "browser.search", "arguments": "{}"}],
                }
            }
        ]
    }
    captured = _install_groq_transport(monkeypatch, body)
    response = await groq_client.generate_realtime(
        "giá vàng?", system_instruction="PERSONA", history=[("user", "chào"), ("model", "dạ")]
    )

    payload = captured["payload"]
    assert payload["model"] == "openai/gpt-oss-120b"
    assert payload["tools"] == [{"type": "browser_search"}]
    assert payload["tool_choice"] == "required"
    assert payload["stream"] is False
    assert [m["role"] for m in payload["messages"]] == ["system", "user", "assistant", "user"]
    assert response.text == "Dạ giá vàng hôm nay 80 triệu anh nhé." and response.searched


@pytest.mark.asyncio
async def test_groq_khong_co_bang_chung_tra_web_thi_bao_loi(monkeypatch):
    _install_groq_transport(
        monkeypatch, {"choices": [{"message": {"content": "Giá vàng khoảng 80 triệu."}}]}
    )
    with pytest.raises(groq_client.RealtimeNoEvidenceError):
        await groq_client.generate_realtime("giá vàng?")


@pytest.mark.asyncio
async def test_groq_tat_web_search_khong_goi_mang(monkeypatch):
    monkeypatch.setattr(config, "GROQ_WEB_SEARCH", False)
    with pytest.raises(openai_compatible.NoSearchEvidenceError):
        await groq_client.generate_realtime("giá vàng?")


@pytest.mark.asyncio
async def test_openrouter_web_search_can_url_citation(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_WEB_SEARCH", True)
    captured = {}

    async def fake_full(client, **kwargs):
        captured.update(kwargs)
        return openai_compatible.CompletionResult(
            "Theo VnExpress...",
            annotations=[
                {"type": "url_citation", "url_citation": {"url": "https://vnexpress.net"}}
            ],
        )

    monkeypatch.setattr(openai_compatible, "post_chat_completion_full", fake_full)
    response = await openrouter_client.generate_search("tin mới?")
    assert response.searched and captured["extra_payload"]["plugins"][0]["id"] == "web"

    async def no_citation(client, **kwargs):
        return openai_compatible.CompletionResult("Em đoán là...")

    monkeypatch.setattr(openai_compatible, "post_chat_completion_full", no_citation)
    with pytest.raises(openai_compatible.NoSearchEvidenceError):
        await openrouter_client.generate_search("tin mới?")


@pytest.mark.asyncio
async def test_post_chat_completion_full_gom_annotations_tu_sse(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"content": "Theo nguồn "}}]},
        {
            "choices": [
                {
                    "delta": {
                        "content": "A.",
                        "annotations": [{"type": "url_citation", "url": "https://a.vn/x"}],
                    }
                }
            ]
        },
    ]
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request):
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await openai_compatible.post_chat_completion_full(
            client,
            base_url="https://r9.example/v1",
            api_key="k",
            messages=[],
            model="m",
            temperature=0.5,
            max_tokens=10,
            provider_label="9Router",
            stream=True,
        )
    assert result.text == "Theo nguồn A." and result.url_citations == ["https://a.vn/x"]


# ─── Kế hoạch tra web của lượt chat ──────────────────────────────────────────


def _tavily(domains):
    from ai import tavily_client

    return tavily_client.TavilySearchResponse(
        query="q",
        answer="",
        results=tuple(
            tavily_client.TavilyResult(f"R{i}", f"https://{d}/{i}", f"nội dung {i}")
            for i, d in enumerate(domains)
        ),
    )


@pytest.mark.asyncio
async def test_plan_search_tavily_tat_thi_native(monkeypatch):
    from ai import tavily_client

    async def disabled():
        return False

    monkeypatch.setattr(tavily_client, "get_enabled", disabled)
    plan = await web_search.plan_search("tin AI mới nhất hôm nay")
    assert plan == web_search.SearchPlan(mode="native")
    assert await web_search.plan_search("ừm ok") == web_search.SearchPlan()


@pytest.mark.asyncio
async def test_plan_search_tavily_tot_thi_off_mong_hoac_loi_thi_native(monkeypatch):
    from ai import tavily_client

    async def enabled():
        return True

    async def never_ask(*args, **kwargs):
        raise AssertionError("chat không còn bước provider tra web hộ riêng")

    monkeypatch.setattr(tavily_client, "get_enabled", enabled)
    monkeypatch.setattr(web_search.orchestrator, "ask", never_ask)

    async def good(*args, **kwargs):
        return _tavily(["a.vn", "b.vn", "c.vn"])

    monkeypatch.setattr(tavily_client, "search_results", good)
    plan = await web_search.plan_search("tin AI mới nhất hôm nay")
    assert plan.mode == "off" and plan.untrusted and "nội dung 0" in plan.grounding

    async def thin(*args, **kwargs):
        return _tavily(["a.vn"])

    monkeypatch.setattr(tavily_client, "search_results", thin)
    plan = await web_search.plan_search("tin AI mới nhất hôm nay")
    assert plan.mode == "native" and "nội dung 0" in plan.grounding

    async def broken(*args, **kwargs):
        raise tavily_client.TavilyError("HTTP 500")

    monkeypatch.setattr(tavily_client, "search_results", broken)
    assert await web_search.plan_search("tin AI mới nhất hôm nay") == web_search.SearchPlan(
        mode="native"
    )


# ─── run_chat_turn: chọn chế độ, lọc link, trí nhớ ───────────────────────────


@pytest.fixture
def turn_fakes(monkeypatch):
    from services import channel_chat_service as ccs

    state = {"plan": web_search.SearchPlan(), "response": None, "chat": None, "memory": None}

    async def telemetry_start(*args, **kwargs):
        return 1

    async def telemetry_noop(*args, **kwargs):
        return None

    async def no_tool(user_id, text):
        return None

    async def plan(text, *, user_id=None):
        return state["plan"]

    async def no_memory(user_id, query=""):
        return ""

    async def fake_chat(user_id, text, **kwargs):
        state["chat"] = kwargs
        return state["response"] or SimpleNamespace(text="Dạ.", used_fallback=False)

    async def save(*args):
        return None

    async def update_memory(user_id, text, reply):
        state["memory"] = reply

    monkeypatch.setattr(ccs.telemetry, "start", telemetry_start)
    monkeypatch.setattr(ccs.telemetry, "success", telemetry_noop)
    monkeypatch.setattr(ccs.telemetry, "failure", telemetry_noop)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", no_tool)
    monkeypatch.setattr(ccs.web_search, "plan_search", plan)
    monkeypatch.setattr(ccs.memory_service, "build_memory_context", no_memory)
    monkeypatch.setattr(ccs.memory_service, "update_memory", update_memory)
    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.db, "add_chat_turn", save)
    return state


@pytest.mark.asyncio
async def test_run_chat_turn_tavily_tat_cau_can_tin_moi_dung_native(turn_fakes):
    from services import channel_chat_service as ccs

    turn_fakes["plan"] = web_search.SearchPlan(mode="native")
    turn_fakes["response"] = SimpleNamespace(
        text="Theo [VnExpress](https://vnexpress.net/x?a=1) ...", used_fallback=False, searched=True
    )
    turn = await ccs.run_chat_turn(1, "tin AI mới nhất hôm nay")
    await ccs.stop_background_tasks()

    assert turn_fakes["chat"]["search_mode"] == "native"
    # Provider tự tra web: link nguồn do provider tìm ra -> không lọc.
    assert "https://vnexpress.net/x?a=1" in turn.text
    assert turn_fakes["memory"] == "", "lượt dựa trên web: chỉ trích nhớ từ lời người dùng"


@pytest.mark.asyncio
async def test_run_chat_turn_chon_che_do_theo_nguon_du_lieu(turn_fakes):
    from services import channel_chat_service as ccs

    turn_fakes["plan"] = web_search.SearchPlan(mode="native")
    await ccs.run_chat_turn(1, "giá vàng SJC hôm nay thế nào?")
    assert turn_fakes["chat"]["search_mode"] == "forced"

    await ccs.run_chat_turn(1, "FPT hôm nay thế nào?", grounding="[Giá FPT từ DNSE] 120")
    assert turn_fakes["chat"]["search_mode"] == "off", "số liệu cổ phiếu chuẩn: không tra chồng"

    turn_fakes["plan"] = web_search.SearchPlan()
    await ccs.run_chat_turn(1, "chào em")
    assert turn_fakes["chat"]["search_mode"] == "auto"
    await ccs.stop_background_tasks()


@pytest.mark.asyncio
async def test_run_chat_turn_luot_tavily_loc_link_la_va_giu_link_nguon(turn_fakes):
    from services import channel_chat_service as ccs

    grounding = untrusted.wrap_untrusted(
        "Nguồn https://cafef.vn/bai-1.chn: VN-Index tăng", "Tavily"
    )
    turn_fakes["plan"] = web_search.SearchPlan(grounding, "off", True)
    turn_fakes["response"] = SimpleNamespace(
        text="VN-Index tăng (https://cafef.vn/bai-1.chn). Xem https://evil.example/?q=bi_mat",
        used_fallback=False,
    )
    turn = await ccs.run_chat_turn(1, "VN-Index hôm nay thế nào?")
    await ccs.stop_background_tasks()

    assert "https://cafef.vn/bai-1.chn" in turn.text
    assert "https://evil.example" not in turn.text and "hxxps://evil[.]example" in turn.text
    assert turn_fakes["memory"] == ""


@pytest.mark.asyncio
async def test_run_chat_turn_chat_thuong_khong_loc_link_va_nho_ca_cau_tra_loi(turn_fakes):
    from services import channel_chat_service as ccs

    turn_fakes["response"] = SimpleNamespace(
        text="Anh vào https://vnexpress.net xem nhé", used_fallback=False
    )
    turn = await ccs.run_chat_turn(1, "báo nào hay em?")
    await ccs.stop_background_tasks()
    assert turn.text == "Anh vào https://vnexpress.net xem nhé"
    assert turn_fakes["memory"] == "Anh vào https://vnexpress.net xem nhé"


@pytest.mark.asyncio
async def test_trich_nho_luot_web_chi_tu_loi_nguoi_dung(monkeypatch):
    from services import memory_service

    prompt = memory_service._build_extraction_prompt([], "", "anh tên Nam", "")
    assert "CHỈ trích fact/highlight từ lời User" in prompt


# ─── /status, /tavily: mô tả chế độ tra web ──────────────────────────────────


@pytest.mark.asyncio
async def test_search_mode_line_tavily_tat_liet_ke_provider_tra_web(monkeypatch):
    from ai import tavily_client

    async def disabled():
        return False

    async def key():
        return ""

    monkeypatch.setattr(tavily_client, "get_enabled", disabled)
    monkeypatch.setattr(tavily_client, "api_key", key)
    line = await search_status.search_mode_line()
    assert "web search của chính provider" in line
    assert "Groq, API 1, API 2" in line
    assert "OpenRouter" not in line and "9Router" not in line

    async def enabled():
        return True

    async def has_key():
        return "tvly"

    monkeypatch.setattr(tavily_client, "get_enabled", enabled)
    monkeypatch.setattr(tavily_client, "api_key", has_key)
    line = await search_status.search_mode_line()
    assert line.startswith("🔎 Tra web: Tavily") and "Groq, API 1, API 2" in line


@pytest.mark.asyncio
async def test_lenh_tavily_tren_zalo_bao_che_do_tra_web(monkeypatch):
    from ai import tavily_client
    from services import channel_command_service as service

    async def key():
        return ""

    monkeypatch.setattr(tavily_client, "api_key", key)
    result, _ = await service.maybe_handle_command(1, "/tavily off")
    assert "Đã tắt Tavily" in result[0] and "web search của chính provider" in result[0]
    result, _ = await service.maybe_handle_command(1, "/status")
    assert "🔎 Tra web:" in result[0]


@pytest.mark.asyncio
async def test_lenh_tavily_tren_telegram_bao_che_do_tra_web(monkeypatch):
    from ai import tavily_client
    from handlers import provider_commands

    replies = []

    async def key():
        return "tvly"

    async def reply_text(text, **kwargs):
        replies.append(text)

    monkeypatch.setattr(tavily_client, "api_key", key)
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_text),
    )
    context = SimpleNamespace(args=["on"])
    await provider_commands.tavily_toggle_cmd(update, context)
    assert "Đã bật tra web Tavily" in replies[-1]
    assert "Tavily lỗi hoặc ít nguồn thì dùng web search của provider" in replies[-1]
