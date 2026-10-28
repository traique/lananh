"""Xử lý ảnh gửi tới bot: phân tích ảnh -> viết prompt tiếng Anh dán sang
app Gemini. Ảnh có caption "hoá đơn"/"bill"/"chi tiêu" thì đọc hoá đơn và ghi
khoản chi nháp (services/expense_service.py) thay vì viết prompt. Caption là câu
hỏi/yêu cầu rõ về ảnh ("ảnh này là gì?", "dịch giúp anh chữ trong ảnh") thì trả
lời về ảnh (services/vision_service.py); không caption vẫn viết prompt.

Template prompt và lý do mỗi dạng (không caption / "cô gái 20" / "mặt tôi")
tả chủ thể khác nhau: xem services/prompt_templates.py.

Hỗ trợ 2 cách gửi ảnh:
- Ảnh nén (filters.PHOTO): đi qua photo_msg trực tiếp.
- File/document ảnh (filters.Document.IMAGE): cũng đi qua photo_msg,
  đọc từ update.message.document thay vì update.message.photo.
"""

import logging

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

import messages
from core import config
from core.text_normalize import nfc
from handlers import common, turn_feedback
from services import conversation_log, expense_service, image_prompt, vision_service
from services.concurrency import TurnCancelled
from services.prompt_identity import resolve_prompt_identity
from services.prompt_templates import build_image_instruction
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

# Độ dài tối đa của chi tiết lỗi in ra Telegram. Bot chỉ phục vụ 1 người nên
# in thẳng loại lỗi + thông điệp là cách nhanh nhất để biết provider nào hỏng,
# thay vì phải mở log Render.
_ERROR_DETAIL_MAX = 300


def _short_error(exc: BaseException) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    if len(detail) > _ERROR_DETAIL_MAX:
        detail = detail[:_ERROR_DETAIL_MAX] + "…"
    return detail


async def _receipt_msg(update: Update, context: ContextTypes.DEFAULT_TYPE, file_obj) -> None:
    """Ảnh hoá đơn: tải về, AI đọc tổng tiền, code kiểm rồi lưu khoản chi NHÁP."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    local_path = config.MEDIA_DIR / f"receipt_{update.message.message_id}.jpg"
    try:
        await common.download_telegram_photo_with_retry(file_obj, local_path)
        text = await expense_service.analyze_receipt(update.effective_user.id, str(local_path))
    except (TimedOut, NetworkError):
        logger.exception("Lỗi tải ảnh hoá đơn từ Telegram")
        text = messages.PHOTO_TIMEOUT_ERROR
    finally:
        await common.safe_delete(local_path)
    caption = nfc((update.message.caption or "").strip())
    await conversation_log.record_exchange(
        update.effective_user.id,
        f"[Gửi ảnh] {caption}".strip(),
        text,
        conversation_log.KIND_EXPENSE,
    )
    await common.reply_long_text(update.message, text)


async def _vision_msg(
    update: Update, context: ContextTypes.DEFAULT_TYPE, file_obj, caption: str, intent
) -> None:
    """Hỏi đáp về ảnh: tải về, gọi vision_service (đã lưu lịch sử), trả lời dạng
    chữ thường (không phải khối prompt)."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    local_path = config.MEDIA_DIR / f"vision_{update.message.message_id}.jpg"
    try:
        await common.download_telegram_photo_with_retry(file_obj, local_path)
        # Task con dưới khoá chủ bot: /huy huỷ được (None = đã trả lời "em dừng rồi").
        result = await turn_feedback.owner_cancellable(
            update.message,
            lambda: vision_service.answer(
                update.effective_user.id, str(local_path), caption, intent
            ),
        )
    except (TimedOut, NetworkError):
        logger.exception("Lỗi tải ảnh hỏi đáp từ Telegram")
        await update.message.reply_text(messages.PHOTO_TIMEOUT_ERROR)
        return
    except Exception as e:
        logger.exception("Lỗi hỏi đáp về ảnh (provider-chain)")
        await update.message.reply_text(
            messages.friendly_error("image", owner=True, detail=_short_error(e))
        )
        return
    finally:
        await common.safe_delete(local_path)
    if result is None:
        return
    text = result.text or messages.friendly_error("empty", owner=True)
    if result.text and result.used_fallback:
        text += "\n\n⚙️ API"
    await common.reply_long_text(update.message, text)


