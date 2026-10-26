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
from ai.timeouts import TurnTimeoutError
from core import database as db, webhook_inbox
from handlers import common
from services import (
    conversation_log,
    divination_commands,
    memory_commands,
    memory_control,
    memory_service,
    price_search,
    prompt_templates,
    rag_clean_service,
    rag_service,
    reply_cache,
    usage_stats,
)
from services.telemetry import telemetry
from services.telegram_processing import record_error

logger = logging.getLogger(__name__)

HISTORY_PROMPT_PREVIEW_MAX = 60
HISTORY_LIMIT = 10

HELP_TEXT = (
    "📖 *Các lệnh hỗ trợ:*\n\n"
    "💬 Gõ tin nhắn bình thường để trò chuyện với em - Lan Anh - như trợ lý cá nhân. "
    'Em tự nhớ ghi chú, nhắc việc, danh mục khi anh nói ("nhắc anh 8h mai họp", "đã mua FPT 100 giá 120").\n\n'
    '📊 *Cổ phiếu Việt Nam*: nhắc tới 1 mã là em báo giá realtime; nói rõ "phân tích FPT" để có báo cáo đầy đủ '
    '(action do hệ thống quyết, AI chỉ diễn giải). "Soi danh mục" để xem cả rổ. Thêm "theo Buffett/Minervini/'
    'Đoàn Vĩnh Bình" (hoặc "3 huyền thoại") để nghe góc nhìn định tính.\n\n'
    "🖼️ *Gửi 1 ảnh chân dung* để em viết prompt (dán sang app Gemini) giữ nguyên khuôn mặt.\n\n"
    "*Lịch, thần số học, tử vi, kỳ môn*\n"
    "/lich <câu hỏi> — tra lịch tự do, không tốn AI: /lich, /lich 20/11, /lich rằm tháng 8, "
    "/lich tháng này, /lich trung thu, /lich ngày tốt tháng này, /lich tuổi 15/3/1990, "
    "/lich sao hạn 1990 nam năm 2027 (/lich help xem thêm ví dụ)\n"
    "/tsh <ngày sinh> [họ tên] — thần số học: con số chủ đạo, biểu đồ ngày sinh (không tốn AI)\n"
    "/tsh\\_chitiet <như trên> — AI diễn giải chi tiết\n"
    "/tuvi <ngày sinh> <giờ> <nam|nữ> — an lá số tử vi (không tốn AI), vd /tuvi 15/3/1990 9h30 nam\n"
    "/tuvi\\_chitiet <như trên> — AI luận giải lá số\n"
    "/kymon [ngày] [giờ] — lập bàn Kỳ Môn Độn Giáp (không tốn AI), mặc định lúc này\n"
    "/kymon\\_chitiet [thời điểm] <việc cần xem> — AI luận giải bàn Kỳ Môn cho việc đó\n\n"
    "*Đời sống (số liệu thật, không tốn AI)*\n"
    "/vang — giá vàng SJC; /tygia [mã|100 usd] — tỷ giá Vietcombank, đổi tiền\n"
    "/xoso [mb|mn|mt|tỉnh] [hôm qua|dd/mm] [số vé] — kết quả xổ số, dò vé\n"
    "/thoitiet [địa điểm] — thời tiết; /thoitiet nha <tỉnh> — đặt thành phố của anh\n"
    "/bantin on|off|<giờ>|xem — bản tin sáng cá nhân (thời tiết, âm lịch, nhắc việc, vàng, "
    "danh mục; bật thì thay digest danh mục 8h)\n\n"
    "*Chat và tiện ích*\n"
    "/prompt <mô tả> — viết prompt tạo ảnh từ mô tả cơ bản\n"
    "/anh <mô tả> — tạo ảnh thật (Agnes AI); /anh on|off — bật/tắt\n"
    "/gia <sản phẩm> — tìm và so sánh giá sản phẩm\n"
    "/dich [ja>vi|vi>ja] <nội dung> — dịch chat công việc Nhật-Việt\n"
    "/reset — xoá ngữ cảnh chat\n"
    "/history — xem 10 lượt gần nhất\n"
    "/memory [on|off] — xem trí nhớ dài hạn (đánh số), bật hoặc tắt\n"
    "/quen <số|từ khoá> — quên 1 mục; /nho <điều cần nhớ> — ghim 1 điều "
    '("nhớ giúp anh là..." cũng được); /ghim, /boghim <số>\n'
    "/forget — xoá hết trí nhớ dài hạn\n"
    "/notes [từ khoá] — xem/tìm ghi chú; /xoaghichu <số> — xoá 1 ghi chú\n"
    "/xuatdulieu — tải file dữ liệu em đang lưu; /xoadulieu — xoá trí nhớ, ghi chú, lịch sử chat\n"
    "/agent <câu hỏi> — agent tự tra cứu nhiều bước để trả lời (thử nghiệm)\n"
    "/rag <câu hỏi> — tra cứu kiến thức trong thư mục rag/\n"
    "/ragxuly <file> — dọn file md OCR trong rag/ ở nền (AI thêm heading, backup bản gốc)\n\n"
    "*Danh mục cổ phiếu*\n"
    "/danhmuc — xem danh mục, lãi/lỗ, cảnh báo stop/target\n"
    "/themcp <MÃ> <SỐ\\_LƯỢNG> <GIÁ\\_VỐN> [STOP] [TARGET] — thêm giao dịch mua (giá dưới 1.000 hiểu là nghìn đồng)\n"
    "/capnhatcp <MÃ> <SỐ\\_LƯỢNG> <GIÁ\\_VỐN> [STOP] [TARGET] — ghi đè vị thế\n"
    "/bancp <MÃ> [SỐ\\_LƯỢNG|hết] [GIÁ\\_BÁN] — bán bớt hoặc bán hết (có giá thì tính lãi/lỗ đã chốt)\n"
    "/xoacp <MÃ> — xoá mã khỏi danh mục\n"
    "/lichsucp [MÃ] — sổ giao dịch; /laicp [THÁNG] — lãi/lỗ đã chốt sau phí, thuế\n"
    "/giaban <MÃ> <GIÁ> — bổ sung giá cho lệnh bán chưa có giá\n"
    "/theodoi [MÃ...] — danh sách theo dõi; /botheodoi <MÃ> — bỏ theo dõi\n"
    '/canhbaogia <MÃ> [về|lên] <GIÁ> — báo khi giá chạm ngưỡng anh đặt (hoặc nhắn "báo anh khi HPG về 25"); /xoacanhbao <MÃ> [GIÁ]\n'
    "Trong giờ giao dịch em báo ngay khi giá chạm ngưỡng/stop/target; cuối phiên (sau 15h) báo breakout, lỗ sâu; 8h sáng ngày giao dịch gửi digest danh mục.\n\n"
    "*Bản tin và thống kê*\n"
    "/bangtinsang (hoặc /bantinsang) — gửi ngay Bản tin sáng tới Zalo + Zoom (bình thường tự gửi lúc 8h)\n"
    "/tintuc — đọc Điểm tin mới ngay trong Telegram, không broadcast sang Zalo/Zoom\n"
    "/thongke [Nd|Ngiờ] — thống kê lượt gọi theo user/model, mặc định 7 ngày\n\n"
    "*Provider AI*\n"
    "/model [tên|auto] — xem/đổi model chat (chỉ áp dụng cho 9Router)\n"
    "/status — xem trạng thái provider chain\n"
    "/loi — xem lỗi gom theo loại hôm nay/hôm qua và số lần bot khởi động lại\n"
    "/userouter9 — ép thử lại 9Router ngay\n"
    "/router9 on|off — bật/tắt 9Router thủ công\n"
    "/tavily on|off — bật/tắt tra web Tavily (tắt thì dùng web search của provider)\n\n"
    "*Zalo bot B*\n"
    "/zalo — đăng nhập bằng QR hoặc xem trạng thái Zalo B; /zalologout — đăng xuất\n"
    "/zalopair <id> [tên] [chị|cô|chú|bạn|em] — cấp quyền thành viên, từ cuối (viết thường) "
    "là cách Lan Anh gọi người đó; /zaloadmin <id> [tên] [xưng hô] — cấp quyền admin\n"
    "/zalohaquyen <id> — hạ admin về thành viên; /zalokhoa, /zalomokhoa <id> — khoá/mở khoá\n"
    "/zaloxoa <id> — xoá pairing kèm toàn bộ dữ liệu cá nhân; /zalodanhsach — xem danh sách\n"
    "/nhom, /themnhom <group\\_id> <tên>, /xoanhom <group\\_id|tên> — nhóm Zalo đang theo dõi\n"
    "/tongket <tên> [24h|7d|homnay|homqua] — tổng kết nhóm bằng AI; /dangnoi <tên> — xem nguyên văn hôm nay\n"
    "/tongket on|off [tên] — bật/tắt tổng kết tự động hằng ngày (bỏ trống tên = mọi nhóm)\n\n"
    "*Zoom*\n"
    "/zoompair <jid> [tên], /zoomxoa, /zoomstatus — quản lý pairing Zoom\n\n"
    "📣 *Zalo → Facebook Page (tách riêng khỏi /tongket):*\n"
    "/fb\\_nhom — xem các nhóm nguồn Facebook\n"
    "/fb\\_themnhom <group\\_id> <tên> — thêm nhóm Zalo làm nguồn đăng Facebook\n"
    "/fb\\_xoanhom <group\\_id|tên> — bỏ nhóm khỏi luồng Facebook\n"
    "/fb\\_pages — xem các Facebook Page đã cấu hình (fb\\_ok sẽ đăng lên tất cả)\n"
    "/fb\\_market <stock|news> [dang] — xem thử bài Page chứng khoán/tin CafeF; thêm 'dang' để đăng thật\n"
    "/fb\\_xem <post\\_id> — xem lại bài đang chờ duyệt\n"
    "/fb\\_sua <post\\_id> <nội dung> — sửa nội dung bài chờ\n"
    "/fb\\_link <post\\_id> <affiliate\\_url> — nhập short-link affiliate khi bài chỉ có 1 link Shopee\n"
    "/fb\\_link <post\\_id> <source\\_url> <affiliate\\_url> — nhập từng link khi bài có nhiều link\n"
    "(đủ link thì AI tự viết lại bài, link affiliate được thả ở bình luận đầu tiên)\n"
    "/fb\\_ok <post\\_id> — duyệt và đăng lên TẤT CẢ Facebook Page đang cấu hình; "
    "page nào lỗi sẽ báo riêng, chạy lại /fb\\_ok để chỉ thử lại page lỗi\n"
    "/fb\\_check <post\\_id> — kiểm tra từng page đã published/công khai chưa, lấy permalink\n"
    "/fb\\_reconcile — đối soát lượt đăng bị gián đoạn (bài kẹt POSTING/UNKNOWN)\n"
    "/fb\\_boqua <post\\_id> — bỏ bài chờ\n"
    "/fb\\_reset xacnhan — xóa bài Facebook đã lưu, reset ID về #1 khi có thể\n"
    "/fb\\_boloc [reset] — thống kê bộ lọc Zalo→Facebook (bài giữ/bỏ/trùng, số bài chờ)\n"
    "/fb\\_loclai — chạy lại bộ lọc cho các bài đang chờ, bỏ bài không đạt\n\n"
    "/start — lời chào; /help — hiển thị hướng dẫn này\n"
    "Trang quản trị: /admin trên URL dịch vụ (thống kê, provider, RAM, DB)."
)


