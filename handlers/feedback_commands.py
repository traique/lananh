"""/sai [lý do], /danhgia [Nd] trên Telegram - logic ở services/answer_feedback.py
(cùng hàm với Zalo/Zoom); /xuateval [Nd] - services/eval_export.py."""

import io

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from services import answer_feedback, eval_export


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


@common.restricted
async def xuateval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/xuateval [Nd] - câu trả lời bị 👎 (Telegram) -> file YAML case đánh giá ứng viên."""
    exported = await eval_export.export(common.extract_arg(context))
    if exported is None:
        await update.message.reply_text(
            "Chưa có câu trả lời nào bị anh bấm 👎 trên Telegram trong khoảng này ạ."
        )
        return
    name, data, count = exported
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename=name,
        caption=(
            f"{count} case ứng viên từ các câu bị 👎. Anh bỏ thông tin cá nhân, thêm check "
            "rồi mới chép vào test/evals/ nha (repo public)."
        ),
    )
