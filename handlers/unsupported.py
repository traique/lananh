"""Tin nhắn Telegram không phải chữ/ảnh/tin thoại/tài liệu.

- Vị trí (filters.LOCATION & filters.UpdateType.MESSAGE): báo thời tiết tại toạ
  độ đó bằng code (services/weather_chat.answer_coordinates), không gọi AI.
  UpdateType.MESSAGE để "vị trí trực tiếp" (live location - cập nhật bằng tin
  sửa) không làm bot trả lời mỗi lần toạ độ đổi.
- Còn lại (sticker, video, file lạ, danh bạ...): 1 câu giọng Lan Anh nói rõ em
  hiểu được những gì, thay vì im lặng. Handler này đăng ký CUỐI CÙNG
  (bot_app.build_application) nên chỉ nhận tin không handler nào khác khớp.

Cả hai đều qua @common.restricted: người lạ vẫn chỉ nhận im lặng.
"""

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from services import conversation_log, media_text, weather_chat


@common.restricted
async def location_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    location = getattr(message, "location", None)
    if location is None:
        return
    lat, lon = float(location.latitude), float(location.longitude)
    venue = getattr(message, "venue", None)
    name = (getattr(venue, "title", "") or "").strip() or "vị trí anh gửi"
    label = f"{name} ({lat:.3f}, {lon:.3f})"
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = await weather_chat.answer_coordinates(lat, lon, label)
    await conversation_log.record_exchange(
        update.effective_user.id, f"[Gửi vị trí] {label}", reply, conversation_log.KIND_WEATHER
    )
    await common.reply_long_text(message, reply)


def _kind(message) -> str:
    if getattr(message, "sticker", None) is not None:
        return "sticker"
    for attr in ("video", "video_note", "animation"):
        if getattr(message, attr, None) is not None:
            return "video"
    if getattr(message, "document", None) is not None:
        return "document"
    if getattr(message, "contact", None) is not None:
        return "contact"
    return "other"


@common.restricted
async def unsupported_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    await message.reply_text(media_text.unsupported_text(_kind(message), owner=True))
