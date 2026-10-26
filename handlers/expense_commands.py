"""Lệnh Telegram /chitieu (tổng chi, ghi khoản, danh sách) và /chitieu_xoa.

Handler chỉ lấy tham số rồi gọi services/expense_service.py (dùng chung với
Zalo/Zoom); số tiền đọc và cộng bằng code, không gọi AI.
"""

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from services import expense_service


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    outputs = await expense_service.handle_command(
        command, common.extract_arg(context), update.effective_user.id
    )
    for text in outputs or []:
        await common.reply_long_text(update.message, text)


@common.restricted
async def chitieu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "/chitieu")


@common.restricted
async def chitieu_xoa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "/chitieu_xoa")
