"""Nhánh Groq của provider-chain (router9 -> groq -> openrouter -> api1 -> api2),
gateway OpenAI-compatible (core.config.GROQ_BASE_URL/GROQ_API_KEY), miễn phí.

generate_realtime() dùng model GROQ_REALTIME_MODEL (mặc định groq/compound-mini,
có tool tìm kiếm web tích hợp sẵn) - dành riêng cho tác vụ require_real_search,
đứng trước api1/api2 (Gemini grounding) trong chuỗi search vì cũng miễn phí.
Không dùng cho ask()/chat() thường vì compound-mini chậm hơn model chat thường.
"""
from typing import Optional

from ai import openai_compatible, provider_overrides
from ai.openai_compatible import Response
from core import config

_pool = openai_compatible.ClientPool(config.GROQ_CALL_TIMEOUT_SEC, config.GROQ_MAX_CONCURRENCY)


async def _api_key() -> str:
    return await provider_overrides.get_api_key_override("groq") or config.GROQ_API_KEY


async def _model() -> str:
    return await provider_overrides.get_model_override("groq") or config.GROQ_MODEL


class RealtimeNoEvidenceError(openai_compatible.OpenAICompatibleError):
    """generate_realtime() trả lời nhưng không có dấu hiệu đã tra web thật
    (không URL/nguồn nào) - coi như thất bại để orchestrator rơi xuống Gemini
    grounding, tránh nuốt câu trả lời bịa từ trí nhớ model."""


async def close() -> None:
    await _pool.close()


async def generate(
    prompt: str,
    *,
    system_instruction: Optional[str] = None,
    history: Optional[list[tuple[str, str]]] = None,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> Response:
    api_key = await _api_key()
    if not api_key:
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình GROQ_API_KEY")
    messages = openai_compatible.build_messages(prompt, system_instruction, history)
    async with _pool.get_semaphore():
        text = await openai_compatible.post_chat_completion(
            _pool.get_client(),
            base_url=config.GROQ_BASE_URL,
            api_key=api_key,
            messages=messages,
            model=model or await _model(),
            temperature=temperature,
            max_tokens=max_tokens,
            provider_label="Groq",
        )
    return Response(text)


async def generate_image_prompt(instruction: str, image_path: str) -> Response:
    api_key = await _api_key()
    if not api_key:
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình GROQ_API_KEY")
    return await openai_compatible.generate_image_prompt(
        _pool,
        base_url=config.GROQ_BASE_URL,
        api_key=api_key,
        vision_model=config.GROQ_VISION_MODEL,
        provider_label="Groq vision",
        instruction=instruction,
        image_path=image_path,
    )


_WEB_EVIDENCE_MARKERS = ("http://", "https://", "nguồn:", "source:")


def _has_web_evidence(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _WEB_EVIDENCE_MARKERS)


async def generate_realtime(
    prompt: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 2048,
) -> Response:
    api_key = await _api_key()
    if not api_key:
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình GROQ_API_KEY")
    messages = openai_compatible.build_messages(prompt)
    async with _pool.get_semaphore():
        text = await openai_compatible.post_chat_completion(
            _pool.get_client(),
            base_url=config.GROQ_BASE_URL,
            api_key=api_key,
            messages=messages,
            model=config.GROQ_REALTIME_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            provider_label="Groq realtime",
        )
    if not _has_web_evidence(text):
        raise RealtimeNoEvidenceError("Groq realtime không có dấu hiệu đã tra web thật")
    return Response(text)


async def check_status() -> tuple[bool, str]:
    return await openai_compatible.check_status(
        generate, api_key=await _api_key(), missing_key_msg="Chưa cấu hình GROQ_API_KEY"
    )
