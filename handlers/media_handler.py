"""Xử lý ảnh gửi tới bot: phân tích ảnh -> viết prompt tiếng Anh dán sang
app Gemini.

Có 3 DẠNG, mỗi dạng LẤY KHUÔN MẶT TỪ MỘT NGUỒN KHÁC NHAU, nên prompt
xuất ra phải tả chủ thể theo một cách khác nhau. Trước đây cả 3 dùng chung
một câu "the subject from the reference image" và không tả mặt gì cả - đó là
lý do ảnh tạo ra khác hẳn ảnh mẫu:

1. Không caption (hoặc caption không có từ khoá) -> TẢ LẠI ĐÚNG NGƯỜI
   TRONG ẢNH thành chữ. Prompt này được dán sang Gemini dưới dạng chữ thuần
   tuý, KHÔNG đính kèm ảnh. Vì vậy câu "the subject from the reference image"
   là câu RỖNG - không có ảnh nào để trỏ tới, model sẽ tự bịa ra một khuôn
   mặt bất kỳ.
2. "cô gái 20" -> TẢ LẠI KHUÔN MẶT TRONG IDENTITY_LOCK_GIRL thành chữ để
   đồng nhất nhân vật qua nhiều ảnh. Ảnh gốc chỉ cho dáng, đồ, bối cảnh -
   TUYỆT ĐỐI không tả mặt người trong ảnh, vì tả vào là đánh nhau với khoá.
3. "mặt tôi" -> user ĐÍNH KÈM ẢNH cùng prompt trên app Gemini. Lúc này câu
   "attached reference image" mới có nghĩa. Ngược lại, KHÔNG được bịa chi
   tiết khuôn mặt vì sẽ chọi với ảnh thật đính kèm.

Ngoài khuôn mặt, 4 thứ dưới đây bắt buộc phải có trong prompt, vì thiếu
chúng thì ảnh tạo ra sai hoàn toàn dù mặt có đúng:
- KHUNG HÌNH: dọc hay ngang, cỡ ảnh, độ cao và góc máy, khoảng cách. Không
  nói thì Gemini mặc định đẻ ra ảnh ngang, người bé tí ở giữa khung.
- DÁNG NGƯỜI: góc tay, khuỷu thẳng hay gập, độ cao bàn tay, hướng lòng bàn
  tay, nghiêng đầu, hướng nhìn, tóc. "arms outstretched" chung chung bị hiểu
  thành động tác nhún vai.
- CHẤT ẢNH: bám theo ảnh gốc. Ảnh gốc bóng bẩy mà ép "visible pores,
  unretouched, zero airbrushing" là đánh nhau với chính ảnh mẫu.
- ỐNG KÍNH: suy từ phối cảnh của ảnh gốc, không chép cứng 35mm của ví dụ
  (35mm là góc rộng, càng đẩy chủ thể ra xa và nhỏ lại).

Hỗ trợ 2 cách gửi ảnh:
- Ảnh nén (filters.PHOTO): đi qua photo_msg trực tiếp.
- File/document ảnh (filters.Document.IMAGE): cũng đi qua photo_msg,
  đọc từ update.message.document thay vì update.message.photo.
"""
import logging

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

import messages
from core import config
from core.text_normalize import nfc
from ai import orchestrator
from handlers import common
from handlers.prompt_identity import render_instruction, resolve_prompt_identity
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

# Độ dài tối đa của chi tiết lỗi in ra Telegram. Bot chỉ phục vụ 1 người nên
# in thẳng loại lỗi + thông điệp là cách nhanh nhất để biết provider nào hỏng,
# thay vì phải mở log Render.
_ERROR_DETAIL_MAX = 300


def _short_error(exc: BaseException) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    if len(detail) > _ERROR_DETAIL_MAX:
        detail = detail[:_ERROR_DETAIL_MAX] + "…"
    return detail


