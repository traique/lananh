"""Text command implementation for non-Telegram channels."""
from ai import orchestrator, router9_client
from core import config, database as db
from handlers import commands as telegram_commands
from services import memory_service, translate_service
from services.telemetry import telemetry

HELP = """📖 Lệnh trên Zalo/Zoom
/prompt <mô tả> — viết prompt tạo ảnh
/gia <sản phẩm> — tìm và so sánh giá
/dich [ja>vi|vi>ja] <nội dung> — dịch chat công việc Nhật-Việt
/reset — xoá ngữ cảnh chat
/history — xem 10 lượt gần nhất
/memory — xem trí nhớ dài hạn
/forget — xoá trí nhớ dài hạn
/notes — xem ghi chú
/model [tên|auto] — xem hoặc đổi model (chỉ admin)
/status — xem trạng thái provider (chỉ admin)
/userouter9 — thử lại 9Router (chỉ admin)
/nhom, /themnhom, /xoanhom, /tongket, /dangnoi — quản lý và xem lại nhóm Zalo
  (dữ liệu nhóm Zalo, dùng được từ cả Zalo lẫn Zoom, chỉ admin)"""


def _arg(text: str) -> str:
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""


async def _prompt(user_id: int, description: str) -> tuple[list[str], str | None]:
    if not description:
        return ["Cú pháp: /prompt <mô tả muốn tạo prompt>"], None
    lower = description.lower()
    c = telegram_commands
    if any(keyword in lower for keyword in c.KEEP_FACE_KEYWORDS):
        lock, rule, subject, subject_rule = c.IDENTITY_LOCK_REFERENCE + "\n\n", c._IDENTITY_RULE_LOCK, c._TEXT_SUBJECT_PHRASE_REFERENCE, c._TEXT_SUBJECT_RULE_REFERENCE
        hint = "📎 Hãy đính kèm ảnh gốc cùng prompt này trên app Gemini."
    elif any(keyword in lower for keyword in c.GIRL_KEYWORDS):
        lock, rule, subject, subject_rule = c.IDENTITY_LOCK_GIRL + "\n\n", c._IDENTITY_RULE_LOCK, c._TEXT_SUBJECT_PHRASE_GIRL, c._TEXT_SUBJECT_RULE_GIRL
        hint = "🔒 Prompt dùng khóa khuôn mặt cố định, không cần đính kèm ảnh."
    else:
        lock, rule, subject, subject_rule = "", c._IDENTITY_RULE_NONE, c._TEXT_SUBJECT_PHRASE_DESCRIBED, c._TEXT_SUBJECT_RULE_DESCRIBED
        hint = "🖼️ Prompt tự mô tả khuôn mặt bằng chữ."
    instruction = c.TEXT_PROMPT_INSTRUCTION_BASE.format(
        identity_lock_block=lock, identity_rule=rule, subject_phrase=subject,
        subject_rule=subject_rule, user_desc=description,
    )
    prompt_id = await telemetry.start(user_id, "prompt_generator", description)
    try:
        response = await orchestrator.ask(instruction)
        output = (response.text or "").strip()
        await telemetry.success(prompt_id, "prompt_generator", output or "(không có nội dung)")
        return ([f"{hint}\n\n{output}"] if output else ["Gemini không trả về prompt, hãy thử lại."]), ("api" if getattr(response, "used_fallback", False) else None)
    except Exception as exc:
        await telemetry.failure(prompt_id, "prompt_generator", exc)
        return ["❌ Có lỗi khi tạo prompt. Hãy thử lại sau."], None


async def _price(user_id: int, product: str) -> tuple[list[str], str | None]:
    if not product:
        return ["Cú pháp: /gia <tên sản phẩm>, ví dụ /gia iPhone 16 Pro"], None
    cached = telegram_commands._get_cached_price(product)
    if cached:
        return [cached], None
    prompt_id = await telemetry.start(user_id, "price_search", product)
    try:
        response = await orchestrator.ask(
            telegram_commands.PRICE_SEARCH_SYSTEM.format(product_name=product),
            enable_search=True,
            require_real_search=True,
        )
        output = await telegram_commands._verify_links((response.text or "").strip())
        if output:
            telegram_commands._set_cached_price(product, output)
        await telemetry.success(prompt_id, "price_search", output or "(không có nội dung)")
        return [output or "Không tìm được giá lúc này."], ("api" if getattr(response, "used_fallback", False) else None)
    except Exception as exc:
        await telemetry.failure(prompt_id, "price_search", exc)
        return ["❌ Có lỗi khi tìm giá sản phẩm."], None


async def _translate(user_id: int, argument: str) -> tuple[list[str], str | None]:
    if not argument.strip():
        return [
            "Cú pháp: /dich [ja>vi|vi>ja] <nội dung>\n"
            "Không chỉ định chiều thì tự nhận diện theo chữ Nhật trong câu.\n"
            "Ví dụ: /dich お世話になります。確認お願いします。"
        ], None

    first_token, _, rest = argument.partition(" ")
    direction = translate_service.parse_explicit_direction(first_token)
    text = rest if direction else argument
    if not text.strip():
        return ["Cú pháp: /dich [ja>vi|vi>ja] <nội dung>"], None

    prompt_id = await telemetry.start(user_id, "translate", text)
    try:
        result, resolved, response = await translate_service.translate(text, direction)
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


async def maybe_handle_command(
    user_id: int, text: str, is_admin: bool = True
) -> tuple[list[str], str | None] | None:
    if not text.startswith("/"):
        return None
    command = text.split(maxsplit=1)[0].lower().split("@", 1)[0]
    argument = _arg(text)

    if command in {"/start", "/help"}:
        return [HELP], None
    if command == "/prompt":
        return await _prompt(user_id, argument)
    if command == "/gia":
        return await _price(user_id, argument)
    if command == "/dich":
        return await _translate(user_id, argument)
    if command == "/reset":
        await orchestrator.reset_chat(); await db.clear_chat(user_id)
        return ["🔄 Đã xoá ngữ cảnh hội thoại."], None
    if command == "/memory":
        facts, summary = await db.get_facts(user_id), await db.get_summary(user_id)
        if not facts and not summary:
            return ["🧠 Chưa có trí nhớ dài hạn."], None
        lines = ["🧠 Trí nhớ dài hạn:"]
        if summary: lines.append(f"\nTóm tắt: {summary}")
        lines.extend(f"• {key}: {value}" for key, value in facts)
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
    if command in {"/status", "/userouter9", "/model"} and not is_admin:
        return ["Lệnh này chỉ dành cho admin."], None
    if command == "/status":
        state = orchestrator.get_provider_state_snapshot()
        return [f"📡 Provider: {state['active_provider']}\nThứ tự: {' → '.join(config.PROVIDER_ORDER)}\nModel API: {config.GOOGLE_AI_STUDIO_MODEL}"], None
    if command == "/userouter9":
        ok, detail = await orchestrator.try_router9_now()
        return (["✅ 9Router hoạt động, đã chuyển về 9Router."] if ok else [f"❌ 9Router vẫn lỗi: {detail[:250]}"]), None
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
