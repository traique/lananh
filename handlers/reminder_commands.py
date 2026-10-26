"""Lệnh Telegram /nhac (nhắc việc) và /ngaynho (giỗ, sinh nhật, kỷ niệm).

Handler chỉ lấy tham số rồi gọi services/reminder_commands.py (dùng chung với
Zalo/Zoom); giờ nhắc tính bằng code, không gọi AI.
"""

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from services import reminder_commands


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    outputs = await reminder_commands.handle(
        command, common.extract_arg(context), update.effective_user.id, "telegram"
    )
    for text in outputs or []:
        await common.reply_long_text(update.message, text)


@common.restricted
async def nhac_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "/nhac")


@common.restricted
async def ngaynho_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "/ngaynho")
