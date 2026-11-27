"""Đầu ra JSON có schema (ai/structured.py): router tool của chat xin Groq json_schema
và Gemini response_json_schema sinh từ danh bạ tool; model từ chối thì lùi về JSON
thường 1 lần và nhớ model đó."""

from types import SimpleNamespace

import pytest

from ai import groq_client, official_client, openai_compatible, orchestrator, structured
from core import config
from services import tool_registry as reg


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(structured, "_unsupported", set())


@pytest.fixture
def groq_http(monkeypatch):
    sent = []
    state = SimpleNamespace(reject=False)

    async def fake_post(client, **kwargs):
        sent.append(kwargs["extra_payload"]["response_format"])
        if state.reject and kwargs["extra_payload"]["response_format"]["type"] == "json_schema":
            raise openai_compatible.OpenAICompatibleError(
                "Groq JSON HTTP 400: response_format `json_schema` is not supported with this model",
                status_code=400,
            )
        return '{"tool": "none", "args": {}}'

    async def key():
        return "gk"

    async def model():
        return "openai/gpt-oss-20b"

    monkeypatch.setattr(openai_compatible, "post_chat_completion", fake_post)
    monkeypatch.setattr(groq_client, "_api_key", key)
    monkeypatch.setattr(groq_client, "_model", model)
    return sent, state


_SCHEMA = {"type": "object", "properties": {"tool": {"type": "string"}}, "required": ["tool"]}


def test_schema_router_sinh_tu_danh_ba_tool():
    specs = [
        reg.ToolSpec(name="save_note", description="x", parameters={}, handler=None),
        reg.ToolSpec(name="list_notes", description="y", parameters={}, handler=None),
    ]
    schema = reg.router_schema(specs)
    assert schema["properties"]["tool"]["enum"] == ["save_note", "list_notes", "none"]
    assert schema["required"] == ["tool"] and schema["properties"]["args"] == {"type": "object"}


async def test_groq_json_schema_khi_co_pham_vi(groq_http):
    sent, _ = groq_http
    assert await groq_client.generate_json("trả JSON") == {"tool": "none", "args": {}}
    with structured.json_schema(_SCHEMA, "tool_router"):
        await groq_client.generate_json("trả JSON")
    assert sent[0] == {"type": "json_object"}
    assert sent[1] == {
        "type": "json_schema",
        "json_schema": {"name": "tool_router", "schema": _SCHEMA},
    }


async def test_groq_tu_choi_schema_thi_lui_json_object_va_nho(groq_http):
    sent, state = groq_http
    state.reject = True
    with structured.json_schema(_SCHEMA):
        assert await groq_client.generate_json("trả JSON") == {"tool": "none", "args": {}}
        await groq_client.generate_json("trả JSON")
    assert [item["type"] for item in sent] == ["json_schema", "json_object", "json_object"]
    assert ("groq", "openai/gpt-oss-20b") in structured._unsupported


async def test_groq_loi_khac_khong_nuot(groq_http, monkeypatch):
    async def boom(client, **kwargs):
        raise openai_compatible.OpenAICompatibleError("Groq JSON HTTP 429: rate", status_code=429)

    monkeypatch.setattr(openai_compatible, "post_chat_completion", boom)
    with structured.json_schema(_SCHEMA), pytest.raises(openai_compatible.OpenAICompatibleError):
        await groq_client.generate_json("trả JSON")
    assert not structured._unsupported


class _Models:
    def __init__(self, reject=False):
        self.configs = []
        self.reject = reject

    async def generate_content(self, model, contents, config):
        self.configs.append(config)
        if self.reject and config.response_json_schema is not None:
            error = RuntimeError("400 INVALID_ARGUMENT: Invalid response_json_schema")
            error.code = 400
            raise error
        return SimpleNamespace(text='{"tool": "none"}', usage_metadata=None, candidates=[])


@pytest.fixture
def gemini(monkeypatch):
    models = _Models()

    async def get_client(idx):
        return SimpleNamespace(aio=SimpleNamespace(models=models))

    async def model_for(idx):
        return "gemini-test-lite"

    async def usable():
        return [1]

    async def no_record(*args, **kwargs):
        return None

    monkeypatch.setattr(official_client, "_get_official_client", get_client)
    monkeypatch.setattr(official_client, "_model_for", model_for)
    monkeypatch.setattr(official_client, "usable_indices", usable)
    monkeypatch.setattr(official_client.call_log, "record", no_record)
    monkeypatch.setattr(config, "HAS_ANY_AI_STUDIO_KEY", True)
    return models


async def test_gemini_response_json_schema_va_lui_khi_bi_tu_choi(gemini):
    await official_client.generate_utility_json("trả JSON")
    assert gemini.configs[-1].response_json_schema is None
    with structured.json_schema(_SCHEMA):
        await official_client.generate_utility_json("trả JSON")
    assert gemini.configs[-1].response_json_schema == _SCHEMA
    gemini.reject = True
    with structured.json_schema(_SCHEMA):
        assert await official_client.generate_utility_json("trả JSON") == {"tool": "none"}
        await official_client.generate_utility_json("trả JSON")
    assert [c.response_json_schema for c in gemini.configs[2:]] == [_SCHEMA, None, None]
    assert ("gemini", "gemini-test-lite") in structured._unsupported


async def test_router_tool_chat_mo_pham_vi_schema(monkeypatch):
    from services import tools, web_search

    seen = {}

    async def fake_utility(prompt):
        seen["schema"] = structured.current()
        return {"tool": "none", "args": {}}

    async def no_history(user_id, turns=3):
        return ""

    monkeypatch.setattr(orchestrator, "utility_json", fake_utility)
    monkeypatch.setattr(web_search, "recent_history_text", no_history)
    outcome = await tools.maybe_run_tool(1, "ghi chú giúp anh mai họp lúc 9h")
    assert outcome.router_ran
    name, schema = seen["schema"]
    assert name == "tool_router"
    assert "save_note" in schema["properties"]["tool"]["enum"]
    assert schema["properties"]["tool"]["enum"][-1] == "none"
    assert structured.current() is None  # hết lượt router là đóng phạm vi
