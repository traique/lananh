import asyncio
import html
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from telegram import Update
from telegram.ext import ContextTypes

import messages
from ai import agnes_client, orchestrator, router9_client, tavily_client
from channels import group_commands, zalo_repository, zalo_users
from core import config, database as db
from handlers import common
from handlers.prompt_identity import render_instruction, resolve_prompt_identity

from services import memory_service
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

HISTORY_PROMPT_PREVIEW_MAX = 60
HISTORY_LIMIT = 10

# ---------------------------------------------------------------------------
# Cache ngắn hạn cho /gia
# ---------------------------------------------------------------------------
_PRICE_CACHE: dict[str, tuple[float, str]] = {}
_PRICE_CACHE_TTL_SECONDS = 30 * 60  # 30 phút


def _normalize_product_key(product_name: str) -> str:
    return " ".join(product_name.lower().split())


def _get_cached_price(product_name: str) -> str | None:
    key = _normalize_product_key(product_name)
    cached = _PRICE_CACHE.get(key)
    if not cached:
        return None
    saved_at, text = cached
    age = time.time() - saved_at
    if age > _PRICE_CACHE_TTL_SECONDS:
        _PRICE_CACHE.pop(key, None)
        return None
    minutes = max(1, int(age // 60))
    return f"{text}\n\n*(⏱️ Kết quả tra cứu cách đây {minutes} phút, lấy từ cache anh nhé)*"


def _set_cached_price(product_name: str, text: str) -> None:
    _PRICE_CACHE[_normalize_product_key(product_name)] = (time.time(), text)


_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)")
_LINK_CHECK_TIMEOUT = 6.0


async def _check_url(client: httpx.AsyncClient, url: str) -> bool | None:
    """True = link sống. False = CHẮC CHẮN hỏng (404/410/không kết nối
    được). None = không chắc (403 bị chặn bot, timeout, lỗi 5xx tạm thời...)
    - những trường hợp này KHÔNG gắn cảnh báo để tránh báo nhầm link tốt
    thành hỏng chỉ vì trang có chặn request tự động."""
    try:
        resp = await client.head(url, timeout=_LINK_CHECK_TIMEOUT, follow_redirects=True)
        if resp.status_code in (403, 405):
            resp = await client.get(url, timeout=_LINK_CHECK_TIMEOUT, follow_redirects=True)
        if resp.status_code < 400:
            return True
        if resp.status_code in (404, 410):
            return False
        return None
    except httpx.ConnectError:
        return False
    except Exception:
        return None


async def _verify_links(text: str) -> str:
    matches = list(_MD_LINK_RE.finditer(text))
    if not matches:
        return text

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LanAnhBot/1.0)"}
        async with httpx.AsyncClient(headers=headers) as client:
            results = await asyncio.gather(
                *(_check_url(client, m.group(2)) for m in matches),
                return_exceptions=False,
            )
    except Exception:
        logger.exception("Lỗi khi verify link giá sản phẩm, giữ nguyên text gốc")
        return text

    out: list[str] = []
    last_end = 0
    for match, is_alive in zip(matches, results):
        out.append(text[last_end:match.start()])
        label, url = match.group(1), match.group(2)
        if is_alive is False:
            out.append(f"[⚠️ {label} (link có thể đã đổi/hết hàng)]({url})")
        else:
            out.append(match.group(0))
        last_end = match.end()
    out.append(text[last_end:])
    return "".join(out)

HELP_TEXT = (
    "📖 *Các lệnh hỗ trợ:*\n\n"
    "💬 Gõ tin nhắn bình thường để trò chuyện với em - Lan Anh - như trợ lý cá nhân.\n\n"
    "📊 Khi anh nhắc tới 1 *mã cổ phiếu Việt Nam*, mặc định em lấy giá khớp lệnh REALTIME.\n"
    "Cần phân tích sâu thì cứ nói rõ (vd \"phân tích giúp anh mã FPT\").\n\n"
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
    "/userouter9 — ép thử lại 9Router ngay\n"
    "/router9 on|off — bật/tắt 9Router thủ công\n"
    "/tavily on|off — bật/tắt tra web Tavily trước khi trả lời\n"
    "/anh <mô tả> — tạo ảnh thật (Agnes AI); /anh on|off — bật/tắt\n"
    "/zoompair, /zoomxoa, /zoomstatus — quản lý pairing Zoom\n"
    "/nhom, /themnhom, /xoanhom, /tongket, /dangnoi — quản lý và xem lại nhóm Zalo\n"
    "/zalopair, /zaloadmin, /zalohaquyen, /zalokhoa, /zalomokhoa, /zaloxoa, /zalodanhsach — quản lý user Zalo\n"
    "/help — hiển thị hướng dẫn này"
)

