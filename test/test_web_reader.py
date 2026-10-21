"""Unit test cho services/web_reader.py - đặc biệt normalize_public_http_url()
(SSRF guard) vì đây là hàm bảo mật quan trọng nhất (URL do người dùng gõ
trong chat, server tự fetch hộ).

Chạy: pytest test/test_web_reader.py -v
"""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import web_reader  # noqa: E402


# ─── normalize_public_http_url (SSRF guard) ────────────────────────────────


def test_chan_localhost_hostname():
    with pytest.raises(web_reader.WebReaderError):
        web_reader.normalize_public_http_url("http://localhost:8000/admin")


def test_chan_ip_loopback():
    with pytest.raises(web_reader.WebReaderError):
        web_reader.normalize_public_http_url("http://127.0.0.1/")


def test_chan_metadata_endpoint_cloud():
    with pytest.raises(web_reader.WebReaderError):
        web_reader.normalize_public_http_url("http://169.254.169.254/latest/meta-data/")


def test_chan_ip_private():
    with pytest.raises(web_reader.WebReaderError):
        web_reader.normalize_public_http_url("http://10.0.0.5/internal")


def test_chan_userinfo_gia_mao():
    # http://x.com@evil.test/ trông giống trỏ vào x.com nhưng thật ra trỏ
    # vào evil.test (x.com chỉ là username) - chặn userinfo hoàn toàn.
    with pytest.raises(web_reader.WebReaderError):
        web_reader.normalize_public_http_url("http://x.com@evil.test/")


def test_chan_scheme_khong_phai_http():
    with pytest.raises(web_reader.WebReaderError):
        web_reader.normalize_public_http_url("file:///etc/passwd")


def test_chan_url_rong():
    with pytest.raises(web_reader.WebReaderError):
        web_reader.normalize_public_http_url("")


def test_chan_ky_tu_dieu_khien():
    with pytest.raises(web_reader.WebReaderError):
        web_reader.normalize_public_http_url("http://example.com/\n\rhack")


def test_url_hop_le_duoc_giu_nguyen():
    url = "https://vnexpress.net/kinh-doanh"
    assert web_reader.normalize_public_http_url(url) == url


# ─── read_url ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_url_tra_ve_text_sach(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/https://example.com")
        return httpx.Response(200, text="Đây là nội dung bài báo.")

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    text = await web_reader.read_url("https://example.com/bai-viet")
    assert "nội dung bài báo" in text


@pytest.mark.asyncio
async def test_read_url_bao_loi_khi_http_that_bai(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(451)

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(web_reader.WebReaderError):
        await web_reader.read_url("https://example.com/bi-chan")


@pytest.mark.asyncio
async def test_read_url_tu_choi_url_noi_bo_truoc_khi_fetch(monkeypatch):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="không nên tới đây")

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(web_reader.WebReaderError):
        await web_reader.read_url("http://localhost/secret")
    assert called is False


@pytest.mark.asyncio
async def test_read_url_cat_bot_khi_qua_dai(monkeypatch):
    long_text = "x" * (web_reader.config.WEB_READER_MAX_CHARS + 500)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=long_text)

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    text = await web_reader.read_url("https://example.com/dai")
    assert len(text) < len(long_text)
    assert "đã cắt bớt" in text


@pytest.mark.asyncio
async def test_read_url_rot_xuong_fetch_truc_tiep_khi_jina_rong(monkeypatch):
    # Jina Reader trả 200 nhưng rỗng (trang chặn riêng Jina) - phải tự fetch
    # trực tiếp URL gốc rồi bóc text bằng BeautifulSoup thay vì báo lỗi luôn.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "r.jina.ai":
            return httpx.Response(200, text="")
        return httpx.Response(
            200,
            text="<html><body><nav>menu</nav><article>Nội dung đọc trực tiếp được.</article></body></html>",
        )

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    text = await web_reader.read_url("https://vietstock.vn/bai-viet")
    assert "Nội dung đọc trực tiếp được" in text
    assert "menu" not in text


