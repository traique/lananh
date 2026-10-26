"""Text command implementation for non-Telegram channels."""

import base64
import contextvars

from ai import agnes_client, orchestrator, router9_client, tavily_client
from core import config, database as db
from core.text_utils import parse_on_off
from services import (
    divination_commands,
    memory_service,
    price_search,
    prompt_templates,
    translate_service,
    usage_stats,
)
from services import daily_commands
from services.telemetry import telemetry

HELP = """📖 Lệnh trên Zalo/Zoom
💬 Nhắn bình thường để trò chuyện. Nhắc tới 1 mã cổ phiếu là có giá realtime; nói "phân tích FPT" để xem báo cáo
(thành viên nhận bản tham khảo, admin nhận bản đầy đủ). Nói "đã mua FPT 100 giá 120" để ghi danh mục.

Lịch, thần số học, tử vi
/lich <câu hỏi> — tra lịch tự do, không tốn AI: /lich, /lich 20/11, /lich rằm tháng 8,
  /lich tháng này, /lich trung thu, /lich ngày tốt tháng này, /lich tuổi 15/3/1990,
  /lich sao hạn 1990 nam năm 2027 (gõ /lich help để xem thêm ví dụ)
/tsh <ngày sinh> [họ tên] — thần số học: con số chủ đạo, biểu đồ ngày sinh (không tốn AI)
/tsh_chitiet <như trên> — AI diễn giải chi tiết
/tuvi <ngày sinh> <giờ> <nam|nữ> — an lá số tử vi (không tốn AI), vd /tuvi 15/3/1990 9h30 nam
/tuvi_chitiet <như trên> — AI luận giải lá số

Đời sống (số liệu thật, không tốn AI)
/vang — giá vàng SJC; /tygia [mã|100 usd] — tỷ giá Vietcombank, đổi tiền
/xoso [mb|mn|mt|tỉnh] [hôm qua|dd/mm] [số vé] — kết quả xổ số, dò vé
/thoitiet [địa điểm] — thời tiết; /thoitiet nha <tỉnh> — đặt thành phố của riêng anh
/bantin on|off|<giờ>|xem — bản tin sáng cá nhân (thời tiết, âm lịch, nhắc việc, vàng, danh mục, tin)

Chat và tiện ích
/prompt <mô tả> — viết prompt tạo ảnh
/anh <mô tả> — tạo ảnh thật (Agnes AI)
/gia <sản phẩm> — tìm và so sánh giá
/dich [ja>vi|vi>ja] <nội dung> — dịch chat công việc Nhật-Việt
/reset — xoá ngữ cảnh chat
/history — xem 10 lượt gần nhất
/memory [on|off] — xem, bật hoặc tắt trí nhớ dài hạn
/forget — xoá trí nhớ dài hạn
/notes — xem ghi chú

Danh mục cổ phiếu (riêng từng người)
/danhmuc — xem danh mục, lãi/lỗ, cảnh báo stop/target
/themcp <MÃ> <SỐ_LƯỢNG> <GIÁ_VỐN> [STOP] [TARGET] — thêm giao dịch mua (giá dưới 1.000 hiểu là nghìn đồng)
/capnhatcp <MÃ> <SỐ_LƯỢNG> <GIÁ_VỐN> [STOP] [TARGET] — ghi đè vị thế
/bancp <MÃ> [SỐ_LƯỢNG|hết] — bán bớt hoặc bán hết
/xoacp <MÃ> — xoá mã khỏi danh mục

Chỉ admin
/model [tên|auto] — xem hoặc đổi model
/status — xem trạng thái provider
/thongke [Nd|Ngiờ] — thống kê lượt gọi theo user/model, mặc định 7 ngày
/userouter9 — thử lại 9Router
/router9 on|off — bật/tắt 9Router thủ công
/tavily on|off — bật/tắt tra web Tavily trước khi trả lời
/anh on|off — bật/tắt tạo ảnh Agnes AI
/agent <câu hỏi> — agent tự tra cứu nhiều bước để trả lời (thử nghiệm)
/rag <câu hỏi> — tra cứu kiến thức trong thư mục rag/
/ragxuly <file> — dọn file md OCR trong rag/ ở nền, kết quả báo về Telegram chủ bot
/bangtinsang (hoặc /bantinsang) — gửi ngay Bản tin sáng tới cả Zalo + Zoom (bình thường tự gửi lúc 8h)
/tintuc — đọc Điểm tin mới ngay tại đúng kênh đang gọi lệnh
/nhom, /themnhom <group_id> <tên>, /xoanhom <group_id|tên> — nhóm Zalo đang theo dõi
/tongket <tên> [24h|7d|homnay|homqua] — tổng kết nhóm bằng AI; /dangnoi <tên> — xem nguyên văn hôm nay
/tongket on|off [tên] — bật/tắt tổng kết tự động hằng ngày (bỏ trống tên = mọi nhóm)
  (dữ liệu nhóm Zalo, dùng được từ cả Zalo lẫn Zoom)

📣 Zalo → Facebook Page (tách riêng khỏi /tongket, chỉ admin):
/fb_nhom — xem nhóm nguồn Facebook
/fb_themnhom <group_id> <tên> — thêm nhóm Zalo làm nguồn đăng Facebook
/fb_xoanhom <group_id|tên> — bỏ nhóm khỏi luồng Facebook
/fb_pages — xem các Facebook Page đã cấu hình (fb_ok sẽ đăng lên tất cả)
/fb_market <stock|news> [dang] — xem thử bài Page chứng khoán/tin CafeF; thêm 'dang' để đăng thật
/fb_xem <post_id> — xem bài đang chờ duyệt
/fb_sua <post_id> <nội dung> — sửa nội dung bài chờ
/fb_link <post_id> <affiliate_url> — nhập short-link affiliate khi bài chỉ có 1 link Shopee
/fb_link <post_id> <source_url> <affiliate_url> — nhập từng link khi bài có nhiều link
(đủ link thì AI tự viết lại bài, link affiliate được thả ở bình luận đầu tiên)
/fb_ok <post_id> — duyệt và đăng lên TẤT CẢ Facebook Page đang cấu hình; page nào lỗi sẽ báo riêng,
  chạy lại /fb_ok để chỉ thử lại page lỗi, page đã đăng OK không bị đụng tới
/fb_check <post_id> — kiểm tra từng page đã published/công khai chưa, lấy permalink
/fb_reconcile — đối soát lượt đăng bị gián đoạn (bài kẹt POSTING/UNKNOWN)
/fb_boqua <post_id> — bỏ bài chờ
/fb_reset — xóa toàn bộ bài Facebook đã lưu của tài khoản, reset ID về #1 khi có thể
/fb_loclai — chạy lại bộ lọc cho các bài đang chờ, bỏ bài không đạt
/fb_boloc [reset] — thống kê bộ lọc: bao nhiêu bài vào hàng chờ, bị bỏ/trùng vì sao, số bài đang chờ
(hàng chờ giữ tối đa 150 bài, bài trùng trong 7 ngày không được đưa vào)

/help — hiển thị hướng dẫn này"""

