"""Điểm vào cho mọi tin nhắn văn bản thường (không phải lệnh /)."""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

import messages
from ai import orchestrator
from core import database as db
from handlers import common, stock_handler
from services import memory_service, portfolio_service, tools
from services.background_tasks import stop_tracked_tasks
from services.telemetry import telemetry
from stock import analysis as stock_analysis

logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task] = set()


def _track_background_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)

    def _done(completed: asyncio.Task) -> None:
        _background_tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            logger.exception("Tác vụ cập nhật trí nhớ chạy nền bị lỗi")

    task.add_done_callback(_done)


async def stop_background_tasks() -> None:
    """Drain các lượt cập nhật memory trước khi database pool bị đóng."""
    await stop_tracked_tasks(
        _background_tasks,
        timeout=15.0,
        logger=logger,
        label="Telegram memory",
    )


@common.restricted
async def chat_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    portfolio_result = await portfolio_service.maybe_handle_natural_language(user_id, text)
    if portfolio_result is not None:
        await common.reply_long_text(update.message, portfolio_result)
        return

    route = await stock_handler.maybe_handle(update, user_id, text)
    if route.handled:
        return

    prompt_id = await telemetry.start(user_id, "chat", text)
    try:
        tool_result = await tools.maybe_run_tool(user_id, text)
        combined_grounding = route.grounding
        if tool_result:
            combined_grounding = (
                f"{route.grounding}\n\n{tool_result}" if route.grounding else tool_result
            )

        memory_context = await memory_service.build_memory_context(user_id, query_text=text)
        response = await orchestrator.chat(
            user_id,
            text,
            grounding=combined_grounding,
            memory_context=memory_context,
            require_real_search=stock_analysis.wants_external_market_data(text),
        )
        reply_text = (response.text or "").strip()

        await telemetry.success(prompt_id, "chat", reply_text or "(không có nội dung)")
        if reply_text:
            await db.add_chat_message(user_id, "user", text)
            await db.add_chat_message(user_id, "model", reply_text)
            _track_background_task(
                asyncio.create_task(memory_service.update_memory(user_id, text, reply_text))
            )
        reply_out = reply_text
        if reply_out and getattr(response, "used_fallback", False):
            reply_out += "\n\n⚙️ API"
        await common.reply_long_text(update.message, reply_out or messages.CHAT_GENERIC_ERROR)
    except Exception as exc:
        logger.exception("Lỗi chat tự nhiên")
        await telemetry.failure(prompt_id, "chat", exc)
        await update.message.reply_text(
            "❌ Có lỗi khi trò chuyện với Gemini. Hãy thử lại sau giây lát."
        )
