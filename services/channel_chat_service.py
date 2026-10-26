"""Luồng chat văn bản dùng chung mọi kênh.

- run_chat_turn(): pipeline lượt chat AI chung cho Telegram, Zalo và Zoom
  (tool/web search/trí nhớ -> orchestrator.chat -> lưu lịch sử, cập nhật trí
  nhớ chạy nền, telemetry). Telegram (handlers/chat_router.py) gọi thẳng hàm
  này sau nhánh cổ phiếu riêng của nó.
- handle_channel_text(): điểm vào đầy đủ cho Zalo/Zoom (lệnh, danh mục, cổ
  phiếu rồi tới run_chat_turn), trả ChannelResult thay vì tự gửi tin.
"""

import asyncio
import dataclasses
import logging
from dataclasses import dataclass

import messages
from ai import drafts, orchestrator
from ai.timeouts import TurnTimeoutError, turn_deadline
from core import config, trace, untrusted, urls
from services import (
    article_reader,
    chat_context,
    conversation_log,
    expense_service,
    lunar_chat,
    weather_chat,
    memory_service,
    portfolio_service,
    reminder_service,
    stock_turn,
    tool_registry,
    tools,
    turn_progress,
    usage_quota,
    web_search,
)
from services import channel_command_service, reply_audience, style_approval
from services import attention, vn_daily_chat
from services.channel_result import ChannelResult
from services.telemetry import telemetry
from stock import analysis as stock_analysis

logger = logging.getLogger(__name__)


async def stop_background_tasks() -> None:
    """Drain các lượt trích trí nhớ nền (mọi kênh) trước khi đóng database
    (task do services/memory_service.after_exchange tạo)."""
    await memory_service.stop_background_tasks()


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
    # Provider/model đã trả lời (ai/orchestrator gắn vào response) - đánh giá 👍/👎.
    provider: str = ""
    model: str = ""
    # id 2 dòng chat_messages của lượt (rỗng = chưa lưu lịch sử) - 🔄/✂️ xoá
    # đúng cặp này sau khi làm lại xong (services/answer_feedback.py).
    history_ids: tuple[int, ...] = ()


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


async def _empty_text() -> str:
    return ""