@common.restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Chào anh, em Lan Anh nè - trợ lý cá nhân của anh đây! 💕\n"
        "Anh cứ nhắn tự nhiên như nhắn tin, vd:\n"
        "• mai Hà Nội mưa không\n"
        "• nhắc anh 8h tối gọi mẹ\n"
        "• giá vàng hôm nay\n"
        "• phân tích FPT\n"
        "Gửi 1 ảnh là em viết prompt tạo ảnh từ ảnh đó. "
        "Gõ /help xem đầy đủ lệnh nha anh."
    )


def help_chunks(text: str = HELP_TEXT, limit: int = 3500) -> list[str]:
    """Cắt HELP_TEXT thành nhiều tin <= limit ký tự, chỉ cắt ở ranh giới mục
    ("\n\n") để mỗi tin vẫn cân dấu Markdown. Telegram giới hạn 4096 ký tự/tin;
    HELP_TEXT đã vượt mức này (gửi 1 tin sẽ lỗi "Message is too long")."""
    chunks: list[str] = []
    for section in text.split("\n\n"):
        if chunks and len(chunks[-1]) + 2 + len(section) <= limit:
            chunks[-1] += "\n\n" + section
        else:
            chunks.append(section)
    return chunks


@common.restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for chunk in help_chunks():
        await update.message.reply_text(chunk, parse_mode="Markdown")


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
            await telemetry.success(prompt_id, "prompt_generator", "(AI không trả về nội dung)")
            await update.message.reply_text(messages.friendly_error("prompt_empty", owner=True))
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
        await update.message.reply_text(
            messages.friendly_error("prompt", owner=True, detail=messages.exception_detail(e))
        )


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
            await telemetry.success(prompt_id, "price_search", "(AI không trả về nội dung)")
            await status.edit_text("Em không tìm được giá lúc này, anh thử lại sau nha.")
            return

        await telemetry.success(prompt_id, "price_search", result_text)
        result_text = await price_search.verify_links(result_text)
        price_search.set_cached_price(product_name, result_text)
        suffix = "\n\n⚙️ API" if used_fallback else ""

        await common.reply_long_text_edit_first(status, result_text + suffix)
    except TurnTimeoutError as e:
        logger.warning("Tìm giá %r hết hạn chót lượt: %s", product_name, e)
        await telemetry.failure(prompt_id, "price_search", e)
        await status.edit_text(messages.friendly_error("timeout", owner=True, detail=str(e)))
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
        # Cache theo sự kiện webhook: lỗi ở bước gửi -> inbox thử lại chỉ gửi
        # lại bản đã dịch, không gọi LLM lần nữa (services/reply_cache.py).
        result, resolved, response = await reply_cache.generate_once(
            "dich",
            lambda: conversation_log.run_and_record(
                user_id,
                text,
                conversation_log.KIND_TRANSLATE,
                lambda: translate_service.translate(text, direction),
            ),
        )
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
async def reset_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    # Trích nốt trí nhớ từ các lượt chưa xử lý trước khi xoá lịch sử phiên.
    await memory_service.flush_before_reset(user_id)
    await orchestrator.reset_chat()
    await db.clear_chat(user_id)
    await update.message.reply_text("🔄 Đã xoá ngữ cảnh hội thoại. Bắt đầu chat mới nhé!")


