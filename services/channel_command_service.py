"""Text command implementation for non-Telegram channels."""
import base64
import contextvars

from ai import agnes_client, orchestrator, router9_client, tavily_client
from core import config, database as db
from handlers import commands as telegram_commands
from handlers.prompt_identity import render_instruction, resolve_prompt_identity
from services import memory_service, translate_service
from services.telemetry import telemetry

HELP = """📖 Lệnh trên Zalo/Zoom
/prompt <mô tả> — viết prompt tạo ảnh
/anh <mô tả> — tạo ảnh thật (Agnes AI)
/gia <sản phẩm> — tìm và so sánh giá
/dich [ja>vi|vi>ja] <nội dung> — dịch chat công việc Nhật-Việt
/reset — xoá ngữ cảnh chat
/history — xem 10 lượt gần nhất
/memory [on|off] — xem, bật hoặc tắt trí nhớ dài hạn
/forget — xoá trí nhớ dài hạn
/notes — xem ghi chú
/model [tên|auto] — xem hoặc đổi model (chỉ admin)
/status — xem trạng thái provider (chỉ admin)
/thongke [Nd|Ngiờ] — thống kê lượt gọi theo user/model, mặc định 7 ngày (chỉ admin)
/userouter9 — thử lại 9Router (chỉ admin)
/router9 on|off — bật/tắt 9Router thủ công (chỉ admin)
/tavily on|off — bật/tắt tra web Tavily trước khi trả lời (chỉ admin)
/anh on|off — bật/tắt tạo ảnh Agnes AI (chỉ admin)
/agent <câu hỏi> — agent tự tra cứu nhiều bước để trả lời (thử nghiệm, chỉ admin)
/bantinsang — gửi ngay bản tin buổi sáng (test thủ công, chỉ admin; bình thường tự gửi lúc 8h)
/nhom, /themnhom, /xoanhom, /tongket, /dangnoi — quản lý và xem lại nhóm Zalo
  (dữ liệu nhóm Zalo, dùng được từ cả Zalo lẫn Zoom, chỉ admin)"""

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


async def _prompt(user_id: int, description: str, channel: str = "zalo") -> tuple[list[str], str | None]:
    if not description:
        return ["Cú pháp: /prompt <mô tả muốn tạo prompt>"], None
    identity = resolve_prompt_identity(description, context="text")
    instruction = render_instruction(
        telegram_commands.TEXT_PROMPT_INSTRUCTION_BASE, identity, user_desc=description
    )
    hint = identity.mode_hint
    prompt_id = await telemetry.start(user_id, "prompt_generator", description, channel=channel)
    try:
        response = await orchestrator.ask(instruction)
        output = (response.text or "").strip()
        await telemetry.success(prompt_id, "prompt_generator", output or "(không có nội dung)")
        return ([f"{hint}\n\n{output}"] if output else ["Gemini không trả về prompt, hãy thử lại."]), ("api" if getattr(response, "used_fallback", False) else None)
    except Exception as exc:
        await telemetry.failure(prompt_id, "prompt_generator", exc)
        return ["❌ Có lỗi khi tạo prompt. Hãy thử lại sau."], None


async def _price(user_id: int, product: str, channel: str = "zalo") -> tuple[list[str], str | None]:
    if not product:
        return ["Cú pháp: /gia <tên sản phẩm>, ví dụ /gia iPhone 16 Pro"], None
    cached = telegram_commands._get_cached_price(product)
    if cached:
        return [cached], None
    prompt_id = await telemetry.start(user_id, "price_search", product, channel=channel)
    try:
        output, used_fallback = await telegram_commands._search_price(product)
        output = await telegram_commands._verify_links(output)
        if output:
            telegram_commands._set_cached_price(product, output)
        await telemetry.success(prompt_id, "price_search", output or "(không có nội dung)")
        return [output or "Không tìm được giá lúc này."], ("api" if used_fallback else None)
    except Exception as exc:
        await telemetry.failure(prompt_id, "price_search", exc)
        return ["❌ Có lỗi khi tìm giá sản phẩm."], None


async def _translate(user_id: int, argument: str, channel: str = "zalo") -> tuple[list[str], str | None]:
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


