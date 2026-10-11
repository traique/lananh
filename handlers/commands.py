"""Lệnh Telegram chung: trợ giúp, /prompt, /gia, /dich, trí nhớ, lịch sử,
/thongke, /agent, /rag, bản tin và error handler.

Handler chỉ parse tham số -> gọi service -> trả lời. Lệnh nhóm khác tách riêng:
- handlers/provider_commands.py: /model, /status, /userouter9, /router9, /tavily, /anh
- handlers/pairing_commands.py: /zoompair, /zoomxoa, /zoomstatus, /zalo* (quản lý user)
- handlers/zalo_bridge_commands.py: /nhom, /themnhom, ..., /fb_*
"""

import html
import logging

from telegram import Update
from telegram.ext import ContextTypes

import messages
from ai import orchestrator
from core import database as db, webhook_inbox
from core.text_utils import parse_on_off
from handlers import common
from services import (
    memory_service,
    price_search,
    prompt_templates,
    rag_clean_service,
    rag_service,
    usage_stats,
)
from services.telemetry import telemetry
from services.telegram_processing import record_error

logger = logging.getLogger(__name__)

HISTORY_PROMPT_PREVIEW_MAX = 60
HISTORY_LIMIT = 10

HELP_TEXT = (
    "📖 *Các lệnh hỗ trợ:*\n\n"
    "💬 Gõ tin nhắn bình thường để trò chuyện với em - Lan Anh - như trợ lý cá nhân.\n\n"
    "📊 Khi anh nhắc tới 1 *mã cổ phiếu Việt Nam*, mặc định em lấy giá khớp lệnh REALTIME.\n"
    'Cần phân tích sâu thì cứ nói rõ (vd "phân tích giúp anh mã FPT").\n\n'
    "🖼️ *Gửi 1 ảnh chân dung* để Gemini viết lại prompt giữ nguyên khuôn mặt.\n\n"
    "/prompt — viết prompt tạo ảnh từ mô tả cơ bản\n"
    "/gia — Tìm và so sánh giá sản phẩm\n"
    "/dich [ja>vi|vi>ja] <nội dung> — dịch chat công việc Nhật-Việt\n"
    "/reset — xoá ngữ cảnh chat\n"
    "/history — xem 10 lượt gần nhất\n"
    "/memory [on|off] — xem, bật hoặc tắt trí nhớ dài hạn\n"
    "/forget — xoá trí nhớ dài hạn\n"
    "/notes — xem ghi chú đã lưu\n"
    "/model — xem/đổi model chat\n"
    "/status — xem trạng thái provider\n"
    "/thongke [Nd|Ngiờ] — thống kê lượt gọi theo user/model, mặc định 7 ngày\n"
    "/agent <câu hỏi> — agent tự tra cứu nhiều bước để trả lời (thử nghiệm)\n"
    "/rag <câu hỏi> — tra cứu kiến thức trong thư mục rag/\n"
    "/ragxuly <file> — dọn file md OCR trong rag/ (AI thêm heading, backup bản gốc)\n"
    "/bangtinsang — gửi ngay Bản tin sáng tới Zalo + Zoom (bình thường tự gửi lúc 8h)\n"
    "/tintuc — đọc Điểm tin mới ngay trong Telegram, không broadcast sang Zalo/Zoom\n"
    "/userouter9 — ép thử lại 9Router ngay\n"
    "/router9 on|off — bật/tắt 9Router thủ công\n"
    "/tavily on|off — bật/tắt tra web Tavily trước khi trả lời\n"
    "/anh <mô tả> — tạo ảnh thật (Agnes AI); /anh on|off — bật/tắt\n"
    "/zoompair, /zoomxoa, /zoomstatus — quản lý pairing Zoom\n"
    "/nhom, /themnhom, /xoanhom, /tongket, /dangnoi — quản lý và xem lại nhóm Zalo\n"
    "📣 *Zalo → Facebook Page (tách riêng khỏi /tongket):*\n"
    "/fb\\_nhom — xem các nhóm nguồn Facebook\n"
    "/fb\\_themnhom <group\\_id> <tên> — thêm nhóm Zalo làm nguồn đăng Facebook\n"
    "/fb\\_pages — xem các Facebook Page đã cấu hình (fb\\_ok sẽ đăng lên tất cả)\n"
    "/fb\\_xoanhom <group\\_id|tên> — bỏ nhóm khỏi luồng Facebook\n"
    "/fb\\_xem <post\\_id> — xem lại bài đang chờ duyệt\n"
    "/fb\\_sua <post\\_id> <nội dung> — sửa nội dung bài chờ\n"
    "/fb\\_link <post\\_id> <affiliate\\_url> — nhập short-link affiliate khi bài chỉ có 1 link Shopee\n"
    "/fb\\_link <post\\_id> <source\\_url> <affiliate\\_url> — nhập từng link khi bài có nhiều link\n"
    "(đủ link thì AI tự viết lại bài, link affiliate được thả ở bình luận đầu tiên)\n"
    "/fb\\_ok <post\\_id> — duyệt và đăng lên TẤT CẢ Facebook Page đang cấu hình; "
    "page nào lỗi sẽ báo riêng, chạy lại /fb\\_ok để chỉ thử lại page lỗi, page đã đăng OK không bị đụng tới\n"
    "/fb\\_check <post\\_id> — kiểm tra từng page đã published/công khai chưa, lấy permalink\n"
    "/fb\\_boqua <post\\_id> — bỏ bài chờ\n"
    "/fb\\_reset — xóa bài Facebook đã lưu, reset ID về #1 khi có thể\n"
    "/fb\\_boloc \\[reset] — thống kê bộ lọc Zalo→Facebook (bài giữ/bỏ/trùng, số bài chờ)\n"
    "/fb\\_loclai — chạy lại bộ lọc cho các bài đang chờ, bỏ bài không đạt\n"
    "/zalopair, /zaloadmin, /zalohaquyen, /zalokhoa, /zalomokhoa, /zaloxoa, /zalodanhsach — quản lý user Zalo\n"
    "/help — hiển thị hướng dẫn này"
)


