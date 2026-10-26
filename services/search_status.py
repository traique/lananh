"""Mô tả chế độ tra web đang chạy cho /status và /tavily (Telegram, Zalo, Zoom).

- Tavily BẬT (có key): tra Tavily trước; Tavily lỗi hoặc ít nguồn thì provider
  đang trả lời tự tra web (ai.orchestrator chế độ "native").
- Tavily TẮT: câu cần thông tin mới dùng web search của chính provider đang trả
  lời (Gemini Google Search, Groq browser_search, OpenRouter/9Router nếu bật).
"""

from __future__ import annotations

import logging

from ai import orchestrator, tavily_client

logger = logging.getLogger(__name__)


async def search_mode_line() -> str:
    """1-2 dòng chữ thường (không HTML) mô tả chế độ tra web hiện tại. Không
    raise: đọc cấu hình lỗi (DB chập chờn) thì trả 1 dòng báo lỗi để /status
    vẫn trả lời được."""
    try:
        return await _search_mode_line()
    except Exception:
        logger.warning("Không đọc được cấu hình tra web cho /status.", exc_info=True)
        return "🔎 Tra web: chưa đọc được cấu hình lúc này (DB lỗi)."


async def _search_mode_line() -> str:
    tavily_on = await tavily_client.get_enabled()
    has_key = bool(await tavily_client.api_key())
    providers = await orchestrator.native_search_providers()
    labels = ", ".join(orchestrator.PROVIDER_LABELS.get(p, p) for p in providers)
    provider_part = (
        f"web search của provider ({labels})"
        if labels
        else "trả lời kèm lưu ý chưa tra được (chưa provider nào tự tra web được)"
    )
    if tavily_on and has_key:
        return f"🔎 Tra web: Tavily — Tavily lỗi hoặc ít nguồn thì dùng {provider_part}."
    note = " Tavily đang bật nhưng chưa có TAVILY_API_KEY." if tavily_on else ""
    if not labels:
        return (
            "🔎 Tra web: chưa provider nào tự tra web được (cần key Gemini API 1/2 hoặc "
            "Groq + GROQ_WEB_SEARCH) — câu cần tin mới sẽ được trả lời kèm lưu ý chưa "
            f"tra được.{note}"
        )
    return f"🔎 Tra web: web search của chính provider đang trả lời ({labels}).{note}"