# Ảnh do lệnh /anh tạo ra được "gửi kèm" bằng ContextVar thay vì đổi kiểu trả
# về của maybe_handle_command() (đang là tuple[list[str], str|None] và có
# ~30 điểm return rải khắp file) - ContextVar cô lập đúng theo từng asyncio
# Task, nên nhiều request Zalo/Zoom chạy đồng thời KHÔNG ghi đè ảnh của nhau,
# khác với 1 biến module thường. Nơi gọi maybe_handle_command() (xem
# services/channel_chat_service.py) phải gọi take_pending_image() ngay sau
# để lấy (và xoá) ảnh, nếu có.
_pending_image: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_pending_image_b64", default=None
)
# Riêng cho Zoom: Chatbot API của Zoom nhận thẳng URL ảnh công khai (Zoom tự
# tải về), KHÔNG cần base64/Buffer như Zalo - xem channels/zoom.py::send_image_message.
_pending_image_url: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_pending_image_url", default=None
)


def take_pending_image() -> str | None:
    image = _pending_image.get()
    if image is not None:
        _pending_image.set(None)
    return image


def take_pending_image_url() -> str | None:
    url = _pending_image_url.get()
    if url is not None:
        _pending_image_url.set(None)
    return url


def _arg(text: str) -> str:
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""