TEXT_PROMPT_INSTRUCTION_BASE = """You are an expert prompt engineer for AI image generation. Produce ONE complete English prompt that is immediately usable in a modern image generator. The prompt must preserve facial identity when an identity lock is supplied while allowing the requested scene, pose, styling and photographic treatment to change.

Use this neutral example only as a structural reference. Replace every scene-specific detail with information from the user's description. Never copy the example's subject, location, clothing, camera settings, lighting or mood unless the user explicitly requests them.

---
{identity_lock_block}Photographic portrait of {subject_phrase} in a simple outdoor setting. Vertical 9:16 portrait orientation, three-quarter body shot framed from mid-thigh upward, camera at chest height and level with the subject, approximately two metres away, with the subject filling most of the frame and modest headroom.

Her arms rest naturally beside her body, elbows slightly relaxed, hands near hip level. Her weight is distributed naturally between both legs, shoulders aligned with the camera, head turned slightly to one side, chin level, gaze directed just off-camera, mouth closed with a restrained natural expression. Her hair follows the requested hairstyle naturally.

She wears simple everyday clothing appropriate to the described setting, with realistic fabric texture, folds and construction.

The background contains only environmental elements appropriate to the described setting, with depth and perspective consistent with the camera position.

Use a focal length and aperture consistent with the requested framing and perspective, with depth of field matching the described photographic look.

Use lighting, colour, contrast, grain and photographic treatment appropriate to the user's requested mood and finish. Keep the result photographic and physically plausible.
---

Rules for what you generate:
{identity_rule}
{subject_rule}
3. SOURCE PRIORITY: for /prompt, derive scene, pose, clothing, environment, mood, camera and finish from the user's description. Never import those details from the example.
4. IDENTITY PRIORITY: when an identity lock is present, treat immutable facial geometry as higher priority than hairstyle, expression, makeup, clothing, pose, camera, lighting or environment. Before finalizing, remove any generated detail that contradicts the locked face.
5. FRAMING IS MANDATORY. State the orientation/aspect ratio in plain words, using one of: "vertical 9:16 portrait orientation", "vertical 4:5 portrait orientation", "square 1:1 framing" or "horizontal 16:9 landscape orientation". Also state the shot size, camera height and angle, approximate camera distance, how much of the frame the subject occupies and the headroom. Prefer the user's explicit framing. If none is given, choose a sensible framing for the subject and scene instead of defaulting to a distant landscape composition.
6. POSE MUST BE GEOMETRICALLY PRECISE but evidence-based. Describe the visible arm and elbow positions, hand height and palm direction when visible, stance and weight distribution, shoulder/torso rotation, head tilt, chin height, gaze and mouth state. Describe hair movement only when visible or requested. Do not invent exact angles or hidden anatomy; use natural neutral wording when the image or description does not support a precise detail.
7. OUTFIT AND MATERIALS: describe clothing, accessories, fit, fabric, seams, folds, wetness and contact with the body only when supported by the user's description. Keep anatomy and clothing physically plausible.
8. LIGHTING AND FINISH MUST MATCH THE USER'S REQUEST. For raw/candid photography use natural skin texture, realistic pores and restrained processing. For polished beauty/fashion work use controlled retouching, luminous skin and refined colour only when requested. Never mix contradictory finish instructions.
9. CAMERA AND LENS MUST MATCH THE FRAMING. Use approximately 70-135mm for tight portraits with compression, 40-55mm for normal half-body/three-quarter views, and 24-35mm only when the environment is intentionally prominent. State an equivalent focal length, aperture and depth of field that are physically consistent with the scene. Do not invent a camera brand or exact model unless the user provides it.
10. REALISM CHECK: preserve believable anatomy, perspective, skin texture, fabric behaviour, rain/water behaviour, reflections, shadows and depth of field. Avoid generic AI embellishment, unnecessary beauty changes and decorative details not requested by the user.
11. OUTPUT ONLY the final English image prompt as plain text. No markdown, no headings, no explanations, no negative-prompt section and no tool-specific flags.

User's basic description: {user_desc}"""