async def _agent(user_id: int, question: str, channel: str = "zalo") -> tuple[list[str], str | None]:
    if not question:
        return ["Dùng: /agent <câu hỏi>\nVí dụ: /agent so sánh giá iPhone 15 và iPhone 15 Pro"], None
    from ai import agent_service

    prompt_id = await telemetry.start(user_id, "agent", question, channel=channel)
    try:
        text, provider = await agent_service.ask_agent(question)
        await telemetry.success(prompt_id, "agent", text)
        return [f"{text}\n\n⚙️ {provider}"], None
    except Exception as exc:
        await telemetry.failure(prompt_id, "agent", exc)
        return ["Agent gặp lỗi, thử lại sau hoặc dùng lệnh thường (/gia, /thongke...) nhé."], None


async def _morning_news_now() -> list[str]:
    from services import morning_news

    result = await morning_news.run_once(force=True)
    if result.content is None:
        return ["Không có nội dung để gửi (tất cả nguồn RSS đều lỗi, hoặc model tổng hợp trả lời bất thường - xem log)."]
    if not result.sent_zalo and not result.sent_zoom:
        return ["Đã tổng hợp được bản tin nhưng KHÔNG gửi được tới đâu cả (chưa pair Zalo lẫn Zoom, hoặc gửi bị lỗi - xem log)."]
    kenh = []
    if result.sent_zalo:
        kenh.append("Zalo")
    if result.sent_zoom:
        kenh.append("Zoom")
    if len(kenh) < 2:
        thieu = "Zoom" if "Zoom" not in kenh else "Zalo"
        return [f"✅ Đã gửi bản tin buổi sáng qua {' và '.join(kenh)} (KHÔNG gửi được {thieu} - chưa pair hoặc lỗi, xem log)."]
    return ["✅ Đã gửi bản tin buổi sáng."]