@common.restricted
async def memory_family_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/memory, /forget, /quen, /nho, /ghim, /boghim, /notes, /xoaghichu,
    /xuatdulieu, /xoadulieu - logic ở services/memory_commands.py."""
    text = (update.message.text or "").strip()
    command = text.split(maxsplit=1)[0].lower().split("@", 1)[0] if text else ""
    reply = await memory_commands.handle(
        command,
        common.extract_arg(context),
        update.effective_user.id,
        channel="telegram",
        about="anh",  # Telegram chỉ phục vụ chủ bot
    )
    if reply is None:
        return
    for message in reply.messages:
        await common.reply_long_text(update.message, message)
    if reply.file is not None:
        raw, name = reply.file
        await update.message.reply_document(
            document=memory_control.export_buffer(raw), filename=name
        )


# Tên cũ bot_app/test đang dùng.
memory_cmd = forget_cmd = notes_cmd = memory_family_cmd


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

        text, provider = await reply_cache.generate_once(
            "agent",
            lambda: conversation_log.run_and_record(
                user_id,
                f"/agent {question}",
                conversation_log.KIND_AGENT,
                lambda: agent_service.ask_agent(question),
            ),
        )
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
async def lich_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lich <câu hỏi> - tra lịch bằng code, không gọi AI."""
    await common.reply_long_text(
        update.message, divination_commands.lich(common.extract_arg(context))
    )