async def _maybe_opt_out(user_id: int, text: str, last_row: dict | None) -> str:
    from services import proactive_service

    if (last_row or {}).get("kind") != conversation_log.KIND_FOLLOWUP:
        return ""
    if not proactive_service.is_opt_out(text):
        return ""
    try:
        await proactive_service.opt_out(user_id)
    except Exception:
        logger.warning("Không tắt được hỏi thăm chủ động user_id=%s.", user_id, exc_info=True)
        return ""
    return proactive_service.OPT_OUT_NOTE


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
    return reply_audience.forms(address_form)


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
        # confirmation_key: các nhóm bắt buộc nối bằng "&", mỗi nhóm gồm nhiều cách
        # viết nối bằng "|" ("20:00|20h|8h&15/11|ngày 15") - mỗi nhóm phải có 1 cách.
        groups = [[k for k in g.split("|") if k] for g in outcome.confirmation_key.split("&")]
        groups = [g for g in groups if g]
        if not reply:
            return f"✅ {outcome.confirmation}"
        if groups and not all(any(k in reply for k in g) for g in groups):
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
    quote_text: str | None = None,
    allow_side_effects: bool = True,
    style_hint: str = "",
) -> ChatTurn:
    """Pipeline chat chung: gom ngữ cảnh -> orchestrator.chat -> lưu lịch sử.

    grounding: ngữ cảnh giá cổ phiếu từ nhánh cổ phiếu (nếu có). channel và
    is_admin chọn gợi ý trình bày theo kênh và biến thể persona (admin Zalo
    khác thành viên thường; chủ bot nhận ra qua user_id); address_form là cách
    gọi riêng của thành viên Zalo (core/address.py). quote_text: tin người dùng
    đang trả lời/trích dẫn (rào như dữ liệu, không gộp vào lời người dùng).
    allow_side_effects=False (lượt chỉ đọc, vd tin nhắn thoại): tool ghi dữ liệu
    (nhắc việc, ghi chú, chi tiêu, danh mục, điều cần nhớ) không chạy và câu trả
    lời nói rõ chưa lưu. style_hint: yêu cầu trình bày riêng của lượt này (nút
    ✂️ Ngắn hơn/🔄 Thử lại - services/answer_feedback.py), nối sau "cách trả lời"
    người dùng đã dặn. Không raise: lỗi trả ChatTurn(failed=True) sau khi đã
    log và ghi telemetry."""
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
            # Tin có link (article_reader không xử lý): đọc trang thay vì tra web
            # theo chữ trong tin - trừ khi người dùng muốn so với nguồn khác.
            links_ok = chat_context.links_allowed(user_id, is_admin)
            page_turn = links_ok and chat_context.first_url(text) is not None
            compare = page_turn and chat_context.wants_outside_sources(text)
            skip_search = daily.skip_search or (page_turn and not compare)
            # Thành viên Zalo đã hết lượt "nặng" hôm nay (services/usage_quota.py):
            # không tra web, câu cần tin mới kèm ghi chú cấm bịa.
            quota_no_search = not skip_search and usage_quota.search_blocked()
            search = (
                _skip_search_plan()
                if skip_search
                else usage_quota.exhausted_search_plan(text)
                if quota_no_search
                else web_search.plan_search(text, user_id=user_id)
            )
            use_kb = chat_context.kb_allowed(
                user_id, is_admin, text, has_stock_grounding=bool(grounding)
            )
            # Các nguồn ngữ cảnh độc lập nhau - chạy song song thay vì tuần tự
            # (trước đây Tavily + memory nối tiếp, thêm vài trăm ms - vài giây
            # độ trễ cho mọi tin nhắn).
            # Đọc link đã do chat_context.read_link lo (theo quyền links_allowed của
            # chủ bot/admin/thành viên): router không được chọn tool read_link đọc
            # lại lần 2, hay đọc giúp người không được phép đọc link.
            excluded = ("read_link",) if (page_turn or not links_ok) else ()
            with (
                tools.admin_scope(is_admin),
                tools.exclude_scope(excluded),
                trace.stage("context"),  # turn_metrics.context_ms
            ):
                tool_result, plan, memory, last_row, page, kb = await asyncio.gather(
                    tools.maybe_run_tool(user_id, text, **tool_kwargs),
                    search,
                    memory_service.build_turn_memory(user_id, text),
                    # Câu trả lời ngay trước (báo cáo cổ phiếu?) - chặn câu chat nói
                    # ngược policy.
                    conversation_log.last_model_row(user_id),
                    chat_context.read_link(user_id, text, allowed=links_ok),
                    chat_context.kb_grounding(text) if use_kb else _empty_text(),
                )
            outcome, tool_text = _tool_outcome(tool_result)
            if outcome.name == "rag_search" and outcome.status == tool_registry.STATUS_OK:
                # Router đã tra kho rag/ theo yêu cầu rõ: không chèn đoạn tự tra trùng.
                kb = ""
            quote = chat_context.quote_grounding(quote_text)
            # "Đừng hỏi/nhắc nữa" ngay sau tin hỏi thăm chủ động: tắt tạm hỏi thăm.
            followup_note = await _maybe_opt_out(user_id, text, last_row)
            # Kho rag/ đặt CUỐI: cắt giữa (prompt_budget) không cắt vào kết quả tra web.
            combined = "\n\n".join(
                part
                for part in (
                    followup_note,
                    quote,
                    grounding,
                    daily.text,
                    tool_text,
                    page.grounding,
                    plan.grounding,
                    kb,
                )
                if part
            )
            forced = not quota_no_search and (
                daily.force_real_search
                or (not skip_search and stock_analysis.wants_external_market_data(text))
            )
            mode = _search_mode(
                plan,
                forced,
                bool(grounding),
                bool(tool_text) and outcome.status == tool_registry.STATUS_OK,
            )
            if page.grounding and not (forced or compare) and mode == orchestrator.SEARCH_AUTO:
                # Đã có nội dung trang (hoặc biết chắc không đọc được): Gemini không
                # tự tra Google chồng lên.
                mode = orchestrator.SEARCH_OFF
            # Kênh báo "em đang tra web/đang soạn..." (services/turn_progress.py).
            searching = mode in (orchestrator.SEARCH_NATIVE, orchestrator.SEARCH_FORCED)
            turn_progress.report(turn_progress.SEARCH if searching else turn_progress.THINK)
            # Bản nháp chữ đang sinh (Telegram) chỉ cho chat thường: lượt có số liệu
            # cổ phiếu, hoặc ngay sau báo cáo cổ phiếu (câu trả lời còn qua bước
            # chặn câu nói ngược policy - conversation_log.guard_stock_reply), không
            # phát nháp. Link trong nháp đã bị turn_feedback.draft_text thay "…".
            stock_followup = (last_row or {}).get("kind") == conversation_log.KIND_STOCK
            with drafts.drafting(not grounding and not stock_followup), trace.stage("llm"):
                response = await orchestrator.chat(
                    user_id,
                    text,
                    grounding=combined,
                    memory_context=memory.context,
                    require_real_search=forced,
                    search_mode=mode,
                    channel=channel,
                    is_admin=is_admin,
                    address_form=address_form,
                    style_rules="\n\n".join(p for p in (memory.style_rules, style_hint) if p),
                )
        form = address_form or reply_audience.current().address_form
        reply = conversation_log.guard_stock_reply(
            (response.text or "").strip(), last_row, user_text=text, address_form=form
        )
        if grounding and not is_admin and not config.is_owner_user_id(user_id):
            # Thành viên Zalo hỏi về mã có số liệu giá (vd "FPT có đáng tích luỹ
            # dài hạn không"): chỉ bản tham khảo - bỏ dòng nhãn hành động/vùng
            # mua/stop/target bằng code, không trông vào persona hay "cách trả
            # lời" người dùng tự dặn.
            reply = conversation_log.reference_only_reply(
                reply, conversation_log.grounding_price_lines(grounding), address_form=form
            )
        searched = bool(getattr(response, "searched", False))
        untrusted_context = plan.untrusted or untrusted.contains_untrusted(combined)
        reply = _filter_reply_links(
            reply,
            (text, combined, memory.context, *_response_citations(response)),
            untrusted_context=untrusted_context,
            searched=searched,
        )
        reply = _honesty_suffix(reply, outcome, address_form)
    except TurnTimeoutError as exc:
        logger.warning("Lượt chat hết hạn chót (kênh %s): %s", channel, exc)
        trace.set_outcome("timeout")
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
        trace.set_outcome("error")
        await telemetry.failure(prompt_id, "chat", exc)
        return ChatTurn(
            failed=True,
            prompt_id=prompt_id,
            error_detail=messages.exception_detail(exc),
            tool_note=_saved_note(outcome),
        )

    # Từ đây câu trả lời ĐÃ sinh xong (đã tốn quota): lỗi ghi DB/telemetry chỉ
    # log, không bao giờ biến thành câu báo lỗi và mất câu trả lời.
    history_ids: list[int] = []
    provider = str(getattr(response, "provider", "") or "")
    model = str(getattr(response, "model", "") or "")
    if provider:
        await telemetry.success(
            prompt_id, "chat", reply or "(không có nội dung)", provider=provider, model=model
        )
    else:
        await telemetry.success(prompt_id, "chat", reply or "(không có nội dung)")
    if reply:
        # Lượt trả lời dựa trên dữ liệu ngoài (Tavily, provider tự tra web, trang
        # người dùng gửi...): câu trả lời có thể mang nội dung bị prompt injection
        # -> kind chat_ext, trích nhớ chỉ đọc lời người dùng. Lưu lịch sử (1
        # transaction) rồi trích nhớ theo lô chạy nền (services/memory_service).
        kind = (
            conversation_log.KIND_CHAT_EXT
            if (untrusted_context or searched)
            else conversation_log.KIND_CHAT
        )
        if not allow_side_effects and (
            outcome.status == tool_registry.STATUS_BLOCKED or memory_service.wants_remember(text)
        ):
            # Lượt chỉ đọc nhờ nhớ/ghi: câu trả lời đã nói "chưa lưu" - bộ trích
            # nhớ không được lưu thay từ chữ chép (có thể nghe nhầm) của lượt này.
            kind = conversation_log.KIND_CHAT_READONLY
        await conversation_log.record_exchange(
            user_id, text, reply, kind, read_only=not allow_side_effects, saved_ids=history_ids
        )
    if searched or plan.untrusted or searching:
        # Lượt có tra web: tính vào trần lượt "nặng" của thành viên Zalo.
        await usage_quota.note_web_search(user_id)
    return ChatTurn(
        reply,
        bool(getattr(response, "used_fallback", False)),
        prompt_id=prompt_id,
        provider=provider,
        model=model,
        history_ids=tuple(history_ids),
    )


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


