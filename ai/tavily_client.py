"""Tavily web search - grounding cho chat khi bật qua /tavily on.

Không thuộc provider-chain. Module giữ cả structured result để caller đánh
giá chất lượng trước khi quyết định dùng/fallback, đồng thời giữ ``search()``
trả ``str`` để tương thích các call site cũ.
"""
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx

from ai import provider_overrides
from core import config, database as db

logger = logging.getLogger(__name__)

_SETTING_ENABLED = "tavily_enabled"
_DEFAULT_COUNTRY = "vietnam"
_DEFAULT_LANGUAGE = "vi"

_client: Optional[httpx.AsyncClient] = None


class TavilyError(RuntimeError):
    """Lỗi khi gọi Tavily (chưa cấu hình key, HTTP lỗi, payload rỗng)."""


@dataclass(frozen=True)
class TavilyResult:
    title: str
    url: str
    content: str
    score: Optional[float] = None
    published_date: Optional[str] = None

    @property
    def domain(self) -> str:
        return _domain_of(self.url)


@dataclass(frozen=True)
class TavilySearchResponse:
    query: str
    answer: str
    results: tuple[TavilyResult, ...]

    @property
    def unique_domains(self) -> set[str]:
        return {item.domain for item in self.results if item.url}


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


# Chỉ dẫn gắn kèm mọi grounding Tavily đưa vào LLM (chat, /gia, /agent...):
# ép model trích dẫn theo số [n] khớp danh sách kết quả bên dưới và giữ
# nguyên số liệu/ngày tháng - adapt ý tưởng citation + "numerical data
# integrity" của dự án Vane (ItzCrazyKns/Vane, MIT License), diễn đạt lại
# bằng tiếng Việt cho phù hợp giọng bot.
_GROUNDING_USAGE_NOTE = (
    "Khi dùng các kết quả bên dưới để trả lời, hãy trích dẫn nguồn bằng số "
    "thứ tự [n] khớp với danh sách (vd: giá tăng 5%[1], theo VnExpress[2]). "
    "Giữ NGUYÊN số liệu, ngày tháng, tỷ lệ % xuất hiện trong kết quả - "
    "KHÔNG làm tròn, KHÔNG khái quát hoá hay suy diễn số khác với nguồn."
)


def format_search_results(response: TavilySearchResponse) -> str:
    lines = [f"[Kết quả tìm kiếm web (Tavily) cho: {response.query}]", _GROUNDING_USAGE_NOTE]
    if response.answer:
        lines.append(f"Tóm tắt: {response.answer}")
    for i, item in enumerate(response.results, start=1):
        title = item.title or item.url or "?"
        lines.append(f"{i}. {title} ({item.url})\n{item.content}")
    return "\n\n".join(lines)


async def search_results(
    query: str,
    max_results: int = 0,
    *,
    search_depth: str = "basic",
    max_results_per_domain: Optional[int] = None,
    country: Optional[str] = _DEFAULT_COUNTRY,
    language: Optional[str] = _DEFAULT_LANGUAGE,
) -> TavilySearchResponse:
    """Tra Tavily và giữ structured result để caller đánh giá chất lượng.

    ``country`` và ``language`` là ranking boost, không hard-filter. Mặc định
    ưu tiên Việt Nam + tiếng Việt vì bot phục vụ truy vấn tiếng Việt; caller
    vẫn có thể truyền ``None`` khi muốn search toàn cầu không localization.
    """
    api_key = await _api_key()
    if not api_key:
        raise TavilyError("Chưa cấu hình TAVILY_API_KEY")

    payload = {
        "query": query,
        "max_results": max_results or config.TAVILY_MAX_RESULTS,
        "include_answer": True,
        "search_depth": search_depth,
    }
    if country:
        payload["country"] = country
    if language:
        payload["language"] = language
        payload["filter_by_language"] = False

    response = await _get_client().post(
        f"{config.TAVILY_BASE_URL}/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
    )
    if response.status_code != 200:
        raise TavilyError(f"Tavily trả lỗi HTTP {response.status_code}: {response.text[:250]}")

    data = response.json()
    raw_results = data.get("results") or []
    if not raw_results and not data.get("answer"):
        raise TavilyError("Tavily không trả về kết quả nào")

    results: list[TavilyResult] = []
    domain_count: dict[str, int] = {}
    for item in raw_results:
        url = str(item.get("url") or "")
        domain = _domain_of(url)
        if max_results_per_domain is not None:
            if domain_count.get(domain, 0) >= max_results_per_domain:
                continue
            domain_count[domain] = domain_count.get(domain, 0) + 1
        results.append(
            TavilyResult(
                title=str(item.get("title") or ""),
                url=url,
                content=str(item.get("content") or ""),
                score=item.get("score"),
                published_date=item.get("published_date"),
            )
        )

    try:
        await db.record_provider_call("tavily", search_depth)
    except Exception:
        logger.warning("Không ghi được lượt gọi Tavily vào DB.", exc_info=True)

    return TavilySearchResponse(
        query=str(data.get("query") or query),
        answer=str(data.get("answer") or ""),
        results=tuple(results),
    )


async def search(
    query: str,
    max_results: int = 0,
    *,
    search_depth: str = "basic",
    max_results_per_domain: Optional[int] = None,
    country: Optional[str] = _DEFAULT_COUNTRY,
    language: Optional[str] = _DEFAULT_LANGUAGE,
) -> str:
    """Compatibility wrapper: tra Tavily rồi format thành grounding text."""
    response = await search_results(
        query,
        max_results,
        search_depth=search_depth,
        max_results_per_domain=max_results_per_domain,
        country=country,
        language=language,
    )
    return format_search_results(response)
