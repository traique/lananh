"""Điểm vào cho mọi tin nhắn văn bản thường (không phải lệnh /).

Phần riêng Telegram nằm ở đây (typing, danh mục bằng lời, nhánh cổ phiếu gửi
bản số liệu trước qua handlers/stock_handler.py, gửi rich text); lượt chat AI
dùng chung pipeline với Zalo/Zoom: services/channel_chat_service.run_chat_turn.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

import messages
from ai import drafts
from ai.timeouts import turn_deadline
from core import config
from core.text_normalize import nfc
from handlers import common, stock_handler, turn_feedback
from services import (
    article_reader,
    channel_chat_service,
    chat_context,
    concurrency,
    conversation_log,
    expense_service,
    lunar_chat,
    portfolio_service,
    reminder_service,
    turn_control,
    turn_progress,
    weather_chat,
)
from services import vn_daily_chat
from services.concurrency import OWNER_TURN_KEY
from services.telemetry import telemetry

logger = logging.getLogger(__name__)


def quoted_text(message) -> str | None:
    """Tin người dùng đang trả lời: đoạn trích chọn riêng (Message.quote) hoặc
    nội dung/chú thích của tin được reply (reply_to_message)."""
    quote = getattr(message, "quote", None)
    text = getattr(quote, "text", None) if quote is not None else None
    if not text:
        replied = getattr(message, "reply_to_message", None)
        if replied is not None:
            text = getattr(replied, "text", None) or getattr(replied, "caption", None)
    return nfc(text.strip())[:600] if isinstance(text, str) and text.strip() else None


@common.restricted
async def chat_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # nfc(): boundary nhận tin nhắn Telegram - xem core/text_normalize.py.
    text = nfc((update.message.text or "").strip())
    if not text:
        return
    user_id = update.effective_user.id
    # "Đang nhập..." suốt lượt (1 lần send_chat_action chỉ sống ~5 giây).
    async with common.typing_keepalive(context.bot, update.effective_chat.id):
        if await _quick_reply(update, user_id, text):
            return
        await _ai_reply(update, context, user_id, text)


async def _quick_reply(update: Update, user_id: int, text: str) -> bool:
    """Các nhánh trả lời bằng code (không tốn lượt AI). True nếu đã trả lời."""
    # Câu trả lời nhanh cũng vào lịch sử để câu hỏi nối tiếp có ngữ cảnh.
    record = conversation_log.record_exchange

    # "hoãn 10p" / "xong" ngay sau tin nhắc việc: code xử lý, không tốn lượt AI.
    shortcut = await reminder_service.maybe_handle_shortcut(user_id, text)
    if shortcut is not None:
        await record(user_id, text, shortcut, conversation_log.KIND_REMINDER)
        await common.reply_long_text(update.message, shortcut)
        return True

    portfolio_result = await portfolio_service.maybe_handle_natural_language(user_id, text)
    if portfolio_result is not None:
        await record(user_id, text, portfolio_result, conversation_log.KIND_PORTFOLIO)
        await common.reply_long_text(update.message, portfolio_result)
        return True

    expense_reply = await expense_service.maybe_handle_chat(user_id, text)
    if expense_reply is not None:
        await record(user_id, text, expense_reply, conversation_log.KIND_EXPENSE)
        await common.reply_long_text(update.message, expense_reply)
        return True

    article = await article_reader.maybe_handle(text)
    if article is not None:
        await record(user_id, text, article, conversation_log.KIND_ARTICLE)
        chat_context.remember_article(user_id, text, article)
        await common.reply_long_text(update.message, article)
        return True

    # Giá vàng/tỷ giá/xổ số, thời tiết và âm lịch: trả lời thẳng bằng code,
    # không tốn lượt AI nào.
    daily_reply = await vn_daily_chat.maybe_handle(text, user_id)
    if daily_reply is not None:
        await record(user_id, text, daily_reply, conversation_log.KIND_DAILY)
        await common.reply_long_text(update.message, daily_reply)
        return True

    weather_reply = await weather_chat.maybe_handle(text, user_id=user_id)
    if weather_reply is not None:
        await record(user_id, text, weather_reply, conversation_log.KIND_WEATHER)
        await common.reply_long_text(update.message, weather_reply)
        return True

    lunar_reply = await lunar_chat.maybe_handle(text)
    if lunar_reply is not None:
        await record(user_id, text, lunar_reply, conversation_log.KIND_LUNAR)
        await common.reply_long_text(update.message, lunar_reply)
        return True
    return False


async def _reply_or_edit(message, status_message, text: str) -> None:
    """Câu ngắn (lỗi, "em dừng rồi"): sửa đè lên dòng trạng thái nếu đã gửi."""
    if status_message is not None:
        try:
            await status_message.edit_text(text)
            return
        except Exception:
            logger.info("Không sửa được dòng trạng thái, gửi tin mới.", exc_info=True)
    await message.reply_text(text)


async def _ai_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str
) -> None:
    record = conversation_log.record_exchange
    tracker = turn_progress.Tracker()
    streamer = (
        turn_feedback.DraftStreamer(context.bot, update.effective_chat.id)
        if config.TELEGRAM_STREAM_DRAFTS
        else None
    )
    # Có bản nháp (hiện "Thinking..." + chữ đang sinh) thì không cần dòng trạng thái.
    status = turn_feedback.StatusLine(
        update.message, tracker, 0 if streamer else config.TELEGRAM_STATUS_DELAY_SEC
    )

    async def run():
        # MỘT hạn chót cho cả nhánh cổ phiếu (debate nhiều lượt gọi AI cho tới 3
        # mã) LẪN lượt chat theo sau, như handle_channel_text của Zalo/Zoom: không
        # có hạn ngoài thì mỗi lượt gọi AI tự mở 200s mới và khoá chủ bot (dùng
        # chung Telegram/Zoom/web) bị giữ gấp nhiều lần CHAT_TURN_TIMEOUT_SEC.
        with turn_deadline():
            route = await stock_handler.maybe_handle(update, user_id, text)
            if route.handled:
                return route, None
            # Nhánh cổ phiếu tự gửi dòng "🔍 Đang phân tích..." riêng: dòng trạng
            # thái/bản nháp chỉ bắt đầu từ lượt chat AI.
            status.start()
            if streamer is not None:
                streamer.start()
            # Telegram chỉ phục vụ chủ bot (@common.restricted) -> kênh "telegram".
            turn = await channel_chat_service.run_chat_turn(
                user_id,
                text,
                grounding=route.grounding,
                channel="telegram",
                quote_text=quoted_text(update.message),
            )
            return route, turn

    async def stop_feedback():
        status_message = await status.stop()
        if streamer is not None:
            await streamer.stop()
        return status_message

    try:
        # Phần việc AI chạy trong task con đăng ký theo khoá chủ bot: /huy huỷ
        # đúng task này (services/concurrency.run_cancellable), không đụng task
        # giao sự kiện của inbox.
        with (
            turn_progress.tracking(tracker),
            drafts.sink_scope(streamer.push if streamer else None),
        ):
            route, turn = await concurrency.run_cancellable(OWNER_TURN_KEY, run)
    except concurrency.TurnCancelled:
        status_message = await stop_feedback()
        await _reply_or_edit(
            update.message, status_message, turn_control.cancelled_text(owner=True)
        )
        return
    except BaseException:
        await stop_feedback()
        raise
    status_message = await stop_feedback()

    if turn is None:
        if route.replies:
            await record(
                user_id,
                text,
                "",
                conversation_log.KIND_STOCK,
                stock_outputs=list(route.replies),
            )
        return
    if turn.failed:
        # Telegram chỉ phục vụ chủ bot: giọng Lan Anh + chi tiết kỹ thuật.
        await _reply_or_edit(
            update.message, status_message, channel_chat_service.failure_message(turn, owner=True)
        )
        return
    reply_out = turn.text
    if reply_out and turn.used_fallback:
        reply_out += "\n\n⚙️ API"
    try:
        final_text = reply_out or messages.CHAT_GENERIC_ERROR
        if status_message is not None:
            await common.reply_long_text_edit_first(status_message, final_text)
        else:
            await common.reply_long_text(update.message, final_text)
    except Exception as exc:
        # Gửi lỗi (Telegram từ chối/timeout): giữ hành vi cũ - ghi telemetry lỗi
        # và báo 1 câu ngắn thay vì để error handler chung xử lý.
        logger.exception("Lỗi gửi câu trả lời chat về Telegram")
        await telemetry.failure(turn.prompt_id, "chat", exc)
        await update.message.reply_text(
            messages.friendly_error("unexpected", owner=True, detail=messages.exception_detail(exc))
        )