@common.restricted
async def tsh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tsh <ngày sinh> [họ tên] - thần số học, ý nghĩa soạn sẵn (không AI)."""
    await common.reply_long_text(
        update.message, divination_commands.tsh(common.extract_arg(context))
    )


@common.restricted
async def tuvi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tuvi <ngày sinh> <giờ> <nam|nữ> - an lá số tử vi (không AI)."""
    await common.reply_long_text(
        update.message, divination_commands.tuvi_chart(common.extract_arg(context))
    )


@common.restricted
async def kymon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kymon [ngày] [giờ] - lập bàn Kỳ Môn Độn Giáp (không AI)."""
    await common.reply_long_text(
        update.message, divination_commands.kymon_chart(common.extract_arg(context))
    )


_CHITIET = {
    "tsh": divination_commands.tsh_chitiet,
    "tuvi": divination_commands.tuvi_chitiet,
    "kymon": divination_commands.kymon_chitiet,
}


async def _reply_chitiet(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    argument = common.extract_arg(context)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    for part in await _CHITIET[kind](update.effective_user.id, argument, "telegram"):
        await common.reply_long_text(update.message, part)


@common.restricted
async def tsh_chitiet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tsh_chitiet - thần số học kèm AI diễn giải (1 lượt AI)."""
    await _reply_chitiet(update, context, "tsh")