_MAX_REPLY_CITATIONS = 40


def _response_citations(response) -> list[str]:
    """URL nguồn provider đã tra trong lượt tự tra web (Response.citations:
    url_citation, executed_tools của Groq, tên miền grounding Gemini)."""
    raw = getattr(response, "citations", None)
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw[:_MAX_REPLY_CITATIONS] if isinstance(item, str)]


def _filter_reply_links(
    reply: str, sources: tuple[str, ...], *, untrusted_context: bool, searched: bool
) -> str:
    """Vô hiệu hoá link lạ trong câu trả lời (core/urls.py) theo REPLY_URL_FILTER
    ("off" tắt hẳn). ``sources`` gồm cả URL trích dẫn của provider tự tra web.

    - Lượt có dữ liệu ngoài đã rào (Tavily, trang, trích dẫn...) - kể cả khi
      provider cũng tự tra web - hoặc chế độ "all" ở lượt không tự tra web: lọc
      đầy đủ.
    - Lượt provider tự tra web (native/forced, Gemini AUTO có grounding): provider
      đọc trang lạ cùng persona/trí nhớ, nhưng nguồn trích dẫn có thể không có
      trong dữ liệu của bot -> lọc lỏng: giữ link không query, vô hiệu link mang
      dữ liệu (query string/userinfo không khớp nguồn, tên miền trần kèm ?/=)."""
    mode = config.REPLY_URL_FILTER
    if not reply or mode == "off":
        return reply
    if untrusted_context or (mode == "all" and not searched):
        strict = True
    elif searched:
        strict = False
    else:
        return reply
    filtered, removed = urls.filter_reply_urls(reply, sources, strict=strict)
    if removed:
        logger.warning(
            "Đã vô hiệu hoá %d link lạ trong câu trả lời chat (%s).",
            removed,
            "đầy đủ" if strict else "lượt tự tra web",
        )
    return filtered


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
            return ChannelResult(
                [
                    messages.stock_symbol_unresolved(
                        reply_audience.current().address_form,
                        owner=await reply_audience.is_owner(user_id),
                    )
                ]
            ), ""
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


