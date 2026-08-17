"""Encrypted persistence for the personal Zalo session and controller pairing."""
import json
from core import crypto, database as db
from core.repositories import settings as settings_repository

_SESSION_KEY = "zalo:session:v1"
_CONTROLLER_KEY = "zalo:controller:v1"

async def load_session() -> dict | None:
    pool = await db.get_pool()
    raw = crypto.decrypt(await settings_repository.get(pool, _SESSION_KEY))
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, json.JSONDecodeError):
        return None

async def save_session(value: dict) -> None:
    pool = await db.get_pool()
    await settings_repository.set(
        pool,
        _SESSION_KEY,
        crypto.encrypt(json.dumps(value, separators=(",", ":"))),
    )

async def clear_session() -> None:
    await settings_repository.set(await db.get_pool(), _SESSION_KEY, "")

async def load_controller() -> str:
    raw = await settings_repository.get(await db.get_pool(), _CONTROLLER_KEY)
    return (crypto.decrypt(raw) or "").strip()

async def save_controller(controller_id: str) -> None:
    await settings_repository.set(
        await db.get_pool(), _CONTROLLER_KEY, crypto.encrypt(controller_id.strip())
    )

async def clear_controller() -> None:
    await settings_repository.set(await db.get_pool(), _CONTROLLER_KEY, "")
