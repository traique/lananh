"""Nhánh OpenRouter của provider-chain (router9 -> groq -> openrouter -> api1
-> api2), gateway OpenAI-compatible (core.config.OPENROUTER_BASE_URL/
OPENROUTER_API_KEY), miễn phí (model mặc định có hậu tố ":free").

generate_search(): OpenRouter tự tra web qua plugin "web" (openrouter.ai/docs/
guides/features/web-search) - TỐN CREDIT kể cả với model ":free", nên chỉ bật
qua OPENROUTER_WEB_SEARCH (mặc định tắt). Bằng chứng đã tra: annotations
url_citation trong message; không có thì coi như chưa tra (NoSearchEvidenceError)."""

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
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình OPENROUTER_API_KEY")
    messages = openai_compatible.build_messages(prompt, system_instruction, history)
    model_name = model or await _model()

    async def _call() -> str:
        async with _pool.get_semaphore():
            return await openai_compatible.post_chat_completion(
                _pool.get_client(),
                base_url=config.OPENROUTER_BASE_URL,
                api_key=api_key,
                messages=messages,
                model=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label="OpenRouter",
            )

    return Response(await _pool.bounded("OpenRouter", _call))


# /agent nhiều bước (ai/agent_service.py): free tier 20 RPM, 50 request/ngày dùng
# chung với chat dự phòng -> max_tokens thấp; require_parameters để chỉ đi tới nhà
# cung cấp thật sự hỗ trợ tools (không âm thầm bỏ qua).
AGENT_MAX_TOKENS = 1024


async def generate_with_tools(
    messages: list[dict],
    tools: list[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = AGENT_MAX_TOKENS,
) -> openai_compatible.ToolCallResponse:
    """1 bước function-calling chuẩn OpenAI cho agent. KHÔNG đánh dấu cooldown khi
    lỗi (429...): agent tự chuyển provider kế, utility_json của chat không bị vạ lây."""
    api_key = await _api_key()
    if not api_key:
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình OPENROUTER_API_KEY")
    model_name = model or await _model()

    async def _call() -> openai_compatible.ToolCallResponse:
        async with _pool.get_semaphore():
            return await openai_compatible.post_chat_completion_with_tools(
                _pool.get_client(),
                base_url=config.OPENROUTER_BASE_URL,
                api_key=api_key,
                messages=messages,
                tools=tools,
                model=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label="OpenRouter tools",
                extra_payload={"provider": {"require_parameters": True}},
            )

    return await _pool.bounded("OpenRouter tools", _call)


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
    api_key = await _api_key()
    if not api_key:
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình OPENROUTER_API_KEY")
    messages = openai_compatible.build_messages(prompt, system_instruction, history)
    model_name = await _model()

    async def _call() -> openai_compatible.CompletionResult:
        async with _pool.get_semaphore():
            return await openai_compatible.post_chat_completion_full(
                _pool.get_client(),
                base_url=config.OPENROUTER_BASE_URL,
                api_key=api_key,
                messages=messages,
                model=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label="OpenRouter web search",
                extra_payload={"plugins": [{"id": "web", "max_results": 5}]},
            )

    result = await _pool.bounded("OpenRouter web search", _call)
    if not result.url_citations:
        raise openai_compatible.NoSearchEvidenceError(
            "OpenRouter trả lời nhưng không kèm url_citation - không tin là đã tra web"
        )
    return Response(result.text, searched=True)


async def generate_image_prompt(instruction: str, image_path: str) -> Response:
    api_key = await _api_key()
    if not api_key:
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình OPENROUTER_API_KEY")
    return await openai_compatible.generate_image_prompt(
        _pool,
        base_url=config.OPENROUTER_BASE_URL,
        api_key=api_key,
        vision_model=config.OPENROUTER_VISION_MODEL,
        provider_label="OpenRouter vision",
        instruction=instruction,
        image_path=image_path,
    )


async def check_status() -> tuple[bool, str]:
    return await openai_compatible.check_status(
        generate, api_key=await _api_key(), missing_key_msg="Chưa cấu hình OPENROUTER_API_KEY"
    )


# Public accessors (dùng bởi web_admin) - tránh module khác gọi hàm private.
api_key = _api_key
model_name = _model
