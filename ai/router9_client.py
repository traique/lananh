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

from ai import openai_compatible, provider_overrides
from ai.openai_compatible import Response
from core import config, database as db

logger = logging.getLogger(__name__)

_SETTING_PREFERRED_MODEL = "preferred_model_name"

_pool = openai_compatible.ClientPool(config.ROUTER9_CALL_TIMEOUT_SEC, config.ROUTER9_MAX_CONCURRENCY)


class Router9Error(RuntimeError):
    """Lỗi khi gọi 9Router (HTTP lỗi, payload rỗng/không hợp lệ...)."""


async def close() -> None:
    await _pool.close()


async def _api_key() -> str:
    return await provider_overrides.get_api_key_override("router9") or config.ROUTER9_API_KEY


async def generate(
    prompt: str,
    *,
    system_instruction: Optional[str] = None,
    history: Optional[list[tuple[str, str]]] = None,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> Response:
    """Gọi 1 lượt chat/completions. `history` là list (role, content) với role
    "user"/"model" (giữ đúng quy ước của official_client.generate)."""
    api_key = await _api_key()
    if not api_key:
        raise Router9Error("Chưa cấu hình ROUTER9_API_KEY")
    messages = openai_compatible.build_messages(prompt, system_instruction, history)
    async with _pool.get_semaphore():
        try:
            text = await openai_compatible.post_chat_completion(
                _pool.get_client(),
                base_url=config.ROUTER9_BASE_URL,
                api_key=api_key,
                messages=messages,
                model=model or config.ROUTER9_MODEL,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label="9Router",
            )
        except openai_compatible.OpenAICompatibleError as exc:
            raise Router9Error(str(exc)) from exc
    return Response(text)


async def generate_with_tools(
    messages: list[dict],
    tools: list[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
):
    """Lượt gọi /chat/completions có kèm `tools` (function-calling chuẩn
    OpenAI) - dùng cho ai/agent_service.py, KHÁC generate() ở trên (không
    dùng build_messages()/prompt+history, vì agent loop cần tự quản lý toàn
    bộ messages list, kể cả các message role "assistant" mang tool_calls và
    role "tool" mang kết quả tool, qua nhiều bước).

    Trả về openai_compatible.ToolCallResponse. Xem cảnh báo quan trọng ở
    docstring openai_compatible.post_chat_completion_with_tools() về việc
    9Router có thể âm thầm bỏ qua `tools`."""
    api_key = await _api_key()
    if not api_key:
        raise Router9Error("Chưa cấu hình ROUTER9_API_KEY")
    async with _pool.get_semaphore():
        try:
            return await openai_compatible.post_chat_completion_with_tools(
                _pool.get_client(),
                base_url=config.ROUTER9_BASE_URL,
                api_key=api_key,
                messages=messages,
                tools=tools,
                model=model or config.ROUTER9_MODEL,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label="9Router",
            )
        except openai_compatible.OpenAICompatibleError as exc:
            raise Router9Error(str(exc)) from exc


async def generate_image_prompt(instruction: str, image_path: str) -> Response:
    api_key = await _api_key()
    if not api_key:
        raise Router9Error("Chưa cấu hình ROUTER9_API_KEY")
    try:
        return await openai_compatible.generate_image_prompt(
            _pool,
            base_url=config.ROUTER9_BASE_URL,
            api_key=api_key,
            vision_model=config.ROUTER9_MODEL,
            provider_label="9Router",
            instruction=instruction,
            image_path=image_path,
        )
    except openai_compatible.OpenAICompatibleError as exc:
        raise Router9Error(str(exc)) from exc


async def list_models() -> list[str]:
    """Danh sách model 9Router quảng cáo qua GET /models (chuẩn OpenAI). Một
    số gateway có thể không hỗ trợ endpoint này - trả về [] thay vì lỗi."""
    api_key = await _api_key()
    if not api_key:
        return []
    headers = {"Authorization": f"Bearer {api_key}"}
    client = _pool.get_client()
    try:
        response = await client.get(
            f"{config.ROUTER9_BASE_URL.rstrip('/')}/models", headers=headers
        )
        response.raise_for_status()
        model_list_payload = response.json()
        return sorted({
            entry.get("id", "")
            for entry in model_list_payload.get("data", [])
            if entry.get("id")
        })
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
    value = await db.get_setting(_SETTING_PREFERRED_MODEL)
    return value or None


async def set_preferred_model_name(name: Optional[str]) -> None:
    await db.set_setting(_SETTING_PREFERRED_MODEL, name or "")


async def check_status() -> tuple[bool, str]:
    return await openai_compatible.check_status(
        generate,
        api_key=await _api_key(),
        missing_key_msg="Chưa cấu hình ROUTER9_API_KEY",
        expected_error=Router9Error,
    )
