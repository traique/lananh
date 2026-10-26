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
from core import address, database as db, untrusted, urls
from services import (
    article_reader,
    lunar_chat,
    weather_chat,
    memory_service,
    portfolio_service,
    reminder_service,
    stock_turn,
    tool_registry,
    tools,
    web_search,
)
from services.background_tasks import stop_tracked_tasks
from services import channel_command_service, reply_audience
from services import vn_daily_chat
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
    """Kết quả 1 lượt chat AI; mỗi kênh tự định dạng câu trả lời/lỗi
    (failure_message / failure_text)."""

    text: str = ""  # rỗng khi model không trả nội dung
    used_fallback: bool = False
    failed: bool = False  # lỗi đã được log + ghi telemetry
    prompt_id: int | None = None
    # Hết hạn chót lượt (TurnTimeoutError): báo "AI chậm quá" thay câu lỗi chung.
    timed_out: bool = False
    # Chi tiết kỹ thuật (loại lỗi + thông điệp) - CHỈ chủ bot được xem.
    error_detail: str = ""
    # Tool GHI dữ liệu đã lưu xong trước khi lượt hỏng (vd nhắc việc): câu xác nhận
    # do code soạn, nối vào câu báo lỗi để người dùng khỏi đặt lại lần nữa.
    tool_note: str = ""


def failure_message(turn: ChatTurn, *, owner: bool, address_form: str | None = None) -> str:
    """Câu báo lượt chat hỏng, giọng Lan Anh (messages.friendly_error): không tên
    provider; chi tiết kỹ thuật + gợi ý /status chỉ dành cho chủ bot."""
    text = messages.friendly_error(
        "timeout" if turn.timed_out else "chat",
        owner=owner,
        address_form=address_form,
        detail=turn.error_detail,
    )
    return f"{text}\n\n✅ {turn.tool_note}" if turn.tool_note else text


async def failure_text(turn: ChatTurn, user_id: int) -> str:
    """failure_message cho người nhận của lượt hiện tại (services/reply_audience.py)."""
    return failure_message(
        turn,
        owner=await reply_audience.is_owner(user_id),
        address_form=reply_audience.current().address_form,
    )


async def _skip_search_plan() -> web_search.SearchPlan:
    """Số liệu code (vàng/tỷ giá/xổ số) đã đủ: không tra Tavily, provider cũng
    không tự tra web chồng lên (dễ lẫn giá web khác giá nguồn chính thức)."""
    return web_search.SearchPlan(mode=orchestrator.SEARCH_OFF)


def _tool_outcome(value) -> tuple[tool_registry.ToolOutcome, str]:
    """(outcome, khối grounding) từ kết quả tools.maybe_run_tool. Chuỗi thường (bản
    cũ/giả lập trong test) giữ nguyên văn làm grounding."""
    if isinstance(value, tool_registry.ToolOutcome):
        return value, value.grounding()
    if value:
        return tool_registry.ToolOutcome(status=tool_registry.STATUS_OK, text=str(value)), str(
            value
        )
    return tool_registry.ToolOutcome(), ""


def _forms(address_form: str | None) -> tuple[str, str]:
    """(cách gọi người dùng, Lan Anh tự xưng) cho câu do code viết."""
    you = address.normalize(address_form or reply_audience.current().address_form) or "anh"
    return you, address.self_word(you)


def _honesty_suffix(
    reply: str, outcome: tool_registry.ToolOutcome, address_form: str | None
) -> str:
    """Câu do CODE nối thêm, không trông vào model: chưa lưu được thì nói rõ; đã
    lưu mà câu trả lời thiếu giờ/nội dung chuẩn thì nối câu xác nhận của code."""
    you, me = _forms(address_form)
    label = outcome.effect_label or tool_registry.intent_label(outcome.intent) or "yêu cầu"
    if outcome.not_saved_hard:
        if outcome.status == tool_registry.STATUS_BLOCKED:
            note = f"⚠️ {me.capitalize()} chưa lưu {label} này: {outcome.text}."
        else:
            note = (
                f"⚠️ {me.capitalize()} chưa lưu được {label} này - {you} nhắn lại giúp "
                f"{me} sau ít phút nha."
            )
        return f"{reply}\n\n{note}" if reply else note
    if outcome.saved and outcome.confirmation:
        key = outcome.confirmation_key
        if not reply:
            return f"✅ {outcome.confirmation}"
        if key and key not in reply:
            return f"{reply}\n\n✅ {outcome.confirmation}"
    return reply


