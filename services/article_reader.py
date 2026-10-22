"""Đọc NGUYÊN BÀI báo ngay trong chat (khỏi mở trình duyệt).

Hai cách gọi (mọi kênh: Telegram, Zalo, Zoom):
- Gửi link bài trên báo Việt Nam (vnexpress.net, tuoitre.vn, cafef.vn...), có
  thể kèm "đọc", "xem bài này". Tin có kèm yêu cầu khác (tóm tắt, phân tích,
  dịch...) vẫn đi chat AI như cũ.
- Nhắn "đọc bài 2": đọc bài số 2 trong mục "TIN ĐÁNG CHÚ Ý" của bản tin gần
  nhất (services/morning_news.py lưu danh sách link qua ``save_digest_links``).

Không qua LLM: trả về đúng chữ của bài (tiêu đề, sapo, các đoạn), cắt ở
MAX_ARTICLE_CHARS. Tải HTML qua ``web_reader._get_public`` (chặn SSRF, trần
byte), bóc nội dung bằng BeautifulSoup; trang lạ không bóc được thì rơi xuống
``web_reader.read_url`` (Jina Reader).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from core import database as db
from services import web_reader

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
LAST_LINKS_KEY = "morning_news:last_links"
MAX_DIGEST_LINKS = 20
MAX_ARTICLE_CHARS = 12000
_READ_TIMEOUT_SEC = 30

# Báo Việt Nam được đọc thẳng khi người dùng dán link (kể cả subdomain như
# e.vnexpress.net, m.cafef.vn).
NEWS_HOSTS = (
    "vnexpress.net",
    "tuoitre.vn",
    "thanhnien.vn",
    "cafef.vn",
    "cafebiz.vn",
    "vietstock.vn",
    "genk.vn",
    "dantri.com.vn",
    "vietnamnet.vn",
    "znews.vn",
    "vneconomy.vn",
    "laodong.vn",
    "tienphong.vn",
    "nld.com.vn",
    "baochinhphu.vn",
    "vtv.vn",
    "kenh14.vn",
    "soha.vn",
    "vov.vn",
    "ndh.vn",
)

# Bỏ cả <>, (), | để bắt đúng link khi kênh bọc link dạng <url>, [chữ](url)
# hoặc <url|chữ> (Zoom/Slack).
_URL_RE = re.compile(r"https?://[^\s<>\"'()|]+", re.IGNORECASE)
_READ_NUMBER_RE = re.compile(
    r"^(?:đọc|doc|xem|mở|mo)\s+(?:bài|bai|tin)\s*(?:số|so)?\s*(\d{1,2})\s*[.!]?$",
    re.IGNORECASE,
)
# Danh sách CHO PHÉP (không phải danh sách chặn): chỉ đọc nguyên bài khi tin
# nhắn chỉ có link, hoặc link kèm lời nhờ đọc ("đọc bài này", "xem giúp em").
# Link kèm câu hỏi bất kỳ ("tin này tốt hay xấu cho HPG") đi chat AI như cũ -
# danh sách chặn cũ sót hầu hết câu hỏi và chặn nhầm cả "dịch vụ".
_READ_ONLY_RE = re.compile(
    r"^(?:(?:cho\s+)?(?:em|anh|chị|chi|mình|minh)\s+)?"
    r"(?:(?:đọc|doc|xem|mở|mo|show)(?:\s+(?:giúp|giup|hộ|ho|full|hết|het|toàn bộ|nguyên))?"
    r"(?:\s+(?:em|anh|chị|chi|mình|minh))?(?:\s+(?:bài|bai|tin|link))?"
    r"(?:\s+(?:này|nay|đó|do|đầy đủ|day du))?)?"
    r"(?:\s*(?:đi|di|nhé|nhe|nha|với|voi|ạ|a))*\s*[.!:]?$",
    re.IGNORECASE,
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)
_JUNK_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "form",
    "svg",
    "button",
    "figure",
    "figcaption",
    "table",
    "nav",
    "aside",
    "footer",
)
# Khung thân bài của các báo hay gặp; trang khác dùng heuristic nhiều chữ nhất.
_BODY_SELECTORS = (
    "article.fck_detail",  # VnExpress
    "[itemprop=articleBody]",
    "div.detail-content",  # Tuổi Trẻ, CafeF, Thanh Niên
    "div.knc-content",  # GenK, Kenh14
    "#vst_detail",  # Vietstock
    "div.singular-content",  # Dân trí
    "div.maincontent",  # VietNamNet
    "div.the-article-body",  # Znews
    "article",
)
_SAPO_SELECTORS = (
    "p.description",
    "h2.detail-sapo",
    "h2.sapo",
    "div.sapo",
    "h2.knc-sapo",
    "p.pHead",
    "h2.singular-sapo",
    "p.the-article-summary",
)
_MIN_BODY_CHARS = 300


class ArticleError(RuntimeError):
    """Không đọc được bài (link lỗi/bị chặn, trang không có nội dung)."""


@dataclass
class Article:
    url: str
    title: str = ""
    sapo: str = ""
    published: str = ""
    paragraphs: list[str] = field(default_factory=list)


def is_news_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith(f".{h}") for h in NEWS_HOSTS)


def find_request(text: str) -> tuple[str, str | int] | None:
    """("url", link) | ("number", n) nếu tin nhắn là yêu cầu đọc bài, ngược lại None."""
    text = (text or "").strip()
    # Kênh chat bọc link kèm chữ hiển thị: <url|chữ>, [chữ](url), <url> -> url.
    text = re.sub(r"<(https?://[^|>\s]+)\|[^>]*>", r"\1", text)
    text = re.sub(r"\[[^\]]*\]\((https?://[^)\s]+)\)", r"\1", text)
    text = re.sub(r"<(https?://[^>\s]+)>", r"\1", text)
    match = _READ_NUMBER_RE.match(text)
    if match:
        return "number", int(match.group(1))
    urls = [u.rstrip(".,;!?") for u in _URL_RE.findall(text)]
    news = [u for u in urls if is_news_url(u)]
    if len(news) != 1 or len(urls) != 1:
        return None
    rest = re.sub(r"\s+", " ", _URL_RE.sub(" ", text)).strip()
    if not _READ_ONLY_RE.match(rest):
        return None
    return "url", news[0]


async def save_digest_links(items: list[tuple[str, str]]) -> None:
    """Lưu (tiêu đề, link) của bản tin vừa tạo để "đọc bài N" tra lại."""
    payload = [{"title": t, "url": u} for t, u in items[:MAX_DIGEST_LINKS]]
    try:
        await db.set_setting(LAST_LINKS_KEY, json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.warning("Không lưu được danh sách link bản tin.", exc_info=True)


async def _digest_link(number: int) -> tuple[str | None, str]:
    """(url, "") hoặc (None, câu báo lỗi)."""
    try:
        raw = await db.get_setting(LAST_LINKS_KEY)
    except Exception:
        logger.warning("Không đọc được danh sách link bản tin.", exc_info=True)
        return None, "Chưa đọc được danh sách bài của bản tin, thử lại sau nhé."
    try:
        links = json.loads(raw) if raw else []
    except ValueError:
        links = []
    if not links:
        return None, "Chưa có bản tin nào có danh sách bài. Gõ /tintuc để lấy bản tin mới."
    if not 1 <= number <= len(links):
        return None, f"Bản tin gần nhất chỉ có {len(links)} bài (từ 1 đến {len(links)})."
    return str(links[number - 1].get("url") or ""), ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _meta(soup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean(tag["content"])
    return ""


def _body_text_len(node) -> int:
    return sum(len(p.get_text(" ", strip=True)) for p in node.find_all("p"))


def _pick_body(soup):
    for selector in _BODY_SELECTORS:
        for node in soup.select(selector):
            if _body_text_len(node) >= _MIN_BODY_CHARS:
                return node
    # Heuristic: phần tử có nhiều chữ nhất trong các <p> con trực tiếp.
    best, best_len = None, 0
    seen: set[int] = set()
    for p in soup.find_all("p"):
        parent = p.parent
        if parent is None or id(parent) in seen:
            continue
        seen.add(id(parent))
        length = sum(
            len(c.get_text(" ", strip=True)) for c in parent.find_all("p", recursive=False)
        )
        if length > best_len:
            best, best_len = parent, length
    return best if best_len >= _MIN_BODY_CHARS else None


def _format_published(raw: str) -> str:
    try:
        published = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if published.tzinfo is None:
        # Báo VN ghi giờ không kèm múi giờ là giờ Việt Nam; astimezone() trên
        # datetime "naive" lại hiểu theo múi giờ của server (UTC trên Render).
        published = published.replace(tzinfo=_VN_TZ)
    return published.astimezone(_VN_TZ).strftime("%H:%M %d/%m/%Y")


def extract_article(html: str, url: str) -> Article:
    """Bóc tiêu đề, sapo, thời gian đăng và các đoạn thân bài từ HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    title = _meta(soup, "og:title") or (_clean(h1.get_text(" ")) if h1 else "")
    sapo = ""
    for selector in _SAPO_SELECTORS:
        node = soup.select_one(selector)
        if node and _clean(node.get_text(" ")):
            sapo = _clean(node.get_text(" "))
            break
    if not sapo:
        sapo = _meta(soup, "og:description", "description")
    published = _format_published(_meta(soup, "article:published_time", "pubdate"))

    for tag in soup.find_all(_JUNK_TAGS):
        tag.decompose()
    body = _pick_body(soup)
    paragraphs: list[str] = []
    if body is not None:
        for node in body.find_all(["p", "h2", "h3"]):
            # <p> lồng trong <p>/<h2> khác đã lấy cùng đoạn cha.
            if node.find_parent(["p", "h2", "h3"]) is not None:
                continue
            text = _clean(node.get_text(" "))
            if not text or text == sapo or (paragraphs and text == paragraphs[-1]):
                continue
            paragraphs.append(text)
    return Article(url=url, title=title, sapo=sapo, published=published, paragraphs=paragraphs)


