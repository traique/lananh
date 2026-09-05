"""Telegram-independent text, command and stock chat service."""

import asyncio
import logging

import messages
from ai import orchestrator, tavily_client
from core import database as db
from services import memory_service, portfolio_service, tools
from services.background_tasks import stop_tracked_tasks
from services import channel_command_service
from services.channel_result import ChannelResult
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
            logger.exception("Channel background task failed")

    task.add_done_callback(_done)


async def stop_background_tasks() -> None:
    """Drain các lượt cập nhật memory của Zalo trước khi đóng database."""
    await stop_tracked_tasks(
        _background_tasks,
        timeout=15.0,
        logger=logger,
        label="channel memory",
    )


def split_for_zalo(text: str, limit: int = 1800) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks, remaining = [], text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _looks_like_search_question(text: str) -> bool:
    """Tin nhắn có cần tìm kiếm web không. Tavily được bật qua admin toggle
    sẽ bắn request cho MỌI tin nhắn nếu không gate - kể cả "ừm", tám chuyện -
    tốn quota + độ trễ + nhiễu prompt. Chỉ search khi câu hỏi thực sự gợi ý
    cần dữ liệu ngoài: đủ dài và có dấu hiệu nghi vấn/tra cứu. Bỏ sót 1-2 câu
    cần search thì Google model (api1/api2) vẫn tự search được bù lại."""
    lower = text.lower()
    if len(lower.split()) <= 3 and "?" not in lower:
        return False
    markers = (
        "?", "bao nhiêu", "bao giờ", "thế nào", "như thế nào", "là gì", "ở đâu",
        "khi nào", "vì sao", "tại sao", "giá", "tỷ giá", "giá vàng", "bitcoin",
        "crypto", "tin", "mới nhất", "hiện tại", "hôm nay", "tuần này", "check",
        "tra giúp", "tìm giúp", "search",
    )
    return any(marker in lower for marker in markers)


async def _maybe_tavily_search(text: str) -> str:
    if not await tavily_client.get_enabled():
        return ""
    if not _looks_like_search_question(text):
        return ""
    try:
        return await tavily_client.search(text)
    except Exception:
        logger.warning("Tavily search lỗi, bỏ qua grounding.", exc_info=True)
        return ""


async def _handle_stock(user_id: int, text: str) -> tuple[ChannelResult | None, str]:
    symbols = await stock_analysis.find_valid_symbols(text)
    if not symbols:
        if stock_analysis.looks_like_price_question(text):
            return ChannelResult([messages.STOCK_SYMBOL_UNRESOLVED]), ""
        return None, ""
    if stock_analysis.wants_portfolio_analysis(text, symbols):
        try:
            return ChannelResult(
                [await stock_analysis.analyze_portfolio(symbols, text, user_id=user_id)]
            ), ""
        except Exception:
            logger.exception("Channel portfolio analysis failed")
            return ChannelResult(["❌ Có lỗi khi soi danh mục."]), ""
    if stock_analysis.wants_full_analysis(text, symbols):
        outputs = []
        for symbol in symbols:
            try:
                outputs.append(
                    await stock_analysis.analyze_symbol(symbol, user_text=text, user_id=user_id)
                )
            except Exception:
                logger.exception("Channel stock analysis failed for %s", symbol)
                outputs.append(messages.STOCK_ANALYZE_FAILED.format(symbol=symbol))
        return ChannelResult(outputs), ""
    if stock_analysis.wants_price_quote(text, symbols):
        results = await asyncio.gather(
            *(stock_analysis.quick_quote(symbol) for symbol in symbols),
            return_exceptions=True,
        )
        return ChannelResult(
            [
                messages.STOCK_QUOTE_FAILED.format(symbol=symbol)
                if isinstance(result, BaseException)
                else result
                for symbol, result in zip(symbols, results)
            ]
        ), ""
    return None, await stock_analysis.build_price_grounding(symbols)


async def handle_channel_text(user_id: int, text: str, is_admin: bool = True, channel: str = "zalo") -> ChannelResult:
    text = (text or "").strip()
    if not text:
        return ChannelResult([])

    portfolio_command = await portfolio_service.handle_command(user_id, text)
    if portfolio_command is not None:
        return ChannelResult([portfolio_command])
    command_result = await channel_command_service.maybe_handle_command(user_id, text, is_admin, channel)
    if command_result is not None:
        outputs, provider = command_result
        return ChannelResult(
            outputs,
            provider,
            channel_command_service.take_pending_image(),
            channel_command_service.take_pending_image_url(),
        )
    portfolio_result = await portfolio_service.maybe_handle_natural_language(user_id, text)
    if portfolio_result is not None:
        return ChannelResult([portfolio_result])

    stock_result, grounding = await _handle_stock(user_id, text)
    if stock_result is not None:
        return stock_result
    prompt_id = await telemetry.start(user_id, "chat", text, channel=channel)
    try:
        # 3 nguồn ngữ cảnh độc lập nhau - chạy song song thay vì tuần tự
        # (trước đây Tavily + memory nối tiếp, thêm vài trăm ms - vài giây
        # độ trễ cho mọi tin nhắn).
        tool_result, search_result, memory = await asyncio.gather(
            tools.maybe_run_tool(user_id, text),
            _maybe_tavily_search(text),
            memory_service.build_memory_context(user_id),
        )
        combined = "\n\n".join(part for part in (grounding, tool_result, search_result) if part)
        response = await orchestrator.chat(
            user_id,
            text,
            grounding=combined,
            memory_context=memory,
            require_real_search=stock_analysis.wants_external_market_data(text),
        )
        reply = (response.text or "").strip()
        await telemetry.success(prompt_id, "chat", reply or "(không có nội dung)")
        if not reply:
            return ChannelResult([messages.CHAT_GENERIC_ERROR])
        await db.add_chat_message(user_id, "user", text)
        await db.add_chat_message(user_id, "model", reply)
        _track_background_task(
            asyncio.create_task(memory_service.update_memory(user_id, text, reply))
        )
        fallback = bool(getattr(response, "used_fallback", False))
        return ChannelResult(
            [reply + ("\n\n⚙️ _Câu trả lời do API dự phòng trả về, ngôn ngữ có thể khác thường ngày._" if fallback else "")],
            "api" if fallback else None,
        )
    except Exception as exc:
        logger.exception("Channel chat failed")
        await telemetry.failure(prompt_id, "chat", exc)
        return ChannelResult(["❌ Có lỗi khi trò chuyện với Gemini. Hãy thử lại sau."])
