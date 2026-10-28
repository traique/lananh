"""Ảnh mẫu -> prompt tạo ảnh (Telegram gửi ảnh, Zalo gửi ảnh): gọi model phân tích
ảnh qua provider-chain; prompt còn chép khuôn mẫu thay vì tả ảnh thì hỏi lại 1 lần."""

import logging

from ai import orchestrator
from services.prompt_templates import TEMPLATE_RETRY_NOTE, copies_template

logger = logging.getLogger(__name__)


async def generate(instruction: str, image_path: str):
    """Phản hồi của orchestrator.analyze_image (có .text, .used_fallback)."""
    response = await orchestrator.analyze_image(instruction, image_path)
    if not copies_template(getattr(response, "text", None) or ""):
        return response
    logger.warning("Prompt ảnh chép khuôn mẫu thay vì tả ảnh, hỏi lại 1 lần.")
    retry = await orchestrator.analyze_image(instruction + TEMPLATE_RETRY_NOTE, image_path)
    return retry if (getattr(retry, "text", None) or "").strip() else response
