"""Nhánh 9Router của provider-chain: gateway OpenAI-compatible
(core.config.ROUTER9_BASE_URL/ROUTER9_API_KEY), thay cho nhánh cookie tài
khoản Gemini cá nhân (gemini-webapi) trước đây.

Khác với cookie (client "sống" giữ ChatSession/Gem phía server Google), 9Router
chỉ là 1 endpoint /chat/completions không trạng thái — lịch sử hội thoại và
persona (chat_skill.yaml) phải được nhét vào messages ở MỖI lượt gọi, giống
hệt cách nhánh api1/api2 (ai/official_client.py) đã làm. Vì vậy router9_client
không có khái niệm ChatSession/Gem như cookie_client cũ.
"""
import asyncio
import base64
import json
import logging
import mimetypes
from typing import Any, Optional

import httpx

from core import config, database as db

logger = logging.getLogger(__name__)

_SETTING_PREFERRED_MODEL = "preferred_model_name"

_client: Optional[httpx.AsyncClient] = None
_semaphore: Optional[asyncio.Semaphore] = None


class Router9Error(RuntimeError):
    """Lỗi khi gọi 9Router (HTTP lỗi, payload rỗng/không hợp lệ...)."""


class Response:
    """Bọc kết quả text trả về, cùng interface `.text` như ModelOutput của
    gemini-webapi/FallbackResponse của official_client, để orchestrator dùng
    chung 1 contract cho cả 3 nhánh provider."""

    def __init__(self, text: str) -> None:
        self.text = text


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=config.ROUTER9_CALL_TIMEOUT_SEC)
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, config.ROUTER9_MAX_CONCURRENCY))
    return _semaphore


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _parse_sse_content(raw: str) -> str:
    """Một số gateway (9Router khi provider phía sau chỉ hỗ trợ streaming) trả
    về text/event-stream ngay cả khi request không xin stream. Gom lại nội
    dung delta/message từ các chunk JSON trong luồng SSE đó."""
    pieces: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices", []):
            delta_content = (choice.get("delta") or {}).get("content")
            if delta_content:
                pieces.append(delta_content)
            msg_content = (choice.get("message") or {}).get("content")
            if msg_content:
                pieces.append(msg_content)
    return "".join(pieces)


async def _post_chat_completion(
    messages: list[dict[str, Any]],
    *,
    model: Optional[str],
    temperature: float,
    max_tokens: int,
) -> str:
    if not config.ROUTER9_API_KEY:
        raise Router9Error("Chưa cấu hình ROUTER9_API_KEY")
    headers = {"Authorization": f"Bearer {config.ROUTER9_API_KEY}"}
    payload = {
        "model": model or config.ROUTER9_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    client = _get_client()
    async with _get_semaphore():
        try:
            response = await client.post(
                f"{config.ROUTER9_BASE_URL.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type or response.text.lstrip().startswith("data:"):
                # Gateway bỏ qua "stream": false và trả SSE (thường do provider
                # phía sau chỉ hỗ trợ streaming) -> parse thủ công.
                text = _parse_sse_content(response.text)
            else:
                completion_payload = response.json()
                text = ((completion_payload.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            raise Router9Error(f"9Router HTTP {exc.response.status_code}: {body}") from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise Router9Error(f"9Router: {type(exc).__name__}: {exc}") from exc
    if not text:
        raise Router9Error("9Router trả kết quả rỗng")
    return text.strip()


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
    "user"/"model" (giữ đúng quy ước của official_client.generate) — "model"
    được map sang "assistant" theo chuẩn OpenAI."""
    messages: list[dict[str, Any]] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    if history:
        for role, content in history:
            messages.append({"role": "assistant" if role == "model" else "user", "content": content})
    messages.append({"role": "user", "content": prompt})

    text = await _post_chat_completion(
        messages, model=model, temperature=temperature, max_tokens=max_tokens
    )
    return Response(text)


async def generate_image_prompt(instruction: str, image_path: str) -> Response:
    """Gửi ảnh dạng base64 data URL theo chuẩn nội dung đa phương tiện OpenAI
    (image_url). Chỉ hoạt động nếu model cấu hình trên 9Router hỗ trợ vision -
    nếu không, gateway/model phía sau sẽ tự trả lỗi rõ ràng."""

    def _read_bytes() -> bytes:
        with open(image_path, "rb") as file:
            return file.read()

    image_bytes = await asyncio.to_thread(_read_bytes)
    mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    text = await _post_chat_completion(messages, model=None, temperature=0.7, max_tokens=4096)
    return Response(text)


async def list_models() -> list[str]:
    """Danh sách model 9Router quảng cáo qua GET /models (chuẩn OpenAI). Một
    số gateway có thể không hỗ trợ endpoint này - trả về [] thay vì lỗi."""
    if not config.ROUTER9_API_KEY:
        return []
    headers = {"Authorization": f"Bearer {config.ROUTER9_API_KEY}"}
    client = _get_client()
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
    if not config.ROUTER9_API_KEY:
        return False, "Chưa cấu hình ROUTER9_API_KEY"
    try:
        await generate("ping", max_tokens=8)
        return True, "OK"
    except Router9Error as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