async def read_article(url: str) -> Article:
    """Tải và bóc 1 bài báo. Raise ArticleError khi không đọc được."""
    try:
        response = await web_reader._get_public(
            url,
            headers={"User-Agent": _USER_AGENT},
            allowed_types=web_reader.HTML_TYPES,
            truncate=True,
        )
        if response.status_code == 200:
            article = extract_article(response.text, url)
            if article.paragraphs:
                return article
        logger.info(
            "Bóc bài '%s' không ra nội dung (HTTP %s), thử Jina.", url, response.status_code
        )
    except (httpx.HTTPError, web_reader.WebReaderError) as exc:
        logger.info("Tải bài '%s' lỗi (%r), thử Jina.", url, exc)
    try:
        text = await web_reader.read_url(url)
    except web_reader.WebReaderError as exc:
        raise ArticleError(str(exc)) from exc
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    if not paragraphs:
        raise ArticleError("Trang này không có nội dung đọc được.")
    return Article(url=url, paragraphs=paragraphs)


def _source_label(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def format_article(article: Article, max_chars: int = MAX_ARTICLE_CHARS) -> str:
    head = [f"📰 {article.title}" if article.title else "📰 Bài báo"]
    meta = " · ".join(x for x in (_source_label(article.url), article.published) if x)
    if meta:
        head.append(meta)
    parts = ["\n".join(head)]
    if article.sapo:
        parts.append(article.sapo)
    used = sum(len(p) for p in parts)
    truncated = False
    for paragraph in article.paragraphs:
        if used + len(paragraph) > max_chars:
            truncated = True
            break
        parts.append(paragraph)
        used += len(paragraph) + 2
    if truncated:
        parts.append("[... bài dài, đã cắt bớt - đọc tiếp ở link bên dưới]")
    parts.append(f"🔗 {article.url}")
    return "\n\n".join(parts)


async def maybe_handle(text: str) -> str | None:
    """Trả nguyên văn bài báo nếu tin nhắn là yêu cầu đọc bài, ngược lại None."""
    request = find_request(text)
    if request is None:
        return None
    kind, value = request
    if kind == "number":
        url, error = await _digest_link(int(value))
        if not url:
            return error
    else:
        url = str(value)
    try:
        article = await asyncio.wait_for(read_article(url), _READ_TIMEOUT_SEC)
    except (ArticleError, TimeoutError) as exc:
        logger.warning("Đọc bài '%s' lỗi: %r", url, exc)
        return f"❌ Không đọc được bài này ({str(exc) or 'quá thời gian chờ'}).\n🔗 {url}"
    return format_article(article)