async def _prompt(
    user_id: int, description: str, channel: str = "zalo"
) -> tuple[list[str], str | None]:
    if not description:
        return ["Cú pháp: /prompt <mô tả muốn tạo prompt>"], None
    instruction, identity = prompt_templates.build_text_instruction(description)
    hint = identity.mode_hint
    prompt_id = await telemetry.start(user_id, "prompt_generator", description, channel=channel)
    try:
        response = await orchestrator.ask(instruction)
        output = (response.text or "").strip()
        await telemetry.success(prompt_id, "prompt_generator", output or "(không có nội dung)")
        return (
            [f"{hint}\n\n{output}"] if output else ["Gemini không trả về prompt, hãy thử lại."]
        ), ("api" if getattr(response, "used_fallback", False) else None)
    except Exception as exc:
        await telemetry.failure(prompt_id, "prompt_generator", exc)
        return ["❌ Có lỗi khi tạo prompt. Hãy thử lại sau."], None


async def _price(user_id: int, product: str, channel: str = "zalo") -> tuple[list[str], str | None]:
    if not product:
        return ["Cú pháp: /gia <tên sản phẩm>, ví dụ /gia iPhone 16 Pro"], None
    cached = price_search.get_cached_price(product)
    if cached:
        return [cached], None
    prompt_id = await telemetry.start(user_id, "price_search", product, channel=channel)
    try:
        output, used_fallback = await price_search.search_price(product)
        output = await price_search.verify_links(output)
        if output:
            price_search.set_cached_price(product, output)
        await telemetry.success(prompt_id, "price_search", output or "(không có nội dung)")
        return [output or "Không tìm được giá lúc này."], ("api" if used_fallback else None)
    except Exception as exc:
        await telemetry.failure(prompt_id, "price_search", exc)
        return ["❌ Có lỗi khi tìm giá sản phẩm."], None


async def _translate(
    user_id: int, argument: str, channel: str = "zalo"
) -> tuple[list[str], str | None]:
    if not argument.strip():
        return [
            "Cú pháp: /dich [ja>vi|vi>ja] <nội dung>\n"
            "Không chỉ định chiều thì tự nhận diện theo chữ Nhật trong câu.\n"
            "Câu tiếng Anh (không dấu) em sẽ không tự đoán chiều - anh chỉ định giúp em.\n"
            "Ví dụ: /dich お世話になります。確認お願いします。"
        ], None

    first_token, _, rest = argument.partition(" ")
    direction = translate_service.parse_explicit_direction(first_token)
    text = rest if direction else argument
    if not text.strip():
        return ["Cú pháp: /dich [ja>vi|vi>ja] <nội dung>"], None

    # Text Latin thuần không dấu tiếng Việt (tiếng Anh...) - KHÔNG đoán mò
    # chiều "Việt→Nhật" như trước, hỏi lại để người dùng chủ động chọn.
    if direction is None and translate_service.looks_english(text):
        return [
            "Câu này là tiếng Anh nên em không tự đoán được anh cần dịch sang ngôn ngữ nào.\n"
            "Anh chỉ định giúp em nhé:\n"
            "• /dich ja>vi <nội dung> — dịch sang tiếng Việt\n"
            "• /dich vi>ja <nội dung> — dịch sang tiếng Nhật"
        ], None

    prompt_id = await telemetry.start(user_id, "translate", text, channel=channel)
    try:
        result, resolved, response = await translate_service.translate(text, direction)
    except translate_service.TextTooLongError as exc:
        return [str(exc)], None
    except ValueError as exc:
        return [f"Cú pháp: /dich [ja>vi|vi>ja] <nội dung>\n({exc})"], None
    except Exception as exc:
        await telemetry.failure(prompt_id, "translate", exc)
        return ["❌ Không dịch được lúc này, thử lại sau nhé."], None

    if not result:
        await telemetry.success(prompt_id, "translate", "(không có nội dung)")
        return ["Chưa dịch được câu này, thử lại nhé."], None

    await telemetry.success(prompt_id, "translate", result)
    label = translate_service.direction_label(resolved)
    provider = "api" if getattr(response, "used_fallback", False) else None
    return [f"🇯🇵↔🇻🇳 {label}\n\n{result}"], provider


async def _agent(
    user_id: int, question: str, channel: str = "zalo"
) -> tuple[list[str], str | None]:
    if not question:
        return [
            "Dùng: /agent <câu hỏi>\nVí dụ: /agent so sánh giá iPhone 15 và iPhone 15 Pro"
        ], None
    from ai import agent_service

    prompt_id = await telemetry.start(user_id, "agent", question, channel=channel)
    try:
        text, provider = await agent_service.ask_agent(question)
        await telemetry.success(prompt_id, "agent", text)
        return [f"{text}\n\n⚙️ {provider}"], None
    except Exception as exc:
        await telemetry.failure(prompt_id, "agent", exc)
        return ["Agent gặp lỗi, thử lại sau hoặc dùng lệnh thường (/gia, /thongke...) nhé."], None


