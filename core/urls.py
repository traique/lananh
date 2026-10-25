"""Tách/chuẩn hoá URL và lọc link lạ trong câu trả lời của bot.

- ``extract_urls``/``normalize_url``: dùng chung cho allowlist URL của /agent
  (ai/agent_service.py) và bộ lọc link ở cuối lượt chat.
- ``filter_reply_urls``: câu trả lời có link KHÔNG xuất hiện nguyên văn trong
  tin người dùng hay dữ liệu tra cứu của lượt -> hiện dạng "vô hiệu hoá"
  (hxxps://ten[.]mien/...) thành chữ thường, không bấm/preview được. Chống
  kiểu tấn công trang web độc xúi model viết https://ke-xau/?q=<trí nhớ> để
  rò rỉ dữ liệu qua bản xem trước link. Đây là lớp phụ: lớp chính là tắt link
  preview của Telegram (bot_app.build_application).

Chạy trên MARKDOWN THÔ (trước markdown_to_html) để bắt cả link ẩn [chữ](url).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

URL_RE = re.compile(r"https?://[^\s<>\"'`\]\[(){}]+", re.IGNORECASE)
URL_TRAILING_PUNCT = ".,;:!?…»”’"
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)", re.IGNORECASE)
# Tên miền trần (không có http) kèm đường dẫn MANG DỮ LIỆU (có ? hoặc =) - kiểu
# "ke-xau.com/x?d=..." Telegram/Zalo vẫn tự nhận là link.
_BARE_DATA_LINK_RE = re.compile(
    r"(?<![\w@/.:-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})"
    r"(/[^\s<>()\"']*[?=][^\s<>()\"']*)",
    re.IGNORECASE,
)
_PLACEHOLDER = "\x00{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def normalize_url(url: str) -> str:
    """Chuẩn hoá để so sánh: scheme/host chữ thường, bỏ "/" cuối và #fragment.
    Trả "" nếu không phải http(s) hợp lệ."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    path = parts.path.rstrip("/") or ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def extract_urls(text: str) -> list[str]:
    urls = []
    for match in URL_RE.finditer(text or ""):
        normalized = normalize_url(match.group(0).rstrip(URL_TRAILING_PUNCT))
        if normalized:
            urls.append(normalized)
    return urls


def _host(netloc: str) -> str:
    return netloc.lower().rsplit("@", 1)[-1].split(":", 1)[0].removeprefix("www.")


def defang_url(url: str) -> str:
    """https://a.b/c -> hxxps://a[.]b/c: vẫn đọc được nhưng không còn là link."""
    match = re.match(r"(?i)(https?)://([^/?#]*)(.*)", url)
    if not match:
        return url.replace(".", "[.]")
    scheme, netloc, rest = match.groups()
    scheme = scheme.lower().replace("tt", "xx")
    return f"{scheme}://{netloc.replace('.', '[.]')}{rest}"


class _Allowlist:
    def __init__(self, sources: Iterable[str]) -> None:
        self.urls: set[str] = set()
        self.host_paths: set[tuple[str, str]] = set()
        self.hosts: set[str] = set()
        for source in sources:
            for url in extract_urls(source or ""):
                parts = urlsplit(url)
                host = _host(parts.netloc)
                self.urls.add(url)
                self.host_paths.add((host, parts.path))
                self.hosts.add(host)

    def permits(self, url: str) -> bool:
        normalized = normalize_url(url.rstrip(URL_TRAILING_PUNCT))
        if not normalized:
            return False
        if normalized in self.urls:
            return True
        parts = urlsplit(normalized)
        host = _host(parts.netloc)
        if parts.query:
            # Có query string mà không khớp nguyên văn: chính chỗ chứa dữ liệu
            # rò rỉ, không nới lỏng.
            return False
        if (host, parts.path) in self.host_paths:
            return True
        return not parts.path and host in self.hosts

    def permits_host(self, host: str) -> bool:
        return _host(host) in self.hosts


def filter_reply_urls(text: str, allowed_sources: Iterable[str]) -> tuple[str, int]:
    """Vô hiệu hoá link không có trong ``allowed_sources``. Trả (text, số link đã
    vô hiệu hoá). Link markdown [chữ](url) lạ thành "chữ (hxxps://...)"."""
    if not text or ("http" not in text.lower() and "." not in text):
        return text, 0
    allow = _Allowlist(allowed_sources)
    kept: list[str] = []
    removed = 0

    def _keep(value: str) -> str:
        kept.append(value)
        return _PLACEHOLDER.format(len(kept) - 1)

    def _md_link(match: re.Match) -> str:
        nonlocal removed
        label, url = match.group(1), match.group(2)
        if allow.permits(url):
            return _keep(match.group(0))
        removed += 1
        return f"{label} ({defang_url(url)})"

    def _bare(match: re.Match) -> str:
        nonlocal removed
        raw = match.group(0)
        url = raw.rstrip(URL_TRAILING_PUNCT)
        trailing = raw[len(url) :]
        if allow.permits(url):
            return _keep(url) + trailing
        removed += 1
        return defang_url(url) + trailing

    def _bare_domain(match: re.Match) -> str:
        nonlocal removed
        host, rest = match.group(1), match.group(2)
        if allow.permits_host(host):
            return match.group(0)
        removed += 1
        return host.replace(".", "[.]") + rest

    out = _MD_LINK_RE.sub(_md_link, text)
    out = URL_RE.sub(_bare, out)
    out = _BARE_DATA_LINK_RE.sub(_bare_domain, out)
    out = _PLACEHOLDER_RE.sub(lambda m: kept[int(m.group(1))], out)
    return out, removed