@common.restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Chào anh, em Lan Anh nè - trợ lý cá nhân của anh đây! 💕\n"
        "Gõ /help xem đầy đủ lệnh nha anh."
    )


@common.restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


@common.restricted
async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.INVALID_COMMAND)


@common.restricted
async def prompt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_desc = common.extract_arg(context)
    if not user_desc:
        await update.message.reply_text(
            "Anh nhập mô tả muốn tạo prompt nhé. Ví dụ: /prompt cô gái đứng trước nhà"
        )
        return

    user_id = update.effective_user.id
    prompt_id = await telemetry.start(user_id, "prompt_generator", user_desc)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    instruction, identity = prompt_templates.build_text_instruction(user_desc)

    try:
        response = await orchestrator.ask(instruction)
        result_text = (response.text or "").strip()

        if not result_text:
            await telemetry.success(prompt_id, "prompt_generator", "(Gemini không trả về nội dung)")
            await update.message.reply_text("Gemini không trả lời được, anh thử lại nha.")
            return

        await telemetry.success(prompt_id, "prompt_generator", result_text)
        # Prompt gửi trong khối <pre> để Telegram hiện nút Copy và giữ nguyên văn
        # các ký tự * / _ (reply_long_text sẽ convert markdown và làm mất chúng).
        # Nhãn "⚙️ API" đặt ở header, không nằm trong khối prompt.
        header = "📝 <b>Prompt gợi ý</b> — chạm vào khối bên dưới để chép:"
        if getattr(response, "used_fallback", False):
            header += "  ⚙️ API"
        header += f"\n\n{identity.mode_hint}"
        await update.message.reply_text(header, parse_mode="HTML")
        await common.reply_code_block(update.message, result_text)
    except Exception as e:
        logger.exception("Lỗi tạo prompt")
        await telemetry.failure(prompt_id, "prompt_generator", e)
        await update.message.reply_text("❌ Có lỗi khi tạo prompt. Hãy thử lại sau giây lát.")


