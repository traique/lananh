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
