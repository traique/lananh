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
# ép giữ nguyên số liệu/ngày tháng, và tránh kiểu văn phong "theo kết quả tìm
# kiếm/kết quả anh gửi" - đọc gượng gạo, lộ rõ đây là raw search dump thay vì
# câu trả lời tự nhiên.
_GROUNDING_USAGE_NOTE = (
    "Dùng thông tin bên dưới để trả lời tự nhiên như thể tự biết, KHÔNG kể "
    "lể quá trình tìm kiếm (không viết \"theo kết quả tìm kiếm\", \"theo các "
    "kết quả anh gửi\", \"nguồn tìm được\" hay tương tự), KHÔNG chèn số thứ tự "
    "trích dẫn kiểu [1][2]. Nếu cần nêu nguồn thì nói tên nguồn tự nhiên "
    "trong câu (vd: theo VnExpress, theo Reuters). Giữ NGUYÊN số liệu, ngày "
    "tháng, tỷ lệ % xuất hiện trong kết quả - KHÔNG làm tròn, KHÔNG khái "
    "quát hoá hay suy diễn số khác với nguồn. Ưu tiên kết quả có ngày đăng "
    "gần ngày hiện tại nhất; kết quả nào ghi ngày đăng đã cũ (vài tháng "
    "trở lên) thì coi là thông tin nền, không phải tin mới nhất."
)


def format_search_results(response: TavilySearchResponse) -> str:
    lines = [f"[Kết quả tìm kiếm web (Tavily) cho: {response.query}]", _GROUNDING_USAGE_NOTE]
    if response.answer:
        lines.append(f"Tóm tắt: {response.answer}")
    for i, item in enumerate(response.results, start=1):
        title = item.title or item.url or "?"
        date_suffix = f" - đăng {item.published_date}" if item.published_date else ""
        lines.append(f"{i}. {title}{date_suffix} ({item.url})\n{item.content}")
    return "\n\n".join(lines)


async def search_results(
    query: str,
    max_results: int = 0,
    *,
    search_depth: str = "basic",
    max_results_per_domain: Optional[int] = None,
    country: Optional[str] = _DEFAULT_COUNTRY,
    language: Optional[str] = _DEFAULT_LANGUAGE,
    topic: Optional[str] = None,
    time_range: Optional[str] = None,
) -> TavilySearchResponse:
    """Tra Tavily và giữ structured result để caller đánh giá chất lượng.

    ``country`` và ``language`` là ranking boost, không hard-filter. Mặc định
    ưu tiên Việt Nam + tiếng Việt vì bot phục vụ truy vấn tiếng Việt; caller
    vẫn có thể truyền ``None`` khi muốn search toàn cầu không localization.

    ``topic="news"`` chuyển Tavily sang index tin tức (ưu tiên bài mới, có
    ngày đăng) thay vì index trang web chung chung. ``time_range`` giới hạn
    kết quả trong khoảng gần đây - 1 trong "day"/"week"/"month"/"year" - dùng
    cho câu hỏi kiểu "tin mới nhất/hôm nay" để tránh Tavily trả bài cũ vẫn
    còn xếp hạng cao do nhiều backlink/traffic.
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
    if topic:
        payload["topic"] = topic
    if time_range:
        payload["time_range"] = time_range

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
    topic: Optional[str] = None,
    time_range: Optional[str] = None,
) -> str:
    """Compatibility wrapper: tra Tavily rồi format thành grounding text."""
    response = await search_results(
        query,
        max_results,
        search_depth=search_depth,
        max_results_per_domain=max_results_per_domain,
        country=country,
        language=language,
        topic=topic,
        time_range=time_range,
    )
    return format_search_results(response)
