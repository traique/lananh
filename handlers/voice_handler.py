"""Tin nhắn thoại / file ghi âm trên Telegram (filters.VOICE | filters.AUDIO).

Chỉ parse rồi gọi service: kiểm trần trước khi tải (kích thước, thời lượng),
tải về file tạm, chép lời (services/voice_service.py), gửi "🎙️ Em nghe là: ..."
rồi cho chữ đi tiếp như tin nhắn thường (handlers/chat_router.route_text) ở lượt
CHỈ ĐỌC - không ghi danh mục/chi tiêu/nhắc việc theo chữ nghe được.

Bước nghe và lượt trả lời dùng chung MỘT hạn chót CHAT_TURN_TIMEOUT_SEC (khoá
lượt chủ bot đã có ở web.py::_process_update).
"""

import asyncio
import functools
import logging

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

import messages
from ai.timeouts import TurnTimeoutError, turn_deadline
from core import config
from handlers import chat_router, common, turn_feedback
from services import conversation_log, voice_service

logger = logging.getLogger(__name__)


def _read_bytes(path) -> bytes:
    with open(path, "rb") as file:
        return file.read()


@common.restricted
async def voice_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    media = message.voice or message.audio
    if media is None:
        return
    user_id = update.effective_user.id
    if not voice_service.enabled():
        await message.reply_text(voice_service.error_reply("disabled", owner=True))
        return
    limit = voice_service.limit_error(
        getattr(media, "file_size", None),
        voice_service.duration_seconds(getattr(media, "duration", None)),
    )
    if limit:
        await message.reply_text(voice_service.error_reply(limit, owner=True))
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    config.ensure_media_dir()
    path = config.MEDIA_DIR / f"voice_{message.message_id}.bin"
    try:
        await common.download_telegram_photo_with_retry(media, path)
        audio = await asyncio.to_thread(_read_bytes, path)
    except (TimedOut, NetworkError, OSError):
        logger.exception("Lỗi tải tin thoại từ Telegram")
        await message.reply_text(voice_service.error_reply("download", owner=True))
        return
    finally:
        await common.safe_delete(path)

    duration = voice_service.duration_seconds(getattr(media, "duration", None))
    with turn_deadline():
        try:
            # Bước nghe chạy trong task con dưới khoá chủ bot: /huy huỷ được cả lúc
            # đang chép lời; lượt chat sau đó (route_text) tự bắt /huy của nó.
            transcript = await turn_feedback.owner_cancellable(
                message,
                functools.partial(
                    voice_service.transcribe, audio, user_id=user_id, duration=duration
                ),
            )
        except voice_service.VoiceError as exc:
            await message.reply_text(
                voice_service.error_reply(exc.kind, owner=True, detail=exc.detail)
            )
            return
        except TurnTimeoutError as exc:
            await message.reply_text(
                messages.friendly_error("timeout", owner=True, detail=str(exc))
            )
            return
        del audio
        if transcript is None:  # /huy lúc đang nghe - đã trả lời "em dừng rồi"
            return
        await message.reply_text(voice_service.echo_line(transcript, owner=True))
        blocked = voice_service.write_statement_kind(transcript)
        if blocked is not None:
            reply = voice_service.write_blocked_reply(blocked, owner=True)
            await conversation_log.record_exchange(
                user_id, transcript, reply, voice_service.write_record_kind(blocked)
            )
            await message.reply_text(reply)
            return
        await chat_router.route_text(update, context, transcript, allow_side_effects=False)
