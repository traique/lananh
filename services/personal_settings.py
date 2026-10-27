"""Khoá bảng settings chứa dữ liệu RIÊNG của từng người (theo internal user id).

Gom về 1 chỗ để channels/zalo_users.py xoá đủ khi /zaloxoa (thành phố là dữ
liệu cá nhân) mà không phải import các service nặng. Telegram và Zoom của chủ
bot dùng chung ALLOWED_USER_ID nên chung thành phố; Zalo (kể cả Zalo của chính
chủ bot) mang id âm riêng nên có bộ khoá riêng - đúng quyết định tách ngữ cảnh
Zalo khỏi Telegram, không đồng bộ qua lại.
"""

from __future__ import annotations

BRIEF_PREFIX = "brief:"
BRIEF_SENT_PREFIX = "brief_sent:"
WEATHER_HOME_PREFIX = "weather_home:"
# Trí nhớ hội thoại (services/memory_service.py): watermark id tin đã trích nhớ,
# danh sách "đừng học lại" sau /quen, ngày gộp fact gần nhất.
MEMORY_WATERMARK_PREFIX = "memory_wm:"
MEMORY_FORGET_PREFIX = "memory_forget:"
MEMORY_CONSOLIDATED_PREFIX = "memory_consolidated:"
# Hỏi thăm chủ động (services/proactive_service.py): bật/tắt, số tin đã gửi trong ngày.
FOLLOWUP_PREFIX = "followup:"
FOLLOWUP_SENT_PREFIX = "followup_sent:"
# Giờ yên lặng riêng (services/notify_hub.py, lệnh /yenlang): "on" | "off" | "HH:MM-HH:MM".
QUIET_PREFIX = "quiet:"
# "Cách trả lời" bộ trích nhớ đề xuất, chờ người dùng duyệt (services/style_approval.py).
STYLE_PENDING_PREFIX = "style_pending:"
# Trả lời kèm giọng nói (services/voice_reply.py, lệnh /noi): "on" | "off" | "auto".
VOICE_REPLY_PREFIX = "voice_reply:"
# Giọng đọc riêng (/noi giong <tên>): tên giọng Gemini TTS, trống = mặc định của bot.
VOICE_NAME_PREFIX = "voice_name:"


def weather_home_key(user_id: int) -> str:
    return f"{WEATHER_HOME_PREFIX}{user_id}"


def brief_key(channel: str, user_id: int) -> str:
    """Cấu hình bản tin sáng (JSON) của 1 người trên 1 kênh."""
    return f"{BRIEF_PREFIX}{channel}:{user_id}"


def brief_sent_key(channel: str, user_id: int) -> str:
    """Ngày (ISO) đã gửi bản tin sáng gần nhất - chống gửi 2 lần/ngày."""
    return f"{BRIEF_SENT_PREFIX}{channel}:{user_id}"


def memory_watermark_key(user_id: int) -> str:
    return f"{MEMORY_WATERMARK_PREFIX}{user_id}"


def memory_forget_key(user_id: int) -> str:
    return f"{MEMORY_FORGET_PREFIX}{user_id}"


def memory_consolidated_key(user_id: int) -> str:
    return f"{MEMORY_CONSOLIDATED_PREFIX}{user_id}"


def followup_key(user_id: int) -> str:
    return f"{FOLLOWUP_PREFIX}{user_id}"


def followup_sent_key(user_id: int) -> str:
    return f"{FOLLOWUP_SENT_PREFIX}{user_id}"


def quiet_key(user_id: int) -> str:
    return f"{QUIET_PREFIX}{user_id}"


def style_pending_key(user_id: int) -> str:
    return f"{STYLE_PENDING_PREFIX}{user_id}"


def voice_reply_key(user_id: int) -> str:
    return f"{VOICE_REPLY_PREFIX}{user_id}"


def voice_name_key(user_id: int) -> str:
    return f"{VOICE_NAME_PREFIX}{user_id}"


def memory_user_keys(user_id: int) -> list[str]:
    """Khoá trí nhớ/hỏi thăm của 1 người (dùng chung cho /zaloxoa và /xoadulieu)."""
    return [
        memory_watermark_key(user_id),
        memory_forget_key(user_id),
        memory_consolidated_key(user_id),
        followup_key(user_id),
        followup_sent_key(user_id),
        style_pending_key(user_id),
    ]


def zalo_user_keys(user_id: int) -> list[str]:
    """Mọi khoá cá nhân của 1 Zalo user (xoá cùng pairing)."""
    return [
        weather_home_key(user_id),
        brief_key("zalo", user_id),
        brief_sent_key("zalo", user_id),
        *memory_user_keys(user_id),
        quiet_key(user_id),
        voice_reply_key(user_id),
        voice_name_key(user_id),
    ]
