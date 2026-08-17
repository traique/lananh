"""Telegram commands for the structured stock portfolio."""

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from services import portfolio_service


@common.restricted
async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or "/danhmuc"
    result = await portfolio_service.handle_command(update.effective_user.id, text)
    if result:
        await common.reply_long_text(update.message, result)