PRICE_SEARCH_SYSTEM = """Bạn là trợ lý Lan Anh. Nhiệm vụ của bạn là sử dụng công cụ Google Search để tìm giá cập nhật mới nhất cho sản phẩm: "{product_name}" tại các hệ thống bán lẻ uy tín ở Việt Nam.

YÊU CẦU QUAN TRỌNG:
1. So khớp CHÍNH XÁC phiên bản/dung lượng.
2. BẮT BUỘC phải trích xuất URL (đường link) gốc của trang sản phẩm để người dùng bấm vào xem.
3. Không tự bịa giá. Nếu hệ thống báo hết hàng hoặc không có giá, hãy ghi chú rõ.
4. BẮT BUỘC dùng công cụ Google Search TRƯỚC, rồi mới trả lời - không được trả lời dựa trên trí nhớ/kiến thức đã học sẵn của bạn. Kiến thức nội bộ của bạn có thể đã LỖI THỜI (sản phẩm mới ra mắt sau thời điểm bạn được huấn luyện). Nếu kết quả tìm kiếm cho thấy sản phẩm đã có bán/có giá, PHẢI tin theo kết quả tìm kiếm dù điều đó trái với những gì bạn "nhớ". Chỉ được kết luận "chưa ra mắt" hoặc "chưa có giá" khi kết quả tìm kiếm thực sự không tìm thấy thông tin nào về sản phẩm này.

Trình bày kết quả theo ĐÚNG định dạng list (KHÔNG dùng bảng markdown vì Telegram không hiển thị được bảng) và văn phong sau:

**{product_name}** — giá cập nhật mới nhất

Dạ em lượn một vòng các đại lý lớn để khảo giá cho anh rồi đây nha:

🏨 **[Tên shop 1]** — **[Giá]đ**
[Màu sắc/Khuyến mãi ngắn gọn]. [Xem sản phẩm]([Link trực tiếp đến sản phẩm])

🏨 **[Tên shop 2]** — **[Giá]đ**
[Màu sắc/Khuyến mãi ngắn gọn]. [Xem sản phẩm]([Link trực tiếp đến sản phẩm])

(lặp lại 1 khối như trên cho mỗi shop tìm được, tối đa 5 shop)

🔥 **Chỗ rẻ nhất em thấy:**
👉 **[Tên shop rẻ nhất]**: [Giá rẻ nhất]đ cho [Màu/phiên bản].

*(Lưu ý nhỏ: Giá này em tra cứu online ngay lúc này, có thể thay đổi tùy tồn kho từng chi nhánh hoặc flash sale anh nhé).*

QUAN TRỌNG VỀ LINK: mỗi link BẮT BUỘC viết đúng cú pháp markdown [Chữ hiển thị](https://url-that-page), không được dán URL trần, không được để URL trong ngoặc đơn kèm mô tả."""

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
        await update.message.reply_text("Anh nhập mô tả muốn tạo prompt nhé. Ví dụ: /prompt cô gái đứng trước nhà")
        return

    user_id = update.effective_user.id
    prompt_id = await telemetry.start(user_id, "prompt_generator", user_desc)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    identity = resolve_prompt_identity(user_desc)
    mode_hint = f"\n\n{identity.mode_hint}"
    instruction = render_instruction(TEXT_PROMPT_INSTRUCTION_BASE, identity, user_desc=user_desc)

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
        header += mode_hint
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
        await update.message.reply_text("Anh nhập tên sản phẩm muốn tìm giá nhé. Ví dụ: /gia iPhone 16 Pro")
        return

    user_id = update.effective_user.id

    cached_text = _get_cached_price(product_name)
    if cached_text is not None:
        await telemetry.start(user_id, "price_search", product_name)
        await update.message.reply_text(f"⚡ Có kết quả gần đây cho \"{product_name}\", gửi anh liền nè:")
        await common.reply_long_text(update.message, cached_text)
        return

    prompt_id = await telemetry.start(user_id, "price_search", product_name)

    status = await update.message.reply_text(f"🔍 Đang dạo siêu thị tìm giá {product_name} cho anh...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    instruction = PRICE_SEARCH_SYSTEM.format(product_name=product_name)

    try:
        response = await orchestrator.ask(instruction, enable_search=True, require_real_search=True)
        result_text = (response.text or "").strip()

        if not result_text:
            await telemetry.success(prompt_id, "price_search", "(Gemini không trả về nội dung)")
            await status.edit_text("Em không tìm được giá lúc này, anh thử lại sau nha.")
            return

        await telemetry.success(prompt_id, "price_search", result_text)
        result_text = await _verify_links(result_text)
        _set_cached_price(product_name, result_text)
        suffix = "\n\n⚙️ API" if getattr(response, "used_fallback", False) else ""

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
        arg = context.args[0].strip().lower()
        if arg in _ROUTER9_ON_ARGS:
            await memory_service.set_enabled(user_id, True)
            await update.message.reply_text("✅ Đã bật trí nhớ dài hạn.")
            return
        if arg in _ROUTER9_OFF_ARGS:
            await memory_service.set_enabled(user_id, False)
            await update.message.reply_text(
                "🔴 Đã tắt trí nhớ dài hạn. Bot sẽ không trích xuất/nhớ thêm gì mới cho tới khi bật lại bằng /memory on."
            )
            return

    facts = await db.get_facts(user_id)
    summary = await db.get_summary(user_id)
    enabled = await memory_service.is_enabled(user_id)
    status_line = f"\nTrạng thái: {'BẬT' if enabled else 'TẮT'} (dùng /memory on hoặc /memory off để đổi)"

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
async def userouter9_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Đang thử lại 9Router, chờ chút...")
    ok, detail = await orchestrator.try_router9_now()
    if ok:
        await update.message.reply_text("✅ 9Router hoạt động, đã chuyển về dùng 9Router.")
    else:
        await update.message.reply_text(f"❌ 9Router vẫn đang lỗi ({html.escape(detail)}).")

_ROUTER9_ON_ARGS = {"on", "bat", "bật"}
_ROUTER9_OFF_ARGS = {"off", "tat", "tắt"}


@common.restricted
async def router9_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = common.extract_arg(context).strip().lower()
    if arg in _ROUTER9_ON_ARGS:
        await orchestrator.set_router9_enabled(True)
        await update.message.reply_text("✅ Đã bật 9Router.")
        return
    if arg in _ROUTER9_OFF_ARGS:
        await orchestrator.set_router9_enabled(False)
        await update.message.reply_text(
            "🔴 Đã tắt 9Router. Bot sẽ bỏ qua 9Router trong provider-chain "
            "cho tới khi gõ /router9 on."
        )
        return
    enabled = orchestrator.get_provider_state_snapshot()["router9_enabled"]
    status = "🟢 đang BẬT" if enabled else "🔴 đang TẮT"
    await update.message.reply_text(
        f"9Router {status}.\nDùng /router9 on hoặc /router9 off để đổi."
    )


@common.restricted
async def anh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = common.extract_arg(context).strip()
    lowered = arg.lower()
    if lowered in _ROUTER9_ON_ARGS:
        await agnes_client.set_enabled(True)
        await update.message.reply_text("✅ Đã bật tạo ảnh (Agnes AI). Dùng /anh <mô tả> để tạo ảnh.")
        return
    if lowered in _ROUTER9_OFF_ARGS:
        await agnes_client.set_enabled(False)
        await update.message.reply_text("🔴 Đã tắt tạo ảnh cho tới khi bật lại bằng /anh on.")
        return
    if not arg:
        enabled = await agnes_client.get_enabled()
        status = "🟢 đang BẬT" if enabled else "🔴 đang TẮT"
        await update.message.reply_text(
            f"Tạo ảnh (Agnes AI) {status}.\nDùng /anh <mô tả> để tạo ảnh, hoặc /anh on|off để bật/tắt."
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    try:
        image = await agnes_client.generate_image(arg)
    except agnes_client.AgnesError as exc:
        await update.message.reply_text(f"❌ Không tạo được ảnh: {exc}")
        return
    await update.message.reply_photo(photo=image.data, caption=arg[:1000])


@common.restricted
async def tavily_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = common.extract_arg(context).strip().lower()
    if arg in _ROUTER9_ON_ARGS:
        await tavily_client.set_enabled(True)
        await update.message.reply_text("✅ Đã bật tra web Tavily trước khi trả lời.")
        return
    if arg in _ROUTER9_OFF_ARGS:
        await tavily_client.set_enabled(False)
        await update.message.reply_text("🔴 Đã tắt Tavily, chat trở lại luồng bình thường.")
        return
    enabled = await tavily_client.get_enabled()
    status = "🟢 đang BẬT" if enabled else "🔴 đang TẮT"
    await update.message.reply_text(
        f"Tavily {status}.\nDùng /tavily on hoặc /tavily off để đổi."
    )


@common.restricted
async def zoompair_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zoompair <jid> [tên hiển thị] — cấp quyền cho ĐÚNG 1 jid Zoom được
    nói chuyện với bot (thay pairing cũ nếu có)."""
    arg = common.extract_arg(context)
    if not arg:
        await update.message.reply_text(
            "Cách dùng: /zoompair <jid> [tên hiển thị]\n"
            "Lấy jid từ log bot khi có người lạ nhắn Zoom cho bot, hoặc từ "
            "cảnh báo bot tự gửi khi có jid chưa pair nhắn tới."
        )
        return
    parts = arg.split(maxsplit=1)
    jid = parts[0].strip()
    display_name = parts[1].strip() if len(parts) > 1 else ""
    await db.zoom_set_pairing(jid, display_name)
    label = f" ({display_name})" if display_name else ""
    await update.message.reply_text(f"✅ Đã cấp quyền Zoom cho jid: {html.escape(jid)}{html.escape(label)}")


@common.restricted
async def zoomxoa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zoomxoa — gỡ pairing Zoom hiện tại (không ai được nhắn cho bot qua Zoom nữa)."""
    pairing = await db.zoom_get_pairing()
    if pairing is None:
        await update.message.reply_text("Hiện chưa pair jid Zoom nào.")
        return
    await db.zoom_clear_pairing()
    await update.message.reply_text(f"🗑️ Đã gỡ pairing Zoom (jid cũ: {html.escape(pairing[0])}).")


@common.restricted
async def zoomstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zoomstatus — xem jid Zoom đang được pair (nếu có)."""
    if not config.ZOOM_ENABLED:
        await update.message.reply_text("⚪ Kênh Zoom đang tắt (ZOOM_ENABLED=false).")
        return
    pairing = await db.zoom_get_pairing()
    if pairing is None:
        await update.message.reply_text(
            "⚪ Kênh Zoom đang bật nhưng chưa pair jid nào.\nDùng /zoompair <jid> để cấp quyền."
        )
        return
    jid, name = pairing
    label = f" ({name})" if name else ""
    await update.message.reply_text(f"✅ Zoom đang pair với jid: {html.escape(jid)}{html.escape(label)}")


_ZALO_ROLE_ICON = {"admin": "👑", "user": "👤"}
_ZALO_STATUS_LABEL = {"active": "hoạt động", "suspended": "đã khóa"}


@common.restricted
async def zalopair_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zalopair <id_zalo> [tên] — cấp quyền THÀNH VIÊN (chỉ tính năng bình
    thường: chat, /prompt, /gia, /dich... KHÔNG có lệnh nhóm)."""
    arg = common.extract_arg(context)
    if not arg:
        await update.message.reply_text(
            "Cú pháp: /zalopair <id_zalo> [tên hiển thị]\n"
            "Lấy id_zalo từ cảnh báo bot tự gửi khi có người lạ nhắn Zalo tới bot."
        )
        return
    external_id, _, display_name = arg.partition(" ")
    user = await zalo_users.pair(external_id.strip(), display_name.strip())
    label = f" ({user.display_name})" if user.display_name else ""
    await update.message.reply_text(
        f"✅ Đã cấp quyền THÀNH VIÊN cho Zalo id: {html.escape(user.external_id)}{html.escape(label)}"
    )


@common.restricted
async def zaloadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zaloadmin <id_zalo> [tên] — cấp/nâng quyền ADMIN (dùng được lệnh nhóm
    /nhom, /themnhom, /xoanhom, /tongket, /dangnoi). Hỗ trợ NHIỀU admin cùng
    lúc, không giới hạn 1 admin duy nhất."""
    arg = common.extract_arg(context)
    if not arg:
        await update.message.reply_text("Cú pháp: /zaloadmin <id_zalo> [tên hiển thị]")
        return
    external_id, _, display_name = arg.partition(" ")
    user = await zalo_users.pair_as_admin(external_id.strip(), display_name.strip())
    label = f" ({user.display_name})" if user.display_name else ""
    await update.message.reply_text(
        f"👑 Đã cấp quyền ADMIN cho Zalo id: {html.escape(user.external_id)}{html.escape(label)} "
        f"(dùng được lệnh nhóm)."
    )


@common.restricted
async def zalohaquyen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zalohaquyen <id_zalo> — hạ 1 admin về thành viên thường (vẫn giữ pairing)."""
    external_id = common.extract_arg(context).strip()
    if not external_id:
        await update.message.reply_text("Cú pháp: /zalohaquyen <id_zalo>")
        return
    found = await zalo_users.demote_to_user(external_id)
    if found:
        await update.message.reply_text(f"👤 Đã hạ {html.escape(external_id)} về thành viên thường.")
    else:
        await update.message.reply_text(f"Không tìm thấy {html.escape(external_id)} đã pair.")


@common.restricted
async def zalokhoa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    external_id = common.extract_arg(context).strip()
    if not external_id:
        await update.message.reply_text("Cú pháp: /zalokhoa <id_zalo>")
        return
    found = await zalo_users.set_status(external_id, zalo_users.STATUS_SUSPENDED)
    if found:
        await update.message.reply_text(f"🔒 Đã khóa {html.escape(external_id)}.")
    else:
        await update.message.reply_text(f"Không tìm thấy {html.escape(external_id)} đã pair.")


@common.restricted
async def zalomokhoa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    external_id = common.extract_arg(context).strip()
    if not external_id:
        await update.message.reply_text("Cú pháp: /zalomokhoa <id_zalo>")
        return
    found = await zalo_users.set_status(external_id, zalo_users.STATUS_ACTIVE)
    if found:
        await update.message.reply_text(f"🔓 Đã mở khóa {html.escape(external_id)}.")
    else:
        await update.message.reply_text(f"Không tìm thấy {html.escape(external_id)} đã pair.")


@common.restricted
async def zaloxoa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    external_id = common.extract_arg(context).strip()
    if not external_id:
        await update.message.reply_text("Cú pháp: /zaloxoa <id_zalo>")
        return
    found = await zalo_users.remove(external_id)
    if found:
        await update.message.reply_text(f"🗑️ Đã xoá pairing {html.escape(external_id)}.")
    else:
        await update.message.reply_text(f"Không tìm thấy {html.escape(external_id)} đã pair.")


@common.restricted
async def zalodanhsach_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = await zalo_users.list_users()
    if not users:
        await update.message.reply_text("Chưa có Zalo user nào được pair.")
        return
    lines = ["📋 <b>Danh sách Zalo user</b>", ""]
    for user in users:
        icon = _ZALO_ROLE_ICON.get(user.role, "👤")
        status = _ZALO_STATUS_LABEL.get(user.status, user.status)
        ten = html.escape(user.display_name) if user.display_name else "không tên"
        lines.append(f"{icon} {html.escape(user.external_id)} ({ten}) — {status}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _group_command(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    """Dùng chung cho /nhom, /themnhom, /xoanhom, /tongket, /dangnoi trên
    Telegram - cùng 1 hàm channels.group_commands.maybe_handle_group_command()
    mà Zalo/Zoom đang dùng, chỉ khác cách lấy account_id (Telegram không có
    sẵn account_id Zalo trong update, phải tự suy ra qua
    zalo_repository.resolve_default_account_id())."""
    account_id = await zalo_repository.resolve_default_account_id()
    if account_id is None:
        await update.message.reply_text(
            "Chưa theo dõi nhóm Zalo nào. Dùng /nhom để xem hướng dẫn lấy group ID, "
            "sau đó /themnhom <group_id> <tên-gợi-nhớ>."
        )
        return
    argument = common.extract_arg(context)
    text = f"{command} {argument}".strip()
    result = await group_commands.maybe_handle_group_command(account_id, text)
    if result is None:
        await update.message.reply_text("Lệnh chưa được hỗ trợ.")
        return
    for message_text in result.messages:
        await common.reply_long_text(update.message, message_text)


@common.restricted
async def nhom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _group_command(update, context, "/nhom")


@common.restricted
async def themnhom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _group_command(update, context, "/themnhom")


@common.restricted
async def xoanhom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _group_command(update, context, "/xoanhom")


@common.restricted
async def tongket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _group_command(update, context, "/tongket")


@common.restricted
async def dangnoi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _group_command(update, context, "/dangnoi")

@common.restricted
async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = common.extract_arg(context)
    if not arg:
        current = await router9_client.get_preferred_model_name()
        try:
            names = await router9_client.list_models()
        except Exception:
            await update.message.reply_text("❌ Không lấy được danh sách model lúc này.")
            return
        lines = [
            f"🧠 Model đang dùng cho chat: <b>{html.escape(current or 'tự động')}</b>",
            "", "Các model khả dụng:"
        ]
        lines += [f"• {html.escape(n)}" for n in names]
        lines.extend([
            "", "Đổi model: <code>/model tên</code>", "Về mặc định: <code>/model auto</code>",
            "", "⚠️ Model này chỉ áp dụng khi dùng 9Router. Nếu 9Router lỗi và bot tự "
            f"chuyển sang API dự phòng, API luôn dùng model mặc định ({html.escape(config.GOOGLE_AI_STUDIO_MODEL)}).",
        ])
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if arg.lower() in {"auto", "default", "reset"}:
        await router9_client.set_preferred_model_name(None)
        await orchestrator.reset_chat()
        await update.message.reply_text("🔄 Đã về chọn model tự động.")
        return

    try:
        model_name = await router9_client.find_model(arg)
    except Exception:
        await update.message.reply_text("❌ Không kiểm tra được model lúc này.")
        return

    if model_name is None:
        await update.message.reply_text(f'Không tìm thấy model khớp "{arg}".')
        return

    await router9_client.set_preferred_model_name(model_name)
    await orchestrator.reset_chat()
    await update.message.reply_text(f"✅ Đã đổi model chat sang: {model_name}")

def _fmt_epoch_vn(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%H:%M %d/%m")

async def _noop_ai_status() -> tuple[bool, str]:
    return False, "Chưa cấu hình"


_STATUS_CACHE_TTL_SECONDS = 90
_status_cache: tuple[float, str] | None = None


async def _build_status_text() -> str:
    """Ping thật cả 5 provider rồi dựng text trạng thái (HTML parse_mode)."""
    (
        (router9_ok, router9_detail),
        groq_status,
        openrouter_status,
        api1_status,
        api2_status,
    ) = await asyncio.gather(
        orchestrator.check_router9_status(),
        orchestrator.check_groq_status() if config.GROQ_API_KEY else _noop_ai_status(),
        orchestrator.check_openrouter_status() if config.OPENROUTER_API_KEY else _noop_ai_status(),
        orchestrator.check_ai_studio_status(1) if config.GOOGLE_AI_STUDIO_API_KEY_1 else _noop_ai_status(),
        orchestrator.check_ai_studio_status(2) if config.GOOGLE_AI_STUDIO_API_KEY_2 else _noop_ai_status(),
    )

    state = orchestrator.get_provider_state_snapshot()
    now = time.time()

    active_map = {"router9": "9Router", "groq": "Groq", "openrouter": "OpenRouter", "api1": "API 1", "api2": "API 2"}
    active_line = f"🔀 Provider đang dùng: <b>{html.escape(active_map.get(state['active_provider'], state['active_provider']))}</b>"

    router9_line = "✅ 9Router: OK" if router9_ok else f"❌ 9Router: lỗi ({html.escape(router9_detail)})"
    if not state["router9_enabled"]:
        router9_line += "\n   ⛔ đang TẮT thủ công (/router9 on để bật lại)"
    if state["router9_dead_since"]:
        router9_line += f"\n   ⥅ chết lúc {_fmt_epoch_vn(state['router9_dead_since'])}"

    def _provider_line(label, key, status, exhausted_until):
        if not key: return f"⚪ {label}: chưa cấu hình"
        ok, detail = status
        line = f"✅ {label}: OK" if ok else f"❌ {label}: lỗi ({html.escape(detail)})"
        if exhausted_until > now:
            line += f"\n   ⥅ cooldown tới {_fmt_epoch_vn(exhausted_until)}"
        return line

    groq_line = _provider_line("Groq", config.GROQ_API_KEY, groq_status, state["groq_exhausted_until"])
    openrouter_line = _provider_line("OpenRouter", config.OPENROUTER_API_KEY, openrouter_status, state["openrouter_exhausted_until"])
    api1_line = _provider_line("API 1", config.GOOGLE_AI_STUDIO_API_KEY_1, api1_status, state["api1_exhausted_until"])
    api2_line = _provider_line("API 2", config.GOOGLE_AI_STUDIO_API_KEY_2, api2_status, state["api2_exhausted_until"])

    preferred = await router9_client.get_preferred_model_name()
    model_line = (
        f"🧠 Model chat: {html.escape(preferred or 'tự động')} "
        f"(9Router: {html.escape(config.ROUTER9_MODEL)}, Groq: {html.escape(config.GROQ_MODEL)}, "
        f"OpenRouter: {html.escape(config.OPENROUTER_MODEL)}, API: {html.escape(config.GOOGLE_AI_STUDIO_MODEL)})"
    )
    order_line = "🔢 PROVIDER_ORDER: " + " → ".join(config.PROVIDER_ORDER)

    lines = [
        "📡 <b>Trạng thái bot</b>", "", active_line, order_line, "",
        router9_line, groq_line, openrouter_line, api1_line, api2_line, "", model_line,
    ]
    return "\n".join(lines)


@common.restricted
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _status_cache

    now = time.time()
    if _status_cache is not None and now - _status_cache[0] <= _STATUS_CACHE_TTL_SECONDS:
        checked_at, cached_text = _status_cache
        await update.message.reply_text(
            f"{cached_text}\n\n<i>⏱️ Kiểm tra lúc {_fmt_epoch_vn(checked_at)} "
            f"({int(now - checked_at)}s trước) - gõ lại sau {_STATUS_CACHE_TTL_SECONDS}s để ping mới.</i>",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text("🔎 Đang kiểm tra provider-chain...")
    text = await _build_status_text()
    checked_at = time.time()
    _status_cache = (checked_at, text)
    await update.message.reply_text(
        f"{text}\n\n<i>⏱️ Kiểm tra lúc {_fmt_epoch_vn(checked_at)}</i>",
        parse_mode="HTML",
    )

@common.restricted
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = await db.get_history(user_id, limit=HISTORY_LIMIT)
    if not rows:
        return await update.message.reply_text("Chưa có lịch sử nào.")
    icon_map = {"image": "🖼️", "chat": "💬", "promptify": "🔍", "stock_analysis": "📊", "stock_price": "💹", "prompt_generator": "✏️", "price_search": "🛒"}
    lines = [f"🕙 <b>{HISTORY_LIMIT} lượt gần nhất:</b>\n"]
    for command_type, prompt, created_at, _result_types in rows:
        short_prompt = prompt[:HISTORY_PROMPT_PREVIEW_MAX] + "…" if len(prompt) > HISTORY_PROMPT_PREVIEW_MAX else prompt
        icon = icon_map.get(command_type, "•")
        date_part = created_at[:16].replace("T", " ")
        lines.append(f"{icon} [{html.escape(command_type)}] {html.escape(short_prompt)} ({date_part})")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Lỗi không được xử lý", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ Đã có lỗi không mong muốn xảy ra. Vui lòng thử lại.")
        except Exception:
            logger.exception("Không gửi được thông báo lỗi cho user")