@common.restricted
async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    product_name = common.extract_arg(context)
    if not product_name:
        await update.message.reply_text(
            "Anh nhập tên sản phẩm muốn tìm giá nhé. Ví dụ: /gia iPhone 16 Pro"
        )
        return

    user_id = update.effective_user.id

    cached_text = price_search.get_cached_price(product_name)
    if cached_text is not None:
        await telemetry.start(user_id, "price_search", product_name)
        await update.message.reply_text(
            f'⚡ Có kết quả gần đây cho "{product_name}", gửi anh liền nè:'
        )
        await common.reply_long_text(update.message, cached_text)
        return

    prompt_id = await telemetry.start(user_id, "price_search", product_name)

    status = await update.message.reply_text(
        f"🔍 Đang dạo siêu thị tìm giá {product_name} cho anh..."
    )
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        result_text, used_fallback = await price_search.search_price(product_name)

        if not result_text:
            await telemetry.success(prompt_id, "price_search", "(Gemini không trả về nội dung)")
            await status.edit_text("Em không tìm được giá lúc này, anh thử lại sau nha.")
            return

        await telemetry.success(prompt_id, "price_search", result_text)
        result_text = await price_search.verify_links(result_text)
        price_search.set_cached_price(product_name, result_text)
        suffix = "\n\n⚙️ API" if used_fallback else ""

        await common.reply_long_text_edit_first(status, result_text + suffix)
    except Exception as e:
        logger.exception("Lỗi tìm giá sản phẩm")
        await telemetry.failure(prompt_id, "price_search", e)
        await status.edit_text("❌ Có lỗi khi cào dữ liệu giá. Anh thử lại sau nhé.")


@common.restricted
async def dich_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services import translate_service

    argument = common.extract_arg(context)
    if not argument.strip():
        await update.message.reply_text(
            "Cú pháp: /dich [ja>vi|vi>ja] <nội dung>\n"
            "Không chỉ định chiều thì tự nhận diện theo chữ Nhật trong câu.\n"
            "Ví dụ: /dich お世話になります。確認お願いします。"
        )
        return

    first_token, _, rest = argument.partition(" ")
    direction = translate_service.parse_explicit_direction(first_token)
    text = rest if direction else argument
    if not text.strip():
        await update.message.reply_text("Cú pháp: /dich [ja>vi|vi>ja] <nội dung>")
        return

    user_id = update.effective_user.id
    prompt_id = await telemetry.start(user_id, "translate", text)
    try:
        result, resolved, response = await translate_service.translate(text, direction)
    except ValueError as exc:
        await update.message.reply_text(f"Cú pháp: /dich [ja>vi|vi>ja] <nội dung>\n({exc})")
        return
    except Exception as exc:
        logger.exception("Lỗi dịch /dich")
        await telemetry.failure(prompt_id, "translate", exc)
        await update.message.reply_text("❌ Không dịch được lúc này, thử lại sau nhé.")
        return

    if not result:
        await telemetry.success(prompt_id, "translate", "(không có nội dung)")
        await update.message.reply_text("Em chưa dịch được câu này, anh thử lại nhé.")
        return

    await telemetry.success(prompt_id, "translate", result)
    label = translate_service.direction_label(resolved)
    suffix = "\n\n⚙️ API" if getattr(response, "used_fallback", False) else ""
    await common.reply_long_text(update.message, f"🇯🇵↔🇻🇳 {label}\n\n{result}{suffix}")


@common.restricted
async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    notes = await db.get_notes(user_id, limit=10)
    if not notes:
        await update.message.reply_text("📝 Chưa có ghi chú nào.")
        return
    lines = ["📝 *Ghi chú gần đây:*"]
    for content, created_at in notes:
        lines.append(f"• {content} _(đặt lúc {created_at.strftime('%H:%M %d/%m')})_")
    await common.reply_long_text(update.message, "\n".join(lines))


@common.restricted
async def reset_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await orchestrator.reset_chat()
    await db.clear_chat(user_id)
    await update.message.reply_text("🔄 Đã xoá ngữ cảnh hội thoại. Bắt đầu chat mới nhé!")


