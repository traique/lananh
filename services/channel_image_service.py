"""Channel-neutral image-to-prompt pipeline used by the Zalo bridge."""
import os
from pathlib import Path

from ai import orchestrator
from core import config
from handlers import common
from handlers.media_handler import IMAGE_ANALYZE_INSTRUCTION_BASE
from handlers.prompt_identity import render_instruction, resolve_prompt_identity
from services.telemetry import telemetry

MAX_ZALO_IMAGE_BYTES = int(os.getenv("ZALO_IMAGE_MAX_BYTES", str(8 * 1024 * 1024)))
_ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _instruction(caption: str) -> tuple[str, str]:
    identity = resolve_prompt_identity(caption)
    text = render_instruction(IMAGE_ANALYZE_INSTRUCTION_BASE, identity)
    if caption:
        text += f"\n\nAdditional user instruction: {caption}"
    return text, identity.mode_hint


async def handle_channel_image(user_id: int, image: bytes, content_type: str, caption: str, message_id: str) -> tuple[list[str], str | None]:
    mime = content_type.split(";", 1)[0].lower().strip()
    if mime not in _ALLOWED_TYPES:
        return ["❌ Zalo chỉ hỗ trợ ảnh JPEG, PNG hoặc WebP cho tính năng này."], None
    if not image or len(image) > MAX_ZALO_IMAGE_BYTES:
        return [f"❌ Ảnh trống hoặc lớn hơn giới hạn {MAX_ZALO_IMAGE_BYTES // 1024 // 1024} MB."], None

    config.ensure_media_dir()
    safe_id = "".join(ch for ch in message_id if ch.isalnum())[:48] or "image"
    path: Path = config.MEDIA_DIR / f"zalo_prompt_{safe_id}{_ALLOWED_TYPES[mime]}"
    prompt_id = await telemetry.start(user_id, "promptify", caption or "(ảnh Zalo không caption)", channel="zalo")
    try:
        path.write_bytes(image)
        instruction, hint = _instruction(caption)
        response = await orchestrator.analyze_image(instruction, str(path))
        output = (getattr(response, "text", None) or "").strip()
        await telemetry.success(prompt_id, "promptify", output or "(không có nội dung)")
        if not output:
            return ["Gemini không trả về prompt. Hãy thử ảnh khác."], None
        fallback = bool(getattr(response, "used_fallback", False))
        return [f"📝 Prompt gợi ý\n{hint}\n\n{output}"], "api" if fallback else None
    except Exception as exc:
        await telemetry.failure(prompt_id, "promptify", exc)
        return [f"❌ Gemini không phân tích được ảnh: {type(exc).__name__}: {str(exc)[:220]}"], None
    finally:
        await common.safe_delete(path)
