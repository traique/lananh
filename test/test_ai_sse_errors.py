"""Chunk {"error": ...} của gateway SAU khi đã có chữ: không trả câu trả lời bị cụt im
lặng mà raise để provider-chain thử provider kế; bản nháp đang stream cũng vậy."""

import json

import httpx
import pytest

from ai import drafts, openai_compatible


def _sse(*chunks: dict) -> str:
    return "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + (
        "data: [DONE]\n\n"
    )


def _text(piece: str, finish: str | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": finish}]}


_ERROR = {"error": {"message": "upstream reset", "code": 502}}


def test_loi_sau_chu_chua_ket_thuc_thi_raise_khong_tra_cau_cut():
    raw = _sse(_text("Dạ giá vàng hôm nay "), _text("là 8"), _ERROR)
    with pytest.raises(openai_compatible.OpenAICompatibleError) as info:
        openai_compatible.parse_sse_content(raw)
    assert "bị cụt" in str(info.value) and "upstream reset" in str(info.value)
    assert info.value.status_code == 502


def test_loi_sau_khi_da_ket_thuc_binh_thuong_van_giu_cau_tra_loi():
    raw = _sse(_text("Dạ em đây."), _text("", "stop"), _ERROR)
    assert openai_compatible.parse_sse_content(raw) == "Dạ em đây."
    # Không có chunk lỗi: stream thiếu finish_reason vẫn trả chữ như cũ.
    assert openai_compatible.parse_sse_content(_sse(_text("Dạ"))) == "Dạ"


def test_chi_co_loi_khong_chu_van_raise_nhu_cu():
    with pytest.raises(openai_compatible.OpenAICompatibleError, match="upstream reset"):
        openai_compatible.parse_sse_content(_sse(_ERROR))


@pytest.mark.asyncio
async def test_ban_nhap_dang_stream_gap_loi_giua_chung_cung_raise():
    body = _sse(_text("Dạ "), _text("em"), _ERROR).encode()

    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    seen: list[str] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with drafts.sink_scope(seen.append), drafts.drafting():
            with pytest.raises(openai_compatible.OpenAICompatibleError, match="bị cụt"):
                await openai_compatible.post_chat_completion(
                    client,
                    base_url="https://gw.example",
                    api_key="k",
                    messages=[{"role": "user", "content": "hi"}],
                    model="m",
                    temperature=0.5,
                    max_tokens=100,
                    provider_label="Test",
                )
    assert seen == ["Dạ ", "Dạ em"]