@common.restricted
async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if context.args:
        toggle = parse_on_off(context.args[0])
        if toggle is True:
            await memory_service.set_enabled(user_id, True)
            await update.message.reply_text("✅ Đã bật trí nhớ dài hạn.")
            return
        if toggle is False:
            await memory_service.set_enabled(user_id, False)
            await update.message.reply_text(
                "🔴 Đã tắt trí nhớ dài hạn. Bot sẽ không trích xuất/nhớ thêm gì mới cho tới khi bật lại bằng /memory on."
            )
            return

    facts = await db.get_facts(user_id)
    summary = await db.get_summary(user_id)
    enabled = await memory_service.is_enabled(user_id)
    status_line = (
        f"\nTrạng thái: {'BẬT' if enabled else 'TẮT'} (dùng /memory on hoặc /memory off để đổi)"
    )

    if not facts and not summary:
        await update.message.reply_text(f"🧠 Em chưa nhớ gì dài hạn về anh cả.{status_line}")
        return

    lines = ["🧠 *Trí nhớ dài hạn về anh:*"]
    if summary:
        lines.append(f"\n_Tóm tắt:_ {summary}")
    if facts:
        lines.append("\n*Các thông tin đã biết:*")
        for key, value in facts:
            lines.append(f"• `{key}`: {value}")
    lines.append(status_line)
    lines.append("\nGõ /forget nếu anh muốn em xoá hết trí nhớ này.")
    await common.reply_long_text(update.message, "\n".join(lines))


@common.restricted
async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await memory_service.clear_memory(user_id)
    await update.message.reply_text("🗑️ Đã xoá toàn bộ trí nhớ dài hạn.")


@common.restricted
async def thongke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hours = usage_stats.parse_thongke_hours(common.extract_arg(context))
    text = await usage_stats.build_thongke_text(hours, use_html=True)
    await update.message.reply_text(text, parse_mode="HTML")


# /agent - AI agent thật (xem ai/agent_service.py): model tự quyết định gọi
# tool nào (tim_gia, xem_thong_ke...) bao nhiêu lần để trả lời, khác mọi lệnh
# khác trong file này (pipeline cố định). Thử nghiệm - chỉ bật ở Telegram,
# chỉ admin, vì mỗi câu hỏi có thể tốn tới MAX_AGENT_STEPS lượt gọi api1/api2
# (quota thấp nhất trong provider-chain).
@common.restricted
async def agent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = common.extract_arg(context)
    if not question:
        await update.message.reply_text(
            "Dùng: /agent <câu hỏi>\nVí dụ: /agent so sánh giá iPhone 15 và iPhone 15 Pro"
        )
        return

    await update.message.reply_text("🤖 Đang suy nghĩ (agent có thể tự tra cứu vài bước)...")
    user_id = update.effective_user.id
    prompt_id = await telemetry.start(user_id, "agent", question)
    try:
        from ai import agent_service

        text, provider = await agent_service.ask_agent(question)
        await telemetry.success(prompt_id, "agent", text)
    except Exception as exc:
        logger.exception("Agent lỗi với câu hỏi: %r", question)
        await telemetry.failure(prompt_id, "agent", exc)
        await update.message.reply_text(
            "Agent gặp lỗi, thử lại sau hoặc dùng lệnh thường (/gia, /thongke...) nhé."
        )
        return

    # reply_long_text nhận markdown-lite (xem tg_format.py), KHÔNG phải HTML -
    # dùng cú pháp _italic_ chứ không phải thẻ <i>.
    await common.reply_long_text(update.message, f"{text}\n\n_⚙️ {provider}_")


@common.restricted
async def rag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rag <câu hỏi> - tra cứu kho kiến thức .md trong thư mục rag/."""
    query = common.extract_arg(context)
    if not query:
        await update.message.reply_text(
            "Dùng: /rag <câu hỏi>\n"
            "Ví dụ: /rag chiến lược vào tiền khi thị trường sideway\n"
            "Em tìm trong các file .md anh để ở thư mục rag/."
        )
        return

    user_id = update.effective_user.id
    prompt_id = await telemetry.start(user_id, "rag", query)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        text = await rag_service.ask(user_id, query)
        await telemetry.success(prompt_id, "rag", text)
    except Exception as exc:
        logger.exception("Lỗi /rag với câu hỏi: %r", query)
        await telemetry.failure(prompt_id, "rag", exc)
        await update.message.reply_text("❌ Có lỗi khi tra cứu kiến thức. Thử lại sau nhé.")
        return

    await common.reply_long_text(update.message, text)


@common.restricted
async def ragxuly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ragxuly <tên-file> - dọn file .md OCR trong rag/ thành kho kiến thức sạch."""
    name = common.extract_arg(context)
    if not name:
        await update.message.reply_text(
            "Dùng: /ragxuly <tên-file-trong-rag>\n"
            "Ví dụ: /ragxuly ghichu.md\n"
            "Em dọn file md OCR (gộp dòng ngắt, bỏ số trang, thêm heading, sửa lỗi OCR), "
            "backup bản gốc vào rag/_goc/ rồi ghi đè file chính."
        )
        return

    user_id = update.effective_user.id
    prompt_id = await telemetry.start(user_id, "ragxuly", name)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        # /ragxuly phải chạy orchestrator trên event loop chính vì provider_state
        # và asyncpg pool được khởi tạo ở loop này. Các lượt AI đều await nên
        # không chặn những request khác trong lúc chờ network.
        text = await rag_clean_service.clean_file(name)
        await telemetry.success(prompt_id, "ragxuly", text)
    except Exception as exc:
        logger.exception("Lỗi /ragxuly với file: %r", name)
        await telemetry.failure(prompt_id, "ragxuly", exc)
        await update.message.reply_text("❌ Có lỗi khi dọn file. Thử lại sau nhé.")
        return

    await update.message.reply_text(text)