async def _side_effect_paths(
    user_id: int, text: str, is_admin: bool, channel: str
) -> ChannelResult | None:
    """Các nhánh có thể GHI dữ liệu trước chat AI: lệnh /, danh mục bằng lời, lối
    tắt "hoãn 10p"/"xong" của nhắc việc, ghi chi tiêu "cafe 45k". Câu trả lời nhanh
    cũng vào lịch sử (services/conversation_log) cho câu hỏi nối tiếp."""
    record = conversation_log.record_exchange
    # "ghi nhớ"/"bỏ" ngay sau câu em hỏi duyệt cách trả lời (services/style_approval).
    approval = await style_approval.maybe_handle_reply(user_id, text)
    if approval is not None:
        return ChannelResult([approval])
    portfolio_command = await portfolio_service.handle_command(user_id, text)
    if portfolio_command is not None:
        return ChannelResult([portfolio_command], market=True)
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
    # Mọi câu trả lời nhanh bên dưới cũng vào lịch sử (services/conversation_log):
    # câu hỏi nối tiếp ("thế còn ngày mai?", "sao em bảo giữ?") mới có ngữ cảnh.
    # "hoãn 10p" / "xong" ngay sau tin nhắc việc: code xử lý, không tốn lượt AI.
    shortcut = await reminder_service.maybe_handle_shortcut(user_id, text)
    if shortcut is not None:
        await record(user_id, text, shortcut, conversation_log.KIND_REMINDER)
        return ChannelResult([shortcut])
    portfolio_result = await portfolio_service.maybe_handle_natural_language(user_id, text)
    if portfolio_result is not None:
        await record(user_id, text, portfolio_result, conversation_log.KIND_PORTFOLIO)
        return ChannelResult([portfolio_result], market=True)
    # "cafe 45k", "đổ xăng 1tr2" (hoặc "huỷ" ngay sau đó): ghi chi tiêu bằng code.
    expense_reply = await expense_service.maybe_handle_chat(user_id, text)
    if expense_reply is not None:
        await record(user_id, text, expense_reply, conversation_log.KIND_EXPENSE)
        return ChannelResult([expense_reply])
    return None


