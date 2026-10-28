"""Channel-neutral image-to-prompt pipeline used by the Zalo bridge.

Ảnh có caption "hoá đơn"/"bill"/"chi tiêu": đọc hoá đơn, ghi khoản chi nháp
(services/expense_service.py) thay vì viết prompt. Caption là câu hỏi/yêu cầu rõ
về ảnh: trả lời về ảnh (services/vision_service.py) theo cách gọi riêng của
thành viên; không caption vẫn viết prompt."""

import os
from pathlib import Path

import messages
from core import config
from core.fileutil import safe_delete
from services import (
    conversation_log,
    expense_service,
    image_prompt,
    reply_audience,
    vision_service,
)
from services.prompt_identity import resolve_prompt_identity
from services.prompt_templates import build_image_instruction
from services.telemetry import telemetry

MAX_ZALO_IMAGE_BYTES = int(os.getenv("ZALO_IMAGE_MAX_BYTES", str(8 * 1024 * 1024)))
_ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _instruction(caption: str) -> tuple[str, str]:
    identity = resolve_prompt_identity(caption)
    return build_image_instruction(caption, identity), identity.mode_hint


async def handle_channel_image(
    user_id: int, image: bytes, content_type: str, caption: str, message_id: str
) -> tuple[list[str], str | None]:
    mime = content_type.split(";", 1)[0].lower().strip()
    if mime not in _ALLOWED_TYPES:
        return ["❌ Zalo chỉ hỗ trợ ảnh JPEG, PNG hoặc WebP cho tính năng này."], None
    if not image or len(image) > MAX_ZALO_IMAGE_BYTES:
        return [
            f"❌ Ảnh trống hoặc lớn hơn giới hạn {MAX_ZALO_IMAGE_BYTES // 1024 // 1024} MB."
        ], None

    config.ensure_media_dir()
    safe_id = "".join(ch for ch in message_id if ch.isalnum())[:48] or "image"
    if expense_service.is_receipt_caption(caption):
        receipt: Path = config.MEDIA_DIR / f"zalo_receipt_{safe_id}{_ALLOWED_TYPES[mime]}"
        try:
            receipt.write_bytes(image)
            reply = await expense_service.analyze_receipt(user_id, str(receipt))
            await conversation_log.record_exchange(
                user_id, f"[Gửi ảnh] {caption}".strip(), reply, conversation_log.KIND_EXPENSE
            )
            return [reply], None
        finally:
            await safe_delete(receipt)
    intent = vision_service.classify_caption(caption)
    if intent.mode == vision_service.MODE_QA:
        return await _vision_reply(user_id, image, caption, intent, safe_id, mime)
    path: Path = config.MEDIA_DIR / f"zalo_prompt_{safe_id}{_ALLOWED_TYPES[mime]}"
    prompt_id = await telemetry.start(
        user_id, "promptify", caption or "(ảnh Zalo không caption)", channel="zalo"
    )
    try:
        path.write_bytes(image)
        instruction, hint = _instruction(caption)
        response = await image_prompt.from_photo(instruction, str(path))
        output = (getattr(response, "text", None) or "").strip()
        await telemetry.success(prompt_id, "promptify", output or "(không có nội dung)")
        if not output:
            return [await reply_audience.error_text("image_empty", user_id)], None
        fallback = bool(getattr(response, "used_fallback", False))
        # Lịch sử chỉ lưu 1 dòng tóm tắt, không lưu prompt tiếng Anh.
        await conversation_log.record_exchange(
            user_id, f"[Gửi ảnh] {caption}".strip(), "-", conversation_log.KIND_IMAGE
        )
        reader = getattr(response, "provider", "") or ""
        if reader and await reply_audience.is_owner(user_id):
            # Chủ bot thấy model nào đã đọc ảnh (prompt tả sai cảnh = model không thấy ảnh).
            model = getattr(response, "model", "") or ""
            hint += f"\n🤖 Đọc ảnh: {reader}" + (f" ({model})" if model else "")
        return [f"📝 Prompt gợi ý\n{hint}\n\n{output}"], "api" if fallback else None
    except Exception as exc:
        await telemetry.failure(prompt_id, "promptify", exc)
        # Thông điệp lỗi có thể mang thân response upstream: chỉ chủ bot (Zalo
        # controller) được xem, thành viên nhận câu giọng Lan Anh không chi tiết.
        detail = messages.exception_detail(exc, limit=250)
        return [await reply_audience.error_text("image", user_id, detail)], None
    finally:
        await safe_delete(path)


async def _vision_reply(
    user_id: int, image: bytes, caption: str, intent, safe_id: str, mime: str
) -> tuple[list[str], str | None]:
    """Hỏi đáp về ảnh (vision_service đã lưu lịch sử); lỗi -> câu giọng Lan Anh."""
    path: Path = config.MEDIA_DIR / f"zalo_vision_{safe_id}{_ALLOWED_TYPES[mime]}"
    try:
        path.write_bytes(image)
        result = await vision_service.answer(user_id, str(path), caption, intent, channel="zalo")
    except Exception as exc:
        detail = messages.exception_detail(exc, limit=250)
        return [await reply_audience.error_text("image", user_id, detail)], None
    finally:
        await safe_delete(path)
    if not result.text:
        return [await reply_audience.error_text("empty", user_id)], None
    return [result.text], "api" if result.used_fallback else None
