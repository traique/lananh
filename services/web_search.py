"""Web-search capability dùng chung cho chat và /agent.

Tavily là backend discovery đầu tiên. Structured result được quality-gate;
khi Tavily lỗi hoặc nguồn quá mỏng, provider-chain real-search (Groq
Compound -> Google Search grounding) làm fallback. Không thêm search engine
hay dependency mới.

Có 2 lớp tăng chất lượng adapt từ dự án Vane (ItzCrazyKns/Vane, MIT
License), diễn đạt lại bằng tiếng Việt và tối giản để phù hợp Render free
tier (không thêm embedding model, không thêm HTTP dependency mới):

1. Viết lại câu hỏi thành truy vấn độc lập (standalone) + tối đa 2 biến thể
   trước khi search (``_rewrite_and_expand_query``), rồi search song song
   nhiều query 1 lượt (multi-query, xem tham số ``extra_queries`` của
   ``search_web``) - chỉ chạy khi có lịch sử hội thoại (``user_id`` truyền
   vào ``maybe_search``), vì mới cần ngữ cảnh để viết lại.
2. Đọc sâu 1-2 kết quả điểm cao nhất qua ``services/web_reader`` (Jina
   Reader có sẵn) khi câu hỏi cần số liệu chính xác (giá, tin tức...) - xem
   ``should_deep_read``/``_deep_read_top_results`` - vì snippet Tavily
   thường quá ngắn để có số liệu cụ thể.
"""
import asyncio
import dataclasses
import logging
from dataclasses import dataclass

from ai import orchestrator, tavily_client
from core import config, database as db

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


# Subset các marker của looks_like_search_question mà kết quả THƯỜNG cần số
# liệu/thông tin cụ thể (giá cả, tin tức) - snippet Tavily hay quá ngắn cho
# các loại câu hỏi này, nên đáng bỏ thêm 1-2 lượt scrape+extract.
_DEEP_READ_TRIGGER_MARKERS = (
    "giá",
    "tỷ giá",
    "giá vàng",
    "bitcoin",
    "crypto",
    "tin",
    "mới nhất",
)
_DEEP_READ_MAX_PAGES = 2
_DEEP_READ_MAX_CHARS = 6000

_QUERY_REWRITE_PROMPT = """Dựa vào đoạn hội thoại gần đây và câu hỏi mới nhất của người dùng bên dưới, hãy:
1. Viết lại câu hỏi mới nhất thành 1 câu truy vấn tìm kiếm ĐỘC LẬP, đầy đủ ngữ cảnh (không cần đọc hội thoại vẫn hiểu được), giữ nguyên ý định người dùng.
2. Đề xuất thêm tối đa 2 cách diễn đạt KHÁC để tìm cùng thông tin đó (đổi từ khoá/góc nhìn khác nhau), giúp tăng khả năng tìm ra kết quả tốt.

Hội thoại gần đây:
{history}

Câu hỏi mới nhất: {query}

CHỈ trả lời đúng tối đa 3 dòng, mỗi dòng 1 câu truy vấn tìm kiếm, KHÔNG đánh số thứ tự, KHÔNG giải thích, KHÔNG thêm gì khác."""

_EXTRACT_PROMPT = """Nội dung trang web dưới đây có thể lẫn nhiều nhiễu (menu, quảng cáo, điều hướng). Trích xuất CHỈ các sự kiện/số liệu liên quan trực tiếp tới truy vấn, dạng gạch đầu dòng ngắn gọn.

Bắt buộc:
- Giữ NGUYÊN số liệu, ngày tháng, tỷ lệ % xuất hiện trong trang - KHÔNG làm tròn, KHÔNG khái quát hoá.
- Bỏ hết câu quảng cáo/điều hướng/lời dẫn không mang thông tin.
- Nếu trang không có thông tin liên quan tới truy vấn, trả lời đúng 1 dòng: "(không có thông tin liên quan)".

Truy vấn: {query}

Nội dung trang:
{content}

Chỉ trả lời phần gạch đầu dòng, không thêm lời dẫn hay giải thích."""


def should_deep_read(text: str) -> bool:
    """Câu hỏi có vẻ cần số liệu chính xác (giá, tin tức...) thì nên đọc sâu
    thêm 1-2 trang thay vì chỉ dùng snippet Tavily."""
    lower = (text or "").lower()
    return any(marker in lower for marker in _DEEP_READ_TRIGGER_MARKERS)


