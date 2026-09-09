"""Unit test cho phần function-calling (tools) mới thêm vào
ai/openai_compatible.py (post_chat_completion_with_tools, _parse_tool_calls).

Chạy: pytest test/test_openai_compatible_tools.py -v
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import openai_compatible  # noqa: E402


def _client_with_response(json_body: dict, status_code: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _client_with_sse(chunks: list[dict]) -> httpx.AsyncClient:
    """Gateway trả SSE dù request stream:true (chuẩn chatgpt-gateway)."""
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert b'"stream":true' in request.content  # luôn phải xin stream
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_stream_sse_gom_text_dung():
    client = _client_with_sse([
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Xin chào "}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "anh!"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])
    from ai import openai_compatible as o
    text = await o.post_chat_completion(
        client, base_url="https://fake.example", api_key="k",
        messages=[{"role": "user", "content": "chào"}], model="m",
        temperature=0.7, max_tokens=100, provider_label="test",
    )
    assert text == "Xin chào anh!"


def _sse_chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


@pytest.mark.asyncio
async def test_sse_tool_calls_delta_gom_dung():
    """Tool_calls dạng delta chuẩn OpenAI: chunk đầu id+name, các chunk sau
    nối arguments theo index - phải ghép lại thành list hoàn chỉnh."""
    args_chunk1 = '{"ten":'
    args_chunk2 = ' "FPT"}'
    client = _client_with_sse([
        _sse_chunk({"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "tim_gia", "arguments": ""}}]}),
        _sse_chunk({"tool_calls": [{"index": 0, "function": {"arguments": args_chunk1}}]}),
        _sse_chunk({"tool_calls": [{"index": 0, "function": {"arguments": args_chunk2}}]}),
        _sse_chunk({}, finish_reason="tool_calls"),
    ])
    result = await openai_compatible.post_chat_completion_with_tools(
        client, base_url="https://fake.example", api_key="k",
        messages=[], tools=[{"type": "function", "function": {"name": "tim_gia", "parameters": {}}}],
        model="m", temperature=0.7, max_tokens=100, provider_label="test",
    )
    assert result.text == ""
    assert result.tool_calls == [{"id": "call_1", "name": "tim_gia", "arguments": {"ten": "FPT"}}]


@pytest.mark.asyncio
async def test_sse_error_chunk_surface_thong_diep_upstream():
    client = _client_with_sse([
        {"error": {"message": "ChatGPT upstream failed.", "type": "upstream_error"}},
    ])
    with pytest.raises(openai_compatible.OpenAICompatibleError, match="ChatGPT upstream failed"):
        await openai_compatible.post_chat_completion_with_tools(
            client, base_url="https://fake.example", api_key="k",
            messages=[], tools=[], model="m", temperature=0.7, max_tokens=100,
            provider_label="test",
        )


@pytest.mark.asyncio
async def test_tool_call_duoc_parse_dung_khi_model_muon_goi_tool():
    body = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {"name": "tim_gia", "arguments": '{"ten_san_pham": "iPhone 15"}'},
                        }
                    ],
                }
            }
        ]
    }
    client = _client_with_response(body)
    result = await openai_compatible.post_chat_completion_with_tools(
        client,
        base_url="https://fake.example",
        api_key="k",
        messages=[{"role": "user", "content": "giá iphone 15"}],
        tools=[],
        model="fake-model",
        temperature=0.7,
        max_tokens=100,
        provider_label="test",
    )
    assert result.tool_calls == [{"id": "call_abc", "name": "tim_gia", "arguments": {"ten_san_pham": "iPhone 15"}}]
    assert result.text == ""


@pytest.mark.asyncio
async def test_khong_tool_call_thi_tra_text_binh_thuong():
    body = {"choices": [{"message": {"content": "Xin chào, em giúp được gì ạ?", "tool_calls": None}}]}
    client = _client_with_response(body)
    result = await openai_compatible.post_chat_completion_with_tools(
        client,
        base_url="https://fake.example",
        api_key="k",
        messages=[{"role": "user", "content": "chào"}],
        tools=[],
        model="fake-model",
        temperature=0.7,
        max_tokens=100,
        provider_label="test",
    )
    assert result.tool_calls == []
    assert result.text == "Xin chào, em giúp được gì ạ?"


@pytest.mark.asyncio
async def test_arguments_json_hong_khong_lam_crash_ca_response():
    body = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "tim_gia", "arguments": "{khong-phai-json"}}
                    ],
                }
            }
        ]
    }
    client = _client_with_response(body)
    result = await openai_compatible.post_chat_completion_with_tools(
        client,
        base_url="https://fake.example",
        api_key="k",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        model="fake-model",
        temperature=0.7,
        max_tokens=100,
        provider_label="test",
    )
    # args hỏng -> {} thay vì raise, để 1 tool_call hỏng không sập cả response.
    assert result.tool_calls == [{"id": "call_1", "name": "tim_gia", "arguments": {}}]


@pytest.mark.asyncio
async def test_http_loi_raise_openaicompatibleerror():
    client = _client_with_response({"error": "rate limited"}, status_code=429)
    with pytest.raises(openai_compatible.OpenAICompatibleError, match="HTTP 429"):
        await openai_compatible.post_chat_completion_with_tools(
            client,
            base_url="https://fake.example",
            api_key="k",
            messages=[],
            tools=[],
            model="fake-model",
            temperature=0.7,
            max_tokens=100,
            provider_label="test",
        )


@pytest.mark.asyncio
async def test_rong_ca_text_lan_tool_calls_raise_loi():
    body = {"choices": [{"message": {"content": "", "tool_calls": None}}]}
    client = _client_with_response(body)
    with pytest.raises(openai_compatible.OpenAICompatibleError, match="trả kết quả rỗng"):
        await openai_compatible.post_chat_completion_with_tools(
            client,
            base_url="https://fake.example",
            api_key="k",
            messages=[],
            tools=[],
            model="fake-model",
            temperature=0.7,
            max_tokens=100,
            provider_label="test",
        )


def test_parse_tool_calls_bo_qua_item_thieu_name():
    raw = [
        {"id": "1", "function": {"name": "", "arguments": "{}"}},
        {"id": "2", "function": {"name": "ok_tool", "arguments": json.dumps({"a": 1})}},
    ]
    parsed = openai_compatible._parse_tool_calls(raw)
    assert parsed == [{"id": "2", "name": "ok_tool", "arguments": {"a": 1}}]


def test_parse_tool_calls_none_tra_ve_rong():
    assert openai_compatible._parse_tool_calls(None) == []