@pytest.mark.asyncio
async def test_read_url_rot_xuong_fetch_truc_tiep_khi_jina_loi_http(monkeypatch):
    # Jina Reader trả lỗi HTTP (vd 403 - bị WAF chặn riêng) - vẫn thử fetch
    # trực tiếp thay vì báo lỗi ngay.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "r.jina.ai":
            return httpx.Response(403)
        return httpx.Response(200, text="<html><body><p>Bài viết đọc được.</p></body></html>")

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    text = await web_reader.read_url("https://vietstock.vn/bai-viet-2")
    assert "Bài viết đọc được" in text


@pytest.mark.asyncio
async def test_read_url_bao_loi_khi_ca_2_cach_deu_that_bai(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(web_reader.WebReaderError):
        await web_reader.read_url("https://vietstock.vn/khong-doc-duoc-ca-2-cach")


def test_extract_readable_text_bo_script_va_nav():
    html = (
        "<html><head><script>alert(1)</script><style>.a{}</style></head>"
        "<body><nav>Menu chính</nav><header>Header</header>"
        "<article>Nội dung chính cần giữ lại.</article>"
        "<footer>Footer</footer></body></html>"
    )
    text = web_reader._extract_readable_text(html)
    assert "Nội dung chính cần giữ lại" in text
    assert "Menu chính" not in text
    assert "Footer" not in text
    assert "alert(1)" not in text


# ─── read_rss ───────────────────────────────────────────────────────────────

_SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Tin kinh tế</title>
    <item>
      <title>VN-Index tăng điểm</title>
      <link>https://example.com/tin-1</link>
      <pubDate>Mon, 01 Sep 2026 09:00:00 +0700</pubDate>
      <description>Thị trường chứng khoán phiên hôm nay tăng nhẹ.</description>
    </item>
    <item>
      <title>Giá vàng biến động</title>
      <link>https://example.com/tin-2</link>
      <description>Giá vàng trong nước điều chỉnh giảm.</description>
    </item>
  </channel>
</rss>"""


@pytest.mark.asyncio
async def test_read_rss_liet_ke_muc_moi_nhat(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_SAMPLE_RSS.encode("utf-8"))

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    text = await web_reader.read_rss("https://example.com/rss.xml")
    assert "VN-Index tăng điểm" in text
    assert "Giá vàng biến động" in text
    assert "Tin kinh tế" in text


@pytest.mark.asyncio
async def test_read_rss_gui_user_agent_trinh_duyet(monkeypatch):
    """VnExpress trả 404 cho UA mặc định python-httpx -> phải gửi UA trình duyệt."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, content=_SAMPLE_RSS.encode("utf-8"))

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await web_reader.read_rss("https://example.com/rss.xml")
    assert seen and seen[0].startswith("Mozilla/5.0")


@pytest.mark.asyncio
async def test_read_rss_gioi_han_so_muc(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_SAMPLE_RSS.encode("utf-8"))

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    text = await web_reader.read_rss("https://example.com/rss.xml", limit=1)
    assert "VN-Index tăng điểm" in text
    assert "Giá vàng biến động" not in text


@pytest.mark.asyncio
async def test_read_rss_bao_loi_khi_khong_phai_feed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Không phải RSS</body></html>")

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(web_reader.WebReaderError):
        await web_reader.read_rss("https://example.com/khong-phai-rss")


@pytest.mark.asyncio
async def test_read_rss_tu_choi_url_noi_bo_truoc_khi_fetch(monkeypatch):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=_SAMPLE_RSS.encode("utf-8"))

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(web_reader.WebReaderError):
        await web_reader.read_rss("http://127.0.0.1/rss.xml")
    assert called is False


@pytest.mark.parametrize(
    "body",
    [
        (
            "Title: Just a moment...\n\n"
            "Warning: This page maybe requiring CAPTCHA\n\n"
            "## Performing security verification\n"
        ),
        "Title: Attention Required! | Cloudflare\n\nRay ID: abc123\n",
    ],
)
def test_antibot_detector_rejects_high_confidence_challenge(body):
    assert web_reader._is_antibot_page(body.encode("utf-8")) is True


def test_antibot_detector_ignores_generic_security_article():
    body = b"# A guide to security verification and DDoS protection"
    assert web_reader._is_antibot_page(body) is False


