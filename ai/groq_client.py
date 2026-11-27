"""Nhánh Groq của provider-chain (router9 -> groq -> openrouter -> api1 -> api2),
gateway OpenAI-compatible (core.config.GROQ_BASE_URL/GROQ_API_KEY).

Phần chung (chat, tools của /agent, ảnh, check_status) nằm ở
openai_compatible.OpenAICompatibleProvider - các tên hàm cũ dưới đây là alias.

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

from ai import openai_compatible, prompt_budget, provider_overrides
from ai.openai_compatible import Response
from core import config
from core.envutil import env_on
from core.text_utils import strip_json_fence

logger = logging.getLogger(__name__)

# Xin token (stream_options.include_usage) cho provider_calls/quota (ai/call_log.py);
# GROQ_STREAM_USAGE=0 để tắt nếu Groq từ chối tham số này.
_INCLUDE_USAGE = env_on("GROQ_STREAM_USAGE")

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


# /agent nhiều bước (ai/agent_service.py): gpt-oss trên Groq không gọi tool song
# song, TPM free nhỏ -> max_tokens thấp, kết quả tool đã cắt ~2.5K ký tự ở agent.
AGENT_MAX_TOKENS = 1024
# Model nhận tham số reasoning_effort (low/medium/high) trên Groq.
_REASONING_MODEL_PREFIX = "openai/gpt-oss"


def _reasoning_payload(model: str) -> dict:
    """reasoning_effort (GROQ_REASONING_EFFORT) cho model gpt-oss; model khác thì
    không gửi (Groq trả 400 với model không suy luận)."""
    effort = config.GROQ_REASONING_EFFORT
    if effort and (model or "").lower().startswith(_REASONING_MODEL_PREFIX):
        return {"reasoning_effort": effort}
    return {}


def _tpm_budget() -> int:
    # Đọc lúc gọi (test đổi được); 0 = không kiểm.
    return prompt_budget.GROQ_TPM_BUDGET


# Getter đọc tên module LÚC GỌI: test/web_admin thay _api_key/_model/_INCLUDE_USAGE
# ở cấp module vẫn có hiệu lực.
_provider = openai_compatible.OpenAICompatibleProvider(
    "Groq",
    pool=_pool,
    base_url=lambda: config.GROQ_BASE_URL,
    api_key=lambda: _api_key(),
    default_model=lambda: _model(),
    vision_model=lambda: config.GROQ_VISION_MODEL,
    missing_key="Chưa cấu hình GROQ_API_KEY",
    include_usage=lambda: _INCLUDE_USAGE,
    extra_payload=lambda model: _reasoning_payload(model),
    tools_payload={"parallel_tool_calls": False},
    tools_temperature=0.3,
    tools_max_tokens=AGENT_MAX_TOKENS,
    tpm_budget=lambda: _tpm_budget(),
)

close = _provider.close
generate = _provider.generate
generate_with_tools = _provider.generate_with_tools
generate_image_prompt = _provider.generate_image_prompt
check_status = _provider.check_status


async def generate_json(prompt: str, *, max_tokens: int = 1024) -> Optional[dict]:
    """Lượt phụ trả JSON (structured output qua response_format=json_object).

    Groq yêu cầu prompt có nhắc tới "JSON" khi dùng json_object - các prompt
    utility trong repo đều có; thêm 1 dòng chốt cho chắc. Trả None nếu model
    trả về không parse được."""
    messages = openai_compatible.build_messages(
        prompt + "\n\nChỉ trả về đúng 1 object JSON hợp lệ, không thêm chữ nào khác.",
        None,
        None,
    )
    raw = await _provider.complete(
        messages,
        label="Groq JSON",
        temperature=0.1,
        max_tokens=max_tokens,
        extra_payload={"response_format": {"type": "json_object"}},
    )
    try:
        data = json.loads(strip_json_fence(raw))
    except (TypeError, ValueError):
        logger.warning("Groq JSON: không parse được output (%d ký tự).", len(raw or ""))
        return None
    return data if isinstance(data, dict) else None


async def transcribe(
    audio: bytes,
    *,
    filename: str,
    mime_type: str,
    language: str = "",
    prompt: str = "",
) -> str:
    """Chép lời 1 đoạn ghi âm bằng Whisper của Groq (GROQ_STT_MODEL, mặc định
    whisper-large-v3-turbo). ``prompt`` (tối đa 224 token theo Groq) gợi ý từ
    vựng: mã cổ phiếu, tên riêng. Có thể trả chuỗi rỗng (không nghe ra lời)."""
    api_key, base_url = await _provider.credentials()
    return await _provider.run(
        "Groq STT",
        lambda client: openai_compatible.post_audio_transcription(
            client,
            base_url=base_url,
            api_key=api_key,
            audio=audio,
            filename=filename,
            mime_type=mime_type,
            model=config.GROQ_STT_MODEL,
            provider_label="Groq STT",
            language=language,
            prompt=prompt,
        ),
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
    messages = openai_compatible.build_messages(prompt, system_instruction, history)
    result = await _provider.complete_full(
        messages,
        label="Groq web search",
        model=search_model(),
        temperature=temperature,
        max_tokens=max_tokens,
        extra_payload={
            "tools": [{"type": "browser_search"}],
            "tool_choice": "required",
            # Groq khuyên reasoning_effort="low" khi dùng browser search:
            # mức cao kéo dài phiên duyệt web, tốn token.
            "reasoning_effort": "low",
        },
    )
    text = result.text
    has_citation = bool(_BROWSER_CITATION_RE.search(text))
    if not (result.executed_tools or has_citation or _has_web_evidence(text)):
        raise RealtimeNoEvidenceError("Groq web search không có dấu hiệu đã tra web thật")
    cleaned = _BROWSER_CITATION_RE.sub("", text).strip()
    if not cleaned:
        raise openai_compatible.OpenAICompatibleError("Groq web search trả kết quả rỗng")
    return Response(cleaned, searched=True, citations=result.source_urls)


# Public accessors (dùng bởi web_admin) - tránh module khác gọi hàm private.
api_key = _api_key
model_name = _model
