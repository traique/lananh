"""Đọc nội dung 1 URL bất kỳ (bài báo, blog, trang web...) hoặc 1 feed
RSS/Atom, dùng làm tool cho /agent (xem ai/agent_service.py). Khác
ai/tavily_client.py (TÌM web theo từ khoá) - module này ĐỌC 1 URL/feed CỤ THỂ
người dùng đưa.

Đọc link qua Jina Reader (https://r.jina.ai/URL) - dịch vụ miễn phí, không
cần API key, trả về text/markdown đã làm sạch (bỏ nav/quảng cáo) từ HTML gốc,
không cần chạy trình duyệt headless. Đọc RSS qua thư viện feedparser.

`normalize_public_http_url()` chặn SSRF: vì URL này do NGƯỜI DÙNG gõ vào chat
rồi server tự fetch hộ, phải đảm bảo không thể trỏ vào localhost/mạng nội bộ/
endpoint metadata của Render/cloud khác - adapt từ agent_reach/utils/url.py
(dự án Agent-Reach, MIT License) vì đã xử lý khá đầy đủ các ca (IP literal,
hostname nội bộ, userinfo giả mạo, ký tự điều khiển...).
"""
import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urlsplit

import httpx

from core import config

logger = logging.getLogger(__name__)

_JINA_READER_BASE_URL = "https://r.jina.ai/"

_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}

_client: Optional[httpx.AsyncClient] = None


class WebReaderError(RuntimeError):
    """Lỗi khi đọc URL/RSS (URL không hợp lệ/bị chặn, HTTP lỗi, feed rỗng)."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=config.WEB_READER_TIMEOUT_SEC, follow_redirects=True)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def normalize_public_http_url(raw_url: str) -> str:
    """Kiểm tra `raw_url` là 1 URL http(s) trỏ tới địa chỉ CÔNG KHAI, trả về
    URL đã chuẩn hoá nếu hợp lệ. Raise WebReaderError nếu:
      - không phải http/https, thiếu hostname, hoặc chứa ký tự điều khiển/
        khoảng trắng (dấu hiệu cố tình lách kiểm tra)
      - hostname là localhost/tên nội bộ đã biết
      - hostname resolve ra (hoặc chính là) 1 IP KHÔNG public (loopback,
        private, link-local - kể cả endpoint metadata cloud như
        169.254.169.254 - hoặc reserved)

    Đây là guard chống SSRF: URL này do người dùng gõ trong chat rồi server
    tự fetch hộ, nếu không chặn thì 1 user có thể ép bot tự gọi vào chính
    server của nó (localhost) hoặc endpoint metadata nội bộ của Render/cloud.
    """
    raw_url = (raw_url or "").strip()
    if not raw_url or any(ch.isspace() or ord(ch) < 0x20 for ch in raw_url):
        raise WebReaderError("URL không hợp lệ.")

    parts = urlsplit(raw_url)
    if parts.scheme not in ("http", "https"):
        raise WebReaderError("Chỉ hỗ trợ URL http:// hoặc https://.")
    if parts.username or parts.password:
        raise WebReaderError("URL không hợp lệ (không hỗ trợ userinfo trong URL).")

    hostname = parts.hostname
    if not hostname:
        raise WebReaderError("URL thiếu tên miền/host.")
    hostname_lower = hostname.lower()
    if hostname_lower in _BLOCKED_HOSTNAMES or hostname_lower.endswith(".local"):
        raise WebReaderError("Không được đọc link trỏ vào địa chỉ nội bộ.")

    # URL dạng IP literal (vd http://169.254.169.254/...) - kiểm tra thẳng.
    try:
        ip = ipaddress.ip_address(hostname)
        if not ip.is_global:
            raise WebReaderError("Không được đọc link trỏ vào địa chỉ IP nội bộ.")
        return raw_url
    except ValueError:
        pass  # hostname không phải IP literal, resolve DNS bên dưới.

    # Hostname dạng tên miền - resolve DNS rồi kiểm tra IP thật trỏ tới đâu,
    # phòng DNS rebinding (domain công khai nhưng trỏ vào IP nội bộ).
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise WebReaderError(f"Không phân giải được tên miền '{hostname}'.") from exc
    for info in infos:
        addr = info[4][0]
        try:
            if not ipaddress.ip_address(addr).is_global:
                raise WebReaderError("Link trỏ vào địa chỉ IP nội bộ, không được đọc.")
        except ValueError:
            continue
    return raw_url


_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript", "form", "svg")


def _extract_readable_text(html: str) -> str:
    """Bóc text đọc được từ HTML thô, bỏ script/style/nav/menu... - dùng khi
    Jina Reader lỗi (xem _fetch_direct)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