@common.restricted
async def tuvi_chitiet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tuvi_chitiet - lá số tử vi kèm AI luận giải (1 lượt AI)."""
    await _reply_chitiet(update, context, "tuvi")


@common.restricted
async def kymon_chitiet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kymon_chitiet [thời điểm] <việc cần xem> - bàn Kỳ Môn kèm AI luận giải (1 lượt AI)."""
    await _reply_chitiet(update, context, "kymon")


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
        # Lệnh gửi để NGOÀI try có chủ đích: Telegram lỗi tạm lúc gửi thì inbox
        # thử lại sự kiện và nhận lại đúng câu trả lời đã sinh từ cache (không
        # gọi LLM lại, không mất câu trả lời như khi nuốt lỗi gửi trong try).
        text = await reply_cache.generate_once("rag", lambda: rag_service.ask(user_id, query))
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
    chat_id = update.effective_chat.id
    bot = context.bot
    prompt_id = await telemetry.start(user_id, "ragxuly", name)

    async def _run() -> None:
        # Chạy orchestrator trên event loop chính (provider_state + asyncpg pool
        # thuộc loop này), nhưng NGOÀI assistant_turn(OWNER): file dài = hàng
        # chục lượt AI, giữ khoá lượt sẽ chặn chat của chính chủ bot hàng chục
        # phút. Kết quả gửi về sau khi xong.
        try:
            text = await rag_clean_service.clean_file(name)
            await telemetry.success(prompt_id, "ragxuly", text)
        except Exception as exc:
            logger.exception("Lỗi /ragxuly với file: %r", name)
            await telemetry.failure(prompt_id, "ragxuly", exc)
            text = "❌ Có lỗi khi dọn file. Thử lại sau nhé."
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.warning("Không gửi được kết quả /ragxuly.", exc_info=True)

    if not rag_clean_service.start_background(name, _run()):
        await update.message.reply_text(
            "⏳ Đang có một lượt /ragxuly khác chạy. Chờ xong rồi gọi lại nhé."
        )
        return
    await update.message.reply_text(
        f"⏳ Đang dọn {name} ở nền (trần {rag_clean_service.total_timeout_sec() // 60} phút). "
        "Anh cứ chat tiếp, xong em báo."
    )


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
            # Bot chỉ phục vụ chủ bot (@common.restricted): giọng Lan Anh + loại lỗi.
            detail = messages.exception_detail(context.error) if context.error else ""
            await update.effective_message.reply_text(
                messages.friendly_error("unexpected", owner=True, detail=detail)
            )
        except Exception:
            logger.exception("Không gửi được thông báo lỗi cho user")
