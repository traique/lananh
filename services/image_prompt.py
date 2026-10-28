"""Viết prompt tạo ảnh qua provider-chain: từ ảnh mẫu (Telegram/Zalo gửi ảnh) và từ mô
tả chữ (/prompt mọi kênh). Prompt còn chép ví dụ mẫu thay vì tả ảnh/làm theo mô tả thì
hỏi lại model 1 lần kèm lời nhắc (services/prompt_templates.py)."""

import logging
from collections.abc import Awaitable, Callable

from ai import orchestrator
from services.prompt_templates import (
    TEMPLATE_RETRY_NOTE,
    TEXT_RETRY_NOTE,
    copies_template,
    copies_text_example,
    strip_slot_labels,
)

logger = logging.getLogger(__name__)


async def from_photo(instruction: str, image_path: str):
    """Phản hồi của orchestrator.analyze_image (có .text, .used_fallback); nhãn ô của
    khuôn ("Outfit:", "Setting:"...) đã được bỏ khỏi .text."""
    response = await _ask_once_more(
        lambda text: orchestrator.analyze_image(text, image_path),
        instruction,
        copies_template,
        TEMPLATE_RETRY_NOTE,
    )
    text = getattr(response, "text", None)
    if text:
        response.text = strip_slot_labels(text)
    return response


async def from_text(instruction: str):
    """Phản hồi của orchestrator.ask (có .text, .used_fallback)."""
    return await _ask_once_more(orchestrator.ask, instruction, copies_text_example, TEXT_RETRY_NOTE)


async def _ask_once_more(
    call: Callable[[str], Awaitable], instruction: str, copied: Callable[[str], bool], note: str
):
    response = await call(instruction)
    if not copied(getattr(response, "text", None) or ""):
        return response
    logger.warning("Prompt tạo ảnh chép ví dụ mẫu, hỏi lại model 1 lần.")
    retry = await call(instruction + note)
    return retry if (getattr(retry, "text", None) or "").strip() else response
