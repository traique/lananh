"""Tavily web search - dùng làm grounding cho chat khi bật qua /tavily on.

Không thuộc provider-chain (router9/groq/openrouter/api1/api2, xem
ai/orchestrator.py) - chỉ tra web rồi chèn kết quả vào prompt trước khi gọi
provider hiện hành, tương tự cách services/tools.py chèn kết quả tool.
"""
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

from ai import provider_overrides
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


async def _api_key() -> str:
    return await provider_overrides.get_api_key_override("tavily") or config.TAVILY_API_KEY


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except Exception:
        return url


async def search(
    query: str,
    max_results: int = 0,
    *,
    search_depth: str = "basic",
    max_results_per_domain: Optional[int] = None,
) -> str:
    """Tra Tavily, trả về text đã format để chèn làm grounding.

    ``search_depth``: "basic" (1 credit/request, mặc định) hoặc "advanced"
    (2 credit/request, kết quả sâu/đa dạng nguồn hơn - xem
    https://docs.tavily.com/documentation/api-credits).

    ``max_results_per_domain``: nếu đặt, chỉ giữ tối đa N kết quả / domain
    (vd 2) - phòng trường hợp 1 domain SEO mạnh chiếm hết top-N kết quả tìm
    kiếm tự nhiên (vd nhiều trang biến thể sản phẩm của CÙNG 1 shop), khiến
    caller (vd /gia, xem handlers/commands.py::_search_price) không có đủ
    nguồn từ NHIỀU shop khác nhau để so sánh dù chỉ tốn 1 request. None
    (mặc định) = giữ nguyên, không lọc - dùng cho các trường hợp chat
    grounding thông thường không cần đa dạng domain.

    Raise TavilyError nếu chưa cấu hình TAVILY_API_KEY hoặc gọi lỗi.
    """
    api_key = await _api_key()
    if not api_key:
        raise TavilyError("Chưa cấu hình TAVILY_API_KEY")

    response = await _get_client().post(
        f"{config.TAVILY_BASE_URL}/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "query": query,
            "max_results": max_results or config.TAVILY_MAX_RESULTS,
            "include_answer": True,
            "search_depth": search_depth,
        },
    )
    if response.status_code != 200:
        raise TavilyError(f"Tavily trả lỗi HTTP {response.status_code}: {response.text[:250]}")

    data = response.json()
    results = data.get("results") or []
    if not results and not data.get("answer"):
        raise TavilyError("Tavily không trả về kết quả nào")

    if max_results_per_domain is not None:
        kept = []
        domain_count: dict[str, int] = {}
        for item in results:
            domain = _domain_of(item.get("url", ""))
            if domain_count.get(domain, 0) >= max_results_per_domain:
                continue
            domain_count[domain] = domain_count.get(domain, 0) + 1
            kept.append(item)
        results = kept

    lines = [f"[Kết quả tìm kiếm web (Tavily) cho: {query}]"]
    if data.get("answer"):
        lines.append(f"Tóm tắt: {data['answer']}")
    for i, item in enumerate(results, start=1):
        title = item.get("title") or item.get("url") or "?"
        lines.append(f"{i}. {title} ({item.get('url', '')})\n{item.get('content', '')}")

    try:
        await db.record_provider_call("tavily", search_depth)
    except Exception:
        logger.warning("Không ghi được lượt gọi Tavily vào DB.", exc_info=True)

    return "\n\n".join(lines)