async def _recent_history_text(user_id: int, turns: int = 3) -> str:
    """Vài lượt hội thoại gần nhất, dạng text ngắn cho prompt viết lại truy
    vấn - CHỈ để hiểu ngữ cảnh đại từ ("nó", "cái đó", "còn X thì sao"...),
    không dùng để trả lời. Lỗi DB thì trả rỗng, không chặn search."""
    try:
        rows = await db.get_session_messages(user_id, turns, config.CHAT_SESSION_TIMEOUT_SEC)
    except Exception:
        logger.warning("Không lấy được lịch sử hội thoại để viết lại truy vấn.", exc_info=True)
        return ""
    return "\n".join(f"{role}: {content}" for role, content in rows)


async def _rewrite_and_expand_query(query: str, history_text: str) -> list[str]:
    """1 lượt LLM: viết lại câu hỏi thành truy vấn độc lập (standalone) + tối
    đa 2 biến thể để search song song (multi-query, tăng recall). Chỉ chạy
    khi có lịch sử hội thoại; lỗi/không parse được thì fallback về đúng câu
    gốc - KHÔNG chặn search."""
    if not history_text:
        return [query]
    try:
        response = await orchestrator.ask(
            _QUERY_REWRITE_PROMPT.format(history=history_text, query=query)
        )
        lines = [
            line.strip(" -•\t\"")
            for line in (response.text or "").splitlines()
            if line.strip()
        ]
        # dict.fromkeys giữ thứ tự xuất hiện, khử trùng lặp
        queries = [q for q in dict.fromkeys(lines) if q][:3]
        return queries or [query]
    except Exception:
        logger.warning("Viết lại truy vấn search lỗi, dùng câu gốc.", exc_info=True)
        return [query]


def _merge_tavily_responses(
    primary_query: str,
    responses: list[tavily_client.TavilySearchResponse],
) -> tavily_client.TavilySearchResponse:
    """Gộp kết quả nhiều query song song (multi-query) thành 1 response duy
    nhất: khử trùng theo URL (giữ bản đầu, nối thêm content nếu cùng URL
    xuất hiện lại từ query khác), giữ `answer` không rỗng đầu tiên gặp được."""
    merged_results: list[tavily_client.TavilyResult] = []
    seen_urls: dict[str, int] = {}
    answer = ""
    for resp in responses:
        if not answer and resp.answer:
            answer = resp.answer
        for item in resp.results:
            if item.url and item.url in seen_urls:
                idx = seen_urls[item.url]
                existing = merged_results[idx]
                if item.content and item.content not in existing.content:
                    merged_results[idx] = dataclasses.replace(
                        existing, content=f"{existing.content}\n{item.content}"
                    )
                continue
            if item.url:
                seen_urls[item.url] = len(merged_results)
            merged_results.append(item)
    return tavily_client.TavilySearchResponse(
        query=primary_query, answer=answer, results=tuple(merged_results)
    )


async def _deep_read_top_results(
    query: str, results: tuple[tavily_client.TavilyResult, ...]
) -> str:
    """Scrape (qua services/web_reader, Jina Reader có sẵn) 1-2 kết quả điểm
    cao nhất rồi ép LLM nén thành fact gạch đầu dòng - snippet Tavily thường
    quá ngắn để có số liệu cụ thể. Không có bước "picker" LLM riêng như Vane
    - chỉ lấy top theo Tavily score sẵn có để đỡ tốn thêm 1 lượt gọi LLM."""
    from services import web_reader

    picks = sorted(
        (r for r in results if r.url), key=lambda r: (r.score or 0), reverse=True
    )[:_DEEP_READ_MAX_PAGES]

    sections: list[str] = []
    for item in picks:
        try:
            page_text = await web_reader.read_url(item.url)
        except web_reader.WebReaderError:
            continue
        except Exception:
            logger.warning("Đọc sâu %r lỗi bất ngờ.", item.url, exc_info=True)
            continue
        try:
            extracted = await orchestrator.ask(
                _EXTRACT_PROMPT.format(query=query, content=page_text[:_DEEP_READ_MAX_CHARS])
            )
            fact_text = (extracted.text or "").strip()
        except Exception:
            logger.warning("Trích xuất nội dung đọc sâu %r lỗi.", item.url, exc_info=True)
            continue
        if fact_text and "không có thông tin liên quan" not in fact_text.lower():
            sections.append(f"Đọc sâu từ {item.title or item.url} ({item.url}):\n{fact_text}")

    return "\n\n".join(sections)