def _saved_note(outcome: tool_registry.ToolOutcome) -> str:
    return outcome.confirmation if outcome.saved else ""


async def run_chat_turn(
    user_id: int,
    text: str,
    *,
    grounding: str = "",
    channel: str = "telegram",
    is_admin: bool = False,
    address_form: str | None = None,
    allow_side_effects: bool = True,
) -> ChatTurn:
    """Pipeline chat chung: gom ngữ cảnh -> orchestrator.chat -> lưu lịch sử.

    grounding: ngữ cảnh giá cổ phiếu từ nhánh cổ phiếu (nếu có). channel và
    is_admin chọn gợi ý trình bày theo kênh và biến thể persona (admin Zalo
    khác thành viên thường; chủ bot nhận ra qua user_id); address_form là cách
    gọi riêng của thành viên Zalo (core/address.py). allow_side_effects=False
    (lượt chỉ đọc, vd tin nhắn thoại): tool ghi dữ liệu (nhắc việc, ghi chú, chi
    tiêu, danh mục) không chạy và câu trả lời nói rõ chưa lưu. Không raise: lỗi
    trả ChatTurn(failed=True) sau khi đã log và ghi telemetry."""
    prompt_id = await telemetry.start(user_id, "chat", text, channel=channel)
    outcome = tool_registry.ToolOutcome()
    # Chỉ truyền tham số khác mặc định: chỗ giả lập maybe_run_tool(user_id, text) cũ vẫn
    # chạy; vai trò admin Zalo đi qua tools.admin_scope().
    tool_kwargs: dict = {} if allow_side_effects else {"allow_side_effects": False}
    try:
        # Hạn chót CHAT_TURN_TIMEOUT_SEC áp cho CẢ lượt (web search có thể tự gọi
        # AI 2-3 lần + orchestrator.chat), không phải từng lượt gọi AI riêng lẻ -
        # nếu không, tổng thời gian vượt mức gateway Zalo chờ (240s). Task con của
        # asyncio.gather thừa hưởng cùng hạn chót qua ContextVar.
        with turn_deadline():
            # Giá vàng/tỷ giá/xổ số: số liệu tra bằng code (thường đã nằm sẵn
            # cache từ nhánh trả lời nhanh). Có đủ số liệu thì bỏ tra web và
            # forced search của lượt này - khỏi trả 2 lần và khỏi chỉ thị mâu
            # thuẫn "hệ thống không có dữ liệu".
            daily = await vn_daily_chat.grounding_for(text)
            search = (
                _skip_search_plan()
                if daily.skip_search
                else web_search.plan_search(text, user_id=user_id)
            )
            # 3 nguồn ngữ cảnh độc lập nhau - chạy song song thay vì tuần tự
            # (trước đây Tavily + memory nối tiếp, thêm vài trăm ms - vài giây
            # độ trễ cho mọi tin nhắn).
            with tools.admin_scope(is_admin):
                tool_result, plan, memory = await asyncio.gather(
                    tools.maybe_run_tool(user_id, text, **tool_kwargs),
                    search,
                    memory_service.build_memory_context(user_id, text),
                )
            outcome, tool_text = _tool_outcome(tool_result)
            combined = "\n\n".join(
                part for part in (grounding, daily.text, tool_text, plan.grounding) if part
            )
            forced = daily.force_real_search or (
                not daily.skip_search and stock_analysis.wants_external_market_data(text)
            )
            response = await orchestrator.chat(
                user_id,
                text,
                grounding=combined,
                memory_context=memory,
                require_real_search=forced,
                search_mode=_search_mode(
                    plan,
                    forced,
                    bool(grounding),
                    bool(tool_text) and outcome.status == tool_registry.STATUS_OK,
                ),
                channel=channel,
                is_admin=is_admin,
                address_form=address_form,
            )
        reply = (response.text or "").strip()
        searched = bool(getattr(response, "searched", False))
        untrusted_context = plan.untrusted or untrusted.contains_untrusted(combined)
        reply = _filter_reply_links(
            reply, (text, combined, memory), untrusted_context=untrusted_context, searched=searched
        )
        reply = _honesty_suffix(reply, outcome, address_form)
    except TurnTimeoutError as exc:
        logger.warning("Lượt chat hết hạn chót (kênh %s): %s", channel, exc)
        await telemetry.failure(prompt_id, "chat", exc)
        return ChatTurn(
            failed=True,
            prompt_id=prompt_id,
            timed_out=True,
            error_detail=str(exc),
            tool_note=_saved_note(outcome),
        )
    except Exception as exc:
        logger.exception("Lỗi lượt chat (kênh %s)", channel)
        await telemetry.failure(prompt_id, "chat", exc)
        return ChatTurn(
            failed=True,
            prompt_id=prompt_id,
            error_detail=messages.exception_detail(exc),
            tool_note=_saved_note(outcome),
        )

    # Từ đây câu trả lời ĐÃ sinh xong (đã tốn quota): lỗi ghi DB/telemetry chỉ
    # log, không bao giờ biến thành câu báo lỗi và mất câu trả lời.
    await telemetry.success(prompt_id, "chat", reply or "(không có nội dung)")
    if reply:
        await _save_turn_best_effort(user_id, text, reply)
        # Lượt trả lời dựa trên dữ liệu web ngoài (Tavily, provider tự tra web):
        # câu trả lời có thể mang nội dung bị prompt injection -> chỉ trích nhớ
        # từ lời người dùng (reply rỗng, xem memory_service._ASSISTANT_SKIPPED).
        memory_reply = "" if (untrusted_context or searched) else reply
        _track_background_task(
            asyncio.create_task(_update_memory_bounded(user_id, text, memory_reply))
        )
    return ChatTurn(reply, bool(getattr(response, "used_fallback", False)), prompt_id=prompt_id)


