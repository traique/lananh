"""Nút bấm inline của Telegram (1 CallbackQueryHandler cho mọi nút).

Chỉ parse callback_data (services/action_buttons.py - whitelist verb, id là số)
rồi gọi service: đánh giá 👍/👎 (services/answer_feedback), 🔄 Thử lại/✂️ Ngắn
hơn câu trả lời mới nhất, duyệt/bỏ bài Facebook chờ (cùng dispatcher /fb_ok,
/fb_boqua có claim nguyên tử), cấp quyền người Zalo lạ (channels.zalo_onboarding),
hoãn nhắc việc vừa tới (services/reminder_service), [Ghi nhớ]/[Bỏ] cách trả lời
em tự rút ra (services/style_approval).

Chỉ chủ bot bấm được (ALLOWED_USER_ID) - lỗ hổng bỏ qua kiểm quyền ở callback
từng xảy ra ở dự án khác (OpenClaw CVE-2026-35661). Luôn trả lời callback (tắt
vòng quay chờ) càng sớm càng tốt; trả lời lỗi (query quá cũ sau cold start, inbox
thử lại) chỉ ghi log, không làm hỏng sự kiện. Nút làm lại chạy trong khoá lượt
chủ bot (web._process_update chọn theo verb); các nút khác chạy ngoài khoá như /fb_*.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import messages

from channels import facebook_commands, zalo_onboarding, zalo_repository, zalo_session
from channels import zalo_users
from core import config
from handlers import common
from services import (
    action_buttons,
    answer_feedback,
    channel_chat_service,
    concurrency,
    reminder_service,
    reply_cache,
    style_approval,
    turn_control,
)
from services.concurrency import OWNER_TURN_KEY

logger = logging.getLogger(__name__)

_ANSWERED_MAX = 128
_KEYBOARDS_MAX = 8
_answered: OrderedDict[str, None] = OrderedDict()
# chat_id -> message_id câu trả lời đang mang nút 👍/👎 (chỉ câu mới nhất giữ nút).
_keyboards: OrderedDict[int, int] = OrderedDict()


def markup(rows: action_buttons.Rows | None) -> InlineKeyboardMarkup | None:
    """Hàng nút trừu tượng [(nhãn, callback_data)] -> InlineKeyboardMarkup."""
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


def answer_markup(prompt_id: int | None, *, regenerable: bool) -> InlineKeyboardMarkup | None:
    if not config.TELEGRAM_ANSWER_BUTTONS or not prompt_id:
        return None
    return markup(action_buttons.answer_rows(prompt_id, regenerable=regenerable))


async def answer_query(query, text: str | None = None) -> None:
    """answerCallbackQuery đúng 1 lần cho mỗi query (tắt vòng quay chờ)."""
    query_id = getattr(query, "id", None)
    if query_id is None or query_id in _answered:
        return
    _answered[query_id] = None
    while len(_answered) > _ANSWERED_MAX:
        _answered.popitem(last=False)
    try:
        await query.answer(text=text[:190] if text else None)
    except Exception as exc:
        logger.info("Không trả lời được callback (bỏ qua): %s", exc)


async def remember_keyboard(bot, chat_id: int, message) -> None:
    """Ghi nhớ câu trả lời mới nhất mang nút; gỡ nút của câu trước (đỡ rối)."""
    message_id = getattr(message, "message_id", None)
    if not isinstance(message_id, int):
        return
    previous = _keyboards.get(chat_id)
    _keyboards[chat_id] = message_id
    _keyboards.move_to_end(chat_id)
    while len(_keyboards) > _KEYBOARDS_MAX:
        _keyboards.popitem(last=False)
    if previous is not None and previous != message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=previous, reply_markup=None
            )
        except Exception as exc:
            logger.info("Không gỡ được nút của câu trả lời trước: %s", exc)


async def _set_rows(query, rows: action_buttons.Rows) -> None:
    try:
        await query.edit_message_reply_markup(reply_markup=markup(rows))
    except Exception as exc:
        logger.info("Không cập nhật được nút bấm: %s", exc)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not common.check_access(update):
        await answer_query(query)
        await common.deny(update)
        return
    action = action_buttons.parse(query.data)
    if action is None:
        await answer_query(query, "Nút này hết hạn rồi anh ơi.")
        return
    await _HANDLERS[action.verb](update, context, query, action)


async def _rate(update, context, query, action: action_buttons.Action) -> None:
    user_id = update.effective_user.id
    prompt_id = int(action.target)
    good = action.option == "u"
    try:
        saved = await answer_feedback.rate(
            user_id, prompt_id, 1 if good else -1, channel="telegram"
        )
    except Exception:
        logger.warning("Không ghi được đánh giá câu trả lời.", exc_info=True)
        await answer_query(query, "Em chưa ghi được, anh bấm lại sau ít phút nha.")
        return
    if not saved:
        await answer_query(query, "Câu này cũ quá, em không còn lưu để ghi nhận nữa.")
    elif good:
        await answer_query(query, "Em ghi nhận rồi ạ 👍")
    else:
        await answer_query(
            query, "Em ghi nhận, lần sau em cố hơn 🙏 Gõ /sai <lý do> để nói rõ hơn."
        )
    regenerable = answer_feedback.latest_regenerable(user_id, prompt_id) is not None
    await _set_rows(query, action_buttons.after_rating_rows(prompt_id, regenerable=regenerable))


async def _regen(update, context, query, action: action_buttons.Action) -> None:
    user_id = update.effective_user.id
    prompt_id = int(action.target)
    shorter = action.option == "s"
    snapshot = answer_feedback.latest_regenerable(user_id, prompt_id)
    message = query.message
    if snapshot is None or message is None:
        await answer_query(query, "Em chỉ làm lại được câu trả lời mới nhất thôi nha anh.")
        if message is not None:
            await message.reply_text("Em chỉ làm lại được câu trả lời chat mới nhất thôi nha anh.")
        return
    await answer_query(query, "✂️ Em viết gọn lại nha…" if shorter else "🔄 Em viết lại nha…")
    # Bỏ nút làm lại ngay để không bấm 2 lần; vẫn giữ 👍/👎 cho câu cũ.
    await _set_rows(query, action_buttons.answer_rows(prompt_id, regenerable=False))
    async with common.typing_keepalive(context.bot, message.chat_id):
        try:
            # Cache theo sự kiện callback: inbox thử lại chỉ gửi lại, không chạy lại.
            turn = await concurrency.run_cancellable(
                OWNER_TURN_KEY,
                lambda: reply_cache.generate_once(
                    f"regen:{prompt_id}:{action.option}",
                    lambda: answer_feedback.regenerate(snapshot, shorter=shorter),
                ),
            )
        except concurrency.TurnCancelled:
            await message.reply_text(turn_control.cancelled_text(owner=True))
            return
    if turn.failed:
        await message.reply_text(channel_chat_service.failure_message(turn, owner=True))
        return
    if not turn.text:
        await message.reply_text(messages.CHAT_GENERIC_ERROR)
        return
    text = turn.text + ("\n\n⚙️ API" if turn.used_fallback else "")
    keyboard = answer_markup(turn.prompt_id, regenerable=True)
    sent = await common.reply_long_text(message, text, reply_markup=keyboard)
    if keyboard is not None:
        await remember_keyboard(context.bot, message.chat_id, sent)
    if turn.prompt_id:
        answer_feedback.remember_answer(
            answer_feedback.AnswerSnapshot(
                prompt_id=turn.prompt_id,
                user_id=user_id,
                channel="telegram",
                prompt_text=snapshot.question,
                answer_text=turn.text,
                provider=turn.provider,
                model=turn.model,
                regenerable=True,
                quote_text=snapshot.quote_text,
                event_key=snapshot.event_key,
                history_ids=turn.history_ids,
            )
        )


async def _zalo_account_id() -> str | None:
    """Cùng thứ tự với handlers/zalo_bridge_commands: session đang đăng nhập trước."""
    account_id = (
        await zalo_session.load_account_id() or await zalo_repository.resolve_default_account_id()
    )
    return str(account_id).strip() if account_id and str(account_id).strip() else None


async def _facebook(update, context, query, action: action_buttons.Action) -> None:
    approve = action.verb == action_buttons.FB_OK
    account_id = await _zalo_account_id()
    if account_id is None:
        await answer_query(query, "Chưa xác định được tài khoản Zalo đang đăng nhập.")
        return
    await answer_query(query, "⏳ Đang đăng bài…" if approve else "🗑 Đã bỏ qua bài.")
    await _set_rows(query, [])  # chống bấm 2 lần; /fb_ok vẫn có claim nguyên tử
    command = f"/fb_ok {action.target}" if approve else f"/fb_boqua {action.target}"
    result = await facebook_commands.maybe_handle_facebook_command(account_id, command)
    if query.message is None or result is None:
        return
    for text in result.messages:
        await common.reply_long_text(query.message, text)


async def _zalo_pair(update, context, query, action: action_buttons.Action) -> None:
    from handlers import pairing_commands

    admin = action.option == "a"
    result = await zalo_onboarding.pair(
        action.target, zalo_users.pending_name(action.target), admin=admin
    )
    await answer_query(query, "👑 Đã cấp quyền admin." if admin else "✅ Đã cấp quyền thành viên.")
    await _set_rows(query, [])
    if query.message is not None:
        await query.message.reply_text(pairing_commands.pair_confirmation(result, admin=admin))


async def _snooze(update, context, query, action: action_buttons.Action) -> None:
    reply = await reminder_service.snooze_delivered(
        update.effective_user.id, int(action.target), action.option
    )
    await answer_query(query, reply)
    await _set_rows(query, [])
    if query.message is not None:
        await query.message.reply_text(reply)


async def _style(update, context, query, action: action_buttons.Action) -> None:
    """[Ghi nhớ]/[Bỏ] đề xuất cách trả lời (services/style_approval.py)."""
    reply = await style_approval.resolve(
        update.effective_user.id, action.option == "k", int(action.target)
    )
    await answer_query(query, reply.split("\n", 1)[0])
    await _set_rows(query, [])
    if query.message is not None:
        await query.message.reply_text(reply)


_HANDLERS = {
    action_buttons.RATE: _rate,
    action_buttons.REGEN: _regen,
    action_buttons.FB_OK: _facebook,
    action_buttons.FB_SKIP: _facebook,
    action_buttons.ZALO_PAIR: _zalo_pair,
    action_buttons.SNOOZE: _snooze,
    action_buttons.STYLE: _style,
}
