"""Nhánh Groq của provider-chain (router9 -> groq -> openrouter -> api1 -> api2),
gateway OpenAI-compatible (core.config.GROQ_BASE_URL/GROQ_API_KEY).

generate_realtime() là lượt Groq TỰ tra web: model GROQ_REALTIME_MODEL (mặc định
openai/gpt-oss-120b) + tool dựng sẵn ``browser_search`` chạy phía server Groq
(console.groq.com/docs/browser-search; groq/compound-mini cũ đã bị khai tử
21/09/2026). Nhận persona + lịch sử + trí nhớ như chat thường để câu trả lời
giữ giọng Lan Anh. Không dùng cho chat thường vì chậm và tốn quota tra web.
"""

import json
import logging
import re
from typing import Optional

from ai import openai_compatible, provider_overrides
from ai.openai_compatible import Response
from core import config
from core.text_utils import strip_json_fence

logger = logging.getLogger(__name__)

_pool = openai_compatible.ClientPool(
    config.GROQ_CALL_TIMEOUT_SEC, config.GROQ_MAX_CONCURRENCY, config.GROQ_TOTAL_TIMEOUT_SEC
)


async def _api_key() -> str:
    return await provider_overrides.get_api_key_override("groq") or config.GROQ_API_KEY


async def _model() -> str:
    return await provider_overrides.get_model_override("groq") or config.GROQ_MODEL


class RealtimeNoEvidenceError(openai_compatible.NoSearchEvidenceError):
    """generate_realtime() trả lời nhưng không có dấu hiệu đã tra web thật
    (không executed_tools, không trích dẫn/URL/nguồn nào) - coi như thất bại để
    orchestrator rơi xuống provider kế tiếp, tránh nuốt câu trả lời bịa từ trí
    nhớ model."""


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
    model_name = model or await _model()

    async def _call() -> str:
        async with _pool.get_semaphore():
            return await openai_compatible.post_chat_completion(
                _pool.get_client(),
                base_url=config.GROQ_BASE_URL,
                api_key=api_key,
                messages=messages,
                model=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label="Groq",
            )

    return Response(await _pool.bounded("Groq", _call))


async def generate_json(prompt: str, *, max_tokens: int = 1024) -> Optional[dict]:
    """Lượt phụ trả JSON (structured output qua response_format=json_object).

    Groq yêu cầu prompt có nhắc tới "JSON" khi dùng json_object - các prompt
    utility trong repo đều có; thêm 1 dòng chốt cho chắc. Trả None nếu model
    trả về không parse được."""
    api_key = await _api_key()
    if not api_key:
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình GROQ_API_KEY")
    messages = openai_compatible.build_messages(
        prompt + "\n\nChỉ trả về đúng 1 object JSON hợp lệ, không thêm chữ nào khác.",
        None,
        None,
    )
    model_name = await _model()

    async def _call() -> str:
        async with _pool.get_semaphore():
            return await openai_compatible.post_chat_completion(
                _pool.get_client(),
                base_url=config.GROQ_BASE_URL,
                api_key=api_key,
                messages=messages,
                model=model_name,
                temperature=0.1,
                max_tokens=max_tokens,
                provider_label="Groq JSON",
                extra_payload={"response_format": {"type": "json_object"}},
            )

    raw = await _pool.bounded("Groq JSON", _call)
    try:
        data = json.loads(strip_json_fence(raw))
    except (TypeError, ValueError):
        logger.warning("Groq JSON: không parse được output (%d ký tự).", len(raw or ""))
        return None
    return data if isinstance(data, dict) else None