async def _rag(user_id: int, question: str) -> tuple[list[str], str | None]:
    if not question:
        return [
            "Dùng: /rag <câu hỏi>\n"
            "Ví dụ: /rag chiến lược vào tiền khi thị trường sideway\n"
            "Em tìm trong các file .md anh để ở thư mục rag/."
        ], None
    from services import rag_service

    # Timeout của orchestrator lo phần chờ AI; ở đây không cần tự bọc thêm.
    text = await rag_service.ask(user_id, question)
    return [text], None


async def _ragxuly(user_id: int, name: str) -> tuple[list[str], str | None]:
    if not name:
        return [
            "Dùng: /ragxuly <tên-file-trong-rag>\n"
            "Ví dụ: /ragxuly ghichu.md\n"
            "Em dọn file md OCR (gộp dòng ngắt, bỏ số trang, thêm heading), "
            "backup bản gốc vào rag/_goc/ rồi ghi đè file chính."
        ], None
    from core import alerts
    from services import rag_clean_service

    async def _run() -> None:
        # Chạy nền ngoài khoá lượt (file dài = hàng chục lượt AI, Zalo gateway
        # chỉ chờ 240s). Kết quả gửi về Telegram chủ bot qua kênh cảnh báo.
        try:
            text = await rag_clean_service.clean_file(name)
        except Exception as exc:
            text = f"❌ /ragxuly {name} lỗi: {exc}"
        alerts.send(f"/ragxuly {name}:\n{text}")

    if not rag_clean_service.start_background(name, _run()):
        return ["⏳ Đang có một lượt /ragxuly khác chạy. Chờ xong rồi gọi lại nhé."], None
    return [
        f"⏳ Đang dọn {name} ở nền (trần {rag_clean_service.total_timeout_sec() // 60} phút). "
        "Xong em báo về Telegram chủ bot."
    ], None


async def _morning_news_now() -> list[str]:
    from services import morning_news

    result = await morning_news.run_once(force=True)
    if result.content is None:
        return [
            "Không có nội dung để gửi (tất cả nguồn RSS đều lỗi, hoặc model tổng hợp trả lời bất thường - xem log)."
        ]
    if not result.sent_zalo and not result.sent_zoom:
        return [
            "Đã tổng hợp được bản tin nhưng KHÔNG gửi được tới đâu cả (chưa pair Zalo lẫn Zoom, hoặc gửi bị lỗi - xem log)."
        ]
    kenh = []
    if result.sent_zalo:
        kenh.append("Zalo")
    if result.sent_zoom:
        kenh.append("Zoom")
    if len(kenh) < 2:
        thieu = "Zoom" if "Zoom" not in kenh else "Zalo"
        return [
            f"✅ Đã gửi Bản tin sáng qua {' và '.join(kenh)} (KHÔNG gửi được {thieu} - chưa pair hoặc lỗi, xem log)."
        ]
    return ["✅ Đã gửi Bản tin sáng tới Zalo và Zoom."]


async def _news_now() -> list[str]:
    """Tạo điểm tin nhưng KHÔNG broadcast.

    ``maybe_handle_command`` trả chuỗi này cho channel_chat_service, nên nội
    dung chỉ đi ngược về đúng Zalo/Zoom nơi người dùng vừa gõ ``/tintuc``.
    """
    from services import morning_news

    content = await morning_news.build_digest(mode="on_demand")
    if not content:
        return [
            "Không tổng hợp được điểm tin lúc này (các nguồn RSS đều lỗi hoặc AI trả lời bất thường)."
        ]
    return [content]


