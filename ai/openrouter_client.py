"""Nhánh OpenRouter của provider-chain (router9 -> groq -> openrouter -> api1
-> api2), gateway OpenAI-compatible (core.config.OPENROUTER_BASE_URL/
OPENROUTER_API_KEY), miễn phí (model mặc định có hậu tố ":free").

Phần chung (chat, tools của /agent, ảnh, check_status) nằm ở
openai_compatible.OpenAICompatibleProvider - các tên hàm cũ dưới đây là alias.

generate_search(): OpenRouter tự tra web qua plugin "web" (openrouter.ai/docs/
guides/features/web-search) - TỐN CREDIT kể cả với model ":free", nên chỉ bật
qua OPENROUTER_WEB_SEARCH (mặc định tắt). Bằng chứng đã tra: annotations
url_citation trong message; không có thì coi như chưa tra (NoSearchEvidenceError)."""

import asyncio
import base64
from typing import Optional

from ai import openai_compatible, provider_overrides
from ai.openai_compatible import Response
from core import config

_pool = openai_compatible.ClientPool(
    config.OPENROUTER_CALL_TIMEOUT_SEC,
    config.OPENROUTER_MAX_CONCURRENCY,
    config.OPENROUTER_TOTAL_TIMEOUT_SEC,
)


async def _api_key() -> str:
    return await provider_overrides.get_api_key_override("openrouter") or config.OPENROUTER_API_KEY


async def _model() -> str:
    return await provider_overrides.get_model_override("openrouter") or config.OPENROUTER_MODEL


# /agent nhiều bước (ai/agent_service.py): free tier 20 RPM, 50 request/ngày dùng
# chung với chat dự phòng -> max_tokens thấp; require_parameters để chỉ đi tới nhà
# cung cấp thật sự hỗ trợ tools (không âm thầm bỏ qua).
AGENT_MAX_TOKENS = 1024

_provider = openai_compatible.OpenAICompatibleProvider(
    "OpenRouter",
    pool=_pool,
    base_url=lambda: config.OPENROUTER_BASE_URL,
    api_key=lambda: _api_key(),
    default_model=lambda: _model(),
    vision_model=lambda: config.OPENROUTER_VISION_MODEL,
    missing_key="Chưa cấu hình OPENROUTER_API_KEY",
    tools_payload={"provider": {"require_parameters": True}},
    tools_temperature=0.3,
    tools_max_tokens=AGENT_MAX_TOKENS,
)

close = _provider.close
generate = _provider.generate
generate_with_tools = _provider.generate_with_tools
generate_image_prompt = _provider.generate_image_prompt
check_status = _provider.check_status


def supports_web_search() -> bool:
    return config.OPENROUTER_WEB_SEARCH


async def generate_search(
    prompt: str,
    *,
    system_instruction: Optional[str] = None,
    history: Optional[list[tuple[str, str]]] = None,
    temperature: float = 0.5,
    max_tokens: int = 4096,
) -> Response:
    """1 lượt OpenRouter TỰ tra web (plugin "web", tối đa 5 kết quả)."""
    if not supports_web_search():
        raise openai_compatible.NoSearchEvidenceError("OpenRouter web search đang tắt")
    messages = openai_compatible.build_messages(prompt, system_instruction, history)
    result = await _provider.complete_full(
        messages,
        label="OpenRouter web search",
        temperature=temperature,
        max_tokens=max_tokens,
        extra_payload={"plugins": [{"id": "web", "max_results": 5}]},
    )
    if not result.url_citations:
        raise openai_compatible.NoSearchEvidenceError(
            "OpenRouter trả lời nhưng không kèm url_citation - không tin là đã tra web"
        )
    return Response(result.text, searched=True, citations=result.source_urls)


# Engine file-parser MIỄN PHÍ (openrouter.ai/docs/features/multimodal/pdfs):
# "pdf-text" cũ đã bị OpenRouter chuyển hướng sang "cloudflare-ai". "mistral-ocr"
# ($2/1.000 trang) và "native" (tính token) KHÔNG BAO GIỜ được dùng.
_FREE_PDF_ENGINES = {"cloudflare-ai": "cloudflare-ai", "pdf-text": "cloudflare-ai"}
PDF_MAX_TOKENS = 4096


def pdf_engine() -> str:
    """Engine đọc PDF đang bật ("" = tắt: chưa đặt hoặc đặt engine tốn tiền)."""
    return _FREE_PDF_ENGINES.get(config.OPENROUTER_PDF_ENGINE, "")


async def analyze_document(instruction: str, path: str, *, filename: str) -> Response:
    """Hỏi đáp 1 file PDF: content part "file" (data URL base64) + plugin
    file-parser GHIM engine miễn phí - không ghim thì model không đọc PDF native
    sẽ rơi vào mistral-ocr tính tiền."""
    engine = pdf_engine()
    if not engine:
        raise openai_compatible.OpenAICompatibleError(
            "OpenRouter đọc PDF đang tắt (OPENROUTER_PDF_ENGINE khác cloudflare-ai)"
        )
    # Kiểm key TRƯỚC khi đọc file vào RAM.
    await _provider.credentials()

    def _read_bytes() -> bytes:
        with open(path, "rb") as file:
            return file.read()

    data = await asyncio.to_thread(_read_bytes)
    data_url = "data:application/pdf;base64," + base64.b64encode(data).decode("ascii")
    del data
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "file", "file": {"filename": filename, "file_data": data_url}},
            ],
        }
    ]
    return Response(
        await _provider.complete(
            messages,
            label="OpenRouter PDF",
            temperature=0.3,
            max_tokens=PDF_MAX_TOKENS,
            extra_payload={"plugins": [{"id": "file-parser", "pdf": {"engine": engine}}]},
        )
    )


# Public accessors (dùng bởi web_admin) - tránh module khác gọi hàm private.
api_key = _api_key
model_name = _model