@common.restricted
async def photo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    caption = nfc((update.message.caption or "").strip())
    if expense_service.is_receipt_caption(caption):
        file_obj = update.message.photo[-1] if update.message.photo else update.message.document
        if file_obj is not None:
            await _receipt_msg(update, context, file_obj)
            return
    intent = vision_service.classify_caption(caption)
    if intent.mode == vision_service.MODE_QA:
        file_obj = update.message.photo[-1] if update.message.photo else update.message.document
        if file_obj is not None:
            await _vision_msg(update, context, file_obj, caption, intent)
            return
    prompt_label = caption or "(gửi ảnh, không có caption)"
    prompt_id = await telemetry.start(user_id, "promptify", prompt_label)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # prompt_id có thể là None khi DB lỗi (telemetry fail-open) - không được
    # để tên file thành "promptify_None.jpg".
    filename = f"promptify_{prompt_id if prompt_id is not None else 'img'}.jpg"
    local_path = config.MEDIA_DIR / filename

    identity = resolve_prompt_identity(caption)
    mode_hint = f"\n\n{identity.mode_hint}"

    # Hỗ trợ cả ảnh gửi dạng nén (photo) lẫn dạng file (document)
    if update.message.photo:
        file_obj = update.message.photo[-1]
    elif update.message.document:
        file_obj = update.message.document
    else:
        await update.message.reply_text("❌ Không đọc được ảnh. Anh thử gửi lại nhé.")
        return

    # ── Pha 1: Tải ảnh từ Telegram ──────────────────────────────
    try:
        await common.download_telegram_photo_with_retry(file_obj, local_path)
    except (TimedOut, NetworkError) as e:
        logger.exception("Lỗi tải ảnh từ Telegram")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text(messages.PHOTO_TIMEOUT_ERROR)
        return
    except Exception as e:
        logger.exception("Lỗi không xác định khi tải ảnh")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text(
            messages.friendly_error("image_download", owner=True, detail=_short_error(e))
        )
        return

    # ── Pha 2: Phân tích bằng Gemini ────────────────────────────────
    # Tách RIÊNG khỏi pha gửi kết quả. Trước đây cả hai nằm chung một khối
    # try/except nên khi lỗi không tài nào biết được là Gemini hỏng hay
    # Telegram hỏng, và thông báo cũ nuốt sạch chi tiết lỗi.
    try:
        instruction = build_image_instruction(caption, identity)

        # Task con dưới khoá chủ bot: /huy huỷ được (None = đã trả lời "em dừng rồi").
        response = await turn_feedback.owner_cancellable(
            update.message, lambda: image_prompt.from_photo(instruction, str(local_path))
        )
        if response is None:
            await telemetry.failure(prompt_id, "promptify", TurnCancelled())
            return
        result_text = (getattr(response, "text", None) or "").strip()
        used_fallback = bool(getattr(response, "used_fallback", False))
    except Exception as e:
        # Nguyên nhân hay gặp: 9Router chết + API hết quota (cả
        # provider-chain sập), key API chưa cấu hình, hoặc ảnh bị chặn vì
        # chính sách nội dung. In thẳng loại lỗi để khỏi phải mò log Render.
        logger.exception("Lỗi phân tích ảnh (provider-chain)")
        await telemetry.failure(prompt_id, "promptify", e)
        # Giọng Lan Anh, không đổ lỗi riêng "Gemini" (có thể là 9Router/Groq...);
        # chi tiết lỗi + gợi ý /status vẫn hiện cho chủ bot.
        await update.message.reply_text(
            messages.friendly_error("image", owner=True, detail=_short_error(e))
        )
        return
    finally:
        await common.safe_delete(local_path)

    if not result_text:
        await telemetry.success(prompt_id, "promptify", "(AI không trả về nội dung)")
        await update.message.reply_text(messages.friendly_error("image_empty", owner=True))
        return

    await telemetry.success(prompt_id, "promptify", result_text)
    # Lịch sử chỉ lưu 1 dòng tóm tắt (prompt tiếng Anh dài kéo câu trả lời sau lệch ngôn ngữ).
    await conversation_log.record_exchange(
        user_id, f"[Gửi ảnh] {caption}".strip(), "-", conversation_log.KIND_IMAGE
    )

    # ── Pha 3: Gửi kết quả về Telegram ──────────────────────────────
    # Header riêng, rồi nội dung prompt gửi trong khối <pre> để Telegram hiện
    # nút Copy. Nhãn "⚙️ API" đặt ở header chứ KHÔNG đặt trong khối prompt,
    # nếu không bấm Copy sẽ chép luôn cả nhãn đó sang app Gemini.
    header = "📝 <b>Prompt gợi ý (dùng cho app Gemini)</b> — chạm vào khối bên dưới để chép:"
    if used_fallback:
        header += "  ⚙️ API"
    header += mode_hint
    try:
        await update.message.reply_text(header, parse_mode="HTML")
        await common.reply_code_block(update.message, result_text)
    except Exception as e:
        logger.exception("Lỗi gửi kết quả prompt về Telegram")
        await update.message.reply_text(
            f"❌ Đã tạo được prompt nhưng gửi về Telegram lỗi.\n🔎 {_short_error(e)}"
        )