IMAGE_ANALYZE_INSTRUCTION_BASE = """You are an expert prompt engineer for AI image generation. Analyze the attached reference photograph and produce ONE complete English prompt that reconstructs the visible composition as closely as possible while respecting the selected identity mode.

Use this neutral example only as a structural reference. Replace every scene-specific detail with information actually visible in the reference image. Never copy the example's framing, clothing, camera, lighting or setting unless the reference genuinely matches them.

---
{identity_lock_block}Photographic portrait of {subject_phrase} in a simple outdoor setting. Vertical 9:16 portrait orientation, three-quarter body shot framed from mid-thigh upward, camera at chest height and level with the subject, approximately two metres away, with the subject filling most of the frame and modest headroom.

Her arms rest naturally beside her body, elbows slightly relaxed, hands near hip level. Her weight is distributed naturally between both legs, shoulders aligned with the camera, head turned slightly to one side, chin level, gaze directed just off-camera, mouth closed with a restrained natural expression. Her hair follows the requested hairstyle naturally.

She wears simple everyday clothing appropriate to the described setting, with realistic fabric texture, folds and construction.

The background contains only environmental elements appropriate to the described setting, with depth and perspective consistent with the camera position.

Use a focal length and aperture consistent with the requested framing and perspective, with depth of field matching the visible photographic look.

Use lighting, colour, contrast, grain and photographic treatment appropriate to the reference image. Keep the result photographic and physically plausible.
---

Rules for what you generate:
{identity_rule}
{subject_rule}
3. REFERENCE PRIORITY: extract scene, pose, framing, clothing, accessories, environment, lighting, mood, perspective and photographic finish from the attached image. Do not import those details from the example.
4. IDENTITY PRIORITY: when the selected mode has an identity lock, facial geometry, facial proportions and body build outrank every variable visual attribute. Never copy a different person's face or body build from the reference into a locked identity mode. When the mode uses the attached photo as identity, the attachment is the sole facial source.
5. FRAMING IS MANDATORY. State the visible orientation/aspect ratio in plain words, using one of: "vertical 9:16 portrait orientation", "vertical 4:5 portrait orientation", "square 1:1 framing" or "horizontal 16:9 landscape orientation". Also state the shot size, camera height and angle, approximate camera distance, subject occupancy and headroom. Use only what the reference supports; estimate broad camera distance when exact distance is unknowable.
6. POSE MUST BE GEOMETRICALLY PRECISE but evidence-based. Describe visible arm/elbow positions, hand height and palm direction when visible, stance, weight distribution, shoulder/torso rotation, head tilt, chin height, gaze and mouth state. Do not invent hidden anatomy or exact degree measurements that cannot be observed.
7. OUTFIT AND MATERIALS: describe visible clothing, accessories, fit, fabric texture, seams, folds, wetness and contact with the body. Do not add garments or accessories that are not visible.
8. LIGHTING AND FINISH MUST MATCH THE REFERENCE. For genuinely raw/candid photography, use natural skin texture and restrained processing. For visibly polished beauty/fashion imagery, use the appropriate refined treatment. Never combine contradictory raw and beauty-filter instructions.
9. CAMERA AND LENS MUST BE INFERRED FROM PERSPECTIVE AND DEPTH OF FIELD. Use approximately 70-135mm for tight compressed portraits, 40-55mm for normal half-body/three-quarter views, and 24-35mm only when the environment is intentionally prominent. State an equivalent focal length, aperture and depth of field consistent with the reference. Do not invent a camera brand or exact model unless it is provided.
10. REALISM CHECK: preserve believable anatomy, perspective, skin texture, fabric behaviour, water/rain behaviour, reflections, shadows and depth of field. Avoid generic AI embellishment and details not supported by the reference. Instead of only writing generic words like "realistic", name one or two concrete everyday imperfections that belong in this exact scene (a scuffed curb, a faint puddle reflection, dust on the pavement, a light crease in the fabric, a slightly windblown strand of hair) - concrete detail reads as real, vague adjectives read as AI.
11. CLOSE WITH ONE SHORT EXCLUSION SENTENCE: end the prompt with a single concise sentence naming only the safeguards genuinely relevant to this scene (choose from: natural hand and finger anatomy, no extra or missing fingers, no warped or duplicated facial features, no distorted body proportions, no altered locked identity/outfit/product details, no watermark or stray text, and - for a candid outdoor or street scene - no illustration or anime look, no CGI rendering, no studio lighting, no overly staged composition). Pick 2 to 4 relevant items - do not pad with irrelevant ones and do not repeat wording already used earlier in the prompt.
12. OUTPUT ONLY the final English image prompt as plain text. No markdown, no headings, no explanations, no separate negative-prompt section and no tool-specific flags."""

