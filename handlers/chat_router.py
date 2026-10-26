"""Điểm vào cho mọi tin nhắn văn bản thường (không phải lệnh /).

Phần riêng Telegram nằm ở đây (typing, danh mục bằng lời, nhánh cổ phiếu gửi
bản số liệu trước qua handlers/stock_handler.py, gửi rich text); lượt chat AI
dùng chung pipeline với Zalo/Zoom: services/channel_chat_service.run_chat_turn.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

import messages
from ai.timeouts import turn_deadline
from core.text_normalize import nfc
from handlers import common, stock_handler
from services import (
    article_reader,
    channel_chat_service,
    expense_service,
    lunar_chat,
    portfolio_service,
    reminder_service,
    weather_chat,
)
from services import vn_daily_chat
from services.telemetry import telemetry

logger = logging.getLogger(__name__)


@common.restricted
async def chat_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # nfc(): boundary nhận tin nhắn Telegram - xem core/text_normalize.py.
    text = nfc((update.message.text or "").strip())
    if not text:
        return
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # "hoãn 10p" / "xong" ngay sau tin nhắc việc: code xử lý, không tốn lượt AI.
    shortcut = await reminder_service.maybe_handle_shortcut(user_id, text)
    if shortcut is not None:
        await common.reply_long_text(update.message, shortcut)
        return

    portfolio_result = await portfolio_service.maybe_handle_natural_language(user_id, text)
    if portfolio_result is not None:
        await common.reply_long_text(update.message, portfolio_result)
        return

    expense_reply = await expense_service.maybe_handle_chat(user_id, text)
    if expense_reply is not None:
        await common.reply_long_text(update.message, expense_reply)
        return

    article = await article_reader.maybe_handle(text)
    if article is not None:
        await common.reply_long_text(update.message, article)
        return

    # Giá vàng/tỷ giá/xổ số, thời tiết và âm lịch: trả lời thẳng bằng code,
    # không tốn lượt AI nào.
    daily_reply = await vn_daily_chat.maybe_handle(text, user_id)
    if daily_reply is not None:
        await common.reply_long_text(update.message, daily_reply)
        return

    weather_reply = await weather_chat.maybe_handle(text, user_id=user_id)
    if weather_reply is not None:
        await common.reply_long_text(update.message, weather_reply)
        return

    lunar_reply = await lunar_chat.maybe_handle(text)
    if lunar_reply is not None:
        await common.reply_long_text(update.message, lunar_reply)
        return

    # MỘT hạn chót cho cả nhánh cổ phiếu (debate nhiều lượt gọi AI cho tới 3 mã)
    # LẪN lượt chat theo sau, như handle_channel_text của Zalo/Zoom: không có hạn
    # ngoài thì mỗi lượt gọi AI tự mở 200s mới và khoá chủ bot (dùng chung
    # Telegram/Zoom/web) bị giữ gấp nhiều lần CHAT_TURN_TIMEOUT_SEC.
    with turn_deadline():
        route = await stock_handler.maybe_handle(update, user_id, text)
        if route.handled:
            return

        # Telegram chỉ phục vụ chủ bot (@common.restricted) -> kênh "telegram".
        turn = await channel_chat_service.run_chat_turn(
            user_id, text, grounding=route.grounding, channel="telegram"
        )
    if turn.failed:
        # Telegram chỉ phục vụ chủ bot: giọng Lan Anh + chi tiết kỹ thuật.
        await update.message.reply_text(channel_chat_service.failure_message(turn, owner=True))
        return
    reply_out = turn.text
    if reply_out and turn.used_fallback:
        reply_out += "\n\n⚙️ API"
    try:
        await common.reply_long_text(update.message, reply_out or messages.CHAT_GENERIC_ERROR)
    except Exception as exc:
        # Gửi lỗi (Telegram từ chối/timeout): giữ hành vi cũ - ghi telemetry lỗi
        # và báo 1 câu ngắn thay vì để error handler chung xử lý.
        logger.exception("Lỗi gửi câu trả lời chat về Telegram")
        await telemetry.failure(turn.prompt_id, "chat", exc)
        await update.message.reply_text(
            messages.friendly_error("unexpected", owner=True, detail=messages.exception_detail(exc))
        )
