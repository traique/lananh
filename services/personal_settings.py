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


def weather_home_key(user_id: int) -> str:
    return f"{WEATHER_HOME_PREFIX}{user_id}"


def brief_key(channel: str, user_id: int) -> str:
    """Cấu hình bản tin sáng (JSON) của 1 người trên 1 kênh."""
    return f"{BRIEF_PREFIX}{channel}:{user_id}"


def brief_sent_key(channel: str, user_id: int) -> str:
    """Ngày (ISO) đã gửi bản tin sáng gần nhất - chống gửi 2 lần/ngày."""
    return f"{BRIEF_SENT_PREFIX}{channel}:{user_id}"


def zalo_user_keys(user_id: int) -> list[str]:
    """Mọi khoá cá nhân của 1 Zalo user (xoá cùng pairing)."""
    return [
        weather_home_key(user_id),
        brief_key("zalo", user_id),
        brief_sent_key("zalo", user_id),
    ]