def is_tavily_quality_sufficient(
    response: tavily_client.TavilySearchResponse,
    *,
    min_results: int = 2,
    min_domains: int = 2,
) -> bool:
    usable_results = [item for item in response.results if item.url and item.content.strip()]
    domains = {item.domain for item in usable_results}
    return len(usable_results) >= min_results and len(domains) >= min_domains


async def _fetch_tavily(
    query: str,
    *,
    max_results: int,
    search_depth: str,
    max_results_per_domain: int | None,
    extra_queries: list[str],
) -> tavily_client.TavilySearchResponse:
    """1 query -> gọi thẳng như cũ; nhiều query -> chạy song song rồi gộp."""
    queries = [query] + [q for q in extra_queries if q and q != query]
    if len(queries) == 1:
        return await tavily_client.search_results(
            query,
            max_results,
            search_depth=search_depth,
            max_results_per_domain=max_results_per_domain,
        )

    responses = await asyncio.gather(
        *(
            tavily_client.search_results(
                q,
                max_results,
                search_depth=search_depth,
                max_results_per_domain=max_results_per_domain,
            )
            for q in queries
        ),
        return_exceptions=True,
    )
    ok_responses = [r for r in responses if isinstance(r, tavily_client.TavilySearchResponse)]
    if not ok_responses:
        first_error = next((r for r in responses if isinstance(r, BaseException)), None)
        raise tavily_client.TavilyError(
            f"Tất cả {len(queries)} query đều lỗi: {first_error}"
        )
    return _merge_tavily_responses(query, ok_responses)


async def search_web(
    query: str,
    *,
    max_results: int = 0,
    search_depth: str = "basic",
    max_results_per_domain: int | None = None,
    min_results: int = 2,
    min_domains: int = 2,
    extra_queries: list[str] | None = None,
    deep_read: bool = False,
) -> SearchGrounding:
    """Search Tavily trước, fallback real-search khi kết quả không đủ đa dạng.

    ``extra_queries``: thêm biến thể câu hỏi để search song song (multi-query,
    tăng recall) - xem ``_rewrite_and_expand_query``. Kết quả gộp, khử trùng
    theo URL.
    ``deep_read``: đọc sâu 1-2 kết quả điểm cao nhất qua ``web_reader`` thay
    vì chỉ dùng snippet - dùng cho câu hỏi cần số liệu chính xác (giá, tin
    tức) hoặc khi caller (như /agent) chủ động muốn nghiên cứu kỹ hơn.
    """
    tavily_response: tavily_client.TavilySearchResponse | None = None
    tavily_error: BaseException | None = None
    try:
        tavily_response = await _fetch_tavily(
            query,
            max_results=max_results,
            search_depth=search_depth,
            max_results_per_domain=max_results_per_domain,
            extra_queries=extra_queries or [],
        )
        if is_tavily_quality_sufficient(
            tavily_response, min_results=min_results, min_domains=min_domains
        ):
            grounding_text = tavily_client.format_search_results(tavily_response)
            if deep_read:
                deep_text = await _deep_read_top_results(query, tavily_response.results)
                if deep_text:
                    grounding_text = f"{grounding_text}\n\n[Đọc sâu thêm nguồn]\n{deep_text}"
            return SearchGrounding(grounding_text, "tavily", True)
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


async def maybe_search(text: str, *, user_id: int | None = None) -> str:
    """Grounding cho chat thường; tôn trọng toggle /tavily và intent gate.

    ``user_id``: khi có, dùng vài lượt hội thoại gần nhất để viết lại câu
    hỏi thành truy vấn độc lập + biến thể (multi-query) trước khi search -
    xem ``_rewrite_and_expand_query``. Không truyền thì search thẳng câu gốc
    (giữ hành vi cũ, ví dụ khi gọi từ nơi chưa gắn được lịch sử hội thoại)."""
    if not await tavily_client.get_enabled() or not looks_like_search_question(text):
        return ""
    try:
        queries = [text]
        if user_id is not None:
            history_text = await _recent_history_text(user_id)
            queries = await _rewrite_and_expand_query(text, history_text)
        primary, extras = queries[0], queries[1:]
        grounding = await search_web(
            primary, extra_queries=extras, deep_read=should_deep_read(text)
        )
        return grounding.text
    except Exception:
        logger.warning("Web search lỗi, bỏ qua grounding.", exc_info=True)
        return ""
