"""Encrypted Shopee Affiliate browser session persistence.

Render Free has an ephemeral filesystem, so browser storage state must live in
PostgreSQL instead of a Chromium profile directory. Values are Fernet-encrypted
with the existing SETTINGS_ENC_KEY before they enter the generic settings table.
"""
from __future__ import annotations

import json
from typing import Any

from core import crypto, database as db
from core.repositories import settings as settings_repository

_SESSION_KEY = "shopee:affiliate:storage_state:v1"


def _validate_storage_state(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Shopee storage_state phải là JSON object.")
    cookies = value.get("cookies", [])
    origins = value.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise ValueError("storage_state không đúng định dạng Playwright.")
    # Avoid accidentally persisting an arbitrarily large request body in settings.
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 2_000_000:
        raise ValueError("storage_state quá lớn (giới hạn 2 MB).")
    return value


async def load() -> dict | None:
    pool = await db.get_pool()
    raw = crypto.decrypt(await settings_repository.get(pool, _SESSION_KEY))
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return _validate_storage_state(value)
    except (TypeError, json.JSONDecodeError, ValueError):
        return None


async def save(value: dict) -> None:
    state = _validate_storage_state(value)
    encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    await settings_repository.set(
        await db.get_pool(),
        _SESSION_KEY,
        crypto.encrypt(encoded),
    )


async def clear() -> None:
    await settings_repository.set(await db.get_pool(), _SESSION_KEY, "")


async def status() -> dict:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT value, updated_at FROM settings WHERE key = $1",
        _SESSION_KEY,
    )
    configured = bool(row and row["value"])
    return {
        "configured": configured,
        "updated_at": row["updated_at"].isoformat() if configured and row["updated_at"] else None,
    }
