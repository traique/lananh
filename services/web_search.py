"""Web-search capability dùng chung cho chat và /agent.

Tavily là backend discovery đầu tiên. Structured result được quality-gate;
khi Tavily lỗi hoặc nguồn quá mỏng, provider-chain real-search (Groq
Compound -> Google Search grounding) làm fallback. Không thêm search engine
hay dependency mới.
"""
import logging
from dataclasses import dataclass

from ai import orchestrator, tavily_client

logger = logging.getLogger(__name__)

_FALLBACK_PROVIDERS = ["groq", "api1", "api2"]


class WebSearchError(RuntimeError):
    """Không backend search nào trả được grounding dùng được."""


@dataclass(frozen=True)
class SearchGrounding:
    text: str
    provider: str
    tavily_quality_ok: bool


def looks_like_search_question(text: str) -> bool:
    """Gate search chủ động để chat ngắn/chuyện phiếm không đốt quota."""
    lower = (text or "").lower()
    if len(lower.split()) <= 3 and "?" not in lower:
        return False
    markers = (
        "?",
        "bao nhiêu",
        "bao giờ",
        "thế nào",
        "như thế nào",
        "là gì",
        "ở đâu",
        "khi nào",
        "vì sao",
        "tại sao",
        "giá",
        "tỷ giá",
        "giá vàng",
        "bitcoin",
        "crypto",
        "tin",
        "mới nhất",
        "hiện tại",
        "hôm nay",
        "tuần này",
        "check",
        "tra giúp",
        "tìm giúp",
        "search",
    )
    return any(marker in lower for marker in markers)


def is_tavily_quality_sufficient(
    response: tavily_client.TavilySearchResponse,
    *,
    min_results: int = 2,
    min_domains: int = 2,
) -> bool:
    usable_results = [item for item in response.results if item.url and item.content.strip()]
    domains = {item.domain for item in usable_results}
    return len(usable_results) >= min_results and len(domains) >= min_domains


async def search_web(
    query: str,
    *,
    max_results: int = 0,
    search_depth: str = "basic",
    max_results_per_domain: int | None = None,
    min_results: int = 2,
    min_domains: int = 2,
) -> SearchGrounding:
    """Search Tavily trước, fallback real-search khi kết quả không đủ đa dạng."""
    tavily_response: tavily_client.TavilySearchResponse | None = None
    tavily_error: BaseException | None = None
    try:
        tavily_response = await tavily_client.search_results(
            query,
            max_results,
            search_depth=search_depth,
            max_results_per_domain=max_results_per_domain,
        )
        if is_tavily_quality_sufficient(
            tavily_response, min_results=min_results, min_domains=min_domains
        ):
            return SearchGrounding(
                tavily_client.format_search_results(tavily_response),
                "tavily",
                True,
            )
        logger.info(
            "Tavily trả kết quả mỏng cho %r (%d results/%d domains), thử grounded fallback.",
            query,
            len(tavily_response.results),
            len(tavily_response.unique_domains),
        )
    except tavily_client.TavilyError as exc:
        tavily_error = exc
        logger.warning("Tavily search lỗi cho %r, thử grounded fallback: %s", query, exc)

    try:
        response = await orchestrator.ask(
            (
                "Tra web để trả lời truy vấn sau. Chỉ nêu thông tin tìm được từ web, "
                "giữ URL/tên nguồn cụ thể trong câu trả lời để làm grounding cho bước sau.\n\n"
                f"Truy vấn: {query}"
            ),
            enable_search=True,
            require_real_search=True,
            providers_override=_FALLBACK_PROVIDERS,
        )
        fallback_text = (response.text or "").strip()
        if fallback_text:
            return SearchGrounding(
                f"[Kết quả tìm kiếm web dự phòng]\n{fallback_text}",
                "fallback",
                False,
            )
    except Exception:
        logger.warning("Grounded search fallback lỗi cho %r.", query, exc_info=True)

    if tavily_response is not None:
        return SearchGrounding(
            tavily_client.format_search_results(tavily_response),
            "tavily-low-quality",
            False,
        )
    raise WebSearchError(str(tavily_error or "Không backend search nào trả kết quả"))


async def maybe_search(text: str) -> str:
    """Grounding cho chat thường; tôn trọng toggle /tavily và intent gate."""
    if not await tavily_client.get_enabled() or not looks_like_search_question(text):
        return ""
    try:
        return (await search_web(text)).text
    except Exception:
        logger.warning("Web search lỗi, bỏ qua grounding.", exc_info=True)
        return ""
