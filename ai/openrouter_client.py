"""Nhánh OpenRouter của provider-chain (router9 -> groq -> openrouter -> api1
-> api2), gateway OpenAI-compatible (core.config.OPENROUTER_BASE_URL/
OPENROUTER_API_KEY), miễn phí (model mặc định có hậu tố ":free").

Không có generate_realtime() như groq_client - model ":free" trên OpenRouter
không đảm bảo có tool tìm kiếm web tích hợp, nên KHÔNG dùng cho
require_real_search (xem ai/orchestrator.py:_search_only_providers)."""
from typing import Optional

from ai import openai_compatible
from ai.openai_compatible import Response
from core import config

_pool = openai_compatible.ClientPool(config.OPENROUTER_CALL_TIMEOUT_SEC, config.OPENROUTER_MAX_CONCURRENCY)


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
    if not config.OPENROUTER_API_KEY:
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình OPENROUTER_API_KEY")
    messages = openai_compatible.build_messages(prompt, system_instruction, history)
    async with _pool.get_semaphore():
        text = await openai_compatible.post_chat_completion(
            _pool.get_client(),
            base_url=config.OPENROUTER_BASE_URL,
            api_key=config.OPENROUTER_API_KEY,
            messages=messages,
            model=model or config.OPENROUTER_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            provider_label="OpenRouter",
        )
    return Response(text)


async def generate_image_prompt(instruction: str, image_path: str) -> Response:
    if not config.OPENROUTER_API_KEY:
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình OPENROUTER_API_KEY")
    return await openai_compatible.generate_image_prompt(
        _pool,
        base_url=config.OPENROUTER_BASE_URL,
        api_key=config.OPENROUTER_API_KEY,
        vision_model=config.OPENROUTER_VISION_MODEL,
        provider_label="OpenRouter vision",
        instruction=instruction,
        image_path=image_path,
    )


async def check_status() -> tuple[bool, str]:
    return await openai_compatible.check_status(
        generate, api_key=config.OPENROUTER_API_KEY, missing_key_msg="Chưa cấu hình OPENROUTER_API_KEY"
    )
