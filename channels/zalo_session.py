"""Encrypted persistence for the personal Zalo session and controller pairing.

Đọc/ghi qua core.settings_cache (db.get_setting/set_setting đã tự nối lại kết nối):
session và controller được đọc ở mỗi tin Zalo và mỗi lượt job nền (tổng kết nhóm, quét
tín hiệu...) nhưng chỉ đổi khi gateway đăng nhập lại - cache giữ bản ĐÃ MÃ HOÁ, giải
mã mỗi lần đọc.
"""

import json

from core import crypto, settings_cache

_SESSION_KEY = "zalo:session:v1"
_CONTROLLER_KEY = "zalo:controller:v1"


async def load_session() -> dict | None:
    raw = crypto.decrypt(await settings_cache.get(_SESSION_KEY))
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, json.JSONDecodeError):
        return None


async def load_account_id() -> str:
    session = await load_session()
    if not session:
        return ""
    return str(session.get("accountId") or session.get("account_id") or "").strip()


async def save_session(value: dict) -> None:
    await settings_cache.set(_SESSION_KEY, crypto.encrypt(json.dumps(value, separators=(",", ":"))))


async def clear_session() -> None:
    await settings_cache.set(_SESSION_KEY, "")


async def load_controller() -> str:
    raw = await settings_cache.get(_CONTROLLER_KEY)
    return (crypto.decrypt(raw) or "").strip()


async def save_controller(controller_id: str) -> None:
    await settings_cache.set(_CONTROLLER_KEY, crypto.encrypt(controller_id.strip()))


async def clear_controller() -> None:
    await settings_cache.set(_CONTROLLER_KEY, "")