@pytest.mark.asyncio
async def test_read_url_falls_back_when_jina_returns_antibot_page(monkeypatch):
    monkeypatch.setattr(
        web_reader.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    challenge = (
        "Title: Just a moment...\n\n"
        "Warning: This page maybe requiring CAPTCHA\n\n"
        "## Performing security verification\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "r.jina.ai":
            return httpx.Response(200, text=challenge)
        return httpx.Response(
            200, text="<html><body><article>Nội dung thật.</article></body></html>"
        )

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    text = await web_reader.read_url("https://example.com/protected")
    assert "Nội dung thật" in text
    assert "CAPTCHA" not in text


@pytest.mark.asyncio
async def test_redirect_to_private_ip_is_blocked(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:9901/secret"})

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(
        web_reader.socket,
        "getaddrinfo",
        lambda host, port: [(web_reader.socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
    )

    with pytest.raises(web_reader.WebReaderError):
        await web_reader._get_public("https://example.com/start")

    assert seen == ["https://example.com/start"]


# ─── Trần byte / loại nội dung / DNS ngoài event loop ───────────────────────


class _EndlessStream(httpx.AsyncByteStream):
    """Body không có Content-Length, chảy mãi - phải dừng đúng ở trần byte."""

    def __init__(self):
        self.sent = 0
        self.closed = False

    async def __aiter__(self):
        while True:
            self.sent += 64 * 1024
            if self.sent > 50 * 1024 * 1024:
                raise AssertionError("đọc quá trần mà không dừng")
            yield b"a" * (64 * 1024)

    async def aclose(self):
        self.closed = True


def _public_dns(monkeypatch):
    monkeypatch.setattr(
        web_reader.socket,
        "getaddrinfo",
        lambda *a, **k: [(web_reader.socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
    )


@pytest.mark.asyncio
async def test_rss_body_over_byte_cap_is_rejected_without_full_download(monkeypatch):
    _public_dns(monkeypatch)
    stream = _EndlessStream()
    monkeypatch.setattr(web_reader.config, "WEB_READER_MAX_BYTES", 256 * 1024)
    monkeypatch.setattr(
        web_reader,
        "_get_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream))
        ),
    )
    with pytest.raises(web_reader.WebReaderError, match="quá lớn"):
        await web_reader.read_rss("https://example.com/huge.xml")
    assert stream.sent <= 256 * 1024 + 64 * 1024 and stream.closed


@pytest.mark.asyncio
async def test_html_over_byte_cap_is_truncated_not_loaded_whole(monkeypatch):
    _public_dns(monkeypatch)
    monkeypatch.setattr(web_reader.config, "WEB_READER_MAX_BYTES", 128 * 1024)
    stream = _EndlessStream()

    def handler(req):
        if req.url.host == "r.jina.ai":
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=stream)

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    text = await web_reader.read_url("https://example.com/very-long")
    assert text.startswith("a") and stream.sent <= 128 * 1024 + 64 * 1024


@pytest.mark.asyncio
async def test_pdf_is_refused_before_downloading_body(monkeypatch):
    _public_dns(monkeypatch)
    stream = _EndlessStream()

    def handler(req):
        if req.url.host == "r.jina.ai":
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "application/pdf"}, stream=stream)

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(web_reader.WebReaderError, match="application/pdf"):
        await web_reader.read_url("https://example.com/report.pdf")
    assert stream.sent == 0


@pytest.mark.asyncio
async def test_gzip_headers_are_not_reapplied_to_decoded_body(monkeypatch):
    import gzip

    _public_dns(monkeypatch)
    body = gzip.compress("Nội dung nén gzip.".encode())

    def handler(req):
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8", "content-encoding": "gzip"},
            content=body,
        )

    monkeypatch.setattr(
        web_reader, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    response = await web_reader._get_public("https://example.com/gz", allowed_types=("text/",))
    assert response.text == "Nội dung nén gzip."


@pytest.mark.asyncio
async def test_dns_resolution_runs_off_the_event_loop(monkeypatch):
    import threading

    loop_thread = threading.get_ident()
    resolver_threads = []

    def fake_getaddrinfo(*args, **kwargs):
        resolver_threads.append(threading.get_ident())
        return [(web_reader.socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(web_reader.socket, "getaddrinfo", fake_getaddrinfo)
    assert await web_reader.normalize_public_http_url_async("https://example.com/x")
    assert resolver_threads and loop_thread not in resolver_threads
