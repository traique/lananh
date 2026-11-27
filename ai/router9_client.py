"""Nhánh 9Router của provider-chain: gateway OpenAI-compatible
(core.config.ROUTER9_BASE_URL/ROUTER9_API_KEY), thay cho nhánh cookie tài
khoản Gemini cá nhân (gemini-webapi) trước đây.

Khác với cookie (client "sống" giữ ChatSession/Gem phía server Google), 9Router
chỉ là 1 endpoint /chat/completions không trạng thái — lịch sử hội thoại và
persona (chat_skill.yaml) phải được nhét vào messages ở MỖI lượt gọi, giống
hệt cách nhánh api1/api2 (ai/official_client.py) đã làm. Vì vậy router9_client
không có khái niệm ChatSession/Gem như cookie_client cũ.
"""

import logging
from typing import Optional
from urllib.parse import urlsplit

from ai import openai_compatible, provider_overrides
from ai.openai_compatible import Response
from core import config, settings_cache

logger = logging.getLogger(__name__)

_SETTING_PREFERRED_MODEL = "preferred_model_name"
_SETTING_BASE_URL = "router9_base_url"

# total_timeout = ROUTER9_CALL_TIMEOUT_SEC: lượt gọi NGOÀI orchestrator (agent,
# probe nền) cũng có trần tổng, không chỉ trông vào timeout từng pha của httpx.
_pool = openai_compatible.ClientPool(
    config.ROUTER9_CALL_TIMEOUT_SEC, config.ROUTER9_MAX_CONCURRENCY, config.ROUTER9_CALL_TIMEOUT_SEC
)


class Router9Error(RuntimeError):
    """Lỗi khi gọi 9Router (HTTP lỗi, payload rỗng/không hợp lệ...)."""


async def _api_key() -> str:
    return await provider_overrides.get_api_key_override("router9") or config.ROUTER9_API_KEY


def normalize_base_url(value: Optional[str]) -> Optional[str]:
    """Validate + normalize Base URL nhập từ admin.

    Chỉ chấp nhận http/https có hostname; không nhận credentials, query hay
    fragment vì caller sẽ tự nối /chat/completions và /models vào cuối URL.
    Trả None nếu không hợp lệ để caller fallback về ROUTER9_BASE_URL env.
    """
    value = (value or "").strip()
    if not value or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        # Truy cập .port để buộc urllib kiểm tra port có đúng định dạng/range.
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    path = parsed.path.rstrip("/").lower()
    if path.endswith("/chat/completions") or path.endswith("/models"):
        return None
    return value.rstrip("/")


async def get_base_url_override() -> Optional[str]:
    # Cache RAM ghi-xuyên (core.settings_cache): đọc ở MỌI lượt gọi 9Router.
    raw = await settings_cache.get(_SETTING_BASE_URL)
    if not raw:
        return None
    normalized = normalize_base_url(raw)
    if normalized is None:
        logger.warning(
            "Base URL override của 9Router không hợp lệ; fallback về ROUTER9_BASE_URL env."
        )
    return normalized


async def get_base_url() -> str:
    """Base URL hiệu lực: admin override hợp lệ, nếu không thì env Render."""
    return await get_base_url_override() or config.ROUTER9_BASE_URL


async def set_base_url_override(value: Optional[str]) -> bool:
    """Lưu Base URL override; URL sai bị xoá để an toàn fallback về env.

    Trả True nếu rỗng (reset env) hoặc URL hợp lệ; False nếu input không hợp
    lệ và đã được reset về ROUTER9_BASE_URL từ environment.
    """
    raw = (value or "").strip()
    if not raw:
        await settings_cache.set(_SETTING_BASE_URL, "")
        return True
    normalized = normalize_base_url(raw)
    if normalized is None:
        await settings_cache.set(_SETTING_BASE_URL, "")
        return False
    await settings_cache.set(_SETTING_BASE_URL, normalized)
    return True


async def _default_model() -> str:
    # Model chat do orchestrator truyền (preferred_model_name - /model); không truyền
    # thì dùng ROUTER9_MODEL.
    return config.ROUTER9_MODEL