async def handle_channel_text(
    user_id: int,
    text: str,
    is_admin: bool = True,
    channel: str = "zalo",
    *,
    quote_text: str | None = None,
    allow_side_effects: bool = True,
) -> ChannelResult:
    """quote_text: tin người dùng đang trả lời trên Zalo (ZaloMessageRequest.quote_text).
    allow_side_effects=False (lượt chỉ đọc, vd chữ chép từ tin nhắn thoại): bỏ
    qua lệnh /, ghi danh mục bằng lời, lối tắt "hoãn/xong", ghi chi tiêu, và tool
    ghi dữ liệu của lượt chat AI - chỉ trả lời, không ghi gì."""
    text = (text or "").strip()
    if not text:
        return ChannelResult([])
    record = conversation_log.record_exchange

    if allow_side_effects:
        side_effect_result = await _side_effect_paths(user_id, text, is_admin, channel)
        if side_effect_result is not None:
            return side_effect_result
    article = await article_reader.maybe_handle(text)
    if article is not None:
        await record(user_id, text, article, conversation_log.KIND_ARTICLE)
        # Giữ bài vừa đọc (đã rào) cho câu hỏi nối tiếp "bài này nói gì về...".
        chat_context.remember_article(user_id, text, article)
        return ChannelResult([article])
    # "Có gì cần anh chú ý?" / "hôm nay có gì cần lo": tin /chuy tính bằng code.
    if attention.is_attention_question(text):
        reply = await channel_command_service.attention_text(user_id, channel, is_admin)
        await record(user_id, text, reply, conversation_log.KIND_DAILY)
        return ChannelResult([reply])
    # Giá vàng/tỷ giá/xổ số, thời tiết (Open-Meteo) và âm lịch (offline): trả
    # lời thẳng, không tốn lượt AI nào; câu không chắc trả None và đi tiếp
    # xuống chat AI như cũ.
    # Lượt chỉ đọc: không lưu thành phố thời tiết mặc định ("anh ở Đà Nẵng").
    daily_reply = await vn_daily_chat.maybe_handle(text, user_id if allow_side_effects else None)
    if daily_reply is not None:
        await record(user_id, text, daily_reply, conversation_log.KIND_DAILY)
        return ChannelResult([daily_reply])
    weather_reply = await weather_chat.maybe_handle(text, user_id=user_id)
    if weather_reply is not None:
        await record(user_id, text, weather_reply, conversation_log.KIND_WEATHER)
        return ChannelResult([weather_reply])
    lunar_reply = await lunar_chat.maybe_handle(text)
    if lunar_reply is not None:
        await record(user_id, text, lunar_reply, conversation_log.KIND_LUNAR)
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
            # Lượt cổ phiếu không qua telemetry: ghi user_calls (thống kê + quota
            # thành viên - tính như lượt thường, không có trần riêng).
            await usage_quota.record(user_id, channel, usage_quota.KIND_STOCK)
            # Thành viên (is_admin=False): bản lưu cũng chỉ là thông tin tham khảo.
            await record(
                user_id,
                text,
                "",
                conversation_log.KIND_STOCK,
                full_access=is_admin,
                stock_outputs=list(stock_result.messages),
            )
            # Zalo tô màu % tăng/giảm trong tin cổ phiếu (channels/zalo_text.py).
            return dataclasses.replace(stock_result, market=True)
        turn = await run_chat_turn(
            user_id,
            text,
            grounding=grounding,
            channel=channel,
            is_admin=is_admin,
            # Router Zalo gắn cách gọi riêng của người gửi (services/reply_audience.py).
            address_form=reply_audience.current().address_form,
            quote_text=quote_text,
            **({} if allow_side_effects else {"allow_side_effects": False}),
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
    outputs = [turn.text + footnote]
    # Đề xuất cách trả lời chờ duyệt: hỏi bằng tin riêng SAU câu trả lời, tối đa
    # 1 lần/ngày (services/style_approval) - người dùng nhắn "ghi nhớ"/"bỏ".
    question = await style_approval.take_question(user_id)
    if question is not None:
        outputs.append(question.text)
    return ChannelResult(outputs, "api" if turn.used_fallback else None)
