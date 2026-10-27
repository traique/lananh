"""Lệnh Telegram /an (nhật ký ăn uống) và /noi (trả lời bằng giọng nói).

Handler chỉ lấy tham số rồi gọi service (dùng chung với Zalo/Zoom):
services/nutrition_service.py và services/voice_reply.py.
"""

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from services import nutrition_service, voice_reply


@common.restricted
async def an_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    outputs = await nutrition_service.handle_command(
        "/an", common.extract_arg(context), update.effective_user.id
    )
    for text in outputs or []:
        await common.reply_long_text(update.message, text)


@common.restricted
async def noi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await voice_reply.noi_command(
        update.effective_user.id, common.extract_arg(context), channel="telegram"
    )
    await update.message.reply_text(text)
