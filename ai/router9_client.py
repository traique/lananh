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

from ai import openai_compatible
from ai.openai_compatible import Response
from core import config, database as db

logger = logging.getLogger(__name__)

_SETTING_PREFERRED_MODEL = "preferred_model_name"

_pool = openai_compatible.ClientPool(config.ROUTER9_CALL_TIMEOUT_SEC, config.ROUTER9_MAX_CONCURRENCY)


class Router9Error(RuntimeError):
    """Lỗi khi gọi 9Router (HTTP lỗi, payload rỗng/không hợp lệ...)."""


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
    """Gọi 1 lượt chat/completions. `history` là list (role, content) với role
    "user"/"model" (giữ đúng quy ước của official_client.generate)."""
    if not config.ROUTER9_API_KEY:
        raise Router9Error("Chưa cấu hình ROUTER9_API_KEY")
    messages = openai_compatible.build_messages(prompt, system_instruction, history)
    async with _pool.get_semaphore():
        try:
            text = await openai_compatible.post_chat_completion(
                _pool.get_client(),
                base_url=config.ROUTER9_BASE_URL,
                api_key=config.ROUTER9_API_KEY,
                messages=messages,
                model=model or config.ROUTER9_MODEL,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label="9Router",
            )
        except openai_compatible.OpenAICompatibleError as exc:
            raise Router9Error(str(exc)) from exc
    return Response(text)


async def generate_image_prompt(instruction: str, image_path: str) -> Response:
    if not config.ROUTER9_API_KEY:
        raise Router9Error("Chưa cấu hình ROUTER9_API_KEY")
    try:
        return await openai_compatible.generate_image_prompt(
            _pool,
            base_url=config.ROUTER9_BASE_URL,
            api_key=config.ROUTER9_API_KEY,
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
    if not config.ROUTER9_API_KEY:
        return []
    headers = {"Authorization": f"Bearer {config.ROUTER9_API_KEY}"}
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
        api_key=config.ROUTER9_API_KEY,
        missing_key_msg="Chưa cấu hình ROUTER9_API_KEY",
        expected_error=Router9Error,
    )