# /agent nhiều bước (ai/agent_service.py): gpt-oss trên Groq không gọi tool song
# song, TPM free nhỏ -> max_tokens thấp, kết quả tool đã cắt ~2.5K ký tự ở agent.
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
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình GROQ_API_KEY")
    model_name = model or await _model()

    async def _call() -> openai_compatible.ToolCallResponse:
        async with _pool.get_semaphore():
            return await openai_compatible.post_chat_completion_with_tools(
                _pool.get_client(),
                base_url=config.GROQ_BASE_URL,
                api_key=api_key,
                messages=messages,
                tools=tools,
                model=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label="Groq tools",
                extra_payload={"parallel_tool_calls": False},
            )

    return await _pool.bounded("Groq tools", _call)


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
# Trích dẫn kiểu 【2†L6-L10】 mà gpt-oss + browser_search chèn vào câu trả lời:
# vừa là bằng chứng đã đọc trang, vừa là rác với người đọc chat -> gỡ đi.
_BROWSER_CITATION_RE = re.compile(r"[ \t]?【[^】\n]{0,60}†[^】\n]{0,60}】")
# Model hỗ trợ browser_search (console.groq.com/docs/browser-search).
_BROWSER_SEARCH_MODEL_PREFIX = "openai/gpt-oss"


def _has_web_evidence(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _WEB_EVIDENCE_MARKERS)


def search_model() -> str:
    return config.GROQ_REALTIME_MODEL


def supports_web_search() -> bool:
    """Groq tự tra web được không: bật GROQ_WEB_SEARCH + model gpt-oss (key
    kiểm riêng ở orchestrator - key có thể đến từ trang admin)."""
    return config.GROQ_WEB_SEARCH and search_model().lower().startswith(
        _BROWSER_SEARCH_MODEL_PREFIX
    )


async def generate_realtime(
    prompt: str,
    *,
    system_instruction: Optional[str] = None,
    history: Optional[list[tuple[str, str]]] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> Response:
    """1 lượt Groq TỰ tra web (browser_search, tool_choice="required" nên luôn
    tra trước khi trả lời). Bằng chứng: executed_tools trong response, hoặc
    trích dẫn 【...†...】/URL/"Nguồn:" trong câu trả lời; không có -> raise
    RealtimeNoEvidenceError. ``max_tokens`` gồm cả token suy luận của gpt-oss."""
    if not supports_web_search():
        raise RealtimeNoEvidenceError(
            f"Groq web search đang tắt hoặc model {search_model()} không hỗ trợ browser_search"
        )
    api_key = await _api_key()
    if not api_key:
        raise openai_compatible.OpenAICompatibleError("Chưa cấu hình GROQ_API_KEY")
    messages = openai_compatible.build_messages(prompt, system_instruction, history)

    async def _call() -> openai_compatible.CompletionResult:
        async with _pool.get_semaphore():
            return await openai_compatible.post_chat_completion_full(
                _pool.get_client(),
                base_url=config.GROQ_BASE_URL,
                api_key=api_key,
                messages=messages,
                model=search_model(),
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label="Groq web search",
                extra_payload={
                    "tools": [{"type": "browser_search"}],
                    "tool_choice": "required",
                    # Groq khuyên reasoning_effort="low" khi dùng browser search:
                    # mức cao kéo dài phiên duyệt web, tốn token.
                    "reasoning_effort": "low",
                },
            )

    result = await _pool.bounded("Groq web search", _call)
    text = result.text
    has_citation = bool(_BROWSER_CITATION_RE.search(text))
    if not (result.executed_tools or has_citation or _has_web_evidence(text)):
        raise RealtimeNoEvidenceError("Groq web search không có dấu hiệu đã tra web thật")
    cleaned = _BROWSER_CITATION_RE.sub("", text).strip()
    if not cleaned:
        raise openai_compatible.OpenAICompatibleError("Groq web search trả kết quả rỗng")
    return Response(cleaned, searched=True)


async def check_status() -> tuple[bool, str]:
    return await openai_compatible.check_status(
        generate, api_key=await _api_key(), missing_key_msg="Chưa cấu hình GROQ_API_KEY"
    )


# Public accessors (dùng bởi web_admin) - tránh module khác gọi hàm private.
api_key = _api_key
model_name = _model
