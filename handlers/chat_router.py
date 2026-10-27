"""Điểm vào cho mọi tin nhắn văn bản thường (không phải lệnh /).

Phần riêng Telegram nằm ở đây (typing, danh mục bằng lời, nhánh cổ phiếu gửi
bản số liệu trước qua handlers/stock_handler.py, gửi rich text); lượt chat AI
dùng chung pipeline với Zalo/Zoom: services/channel_chat_service.run_chat_turn.
"""

import logging

from telegram import InputFile, Update
from telegram.ext import ContextTypes

import messages
from ai import drafts
from ai.timeouts import turn_deadline
from core import config, trace
from core.text_normalize import nfc
from handlers import callbacks, common, stock_handler, turn_feedback
from services import (
    action_buttons,
    answer_feedback,
    article_reader,
    channel_chat_service,
    chat_context,
    concurrency,
    conversation_log,
    expense_service,
    lunar_chat,
    nutrition_service,
    portfolio_service,
    reminder_service,
    style_approval,
    turn_control,
    turn_progress,
    voice_reply,
    weather_chat,
)
from services import attention, vn_daily_chat
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
    await route_text(update, context, text)


async def route_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    allow_side_effects: bool = True,
    voice_in: bool = False,
) -> None:
    """Định tuyến 1 câu chữ như tin nhắn thường. allow_side_effects=False (chữ
    chép từ tin nhắn thoại - handlers/voice_handler.py): bỏ lối tắt nhắc việc,
    ghi danh mục/chi tiêu bằng lời và tool ghi dữ liệu - chỉ trả lời.
    voice_in: người dùng vừa gửi tin thoại (/noi auto đọc kèm câu trả lời).

    MỘT hạn chót CHAT_TURN_TIMEOUT_SEC cho cả lượt: trả lời chữ + tin thoại /noi
    (lồng trong hạn của tin thoại thì giữ hạn sớm hơn)."""
    user_id = update.effective_user.id
    # "Đang nhập..." suốt lượt (1 lần send_chat_action chỉ sống ~5 giây).
    with turn_deadline():
        async with common.typing_keepalive(context.bot, update.effective_chat.id):
            reply = await _quick_reply(update, user_id, text, allow_side_effects=allow_side_effects)
            if reply is None:
                # Lượt AI tự đọc kèm ngay sau câu trả lời (trước câu hỏi duyệt cách trả lời).
                await _ai_reply(
                    update,
                    context,
                    user_id,
                    text,
                    allow_side_effects=allow_side_effects,
                    voice_in=voice_in,
                )
                return
        if reply:
            await _speak(update, context, user_id, reply, voice_in=voice_in)


async def _speak(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, *, voice_in: bool
) -> None:
    """Tin thoại /noi đọc câu trả lời đã gửi (services/voice_reply.py): OGG/Opus qua
    sendVoice khi máy có ffmpeg, không thì file WAV qua sendDocument (Bot API: voice
    chỉ nhận OGG/Opus, MP3, M4A). Lỗi chỉ log - bản chữ đã gửi rồi."""
    if not await voice_reply.wants_voice(user_id, voice_in=voice_in):
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
    except Exception:
        logger.debug("Không gửi được trạng thái đang ghi âm.", exc_info=True)
    clip = await voice_reply.make_clip(user_id, text, channel="telegram", voice_in=voice_in)
    if clip is None:
        return
    try:
        with trace.stage("send"):
            if clip.ext == "ogg":
                await update.message.reply_voice(
                    voice=InputFile(clip.data, filename="lan-anh.ogg"),
                    duration=clip.duration_sec,
                )
            else:
                await update.message.reply_document(
                    document=InputFile(clip.data, filename=voice_reply.TELEGRAM_WAV_NAME),
                    caption="🔊 Bản đọc câu trả lời",
                )
    except Exception:
        logger.warning("Không gửi được tin thoại trả lời về Telegram.", exc_info=True)