def _search_mode(
    plan: web_search.SearchPlan, forced: bool, has_stock_grounding: bool, has_tool_result: bool
) -> str:
    """search_mode cho orchestrator.chat. Số liệu ngoài sàn VN (vàng, tỷ giá...)
    luôn "forced". Có số liệu cổ phiếu chuẩn (DNSE) thì provider không tự tra web
    chồng lên (dễ lẫn giá web khác giá sàn); có kết quả tool (thời tiết, âm lịch,
    ghi chú...) thì không để Gemini tự tra thêm ở lượt chat thường."""
    if forced:
        return orchestrator.SEARCH_FORCED
    if has_stock_grounding and plan.mode in (orchestrator.SEARCH_AUTO, orchestrator.SEARCH_NATIVE):
        return orchestrator.SEARCH_OFF
    if has_tool_result and plan.mode == orchestrator.SEARCH_AUTO:
        return orchestrator.SEARCH_OFF
    return plan.mode


def _filter_reply_links(
    reply: str, sources: tuple[str, ...], *, untrusted_context: bool, searched: bool
) -> str:
    """Vô hiệu hoá link lạ trong câu trả lời (core/urls.py) theo
    REPLY_URL_FILTER. Bỏ qua lượt provider tự tra web: link nguồn do provider
    tìm ra, không có trong dữ liệu của bot nên lọc sẽ hỏng trích dẫn thật."""
    from core import config

    mode = config.REPLY_URL_FILTER
    if not reply or searched or mode == "off":
        return reply
    if mode != "all" and not untrusted_context:
        return reply
    filtered, removed = urls.filter_reply_urls(reply, sources)
    if removed:
        logger.warning("Đã vô hiệu hoá %d link lạ trong câu trả lời chat.", removed)
    return filtered