async def _generate_image(
    argument: str, is_admin: bool, channel: str
) -> tuple[list[str], str | None]:
    enabled = parse_on_off(argument)
    if enabled is not None:
        if not is_admin:
            return ["Lệnh này chỉ dành cho admin."], None
        await agnes_client.set_enabled(enabled)
        return (
            ["✅ Đã bật tạo ảnh (Agnes AI)."]
            if enabled
            else ["🔴 Đã tắt tạo ảnh cho tới khi bật lại bằng /anh on."]
        ), None
    if not argument.strip():
        enabled = await agnes_client.get_enabled()
        return [
            f"🖼️ Tạo ảnh (Agnes AI) đang {'BẬT' if enabled else 'TẮT'}.\n"
            "Dùng /anh <mô tả> để tạo ảnh, hoặc /anh on|off để bật/tắt (admin)."
        ], None
    try:
        image = await agnes_client.generate_image(argument.strip())
    except agnes_client.AgnesError as exc:
        return [f"❌ Không tạo được ảnh: {exc}"], None

    if channel == "zoom":
        # Zoom gửi ảnh qua URL công khai (Zoom tự tải), KHÔNG qua base64 -
        # xem channels/zoom.py::send_image_message + web.py::_process_zoom_event.
        if not image.url:
            return ["❌ Tạo ảnh thành công nhưng thiếu URL để gửi qua Zoom - thử lại nhé."], None
        _pending_image_url.set(image.url)
        return [], None

    # Zalo (và mọi kênh khác dùng ChannelResult.image_b64 sau này): gửi qua
    # base64 để zalo-gateway tự dựng Buffer, xem take_pending_image() đầu file.
    _pending_image.set(base64.b64encode(image.data).decode("ascii"))
    return [], None