async def _quick_reply(
    update: Update, user_id: int, text: str, *, allow_side_effects: bool = True
) -> str | None:
    """Các nhánh trả lời bằng code (không tốn lượt AI). Đã trả lời thì trả câu vừa
    gửi (để /noi đọc kèm), không thì None."""
    # Câu trả lời nhanh cũng vào lịch sử để câu hỏi nối tiếp có ngữ cảnh.
    record = conversation_log.record_exchange

    # "ghi nhớ"/"bỏ" ngay sau câu em hỏi duyệt cách trả lời (services/style_approval).
    approval = (
        await style_approval.maybe_handle_reply(user_id, text) if allow_side_effects else None
    )
    if approval is not None:
        await common.reply_long_text(update.message, approval)
        return approval

    # "hoãn 10p" / "xong" ngay sau tin nhắc việc: code xử lý, không tốn lượt AI.
    shortcut = (
        await reminder_service.maybe_handle_shortcut(user_id, text) if allow_side_effects else None
    )
    if shortcut is not None:
        await record(user_id, text, shortcut, conversation_log.KIND_REMINDER)
        await common.reply_long_text(update.message, shortcut)
        return shortcut

    portfolio_result = (
        await portfolio_service.maybe_handle_natural_language(user_id, text)
        if allow_side_effects
        else None
    )
    if portfolio_result is not None:
        await record(user_id, text, portfolio_result, conversation_log.KIND_PORTFOLIO)
        await common.reply_long_text(update.message, portfolio_result)
        return portfolio_result

    expense_reply = (
        await expense_service.maybe_handle_chat(user_id, text) if allow_side_effects else None
    )
    if expense_reply is not None:
        await record(user_id, text, expense_reply, conversation_log.KIND_EXPENSE)
        await common.reply_long_text(update.message, expense_reply)
        return expense_reply

    # "trưa nay anh ăn bún chả" (chỉ khi NUTRITION_NL=1): ghi bữa ăn bằng code.
    meal_reply = (
        await nutrition_service.maybe_handle_chat(user_id, text) if allow_side_effects else None
    )
    if meal_reply is not None:
        await record(user_id, text, meal_reply, conversation_log.KIND_MEAL)
        await common.reply_long_text(update.message, meal_reply)
        return meal_reply

    article = await article_reader.maybe_handle(text)
    if article is not None:
        await record(user_id, text, article, conversation_log.KIND_ARTICLE)
        chat_context.remember_article(user_id, text, article)
        await common.reply_long_text(update.message, article)
        return article

    # "Có gì cần anh chú ý?": tin /chuy tính bằng code (services/attention.py).
    if attention.is_attention_question(text):
        reply = await attention.build(user_id, "telegram")
        await record(user_id, text, reply, conversation_log.KIND_DAILY)
        await common.reply_long_text(update.message, reply)
        return reply

    # Giá vàng/tỷ giá/xổ số, thời tiết và âm lịch: trả lời thẳng bằng code,
    # không tốn lượt AI nào.
    # Lượt chỉ đọc: không lưu thành phố thời tiết mặc định ("anh ở Đà Nẵng").
    daily_reply = await vn_daily_chat.maybe_handle(text, user_id if allow_side_effects else None)
    if daily_reply is not None:
        await record(user_id, text, daily_reply, conversation_log.KIND_DAILY)
        await common.reply_long_text(update.message, daily_reply)
        return daily_reply

    weather_reply = await weather_chat.maybe_handle(text, user_id=user_id)
    if weather_reply is not None:
        await record(user_id, text, weather_reply, conversation_log.KIND_WEATHER)
        await common.reply_long_text(update.message, weather_reply)
        return weather_reply

    lunar_reply = await lunar_chat.maybe_handle(text)
    if lunar_reply is not None:
        await record(user_id, text, lunar_reply, conversation_log.KIND_LUNAR)
        await common.reply_long_text(update.message, lunar_reply)
        return lunar_reply
    return None


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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
    *,
    allow_side_effects: bool = True,
    voice_in: bool = False,
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
                **({} if allow_side_effects else {"allow_side_effects": False}),
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
    # Nút 👍/👎 (+ 🔄 Thử lại/✂️ Ngắn hơn cho câu chat thường không ghi dữ liệu).
    quote = quoted_text(update.message)
    regenerable = not quote and answer_feedback.can_regenerate(text, grounding=route.grounding)
    keyboard = (
        callbacks.answer_markup(turn.prompt_id, regenerable=regenerable) if turn.text else None
    )
    try:
        final_text = reply_out or messages.CHAT_GENERIC_ERROR
        if status_message is not None:
            sent = await _send_answer(
                common.reply_long_text_edit_first, status_message, final_text, keyboard
            )
        else:
            sent = await _send_answer(common.reply_long_text, update.message, final_text, keyboard)
    except Exception as exc:
        # Gửi lỗi (Telegram từ chối/timeout): giữ hành vi cũ - ghi telemetry lỗi
        # và báo 1 câu ngắn thay vì để error handler chung xử lý.
        logger.exception("Lỗi gửi câu trả lời chat về Telegram")
        await telemetry.failure(turn.prompt_id, "chat", exc)
        await update.message.reply_text(
            messages.friendly_error("unexpected", owner=True, detail=messages.exception_detail(exc))
        )
        return
    if keyboard is not None:
        await callbacks.remember_keyboard(context.bot, update.effective_chat.id, sent)
    if turn.prompt_id and turn.text:
        answer_feedback.remember_answer(
            answer_feedback.AnswerSnapshot(
                prompt_id=turn.prompt_id,
                user_id=user_id,
                channel="telegram",
                prompt_text=text,
                answer_text=turn.text,
                provider=turn.provider,
                model=turn.model,
                regenerable=regenerable,
                event_key=_event_key(update),
                history_ids=turn.history_ids,
            )
        )
    if turn.text:
        # /noi: tin thoại đọc câu trả lời, ngay sau bản chữ (services/voice_reply.py).
        await _speak(update, context, user_id, turn.text, voice_in=voice_in)
        await _ask_style_approval(update.message, user_id)


async def _ask_style_approval(message, user_id: int) -> None:
    """Đề xuất cách trả lời chờ duyệt: hỏi bằng tin RIÊNG sau câu trả lời (không chen
    giữa), tối đa 1 lần/ngày - services/style_approval. Lỗi gửi chỉ log."""
    question = await style_approval.take_question(user_id, buttons=True)
    if question is None:
        return
    try:
        await message.reply_text(
            question.text,
            reply_markup=callbacks.markup(action_buttons.style_approval_rows(question.proposal_id)),
        )
    except Exception:
        logger.warning("Không gửi được câu hỏi duyệt cách trả lời.", exc_info=True)


def _event_key(update) -> str | None:
    """event_key của tin gốc (như web._process_update) - làm lại câu trả lời dùng
    lại khoá này nên tool không bao giờ ghi trùng."""
    update_id = getattr(update, "update_id", None)
    return f"telegram:{update_id}" if update_id is not None else None


async def _send_answer(send, target, text: str, keyboard):
    """Gửi câu trả lời; có bàn phím thì gắn vào đoạn cuối và trả tin cuối đã gửi.
    Thời gian gửi vào turn_metrics.send_ms của lượt (core/trace.py)."""
    with trace.stage("send"):
        if keyboard is None:
            return await send(target, text)
        return await send(target, text, reply_markup=keyboard)
