"""Lệnh Telegram /vang, /tygia, /xoso, /thoitiet, /bantin, /chuy, /yenlang.

Handler chỉ lấy tham số rồi gọi services/daily_commands.py, services/attention.py,
services/notify_hub.py (dùng chung với Zalo/Zoom); tính bằng code, không gọi AI.
"""

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from services import attention, daily_commands, notify_hub


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    outputs = await daily_commands.handle(
        command, common.extract_arg(context), update.effective_user.id, "telegram"
    )
    for text in outputs or []:
        await common.reply_long_text(update.message, text)


@common.restricted
async def vang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "/vang")


@common.restricted
async def tygia_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "/tygia")


@common.restricted
async def xoso_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "/xoso")


@common.restricted
async def thoitiet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "/thoitiet")


@common.restricted
async def bantin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "/bantin")


@common.restricted
async def chuy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    text = await attention.build(update.effective_user.id, "telegram")
    await common.reply_long_text(update.message, text)


@common.restricted
async def yenlang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await notify_hub.yenlang(update.effective_user.id, common.extract_arg(context))
    await common.reply_long_text(update.message, text)
