"""Agnes AI (gateway bên thứ ba, tương thích chuẩn OpenAI) - dùng riêng cho
lệnh /anh (tạo ảnh thật từ mô tả), KHÔNG thuộc provider-chain chat chính
(router9/groq/openrouter/api1/api2, xem ai/orchestrator.py).

Khác với handlers/media_handler.py (phân tích ảnh mẫu -> viết PROMPT để
người dùng tự dán sang app Gemini), module này gọi thẳng
POST /v1/images/generations và trả về ẢNH THẬT (bytes) để bot gửi lại ngay
trong Telegram/Zalo/Zoom - không cần bước copy-paste thủ công nào.
"""
import logging
import json
from dataclasses import dataclass
from typing import Optional

import httpx

from ai import provider_overrides
from core import config, database as db

logger = logging.getLogger(__name__)

_SETTING_ENABLED = "agnes_enabled"

_client: Optional[httpx.AsyncClient] = None
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_GENERATION_JSON_BYTES = 12 * 1024 * 1024


class AgnesError(RuntimeError):
    """Lỗi khi gọi Agnes AI (chưa cấu hình key, HTTP lỗi, payload rỗng)."""


@dataclass(frozen=True)
class GeneratedImage:
    """data: bytes ảnh đã tải sẵn về - dùng cho Telegram (reply_photo) và
    Zalo (đóng gói base64 gửi cho zalo-gateway, xem services/channel_command_service.py).
    url: URL gốc trên CDN của Agnes AI (None nếu response_format=b64_json) -
    dùng cho Zoom, vì Chatbot API của Zoom (channels/zoom.py::send_image_message)
    nhận thẳng img_url để TỰ Zoom tải về, không cần bot upload/host lại."""
    data: bytes
    url: Optional[str] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=config.AGNES_CALL_TIMEOUT_SEC)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def get_enabled() -> bool:
    return await db.get_setting(_SETTING_ENABLED) == "1"


async def set_enabled(enabled: bool) -> None:
    await db.set_setting(_SETTING_ENABLED, "1" if enabled else "0")
    logger.info("Agnes AI (tạo ảnh) %s.", "bật" if enabled else "tắt")


async def _api_key() -> str:
    return await provider_overrides.get_api_key_override("agnes") or config.AGNES_API_KEY


async def _model() -> str:
    return await provider_overrides.get_model_override("agnes") or config.AGNES_IMAGE_MODEL


async def _request_bytes(method: str, url: str, limit: int, **kwargs) -> bytes:
    async with _get_client().stream(method, url, **kwargs) as response:
        if response.status_code != 200:
            raise AgnesError(f"Agnes AI trả lỗi HTTP {response.status_code}")
        try:
            declared = int(response.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > limit:
            raise AgnesError("Ảnh/response vượt giới hạn RAM của Render (ảnh tối đa 8 MB).")
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise AgnesError("Ảnh/response vượt giới hạn RAM của Render (ảnh tối đa 8 MB).")
            chunks.append(chunk)
        return b"".join(chunks)


async def generate_image(prompt: str, *, size: str = "1024x1024") -> GeneratedImage:
    """Sinh 1 ảnh từ mô tả. Trả về GeneratedImage(data, url) - xem docstring
    class ở trên để biết nơi nào dùng data, nơi nào dùng url.

    Raise AgnesError nếu chưa bật (get_enabled), chưa cấu hình
    AGNES_API_KEY, hoặc gọi lỗi."""
    if not await get_enabled():
        raise AgnesError("Tạo ảnh (Agnes AI) đang tắt - dùng /anh on để bật.")
    api_key = await _api_key()
    if not api_key:
        raise AgnesError("Chưa cấu hình AGNES_API_KEY")

    response_body = await _request_bytes(
        "POST",
        f"{config.AGNES_BASE_URL}/images/generations",
        _MAX_GENERATION_JSON_BYTES,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": await _model(),
            "prompt": prompt,
            "size": size,
            "extra_body": {"response_format": "url"},
        },
    )
    data = json.loads(response_body)
    items = data.get("data") or []
    if not items:
        raise AgnesError("Agnes AI không trả về ảnh nào")

    async def _record_call() -> None:
        try:
            await db.record_provider_call("agnes", await _model())
        except Exception:
            logger.warning("Không ghi được lượt gọi Agnes AI vào DB.", exc_info=True)

    item = items[0]
    image_url = item.get("url")
    b64 = item.get("b64_json")
    if image_url:
        image = await _request_bytes("GET", image_url, _MAX_IMAGE_BYTES)
        await _record_call()
        return GeneratedImage(data=image, url=image_url)
    if b64:
        import base64

        if len(b64) > ((_MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise AgnesError("Ảnh base64 vượt giới hạn 8 MB.")
        image = base64.b64decode(b64, validate=True)
        if len(image) > _MAX_IMAGE_BYTES:
            raise AgnesError("Ảnh base64 vượt giới hạn 8 MB.")
        await _record_call()
        return GeneratedImage(data=image, url=None)
    raise AgnesError("Agnes AI trả về ảnh không có url hoặc b64_json")


# Public accessors (dùng bởi web_admin) - tránh module khác gọi hàm private.
api_key = _api_key
model_name = _model