async def _fetch_direct(url: str) -> str:
    """Fallback khi Jina Reader lỗi/rỗng: 1 số trang (đặc biệt tin tức VN)
    chặn/rate-limit riêng IP hoặc User-Agent của các dịch vụ reader/proxy
    như Jina bằng WAF, dù vẫn cho trình duyệt (hoặc fetcher có User-Agent
    giống trình duyệt) truy cập nội dung công khai bình thường - tự fetch
    HTML thô với User-Agent giả trình duyệt rồi bóc text bằng BeautifulSoup
    (đã có sẵn qua dependency của vnstock, không thêm gánh nặng RAM đáng kể
    trên Render free)."""
    response = await _get_client().get(url, headers={"User-Agent": _FALLBACK_USER_AGENT})
    if response.status_code != 200:
        raise WebReaderError(f"Không đọc được link (HTTP {response.status_code}).")

    text = _extract_readable_text(response.text)
    if not text:
        raise WebReaderError("Trang này không có nội dung đọc được (có thể chặn bot).")
    return text


async def read_url(raw_url: str) -> str:
    """Đọc 1 URL, trả về text đã làm sạch (cắt ở WEB_READER_MAX_CHARS ký tự).
    Thử Jina Reader trước; nếu lỗi/rỗng (1 số trang tin VN chặn riêng IP/UA
    của các dịch vụ reader/proxy như Jina dù vẫn cho trình duyệt thường
    truy cập bình thường) thì rơi xuống _fetch_direct() - tự fetch HTML rồi
    bóc text bằng BeautifulSoup. Raise WebReaderError nếu URL không hợp lệ/
    bị chặn, hoặc CẢ 2 cách đều lỗi."""
    url = normalize_public_http_url(raw_url)

    text = ""
    status_code: Optional[int] = None
    try:
        response = await _get_client().get(f"{_JINA_READER_BASE_URL}{url}")
        status_code = response.status_code
        if status_code == 200:
            text = response.text.strip()
    except httpx.HTTPError as exc:
        logger.warning("Jina Reader lỗi mạng cho '%s' (%s), thử fetch trực tiếp.", url, exc)

    if not text:
        if status_code is not None and status_code != 200:
            logger.warning("Jina Reader trả HTTP %d cho '%s', thử fetch trực tiếp.", status_code, url)
        logger.info("Jina Reader không đọc được '%s', thử fetch trực tiếp.", url)
        text = await _fetch_direct(url)

    if len(text) > config.WEB_READER_MAX_CHARS:
        text = text[: config.WEB_READER_MAX_CHARS] + "\n\n[... đã cắt bớt, nội dung dài hơn]"
    return text


async def read_rss(raw_url: str, limit: int = 0) -> str:
    """Đọc feed RSS/Atom, trả về text liệt kê tối đa `limit` (mặc định
    RSS_READER_MAX_ITEMS) mục mới nhất: tiêu đề, ngày đăng, tóm tắt ngắn,
    link. Raise WebReaderError nếu URL không hợp lệ/bị chặn hoặc feed lỗi."""
    import feedparser

    url = normalize_public_http_url(raw_url)
    limit = limit if limit and limit > 0 else config.RSS_READER_MAX_ITEMS

    response = await _get_client().get(url)
    if response.status_code != 200:
        raise WebReaderError(f"Không đọc được feed (HTTP {response.status_code}).")

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        raise WebReaderError("URL này không phải feed RSS/Atom hợp lệ.")
    if not parsed.entries:
        raise WebReaderError("Feed không có mục nào.")

    feed_title = parsed.feed.get("title") or url
    lines = [f"[Feed: {feed_title}]"]
    for i, entry in enumerate(parsed.entries[:limit], start=1):
        title = entry.get("title") or "(không có tiêu đề)"
        published = entry.get("published") or entry.get("updated") or ""
        summary = (entry.get("summary") or "").strip()
        if len(summary) > 300:
            summary = summary[:300] + "..."
        link = entry.get("link") or ""
        header = f"{i}. {title}" + (f" ({published})" if published else "")
        lines.append(f"{header}\n{summary}\n{link}".strip())

    return "\n\n".join(lines)
