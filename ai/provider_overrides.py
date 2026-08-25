"""Override model/api_key/enabled theo provider (groq|openrouter|api1|api2;
router9 model đã có sẵn cơ chế riêng - xem router9_client.get_preferred_model_name),
lưu bảng settings qua core.database, cache RAM lười (nạp 1 lần/khoá, cập nhật
lại ngay khi ghi). api_key mã hoá bằng core.crypto trước khi lưu, cùng cơ chế
với channels/zalo_session.py. Dùng bởi ai/*_client.py và trang admin (web.py).
"""
from typing import Optional

from core import crypto
from core import database as db

PROVIDERS = ("router9", "groq", "openrouter", "api1", "api2")
MODEL_OVERRIDABLE = ("groq", "openrouter", "api1", "api2")
ENABLE_OVERRIDABLE = ("groq", "openrouter", "api1", "api2")  # router9: ai/provider_state.py

_model_cache: dict[str, Optional[str]] = {}
_api_key_cache: dict[str, Optional[str]] = {}
_enabled_cache: dict[str, bool] = {}


async def get_model_override(provider: str) -> Optional[str]:
    if provider not in _model_cache:
        _model_cache[provider] = await db.get_setting(f"provider_model_{provider}") or None
    return _model_cache[provider]


async def set_model_override(provider: str, model: Optional[str]) -> None:
    model = (model or "").strip()
    _model_cache[provider] = model or None
    await db.set_setting(f"provider_model_{provider}", model)


async def get_api_key_override(provider: str) -> Optional[str]:
    if provider not in _api_key_cache:
        raw = await db.get_setting(f"provider_api_key_{provider}")
        _api_key_cache[provider] = crypto.decrypt(raw) if raw else None
    return _api_key_cache[provider]


async def set_api_key_override(provider: str, api_key: Optional[str]) -> None:
    api_key = (api_key or "").strip()
    _api_key_cache[provider] = api_key or None
    await db.set_setting(f"provider_api_key_{provider}", crypto.encrypt(api_key) if api_key else "")


async def is_enabled(provider: str) -> bool:
    if provider not in ENABLE_OVERRIDABLE:
        return True
    if provider not in _enabled_cache:
        raw = await db.get_setting(f"provider_enabled_{provider}")
        _enabled_cache[provider] = raw != "0"
    return _enabled_cache[provider]


async def set_enabled(provider: str, enabled: bool) -> None:
    _enabled_cache[provider] = enabled
    await db.set_setting(f"provider_enabled_{provider}", "1" if enabled else "0")
