"""Channel-neutral image-to-prompt pipeline used by the Zalo bridge."""
import os
from pathlib import Path

from ai import orchestrator
from core import config
from handlers import common
from handlers.media_handler import (
    GIRL_KEYWORDS,
    IDENTITY_LOCK_GIRL,
    IDENTITY_LOCK_REFERENCE,
    IMAGE_ANALYZE_INSTRUCTION_BASE,
    KEEP_FACE_KEYWORDS,
    _IDENTITY_RULE_LOCK,
    _PHOTO_IDENTITY_RULE_NONE,
    _PHOTO_SUBJECT_RULE_DESCRIBED,
    _PHOTO_SUBJECT_RULE_GIRL,
    _PHOTO_SUBJECT_RULE_REFERENCE,
    _SUBJECT_PHRASE_DESCRIBED,
    _SUBJECT_PHRASE_GIRL,
    _SUBJECT_PHRASE_REFERENCE,
)
from services.telemetry import telemetry

MAX_ZALO_IMAGE_BYTES = int(os.getenv("ZALO_IMAGE_MAX_BYTES", str(8 * 1024 * 1024)))
_ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _instruction(caption: str) -> tuple[str, str]:
    lower = caption.lower()
    if any(word in lower for word in KEEP_FACE_KEYWORDS):
        lock, rule, subject, subject_rule = IDENTITY_LOCK_REFERENCE + "\n\n", _IDENTITY_RULE_LOCK, _SUBJECT_PHRASE_REFERENCE, _PHOTO_SUBJECT_RULE_REFERENCE
        hint = "📎 Hãy đính kèm lại ảnh gốc cùng prompt này trên app Gemini."
    elif any(word in lower for word in GIRL_KEYWORDS):
        lock, rule, subject, subject_rule = IDENTITY_LOCK_GIRL + "\n\n", _IDENTITY_RULE_LOCK, _SUBJECT_PHRASE_GIRL, _PHOTO_SUBJECT_RULE_GIRL
        hint = "🔒 Prompt dùng khóa khuôn mặt cố định, không cần đính kèm ảnh."
    else:
        lock, rule, subject, subject_rule = "", _PHOTO_IDENTITY_RULE_NONE, _SUBJECT_PHRASE_DESCRIBED, _PHOTO_SUBJECT_RULE_DESCRIBED
        hint = "🖼️ Prompt tự mô tả khuôn mặt bằng chữ, không cần đính kèm ảnh."
    text = IMAGE_ANALYZE_INSTRUCTION_BASE.format(identity_lock_block=lock, subject_phrase=subject, identity_rule=rule, subject_rule=subject_rule)
    if caption:
        text += f"\n\nAdditional user instruction: {caption}"
    return text, hint


async def handle_channel_image(user_id: int, image: bytes, content_type: str, caption: str, message_id: str) -> tuple[list[str], str | None]:
    mime = content_type.split(";", 1)[0].lower().strip()
    if mime not in _ALLOWED_TYPES:
        return ["❌ Zalo chỉ hỗ trợ ảnh JPEG, PNG hoặc WebP cho tính năng này."], None
    if not image or len(image) > MAX_ZALO_IMAGE_BYTES:
        return [f"❌ Ảnh trống hoặc lớn hơn giới hạn {MAX_ZALO_IMAGE_BYTES // 1024 // 1024} MB."], None

    config.ensure_media_dir()
    safe_id = "".join(ch for ch in message_id if ch.isalnum())[:48] or "image"
    path: Path = config.MEDIA_DIR / f"zalo_prompt_{safe_id}{_ALLOWED_TYPES[mime]}"
    prompt_id = await telemetry.start(user_id, "promptify", caption or "(ảnh Zalo không caption)")
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
