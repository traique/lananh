"""Danh sách địa điểm hay xem ở trang /thoi-tiet, chủ bot thêm/xoá trên web.

Lưu 1 dòng JSON trong bảng settings (không cần migration), cache trong RAM
(1 worker) nên mỗi lượt xem trang không đọc DB. Có trần số mục và độ dài tên
(RAM 512 MB). Đọc DB lỗi thì dùng danh sách mặc định, ghi lỗi thì báo lại.
"""

from __future__ import annotations

import asyncio
import json
import logging

from core import database as db
from core.text_normalize import fold

logger = logging.getLogger(__name__)

SETTING_KEY = "web_weather_places"
DEFAULT_PLACES = [
    "TP.HCM",
    "Hà Nội",
    "Đà Nẵng",
    "Huế",
    "Nha Trang",
    "Đà Lạt",
    "Cần Thơ",
    "Hải Phòng",
]
MAX_PLACES = 16
MAX_NAME_LEN = 40


class PlacesError(ValueError):
    """Tên không hợp lệ ("ten"), danh sách đầy ("day") hoặc lỗi DB ("luu")."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_cache: list[str] | None = None
_lock = asyncio.Lock()


def clean_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name or len(name) > MAX_NAME_LEN or any(ch in name for ch in "<>\"'`\\/{}"):
        raise PlacesError("ten", f"Tên địa điểm 1-{MAX_NAME_LEN} ký tự, không chứa ký tự đặc biệt.")
    return name


async def _load() -> list[str] | None:
    """Danh sách hiện tại (gọi khi đã giữ _lock). None nếu đọc DB lỗi."""
    global _cache
    if _cache is None:
        try:
            raw = await db.get_setting(SETTING_KEY)
            loaded = json.loads(raw) if raw else None
        except Exception:
            logger.warning("Không đọc được địa điểm thời tiết.", exc_info=True)
            return None  # không cache: lần sau thử đọc lại
        if isinstance(loaded, list):
            _cache = [p for p in loaded if isinstance(p, str)][:MAX_PLACES]
        else:
            _cache = list(DEFAULT_PLACES)
    return list(_cache)


async def get() -> list[str]:
    if _cache is not None:
        return list(_cache)
    async with _lock:
        places = await _load()
    return list(DEFAULT_PLACES) if places is None else places


async def _save(places: list[str]) -> None:
    global _cache
    try:
        await db.set_setting(SETTING_KEY, json.dumps(places, ensure_ascii=False))
    except Exception as exc:
        logger.warning("Không lưu được địa điểm thời tiết.", exc_info=True)
        raise PlacesError("luu", "Không lưu được, thử lại sau.") from exc
    _cache = places


async def add(raw: str) -> list[str]:
    name = clean_name(raw)
    async with _lock:
        places = await _load()
        if places is None:
            # Đọc lỗi thì không ghi: tránh đè danh sách thật bằng danh sách mặc định.
            raise PlacesError("luu", "Không đọc được danh sách hiện tại, thử lại sau.")
        if any(fold(p) == fold(name) for p in places):
            return places
        if len(places) >= MAX_PLACES:
            raise PlacesError("day", f"Tối đa {MAX_PLACES} địa điểm, xoá bớt trước khi thêm.")
        places.append(name)
        await _save(places)
    return list(places)


async def remove(raw: str) -> list[str]:
    target = fold(" ".join(raw.split()))
    async with _lock:
        places = await _load()
        if places is None:
            raise PlacesError("luu", "Không đọc được danh sách hiện tại, thử lại sau.")
        kept = [p for p in places if fold(p) != target]
        if len(kept) != len(places):
            await _save(kept)
    return list(kept)
