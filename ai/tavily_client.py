"""Tavily web search - dùng làm grounding cho chat khi bật qua /tavily on.

Không thuộc provider-chain (router9/groq/openrouter/api1/api2, xem
ai/orchestrator.py) - chỉ tra web rồi chèn kết quả vào prompt trước khi gọi
provider hiện hành, tương tự cách services/tools.py chèn kết quả tool.
"""
import logging
from typing import Optional

import httpx

from core import config, database as db

logger = logging.getLogger(__name__)

_SETTING_ENABLED = "tavily_enabled"

_client: Optional[httpx.AsyncClient] = None


class TavilyError(RuntimeError):
    """Lỗi khi gọi Tavily (chưa cấu hình key, HTTP lỗi, payload rỗng)."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=config.TAVILY_CALL_TIMEOUT_SEC)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def get_enabled() -> bool:
    return await db.get_setting(_SETTING_ENABLED) == "1"


async def set_enabled(enabled: bool) -> None:
    await db.set_setting(_SETTING_ENABLED, "1" if enabled else "0")
    logger.info("Tavily search %s.", "bật" if enabled else "tắt")


async def search(query: str, max_results: int = 0) -> str:
    """Tra Tavily, trả về text đã format để chèn làm grounding. Raise
    TavilyError nếu chưa cấu hình TAVILY_API_KEY hoặc gọi lỗi."""
    if not config.TAVILY_API_KEY:
        raise TavilyError("Chưa cấu hình TAVILY_API_KEY")

    response = await _get_client().post(
        f"{config.TAVILY_BASE_URL}/search",
        headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"},
        json={
            "query": query,
            "max_results": max_results or config.TAVILY_MAX_RESULTS,
            "include_answer": True,
        },
    )
    if response.status_code != 200:
        raise TavilyError(f"Tavily trả lỗi HTTP {response.status_code}: {response.text[:250]}")

    data = response.json()
    results = data.get("results") or []
    if not results and not data.get("answer"):
        raise TavilyError("Tavily không trả về kết quả nào")

    lines = [f"[Kết quả tìm kiếm web (Tavily) cho: {query}]"]
    if data.get("answer"):
        lines.append(f"Tóm tắt: {data['answer']}")
    for i, item in enumerate(results, start=1):
        title = item.get("title") or item.get("url") or "?"
        lines.append(f"{i}. {title} ({item.get('url', '')})\n{item.get('content', '')}")
    return "\n\n".join(lines)
