"""Hỏi thăm chủ động dựa trên trí nhớ (bản đầy đủ ở commit sau của gói)."""

from __future__ import annotations


async def events_enabled(user_id: int) -> bool:
    """Có trích sự kiện hỏi thăm cho người này không."""
    return False


async def add_extracted_events(user_id: int, events: list, user_rows: list[dict]) -> int:
    """Lưu sự kiện bộ trích xuất trả về; trả số sự kiện mới."""
    return 0
