"""Luồng chat văn bản dùng chung mọi kênh.

- run_chat_turn(): pipeline lượt chat AI chung cho Telegram, Zalo và Zoom
  (tool/web search/trí nhớ -> orchestrator.chat -> lưu lịch sử, cập nhật trí
  nhớ chạy nền, telemetry). Telegram (handlers/chat_router.py) gọi thẳng hàm
  này sau nhánh cổ phiếu riêng của nó.
- handle_channel_text(): điểm vào đầy đủ cho Zalo/Zoom (lệnh, danh mục, cổ
  phiếu rồi tới run_chat_turn), trả ChannelResult thay vì tự gửi tin.
"""

import asyncio
import logging
from dataclasses import dataclass

import messages
from ai import orchestrator
from ai.timeouts import TurnTimeoutError, turn_deadline
from core import database as db
from services import article_reader, memory_service, portfolio_service, tools, web_search
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
            logger.exception("Tác vụ cập nhật trí nhớ chạy nền bị lỗi")

    task.add_done_callback(_done)


async def stop_background_tasks() -> None:
    """Drain các lượt cập nhật memory (mọi kênh) trước khi đóng database."""
    await stop_tracked_tasks(
        _background_tasks,
        timeout=15.0,
        logger=logger,
        label="chat memory",
    )


# Tối đa 2 lượt cập nhật trí nhớ chạy nền cùng lúc: mỗi lượt là 1 lời gọi
# Gemini utility ngoài semaphore MAX_CONCURRENT_TURNS (RAM 512 MB).
_memory_update_slots = asyncio.Semaphore(2)


async def _update_memory_bounded(user_id: int, text: str, reply: str) -> None:
    """Cập nhật trí nhớ nền với hạn chót riêng (MEMORY_UPDATE_TIMEOUT_SEC).

    Task được tạo SAU khi ``turn_deadline()`` của lượt đã thoát nên không thừa
    hưởng hạn chót nào - đặt lại ở đây để official_client.generate_utility_json
    (đi qua ai.timeouts.bounded) không treo vô hạn."""
    from core import config

    async with _memory_update_slots:
        with turn_deadline(config.MEMORY_UPDATE_TIMEOUT_SEC):
            try:
                await memory_service.update_memory(user_id, text, reply)
            except TurnTimeoutError as exc:
                logger.warning("Cập nhật trí nhớ nền hết hạn chót: %s", exc)


@dataclass(frozen=True)
class ChatTurn:
    """Kết quả 1 lượt chat AI; mỗi kênh tự định dạng câu trả lời/lỗi."""

    text: str = ""  # rỗng khi model không trả nội dung
    used_fallback: bool = False
    failed: bool = False  # lỗi đã được log + ghi telemetry
    prompt_id: int | None = None
    # Thông báo lỗi cụ thể cho người dùng (vd hết hạn chót lượt); rỗng thì kênh
    # dùng câu lỗi chung của mình.
    error_text: str = ""


async def run_chat_turn(
    user_id: int, text: str, *, grounding: str = "", channel: str = "telegram"
) -> ChatTurn:
    """Pipeline chat chung: gom ngữ cảnh -> orchestrator.chat -> lưu lịch sử.

    grounding: ngữ cảnh giá cổ phiếu từ nhánh cổ phiếu (nếu có). Không raise:
    lỗi trả ChatTurn(failed=True) sau khi đã log và ghi telemetry."""
    prompt_id = await telemetry.start(user_id, "chat", text, channel=channel)
    try:
        # Hạn chót CHAT_TURN_TIMEOUT_SEC áp cho CẢ lượt (web search có thể tự gọi
        # AI 2-3 lần + orchestrator.chat), không phải từng lượt gọi AI riêng lẻ -
        # nếu không, tổng thời gian vượt mức gateway Zalo chờ (240s). Task con của
        # asyncio.gather thừa hưởng cùng hạn chót qua ContextVar.
        with turn_deadline():
            # 3 nguồn ngữ cảnh độc lập nhau - chạy song song thay vì tuần tự
            # (trước đây Tavily + memory nối tiếp, thêm vài trăm ms - vài giây
            # độ trễ cho mọi tin nhắn).
            tool_result, search_result, memory = await asyncio.gather(
                tools.maybe_run_tool(user_id, text),
                web_search.maybe_search(text, user_id=user_id),
                memory_service.build_memory_context(user_id, text),
            )
            combined = "\n\n".join(part for part in (grounding, tool_result, search_result) if part)
            response = await orchestrator.chat(
                user_id,
                text,
                grounding=combined,
                memory_context=memory,
                require_real_search=stock_analysis.wants_external_market_data(text),
                web_grounded=bool(search_result),
            )
        reply = (response.text or "").strip()
        await telemetry.success(prompt_id, "chat", reply or "(không có nội dung)")
        if reply:
            await db.add_chat_message(user_id, "user", text)
            await db.add_chat_message(user_id, "model", reply)
            _track_background_task(
                asyncio.create_task(_update_memory_bounded(user_id, text, reply))
            )
        return ChatTurn(reply, bool(getattr(response, "used_fallback", False)), prompt_id=prompt_id)
    except TurnTimeoutError as exc:
        logger.warning("Lượt chat hết hạn chót (kênh %s): %s", channel, exc)
        await telemetry.failure(prompt_id, "chat", exc)
        return ChatTurn(failed=True, prompt_id=prompt_id, error_text=f"⏱️ {exc}")
    except Exception as exc:
        logger.exception("Lỗi lượt chat (kênh %s)", channel)
        await telemetry.failure(prompt_id, "chat", exc)
        return ChatTurn(failed=True, prompt_id=prompt_id)


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