async def _generate_image(argument: str, is_admin: bool, channel: str) -> tuple[list[str], str | None]:
    lowered = argument.strip().lower()
    if lowered in telegram_commands._ROUTER9_ON_ARGS or lowered in telegram_commands._ROUTER9_OFF_ARGS:
        if not is_admin:
            return ["Lệnh này chỉ dành cho admin."], None
        enabled = lowered in telegram_commands._ROUTER9_ON_ARGS
        await agnes_client.set_enabled(enabled)
        return (
            ["✅ Đã bật tạo ảnh (Agnes AI)."] if enabled
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
    if command == "/prompt":
        return await _prompt(user_id, argument, channel)
    if command == "/gia":
        return await _price(user_id, argument, channel)
    if command == "/dich":
        return await _translate(user_id, argument, channel)
    if command == "/anh":
        return await _generate_image(argument, is_admin, channel)
    if command == "/reset":
        await orchestrator.reset_chat(); await db.clear_chat(user_id)
        return ["🔄 Đã xoá ngữ cảnh hội thoại."], None
    if command == "/memory":
        lowered = argument.strip().lower()
        if lowered in telegram_commands._ROUTER9_ON_ARGS:
            await memory_service.set_enabled(user_id, True)
            return ["✅ Đã bật trí nhớ dài hạn."], None
        if lowered in telegram_commands._ROUTER9_OFF_ARGS:
            await memory_service.set_enabled(user_id, False)
            return ["🔴 Đã tắt trí nhớ dài hạn. Dùng /memory on để bật lại."], None
        facts, summary = await db.get_facts(user_id), await db.get_summary(user_id)
        enabled = await memory_service.is_enabled(user_id)
        status_line = f"\nTrạng thái: {'BẬT' if enabled else 'TẮT'} (/memory on|off để đổi)"
        if not facts and not summary:
            return [f"🧠 Chưa có trí nhớ dài hạn.{status_line}"], None
        lines = ["🧠 Trí nhớ dài hạn:"]
        if summary: lines.append(f"\nTóm tắt: {summary}")
        lines.extend(f"• {key}: {value}" for key, value in facts)
        lines.append(status_line)
        return ["\n".join(lines)], None
    if command == "/forget":
        await memory_service.clear_memory(user_id)
        return ["🗑️ Đã xoá toàn bộ trí nhớ dài hạn."], None
    if command == "/notes":
        notes = await db.get_notes(user_id, limit=10)
        return (["📝 Chưa có ghi chú nào."] if not notes else ["📝 Ghi chú gần đây:\n" + "\n".join(f"• {content} ({created.strftime('%H:%M %d/%m')})" for content, created in notes)]), None
    if command == "/history":
        rows = await db.get_history(user_id, limit=10)
        return (["Chưa có lịch sử nào."] if not rows else ["🕙 10 lượt gần nhất:\n" + "\n".join(f"• [{kind}] {prompt[:80]} ({created[:16].replace('T', ' ')})" for kind, prompt, created, _ in rows)]), None

    # /status, /userouter9, /model đổi CẤU HÌNH TOÀN CỤC (provider đang dùng,
    # model chat) - ảnh hưởng MỌI người dùng trên MỌI kênh, không riêng người
    # gõ lệnh. Chỉ admin (Zalo: role=admin; Zoom/kênh khác chỉ có đúng 1 người
    # pair nên is_admin mặc định True) mới được đổi, để 1 thành viên thường
    # không thể vô tình/cố ý phá cấu hình chung của cả bot.
    if command in {"/status", "/userouter9", "/router9", "/tavily", "/model", "/thongke", "/agent", "/bantinsang"} and not is_admin:
        return ["Lệnh này chỉ dành cho admin."], None
    if command == "/agent":
        return await _agent(user_id, argument, channel)
    if command == "/bantinsang":
        return await _morning_news_now(), None
    if command == "/status":
        state = orchestrator.get_provider_state_snapshot()
        return [f"📡 Provider: {state['active_provider']}\nThứ tự: {' → '.join(config.PROVIDER_ORDER)}\nModel API: {config.GOOGLE_AI_STUDIO_MODEL}"], None
    if command == "/thongke":
        # Dùng chung logic/format với Telegram (handlers/commands.py) - xem
        # docstring _build_thongke_text ở đó.
        hours = telegram_commands._parse_thongke_hours(argument)
        return [await telegram_commands._build_thongke_text(hours, use_html=False)], None
    if command == "/userouter9":
        ok, detail = await orchestrator.try_router9_now()
        return (["✅ 9Router hoạt động, đã chuyển về 9Router."] if ok else [f"❌ 9Router vẫn lỗi: {detail[:250]}"]), None
    if command == "/router9":
        lowered = argument.strip().lower()
        if lowered in telegram_commands._ROUTER9_ON_ARGS:
            await orchestrator.set_router9_enabled(True)
            return ["✅ Đã bật 9Router."], None
        if lowered in telegram_commands._ROUTER9_OFF_ARGS:
            await orchestrator.set_router9_enabled(False)
            return ["🔴 Đã tắt 9Router cho tới khi bật lại bằng /router9 on."], None
        enabled = orchestrator.get_provider_state_snapshot()["router9_enabled"]
        return [f"9Router đang {'BẬT' if enabled else 'TẮT'}. Dùng /router9 on hoặc /router9 off để đổi."], None
    if command == "/tavily":
        lowered = argument.strip().lower()
        if lowered in telegram_commands._ROUTER9_ON_ARGS:
            await tavily_client.set_enabled(True)
            return ["✅ Đã bật tra web Tavily trước khi trả lời."], None
        if lowered in telegram_commands._ROUTER9_OFF_ARGS:
            await tavily_client.set_enabled(False)
            return ["🔴 Đã tắt Tavily, chat trở lại luồng bình thường."], None
        enabled = await tavily_client.get_enabled()
        return [f"Tavily đang {'BẬT' if enabled else 'TẮT'}. Dùng /tavily on hoặc /tavily off để đổi."], None
    if command == "/model":
        if not argument:
            current = await router9_client.get_preferred_model_name()
            return [
                f"🧠 Model hiện tại: {current or 'tự động'}\nĐổi bằng /model <tên>; mặc định bằng /model auto\n"
                f"⚠️ Model này chỉ áp dụng khi dùng 9Router — nếu bot tự chuyển sang API dự "
                f"phòng, API luôn dùng model mặc định ({config.GOOGLE_AI_STUDIO_MODEL})."
            ], None
        if argument.lower() in {"auto", "default", "reset"}:
            await router9_client.set_preferred_model_name(None); await orchestrator.reset_chat()
            return ["🔄 Đã về model tự động."], None
        name = await router9_client.find_model(argument)
        if name is None:
            return [f"Không tìm thấy model khớp “{argument}”."], None
        await router9_client.set_preferred_model_name(name); await orchestrator.reset_chat()
        return [f"✅ Đã đổi model sang {name}."], None
    return ["Lệnh chưa được hỗ trợ. Gõ /help để xem danh sách."], None