async def _save_turn_best_effort(user_id: int, text: str, reply: str) -> None:
    """Lưu cả lượt vào lịch sử (1 transaction, core.database.add_chat_turn); lỗi
    thì lượt sau chỉ thiếu ngữ cảnh lượt này - không chặn việc gửi câu trả lời.
    Không giữ hàng đợi ghi bù: dòng ghi muộn mang id/created_at mới sẽ làm lộn
    thứ tự lịch sử phiên."""
    try:
        await db.add_chat_turn(user_id, text, reply)
    except Exception:
        logger.warning(
            "Không lưu được lịch sử chat user_id=%s (DB lỗi) - vẫn gửi câu trả lời.",
            user_id,
            exc_info=True,
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
        # Mã lỗi/hết hạn chót lượt gom thành 1 dòng (services/stock_turn.py).
        kept, failed = stock_turn.split_persona_results(symbols, outputs)
        if failed:
            kept.append(stock_turn.persona_failure_note(failed))
        return ChannelResult(kept), ""
    if stock_analysis.wants_full_analysis(text, symbols):
        outputs: list[str] = []
        # Mã có bản số liệu nhưng không có phần diễn giải AI (LLM lỗi/hết hạn chót
        # lượt): 1 câu báo cuối thay cho lời hứa "⏳ ... sẽ gửi tiếp" rồi im lặng.
        skipped: list[str] = []
        for symbol in symbols:
            # Giống Telegram: bản số liệu rule-based (nguồn số chuẩn) luôn là
            # tin đầu tiên, bản diễn giải LLM (đã kiểm hợp đồng policy) theo sau.
            reports_before = len(outputs)

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
                elif len(outputs) > reports_before:
                    skipped.append(symbol)
            except Exception:
                logger.exception("Channel stock analysis failed for %s", symbol)
                outputs.append(messages.STOCK_ANALYZE_FAILED.format(symbol=symbol))
        if skipped:
            outputs.append(stock_turn.commentary_skipped_note(skipped))
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
    # "hoãn 10p" / "xong" ngay sau tin nhắc việc: code xử lý, không tốn lượt AI.
    shortcut = await reminder_service.maybe_handle_shortcut(user_id, text)
    if shortcut is not None:
        return ChannelResult([shortcut])
    portfolio_result = await portfolio_service.maybe_handle_natural_language(user_id, text)
    if portfolio_result is not None:
        return ChannelResult([portfolio_result])
    article = await article_reader.maybe_handle(text)
    if article is not None:
        return ChannelResult([article])
    # Giá vàng/tỷ giá/xổ số, thời tiết (Open-Meteo) và âm lịch (offline): trả
    # lời thẳng, không tốn lượt AI nào; câu không chắc trả None và đi tiếp
    # xuống chat AI như cũ.
    daily_reply = await vn_daily_chat.maybe_handle(text, user_id)
    if daily_reply is not None:
        return ChannelResult([daily_reply])
    weather_reply = await weather_chat.maybe_handle(text, user_id=user_id)
    if weather_reply is not None:
        return ChannelResult([weather_reply])
    lunar_reply = await lunar_chat.maybe_handle(text)
    if lunar_reply is not None:
        return ChannelResult([lunar_reply])

    # MỘT hạn chót cho cả nhánh cổ phiếu (debate nhiều lượt gọi AI) LẪN lượt chat
    # theo sau: turn_deadline lồng nhau giữ hạn SỚM hơn, nên run_chat_turn không
    # được cộng thêm 200s nữa (trước đây tổng có thể tới 400s > 240s gateway
    # Zalo chờ, người dùng nhận im lặng).
    with turn_deadline():
        try:
            stock_result, grounding = await _handle_stock(user_id, text, is_admin)
        except TurnTimeoutError as exc:
            logger.warning("Nhánh cổ phiếu hết hạn chót lượt: %s", exc)
            return ChannelResult([await reply_audience.error_text("timeout", user_id, str(exc))])
        if stock_result is not None:
            return stock_result
        turn = await run_chat_turn(
            user_id,
            text,
            grounding=grounding,
            channel=channel,
            is_admin=is_admin,
            # Router Zalo gắn cách gọi riêng của người gửi (services/reply_audience.py).
            address_form=reply_audience.current().address_form,
        )
    if turn.failed:
        return ChannelResult([await failure_text(turn, user_id)])
    if not turn.text:
        return ChannelResult([await reply_audience.error_text("empty", user_id)])
    # Nhãn câu trả lời dự phòng là chẩn đoán cho chủ bot (Zoom, Zalo controller);
    # thành viên chỉ cần câu trả lời.
    footnote = (
        "\n\n⚙️ _Câu trả lời do API dự phòng trả về, ngôn ngữ có thể khác thường ngày._"
        if turn.used_fallback and await reply_audience.is_owner(user_id)
        else ""
    )
    return ChannelResult([turn.text + footnote], "api" if turn.used_fallback else None)
