"""Lệnh Telegram /an (nhật ký ăn uống), /noi (trả lời bằng giọng nói) và /app (ghép
app Android màn hình thông tin).

Handler chỉ lấy tham số rồi gọi service: services/nutrition_service.py,
services/voice_reply.py (dùng chung với Zalo/Zoom) và services/app_devices.py.
"""

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from services import app_devices, nutrition_service, voice_reply


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


@common.restricted
async def app_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/app, /app moi <tên>, /app xoa <số> - thiết bị app màn hình thông tin."""
    text = await app_devices.handle_command(
        update.effective_user.id, common.extract_arg(context).split()
    )
    await update.message.reply_text(text)