_provider = openai_compatible.OpenAICompatibleProvider(
    "9Router",
    pool=_pool,
    base_url=lambda: get_base_url(),
    api_key=lambda: _api_key(),
    default_model=_default_model,
    vision_model=lambda: config.ROUTER9_MODEL,
    missing_key="Chưa cấu hình ROUTER9_API_KEY",
    # Agent qua router9 (_ROUTER9_NATIVE_TOOLS_ENABLED) giữ tham số cũ.
    tools_temperature=0.7,
    tools_max_tokens=4096,
    error_cls=Router9Error,
)

close = _provider.close
generate = _provider.generate
# Function-calling chuẩn OpenAI cho ai/agent_service.py - xem cảnh báo ở docstring
# openai_compatible.post_chat_completion_with_tools() về việc 9Router có thể âm
# thầm bỏ qua `tools`.
generate_with_tools = _provider.generate_with_tools
generate_image_prompt = _provider.generate_image_prompt


def supports_web_search() -> bool:
    return config.ROUTER9_WEB_SEARCH


async def generate_search(
    prompt: str,
    *,
    system_instruction: Optional[str] = None,
    history: Optional[list[tuple[str, str]]] = None,
    model: Optional[str] = None,
    temperature: float = 0.5,
    max_tokens: int = 4096,
) -> Response:
    """1 lượt 9Router TỰ tra web qua ``web_search_options`` (chuẩn OpenAI Chat
    Completions cho model search). Chỉ bật qua ROUTER9_WEB_SEARCH: tài liệu
    9Router không mô tả tính năng này, và gateway từng âm thầm bỏ qua `tools`
    - nên CHỈ tin khi response kèm annotations url_citation (URL trong chữ có
    thể là model tự bịa từ trí nhớ)."""
    if not supports_web_search():
        raise Router9Error("9Router web search đang tắt") from (
            openai_compatible.NoSearchEvidenceError("9Router web search đang tắt")
        )
    await _provider.credentials()
    messages = openai_compatible.build_messages(prompt, system_instruction, history)
    model_name = (
        config.ROUTER9_WEB_SEARCH_MODEL
        or model
        or await get_preferred_model_name()
        or config.ROUTER9_MODEL
    )
    result = await _provider.complete_full(
        messages,
        label="9Router web search",
        model=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_payload={"web_search_options": {"search_context_size": "low"}},
        # Qua proxy Render: phải stream để không bị cắt 504 (xem
        # openai_compatible.post_chat_completion).
        stream=True,
    )
    if not result.url_citations:
        evidence = openai_compatible.NoSearchEvidenceError(
            "9Router trả lời nhưng không kèm url_citation - không tin là đã tra web"
        )
        raise Router9Error(str(evidence)) from evidence
    return Response(result.text, searched=True, citations=result.source_urls)


async def list_models() -> list[str]:
    """Danh sách model 9Router quảng cáo qua GET /models (chuẩn OpenAI). Một
    số gateway có thể không hỗ trợ endpoint này - trả về [] thay vì lỗi."""
    api_key = await _api_key()
    if not api_key:
        return []
    headers = {"Authorization": f"Bearer {api_key}"}
    client = _pool.get_client()
    base_url = await get_base_url()
    try:
        response = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
        response.raise_for_status()
        model_list_payload = response.json()
        return sorted(
            {entry.get("id", "") for entry in model_list_payload.get("data", []) if entry.get("id")}
        )
    except Exception:
        logger.warning("Không lấy được danh sách model từ 9Router.", exc_info=True)
        return []


async def find_model(query: str) -> Optional[str]:
    query_lower = query.strip().lower()
    for name in await list_models():
        if query_lower == name.lower() or query_lower in name.lower():
            return name
    return None


async def get_preferred_model_name() -> Optional[str]:
    value = await settings_cache.get(_SETTING_PREFERRED_MODEL)
    return value or None


async def set_preferred_model_name(name: Optional[str]) -> None:
    await settings_cache.set(_SETTING_PREFERRED_MODEL, name or "")


check_status = _provider.check_status


# Public accessors (dùng bởi web_admin) - tránh module khác gọi hàm private.
api_key = _api_key