@common.restricted
async def bangtinsang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gửi ngay bản tin sáng theo đúng luồng scheduler tới Zalo + Zoom."""
    from services import morning_news

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        result = await morning_news.run_once(force=True)
    except Exception:
        logger.exception("Lỗi /bangtinsang")
        await update.message.reply_text("❌ Có lỗi khi tạo/gửi Bản tin sáng. Thử lại sau nhé.")
        return

    if result.content is None:
        await update.message.reply_text(
            "Không có nội dung để gửi (các nguồn RSS đều lỗi hoặc AI tổng hợp trả lời bất thường)."
        )
        return

    sent = []
    if result.sent_zalo:
        sent.append("Zalo")
    if result.sent_zoom:
        sent.append("Zoom")
    if not sent:
        await update.message.reply_text(
            "Đã tổng hợp được Bản tin sáng nhưng chưa gửi được tới Zalo/Zoom (chưa pair hoặc gửi lỗi)."
        )
        return

    await update.message.reply_text(f"✅ Đã gửi Bản tin sáng tới {' và '.join(sent)}.")


@common.restricted
async def tintuc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tạo Điểm tin theo yêu cầu và chỉ trả về đúng cuộc chat Telegram hiện tại."""
    from services import morning_news

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        content = await morning_news.build_digest(mode="on_demand")
    except Exception:
        logger.exception("Lỗi /tintuc")
        await update.message.reply_text("❌ Có lỗi khi tổng hợp Điểm tin. Thử lại sau nhé.")
        return

    if not content:
        await update.message.reply_text(
            "Không tổng hợp được Điểm tin lúc này (các nguồn RSS đều lỗi hoặc AI trả lời bất thường)."
        )
        return

    await common.reply_long_text(update.message, content)


@common.restricted
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = await db.get_history(user_id, limit=HISTORY_LIMIT)
    if not rows:
        return await update.message.reply_text("Chưa có lịch sử nào.")
    icon_map = {
        "image": "🖼️",
        "chat": "💬",
        "promptify": "🔍",
        "stock_analysis": "📊",
        "stock_price": "💹",
        "prompt_generator": "✏️",
        "price_search": "🛒",
    }
    lines = [f"🕙 <b>{HISTORY_LIMIT} lượt gần nhất:</b>\n"]
    for command_type, prompt, created_at, _result_types in rows:
        short_prompt = (
            prompt[:HISTORY_PROMPT_PREVIEW_MAX] + "…"
            if len(prompt) > HISTORY_PROMPT_PREVIEW_MAX
            else prompt
        )
        icon = icon_map.get(command_type, "•")
        date_part = created_at[:16].replace("T", " ")
        lines.append(
            f"{icon} [{html.escape(command_type)}] {html.escape(short_prompt)} ({date_part})"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    record_error(context.error)
    logger.error(
        "Lỗi không được xử lý (lần thử %s)",
        webhook_inbox.current_attempt() or 1,
        exc_info=context.error,
    )
    # Webhook inbox tự thử lại sự kiện lỗi nhiều lần: chỉ báo lỗi cho người dùng ở
    # lần đầu, các lần sau im lặng (hết lượt thử thì chủ bot nhận cảnh báo DEAD).
    if not webhook_inbox.is_first_attempt():
        return
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Đã có lỗi không mong muốn xảy ra. Vui lòng thử lại."
            )
        except Exception:
            logger.exception("Không gửi được thông báo lỗi cho user")