@common.restricted
async def photo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    caption = nfc((update.message.caption or "").strip())
    prompt_label = caption or "(gửi ảnh, không có caption)"
    prompt_id = await telemetry.start(user_id, "promptify", prompt_label)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # prompt_id có thể là None khi DB lỗi (telemetry fail-open) - không được
    # để tên file thành "promptify_None.jpg".
    filename = f"promptify_{prompt_id if prompt_id is not None else 'img'}.jpg"
    local_path = config.MEDIA_DIR / filename

    identity = resolve_prompt_identity(caption)
    mode_hint = f"\n\n{identity.mode_hint}"


    # Hỗ trợ cả ảnh gửi dạng nén (photo) lẫn dạng file (document)
    if update.message.photo:
        file_obj = update.message.photo[-1]
    elif update.message.document:
        file_obj = update.message.document
    else:
        await update.message.reply_text("❌ Không đọc được ảnh. Anh thử gửi lại nhé.")
        return

    # ── Pha 1: Tải ảnh từ Telegram ──────────────────────────────
    try:
        await common.download_telegram_photo_with_retry(file_obj, local_path)
    except (TimedOut, NetworkError) as e:
        logger.exception("Lỗi tải ảnh từ Telegram")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text(messages.PHOTO_TIMEOUT_ERROR)
        return
    except Exception as e:
        logger.exception("Lỗi không xác định khi tải ảnh")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text(
            f"❌ Không tải được ảnh. Anh thử gửi lại nhé.\n🔎 {_short_error(e)}"
        )
        return

    # ── Pha 2: Phân tích bằng Gemini ────────────────────────────────
    # Tách RIÊNG khỏi pha gửi kết quả. Trước đây cả hai nằm chung một khối
    # try/except nên khi lỗi không tài nào biết được là Gemini hỏng hay
    # Telegram hỏng, và thông báo cũ nuốt sạch chi tiết lỗi.
    try:
        instruction = render_instruction(IMAGE_ANALYZE_INSTRUCTION_BASE, identity)
        if caption:
            instruction += f"\n\nAdditional user instruction: {caption}"

        response = await orchestrator.analyze_image(instruction, str(local_path))
        result_text = (getattr(response, "text", None) or "").strip()
        used_fallback = bool(getattr(response, "used_fallback", False))
    except Exception as e:
        # Nguyên nhân hay gặp: 9Router chết + API hết quota (cả
        # provider-chain sập), key API chưa cấu hình, hoặc ảnh bị chặn vì
        # chính sách nội dung. In thẳng loại lỗi để khỏi phải mò log Render.
        logger.exception("Lỗi phân tích ảnh (provider-chain)")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text(
            "❌ Gemini không phân tích được ảnh lúc này.\n"
            f"🔎 {_short_error(e)}\n"
            "Anh gõ /status xem provider nào đang sống nhé."
        )
        return
    finally:
        await common.safe_delete(local_path)

    if not result_text:
        await telemetry.success(prompt_id, "promptify", "(Gemini không trả về nội dung)")
        await update.message.reply_text(
            "Gemini không trả về nội dung phân tích. Thử gửi lại ảnh hoặc ảnh khác nhé."
        )
        return

    await telemetry.success(prompt_id, "promptify", result_text)

    # ── Pha 3: Gửi kết quả về Telegram ──────────────────────────────
    # Header riêng, rồi nội dung prompt gửi trong khối <pre> để Telegram hiện
    # nút Copy. Nhãn "⚙️ API" đặt ở header chứ KHÔNG đặt trong khối prompt,
    # nếu không bấm Copy sẽ chép luôn cả nhãn đó sang app Gemini.
    header = "📝 <b>Prompt gợi ý (dùng cho app Gemini)</b> — chạm vào khối bên dưới để chép:"
    if used_fallback:
        header += "  ⚙️ API"
    header += mode_hint
    try:
        await update.message.reply_text(header, parse_mode="HTML")
        await common.reply_code_block(update.message, result_text)
    except Exception as e:
        logger.exception("Lỗi gửi kết quả prompt về Telegram")
        await update.message.reply_text(
            "❌ Đã tạo được prompt nhưng gửi về Telegram lỗi.\n"
            f"🔎 {_short_error(e)}"
        )
