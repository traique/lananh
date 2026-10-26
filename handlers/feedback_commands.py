"""/sai [lý do], /danhgia [Nd] trên Telegram - logic ở services/answer_feedback.py
(cùng hàm với Zalo/Zoom)."""

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from services import answer_feedback


async def _run(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    replies = await answer_feedback.handle_command(
        command, common.extract_arg(context), update.effective_user.id, "telegram"
    )
    for text in replies or []:
        await common.reply_long_text(update.message, text)


@common.restricted
async def sai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sai [lý do] - báo câu trả lời vừa rồi chưa ổn (👎 kèm lý do)."""
    await _run(update, context, "/sai")


@common.restricted
async def danhgia_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/danhgia [Nd] - số 👍/👎 theo provider/model."""
    await _run(update, context, "/danhgia")
