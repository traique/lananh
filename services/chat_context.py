"""Ngữ cảnh phụ cho lượt chat AI (services/channel_chat_service.run_chat_turn).

- ``quote_grounding``: tin người dùng đang trả lời/trích dẫn (Telegram
  reply_to_message/quote, Zalo quote_text) - rào như dữ liệu ngoài, KHÔNG gộp
  vào lời người dùng.
- ``kb_grounding``: tra tự động kho kiến thức rag/ (BM25 cục bộ, không gọi AI)
  - chỉ chủ bot/admin, bỏ qua lượt cổ phiếu/số liệu thị trường để quy tắc giao
  dịch ghi trong rag/ không bao giờ vượt mặt stock/policy.py; tối đa 1 đoạn
  ~1.500 ký tự, đặt SAU grounding cổ phiếu/tool/tra web.
- ``read_link``: tin chat có link (mà article_reader không xử lý) -> đọc 1 trang
  qua services/web_reader (chặn SSRF, trần byte/ký tự) trong hạn chót riêng
  min(20s, 30% thời gian còn lại của lượt), rào nội dung, nhớ trang vừa đọc
  (có trần + TTL) cho câu hỏi nối tiếp "bài này...". Chủ bot và admin Zalo mặc
  định; thành viên chỉ khi CHAT_READ_LINKS_MEMBERS=1.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlsplit

from ai.timeouts import remaining_turn_seconds
from core import config, untrusted, urls
from core.envutil import env_on

logger = logging.getLogger(__name__)

QUOTE_MAX_CHARS = 600
_QUOTE_HEADER = (
    "[Người dùng đang trả lời/trích dẫn tin nhắn dưới đây - hiểu câu hỏi theo đúng tin đó]"
)


def clip_quote(text: str | None) -> str | None:
    """Chuẩn hoá tin trích dẫn: rỗng -> None, cắt QUOTE_MAX_CHARS ký tự."""
    text = (text or "").strip()
    return text[:QUOTE_MAX_CHARS] if text else None


def quote_grounding(quote_text: str | None) -> str:
    quote = clip_quote(quote_text)
    if not quote:
        return ""
    fenced = untrusted.wrap_untrusted(quote, "tin được trích dẫn", max_chars=QUOTE_MAX_CHARS)
    return f"{_QUOTE_HEADER}\n{fenced}" if fenced else ""


# ─── Kho kiến thức rag/ (tra tự động) ────────────────────────────────────────


def kb_allowed(user_id: int, is_admin: bool, text: str, *, has_stock_grounding: bool) -> bool:
    """Kho rag/ là kiến thức riêng của chủ bot: chỉ chủ bot/admin; không tra ở
    lượt có số liệu cổ phiếu hay hỏi số liệu thị trường (rag/README gợi ý ghi
    quy tắc vào tiền/tỷ trọng - không được vượt mặt stock/policy.py)."""
    if not env_on("CHAT_AUTO_RAG"):
        return False
    if not (config.is_owner_user_id(user_id) or is_admin) or has_stock_grounding:
        return False
    from stock import analysis as stock_analysis

    return not stock_analysis.wants_external_market_data(strip_urls(text))


async def kb_grounding(text: str) -> str:
    from services import rag_service

    try:
        match = await asyncio.to_thread(rag_service.auto_lookup, text)
    except Exception:
        logger.warning("Tra tự động kho rag/ lỗi - bỏ qua.", exc_info=True)
        return ""
    if match is None:
        return ""
    fenced = untrusted.wrap_untrusted(
        match.chunk.content, "kho kiến thức rag", max_chars=rag_service.AUTO_MAX_CHARS
    )
    if not fenced:
        return ""
    return (
        f"[Tham khảo từ kho kiến thức riêng: {match.chunk.display_name} - chỉ dùng nếu đúng "
        "ý câu hỏi, dùng thì nêu nguồn; không bắt buộc trả lời theo tài liệu]\n" + fenced
    )


# ─── Đọc link người dùng gửi trong chat ──────────────────────────────────────

PAGE_MAX_CHARS = 5000
READ_TIMEOUT_SEC = 20.0
MIN_READ_TIMEOUT_SEC = 1.0
_READ_TIMEOUT_SHARE = 0.3
PAGE_CACHE_MAX = 20
PAGE_CACHE_TTL_SEC = 600
_pages: OrderedDict[int, tuple[float, str, str]] = OrderedDict()
_COMPARE_RE = re.compile(
    r"so sánh|so sanh|đối chiếu|doi chieu|tìm thêm|tim them|tra thêm|tra them|nguồn khác|"
    r"nguon khac|kiểm chứng|kiem chung|kiểm tra lại|kiem tra lai",
    re.IGNORECASE,
)
_PAGE_FOLLOW_UP_RE = re.compile(
    r"\b(?:bài|bai|trang|link|đường link|duong link|bài viết|bai viet|bài báo|bai bao)\s+"
    r"(?:này|nay|đó|do|trên|tren|vừa rồi|vua roi|kia)\b|\btrong bài\b|\btheo bài\b",
    re.IGNORECASE,
)


def strip_urls(text: str) -> str:
    """Bỏ mọi URL khỏi câu (slug "hpg-lai-ky-luc" không được thành mã cổ phiếu)."""
    return re.sub(r"\s+", " ", urls.URL_RE.sub(" ", text or "")).strip()


def first_url(text: str) -> str | None:
    for match in urls.URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(urls.URL_TRAILING_PUNCT)
        if urls.normalize_url(url):
            return url
    return None


def links_allowed(user_id: int, is_admin: bool) -> bool:
    if not env_on("CHAT_READ_LINKS"):
        return False
    if config.is_owner_user_id(user_id) or is_admin:
        return True
    return os.getenv("CHAT_READ_LINKS_MEMBERS", "0").strip().lower() in {"1", "true", "yes", "on"}


def wants_outside_sources(text: str) -> bool:
    """Câu có link nhưng vẫn muốn tra thêm nguồn khác ("so sánh với...")."""
    return bool(_COMPARE_RE.search(strip_urls(text)))


def _remember_page(user_id: int, url: str, block: str) -> None:
    _pages[user_id] = (time.monotonic(), url, block)
    _pages.move_to_end(user_id)
    while len(_pages) > PAGE_CACHE_MAX:
        _pages.popitem(last=False)


def _recent_page(user_id: int) -> tuple[str, str] | None:
    item = _pages.get(user_id)
    if item is None:
        return None
    stamp, url, block = item
    if time.monotonic() - stamp > PAGE_CACHE_TTL_SEC:
        _pages.pop(user_id, None)
        return None
    return url, block


def forget_pages(user_id: int) -> None:
    _pages.pop(user_id, None)


@dataclass(frozen=True)
class PageContext:
    grounding: str = ""
    url: str = ""
    read_ok: bool = False  # đã có nội dung trang (vừa đọc hoặc từ cache)
    attempted: bool = False  # tin có link và được phép đọc


def _page_block(url: str, text: str) -> str:
    host = (urlsplit(url).hostname or "trang").lower()
    fenced = untrusted.wrap_untrusted(text, f"trang {host}", max_chars=PAGE_MAX_CHARS)
    return f"[Nội dung trang người dùng gửi: {url}]\n{fenced}" if fenced else ""


def _read_timeout() -> float:
    remaining = remaining_turn_seconds()
    if remaining is None:
        return READ_TIMEOUT_SEC
    return max(MIN_READ_TIMEOUT_SEC, min(READ_TIMEOUT_SEC, remaining * _READ_TIMEOUT_SHARE))


async def read_link(user_id: int, text: str, *, allowed: bool) -> PageContext:
    """Đọc link đầu tiên trong câu (hoặc dùng lại trang vừa đọc khi câu hỏi nối
    tiếp "bài này..."). Không raise."""
    if not allowed:
        return PageContext()
    url = first_url(text)
    if url is None:
        cached = _recent_page(user_id) if _PAGE_FOLLOW_UP_RE.search(text or "") else None
        if cached is None:
            return PageContext()
        cached_url, block = cached
        return PageContext(grounding=block, url=cached_url, read_ok=True)
    from services import web_reader

    try:
        page = await asyncio.wait_for(web_reader.read_url(url), _read_timeout())
    except (web_reader.WebReaderError, TimeoutError) as exc:
        reason = "quá thời gian chờ" if isinstance(exc, TimeoutError) else str(exc)[:120]
        logger.info("Không đọc được link trong chat (%s): %s", url, reason)
        marker = (
            f"[Không đọc được trang người dùng gửi ({url}): {reason}. Nói thật là chưa đọc "
            "được trang, ĐỪNG đoán nội dung trang.]"
        )
        return PageContext(grounding=marker, url=url, attempted=True)
    except Exception:
        logger.warning("Đọc link trong chat lỗi bất ngờ (%s).", url, exc_info=True)
        marker = (
            f"[Không đọc được trang người dùng gửi ({url}). Nói thật là chưa đọc được trang, "
            "ĐỪNG đoán nội dung trang.]"
        )
        return PageContext(grounding=marker, url=url, attempted=True)
    block = _page_block(url, page)
    if not block:
        return PageContext(url=url, attempted=True)
    _remember_page(user_id, url, block)
    return PageContext(grounding=block, url=url, read_ok=True, attempted=True)


def remember_article(user_id: int, text: str, article: str) -> None:
    """article_reader vừa trả nguyên bài: giữ (đã rào) cho câu hỏi nối tiếp."""
    url = first_url(text)
    if url and article:
        block = _page_block(url, article)
        if block:
            _remember_page(user_id, url, block)