async def maybe_handle_command(
    user_id: int, text: str, is_admin: bool = True, channel: str = "zalo"
) -> tuple[list[str], str | None] | None:
    if not text.startswith("/"):
        return None
    command = text.split(maxsplit=1)[0].lower().split("@", 1)[0]
    argument = _arg(text)

    if command in {"/start", "/help"}:
        return [HELP], None
    # /lich, /tsh, /tuvi: tính bằng code (không AI); *_chitiet mới gọi AI.
    divination = await divination_commands.handle(command, argument, user_id, channel)
    if divination is not None:
        return divination, None
    # /vang, /tygia, /xoso, /thoitiet, /bantin: số liệu thật tính bằng code.
    daily = await daily_commands.handle(command, argument, user_id, channel)
    if daily is not None:
        return daily, None
    if command == "/prompt":
        return await _prompt(user_id, argument, channel)
    if command == "/gia":
        return await _price(user_id, argument, channel)
    if command == "/dich":
        return await _translate(user_id, argument, channel)
    if command == "/anh":
        return await _generate_image(argument, is_admin, channel)
    if command == "/reset":
        await orchestrator.reset_chat()
        await db.clear_chat(user_id)
        return ["🔄 Đã xoá ngữ cảnh hội thoại."], None
    if command == "/memory":
        toggle = parse_on_off(argument)
        if toggle is True:
            await memory_service.set_enabled(user_id, True)
            return ["✅ Đã bật trí nhớ dài hạn."], None
        if toggle is False:
            await memory_service.set_enabled(user_id, False)
            return ["🔴 Đã tắt trí nhớ dài hạn. Dùng /memory on để bật lại."], None
        facts, summary = await db.get_facts(user_id), await db.get_summary(user_id)
        enabled = await memory_service.is_enabled(user_id)
        status_line = f"\nTrạng thái: {'BẬT' if enabled else 'TẮT'} (/memory on|off để đổi)"
        if not facts and not summary:
            return [f"🧠 Chưa có trí nhớ dài hạn.{status_line}"], None
        lines = ["🧠 Trí nhớ dài hạn:"]
        if summary:
            lines.append(f"\nTóm tắt: {summary}")
        lines.extend(f"• {key}: {value}" for key, value in facts)
        lines.append(status_line)
        return ["\n".join(lines)], None
    if command == "/forget":
        await memory_service.clear_memory(user_id)
        return ["🗑️ Đã xoá toàn bộ trí nhớ dài hạn."], None
    if command == "/notes":
        notes = await db.get_notes(user_id, limit=10)
        return (
            ["📝 Chưa có ghi chú nào."]
            if not notes
            else [
                "📝 Ghi chú gần đây:\n"
                + "\n".join(
                    f"• {content} ({created.strftime('%H:%M %d/%m')})" for content, created in notes
                )
            ]
        ), None
    if command == "/history":
        rows = await db.get_history(user_id, limit=10)
        return (
            ["Chưa có lịch sử nào."]
            if not rows
            else [
                "🕙 10 lượt gần nhất:\n"
                + "\n".join(
                    f"• [{kind}] {prompt[:80]} ({created[:16].replace('T', ' ')})"
                    for kind, prompt, created, _ in rows
                )
            ]
        ), None

    # /status, /userouter9, /model đổi CẤU HÌNH TOÀN CỤC (provider đang dùng,
    # model chat) - ảnh hưởng MỌI người dùng trên MỌI kênh, không riêng người
    # gõ lệnh. Chỉ admin (Zalo: role=admin; Zoom/kênh khác chỉ có đúng 1 người
    # pair nên is_admin mặc định True) mới được đổi, để 1 thành viên thường
    # không thể vô tình/cố ý phá cấu hình chung của cả bot.
    # /rag: kiến thức cá nhân trong rag/ nên cũng khóa admin như /agent - thành
    # viên thường không được mổ xẻ kho kiến thức riêng của chủ bot.
    if (
        command
        in {
            "/status",
            "/userouter9",
            "/router9",
            "/tavily",
            "/model",
            "/thongke",
            "/agent",
            "/rag",
            "/ragxuly",
            "/bangtinsang",
            "/bantinsang",
            "/tintuc",
        }
        and not is_admin
    ):
        return ["Lệnh này chỉ dành cho admin."], None
    if command == "/agent":
        return await _agent(user_id, argument, channel)
    if command == "/rag":
        return await _rag(user_id, argument)
    if command == "/ragxuly":
        return await _ragxuly(user_id, argument)
    if command in {"/bangtinsang", "/bantinsang"}:
        return await _morning_news_now(), None
    if command == "/tintuc":
        return await _news_now(), None
    if command == "/status":
        state = orchestrator.get_provider_state_snapshot()
        return [
            f"📡 Provider: {state['active_provider']}\nThứ tự: {' → '.join(config.PROVIDER_ORDER)}\nModel API: {config.GOOGLE_AI_STUDIO_MODEL}"
        ], None
    if command == "/thongke":
        # Dùng chung logic/format với Telegram - xem services/usage_stats.py.
        hours = usage_stats.parse_thongke_hours(argument)
        return [await usage_stats.build_thongke_text(hours, use_html=False)], None
    if command == "/userouter9":
        ok, detail = await orchestrator.try_router9_now()
        return (
            ["✅ 9Router hoạt động, đã chuyển về 9Router."]
            if ok
            else [f"❌ 9Router vẫn lỗi: {detail[:250]}"]
        ), None
    if command == "/router9":
        toggle = parse_on_off(argument)
        if toggle is True:
            await orchestrator.set_router9_enabled(True)
            return ["✅ Đã bật 9Router."], None
        if toggle is False:
            await orchestrator.set_router9_enabled(False)
            return ["🔴 Đã tắt 9Router cho tới khi bật lại bằng /router9 on."], None
        enabled = orchestrator.get_provider_state_snapshot()["router9_enabled"]
        return [
            f"9Router đang {'BẬT' if enabled else 'TẮT'}. Dùng /router9 on hoặc /router9 off để đổi."
        ], None
    if command == "/tavily":
        toggle = parse_on_off(argument)
        if toggle is True:
            await tavily_client.set_enabled(True)
            return ["✅ Đã bật tra web Tavily trước khi trả lời."], None
        if toggle is False:
            await tavily_client.set_enabled(False)
            return ["🔴 Đã tắt Tavily, chat trở lại luồng bình thường."], None
        enabled = await tavily_client.get_enabled()
        return [
            f"Tavily đang {'BẬT' if enabled else 'TẮT'}. Dùng /tavily on hoặc /tavily off để đổi."
        ], None
    if command == "/model":
        if not argument:
            current = await router9_client.get_preferred_model_name()
            return [
                f"🧠 Model hiện tại: {current or 'tự động'}\nĐổi bằng /model <tên>; mặc định bằng /model auto\n"
                f"⚠️ Model này chỉ áp dụng khi dùng 9Router — nếu bot tự chuyển sang API dự "
                f"phòng, API luôn dùng model mặc định ({config.GOOGLE_AI_STUDIO_MODEL})."
            ], None
        if argument.lower() in {"auto", "default", "reset"}:
            await router9_client.set_preferred_model_name(None)
            await orchestrator.reset_chat()
            return ["🔄 Đã về model tự động."], None
        name = await router9_client.find_model(argument)
        if name is None:
            return [f"Không tìm thấy model khớp “{argument}”."], None
        await router9_client.set_preferred_model_name(name)
        await orchestrator.reset_chat()
        return [f"✅ Đã đổi model sang {name}."], None
    return ["Lệnh chưa được hỗ trợ. Gõ /help để xem danh sách."], None
