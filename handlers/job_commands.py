"""Lệnh Telegram /viec (việc chạy nền) và /lichtacvu (tác vụ định kỳ).

Handler chỉ lấy tham số rồi gọi services/background_jobs.py, services/routines.py
(dùng chung với Zalo/Zoom).
"""

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from services import background_jobs, routines


async def _reply(update: Update, outputs: list[str] | None) -> None:
    for text in outputs or []:
        await common.reply_long_text(update.message, text)


@common.restricted
async def viec_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    outputs = await background_jobs.handle_command(
        "/viec", common.extract_arg(context), update.effective_user.id, "telegram"
    )
    await _reply(update, outputs)


@common.restricted
async def lichtacvu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    outputs = await routines.handle_command(
        "/lichtacvu", common.extract_arg(context), update.effective_user.id, "telegram"
    )
    await _reply(update, outputs)
