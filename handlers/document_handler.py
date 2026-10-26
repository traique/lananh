"""File tài liệu gửi tới bot trên Telegram: PDF, TXT/MD, Word (.docx).

Chỉ parse rồi gọi services/document_service.py: kiểm kích thước trước khi tải,
tải về file tạm (tên an toàn do bot đặt), trả lời câu hỏi trong caption (không
caption = tóm tắt), xoá file ngay sau lượt. Caption "lưu vào kho" thì lưu tài
liệu vào kho rag/ (chỉ chủ bot). Ảnh gửi dạng file vẫn đi handlers/media_handler.
"""

import logging

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

from core import config
from core.text_normalize import nfc
from handlers import common, turn_feedback
from services import document_service

logger = logging.getLogger(__name__)


@common.restricted
async def document_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    document = message.document
    if document is None:
        return
    user_id = update.effective_user.id
    file_size = getattr(document, "file_size", None)
    if file_size and file_size > document_service.max_bytes():
        await message.reply_text(await document_service.error_text("too_big", user_id))
        return
    filename = document.file_name or "tai-lieu"
    mime = document.mime_type or ""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    config.ensure_media_dir()
    path = config.MEDIA_DIR / (
        f"doc_{message.message_id}{document_service.safe_extension(filename, mime)}"
    )
    try:
        try:
            await common.download_telegram_photo_with_retry(document, path)
        except (TimedOut, NetworkError):
            logger.exception("Lỗi tải tài liệu từ Telegram")
            await message.reply_text(
                "📥 Em chưa tải được file về (mạng chập chờn). Anh gửi lại giúp em nha."
            )
            return
        caption = nfc((message.caption or "").strip())
        # Task con dưới khoá chủ bot: /huy huỷ được lượt đọc tài liệu dài.
        reply = await turn_feedback.owner_cancellable(
            message,
            lambda: document_service.handle_file(
                user_id, path, filename=filename, mime=mime, caption=caption, channel="telegram"
            ),
        )
    finally:
        await common.safe_delete(path)
    if reply is None:  # /huy - đã trả lời "em dừng rồi"
        return
    text = reply.text + ("\n\n⚙️ API" if reply.used_fallback else "")
    await common.reply_long_text(message, text)
