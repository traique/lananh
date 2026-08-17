"""Helper dùng chung cho mọi handler Telegram: kiểm soát truy cập (bot chỉ
phục vụ 1 user), tải file an toàn, và reply dài/rich text. Tách riêng khỏi
từng handlers/*.py cụ thể vì cả commands.py, chat_router.py, stock_handler.py,
media_handler.py đều cần.
"""
import asyncio
import functools
import logging
import os
from pathlib import Path

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

import tg_format
import tg_format_codeblock
from core import config

logger = logging.getLogger(__name__)

TELEGRAM_TEXT_MAX = 4096


def check_access(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == config.ALLOWED_USER_ID


async def deny(update: Update) -> None:
    """Im lặng với người lạ - không trả lời gì cả. Bot lộ token qua Telegram
    public API (@username tìm được), nên trả lời xác nhận "bot chỉ phục vụ 1
    người" cho BẤT KỲ ai chat thử vô tình biến bot thành mục tiêu dễ bị dò/
    spam liên tục (kẻ lạ biết chắc bot có tồn tại và đang chạy). Vẫn log đầy
    đủ để chủ bot theo dõi qua log server."""
    user = update.effective_user
    actual_id = user.id if user else "unknown"
    logger.warning(
        "Truy cập bị từ chối (im lặng, không trả lời) - user.id=%s (@%s) | ALLOWED_USER_ID đang cấu hình=%s",
        actual_id,
        user.username if user else "?",
        config.ALLOWED_USER_ID,
    )


def restricted(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_access(update):
            return await deny(update)
        return await handler(update, context)

    return wrapper


def extract_arg(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args).strip() if context.args else ""


async def read_file_bytes(path) -> bytes:
    def _read():
        with open(path, "rb") as f:
            return f.read()

    return await asyncio.to_thread(_read)


async def safe_delete(path) -> None:
    try:
        await asyncio.to_thread(os.remove, path)
    except OSError:
        pass


async def reply_long_text(message, text: str) -> None:
    await tg_format.reply_rich(message, text, max_len=TELEGRAM_TEXT_MAX)


async def reply_code_block(message, text: str) -> None:
    """Trả về text NGUYÊN VĂN trong khối <pre> để Telegram hiện nút Copy.
    Dùng cho /prompt và ảnh -> prompt: nội dung đó sinh ra để chép nguyên khối
    dán sang app Gemini, không phải để đọc, nên KHÔNG được convert markdown
    (sẽ làm mất các dấu * và _ có trong prompt)."""
    await tg_format_codeblock.reply_code_block(message, text, max_len=TELEGRAM_TEXT_MAX)


async def reply_long_text_edit_first(status_message, text: str) -> None:
    """Như reply_long_text, nhưng edit vào status_message có sẵn thay vì
    gửi tin mới - dùng khi đã có 1 tin nhắn trạng thái "đang xử lý..."."""
    await tg_format.reply_rich_edit_first(status_message, text, max_len=TELEGRAM_TEXT_MAX)


async def download_telegram_photo_with_retry(photo, local_path: Path) -> None:
    attempts = max(1, config.TELEGRAM_MEDIA_RETRIES)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            if local_path.exists():
                await safe_delete(local_path)
            tg_file = await photo.get_file(
                read_timeout=config.TELEGRAM_READ_TIMEOUT,
                connect_timeout=config.TELEGRAM_CONNECT_TIMEOUT,
                write_timeout=config.TELEGRAM_WRITE_TIMEOUT,
                pool_timeout=config.TELEGRAM_POOL_TIMEOUT,
            )
            await tg_file.download_to_drive(
                custom_path=str(local_path),
                read_timeout=config.TELEGRAM_READ_TIMEOUT,
                connect_timeout=config.TELEGRAM_CONNECT_TIMEOUT,
                write_timeout=config.TELEGRAM_WRITE_TIMEOUT,
                pool_timeout=config.TELEGRAM_POOL_TIMEOUT,
            )
            return
        except (TimedOut, NetworkError) as e:
            last_error = e
            logger.warning(
                "Tải ảnh từ Telegram lỗi mạng/timeout lần %s/%s: %s",
                attempt,
                attempts,
                e,
            )
            if attempt < attempts:
                await asyncio.sleep(1.5 * attempt)

    if last_error is not None:
        raise last_error