async def _handle_stock(
    user_id: int, text: str, is_admin: bool = True
) -> tuple[ChannelResult | None, str]:
    """Nhánh cổ phiếu cho Zalo/Zoom. is_admin=False (thành viên Zalo): chỉ bản
    THAM KHẢO - không nhãn hành động, vùng mua, stop, target, R:R, tỷ trọng,
    không giữ/cắt danh mục (stock.analysis full_access=False). Chủ bot (Zoom)
    và admin Zalo giữ bản đầy đủ như Telegram."""
    symbols = await stock_analysis.find_valid_symbols(text)
    if not symbols:
        if stock_analysis.looks_like_price_question(text):
            return ChannelResult([messages.STOCK_SYMBOL_UNRESOLVED]), ""
        return None, ""
    if stock_analysis.wants_portfolio_analysis(text, symbols):
        try:
            return ChannelResult(
                [
                    await stock_analysis.analyze_portfolio(
                        symbols, text, user_id=user_id, full_access=is_admin
                    )
                ]
            ), ""
        except Exception:
            logger.exception("Channel portfolio analysis failed")
            return ChannelResult(["❌ Có lỗi khi soi danh mục."]), ""
    persona_ids = stock_analysis.stock_personas.detect_personas(text)
    if persona_ids:
        outputs = []
        for symbol in symbols:
            try:
                outputs.append(
                    await stock_analysis.analyze_persona(
                        symbol, persona_ids, text, user_id=user_id, full_access=is_admin
                    )
                )
            except Exception:
                logger.exception("Channel persona view failed for %s", symbol)
                outputs.append(messages.STOCK_ANALYZE_FAILED.format(symbol=symbol))
        return ChannelResult(outputs), ""
    if stock_analysis.wants_full_analysis(text, symbols):
        outputs: list[str] = []
        for symbol in symbols:
            # Giống Telegram: bản số liệu rule-based (nguồn số chuẩn) luôn là
            # tin đầu tiên, bản diễn giải LLM (đã kiểm hợp đồng policy) theo sau.
            async def collect_rule_report(report: str) -> None:
                outputs.append(report)

            try:
                result = await stock_analysis.analyze_symbol(
                    symbol,
                    user_text=text,
                    user_id=user_id,
                    on_rule_report=collect_rule_report,
                    full_access=is_admin,
                )
                if result:
                    outputs.append(result)
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
                for symbol, result in zip(symbols, results, strict=False)
            ]
        ), ""
    return None, await stock_analysis.build_price_grounding(symbols)


async def handle_channel_text(
    user_id: int, text: str, is_admin: bool = True, channel: str = "zalo"
) -> ChannelResult:
    text = (text or "").strip()
    if not text:
        return ChannelResult([])

    portfolio_command = await portfolio_service.handle_command(user_id, text)
    if portfolio_command is not None:
        return ChannelResult([portfolio_command])
    command_result = await channel_command_service.maybe_handle_command(
        user_id, text, is_admin, channel
    )
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
    article = await article_reader.maybe_handle(text)
    if article is not None:
        return ChannelResult([article])

    # MỘT hạn chót cho cả nhánh cổ phiếu (debate nhiều lượt gọi AI) LẪN lượt chat
    # theo sau: turn_deadline lồng nhau giữ hạn SỚM hơn, nên run_chat_turn không
    # được cộng thêm 200s nữa (trước đây tổng có thể tới 400s > 240s gateway
    # Zalo chờ, người dùng nhận im lặng).
    with turn_deadline():
        try:
            stock_result, grounding = await _handle_stock(user_id, text, is_admin)
        except TurnTimeoutError as exc:
            logger.warning("Nhánh cổ phiếu hết hạn chót lượt: %s", exc)
            return ChannelResult([f"⏱️ {exc}"])
        if stock_result is not None:
            return stock_result
        turn = await run_chat_turn(user_id, text, grounding=grounding, channel=channel)
    if turn.failed:
        return ChannelResult(
            [turn.error_text or "❌ Có lỗi khi trò chuyện với Gemini. Hãy thử lại sau."]
        )
    if not turn.text:
        return ChannelResult([messages.CHAT_GENERIC_ERROR])
    return ChannelResult(
        [
            turn.text
            + (
                "\n\n⚙️ _Câu trả lời do API dự phòng trả về, ngôn ngữ có thể khác thường ngày._"
                if turn.used_fallback
                else ""
            )
        ],
        "api" if turn.used_fallback else None,
    )
