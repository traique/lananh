"""Test services/article_reader.py: nhận diện yêu cầu, bóc bài, đọc theo số."""

import json

import httpx
import pytest

from core import database as db
from services import article_reader, web_reader

_VNEXPRESS_HTML = """<html><head>
<meta property="og:title" content="Giá vàng tăng mạnh">
<meta property="article:published_time" content="2026-10-21T07:15:00+07:00">
</head><body>
<nav><p>Menu Thời sự Thế giới Kinh doanh</p></nav>
<h1 class="title-detail">Giá vàng tăng mạnh</h1>
<p class="description">Giá vàng miếng sáng nay tăng 1 triệu đồng mỗi lượng.</p>
<article class="fck_detail">
  <p class="Normal">Đoạn một nói về diễn biến giá vàng trong nước sáng nay, các doanh
  nghiệp đồng loạt điều chỉnh giá niêm yết theo đà tăng của thế giới.</p>
  <figure><figcaption><p>Chú thích ảnh không lấy</p></figcaption></figure>
  <h2>Thế giới</h2>
  <p class="Normal">Đoạn hai nói về giá vàng thế giới vượt mốc mới trong phiên đêm qua
  khi đồng USD suy yếu và nhu cầu trú ẩn tăng cao trên thị trường.</p>
  <p class="Normal">Đoạn ba là nhận định của chuyên gia về xu hướng trong tuần tới, khuyến
  nghị nhà đầu tư thận trọng khi mua vào ở vùng giá cao.</p>
  <script>var x = 1;</script>
</article>
<footer><p>Bản quyền</p></footer>
</body></html>"""


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "https://vnexpress.net/gia-vang-123.html",
            ("url", "https://vnexpress.net/gia-vang-123.html"),
        ),
        ("đọc bài này https://e.vnexpress.net/a.html.", ("url", "https://e.vnexpress.net/a.html")),
        ("Giá vàng tăng mạnh https://cafef.vn/x.chn", ("url", "https://cafef.vn/x.chn")),
        # Link bị kênh chat (Zoom...) bọc lại.
        ("<https://vnexpress.net/a.html>", ("url", "https://vnexpress.net/a.html")),
        ("[Giá vàng](https://vnexpress.net/a.html)", ("url", "https://vnexpress.net/a.html")),
        ("<https://vnexpress.net/a.html|Giá vàng>", ("url", "https://vnexpress.net/a.html")),
        ("đọc bài 2", ("number", 2)),
        ("Xem tin số 10", ("number", 10)),
        ("tóm tắt giúp anh https://vnexpress.net/a.html", None),
        ("https://shopee.vn/sp-123", None),
        ("so 2 link https://vnexpress.net/a.html https://tuoitre.vn/b.htm", None),
        ("đọc bài thơ này đi em", None),
    ],
)
def test_find_request(text, expected):
    assert article_reader.find_request(text) == expected


def test_is_news_url_chan_domain_gia():
    assert article_reader.is_news_url("https://m.cafef.vn/a.chn")
    assert not article_reader.is_news_url("https://vnexpress.net.evil.com/a")
    assert not article_reader.is_news_url("https://notvnexpress.net/a")


def test_extract_article_vnexpress():
    article = article_reader.extract_article(_VNEXPRESS_HTML, "https://vnexpress.net/a.html")

    assert article.title == "Giá vàng tăng mạnh"
    assert article.sapo == "Giá vàng miếng sáng nay tăng 1 triệu đồng mỗi lượng."
    assert article.published == "07:15 21/10/2026"
    assert [p[:8] for p in article.paragraphs] == ["Đoạn một", "Thế giới", "Đoạn hai", "Đoạn ba "]
    text = article_reader.format_article(article)
    assert text.startswith("📰 Giá vàng tăng mạnh\nvnexpress.net · 07:15 21/10/2026")
    assert "Chú thích ảnh" not in text and "Menu" not in text and "var x" not in text
    assert text.endswith("🔗 https://vnexpress.net/a.html")


def test_extract_article_trang_la_dung_heuristic():
    html = (
        "<html><head><title>T</title></head><body><div><p>ngắn</p></div>"
        "<div class='x'>"
        + "".join(f"<p>Đoạn {i} " + "chữ " * 30 + "</p>" for i in range(4))
        + "</div></body></html>"
    )
    article = article_reader.extract_article(html, "https://dantri.com.vn/a.htm")
    assert len(article.paragraphs) == 4


def test_format_article_cat_bai_dai():
    article = article_reader.Article(
        url="https://vnexpress.net/a.html", title="T", paragraphs=["x" * 1000] * 30
    )
    text = article_reader.format_article(article, max_chars=5000)
    assert len(text) < 5300
    assert "đã cắt bớt" in text


@pytest.fixture
def settings(monkeypatch):
    store: dict[str, str] = {}

    async def get(key):
        return store.get(key)

    async def set_(key, value):
        store[key] = value

    monkeypatch.setattr(db, "get_setting", get)
    monkeypatch.setattr(db, "set_setting", set_)
    return store


async def test_doc_bai_theo_so_trong_ban_tin(monkeypatch, settings):
    await article_reader.save_digest_links(
        [("Bài A", "https://vnexpress.net/a.html"), ("Bài B", "https://cafef.vn/b.chn")]
    )
    assert json.loads(settings[article_reader.LAST_LINKS_KEY])[1]["url"] == "https://cafef.vn/b.chn"
    fetched = []

    async def fake_get_public(url, **kwargs):
        fetched.append(url)
        return httpx.Response(200, text=_VNEXPRESS_HTML)

    monkeypatch.setattr(web_reader, "_get_public", fake_get_public)

    text = await article_reader.maybe_handle("đọc bài 2")

    assert fetched == ["https://cafef.vn/b.chn"]
    assert "Đoạn hai" in text
    assert "chỉ có 2 bài" in await article_reader.maybe_handle("đọc bài 5")


async def test_chua_co_ban_tin(settings):
    assert "Chưa có bản tin" in await article_reader.maybe_handle("đọc bài 1")


async def test_boc_khong_ra_thi_dung_jina(monkeypatch):
    async def fake_get_public(url, **kwargs):
        return httpx.Response(200, text="<html><body><p>ngắn</p></body></html>")

    async def fake_read_url(url):
        return "Tiêu đề\n\nNội dung qua Jina"

    monkeypatch.setattr(web_reader, "_get_public", fake_get_public)
    monkeypatch.setattr(web_reader, "read_url", fake_read_url)

    text = await article_reader.maybe_handle("https://vnexpress.net/a.html")

    assert "Nội dung qua Jina" in text


async def test_loi_tra_thong_bao_kem_link(monkeypatch):
    async def fake_get_public(url, **kwargs):
        raise httpx.ConnectError("down")

    async def fake_read_url(url):
        raise web_reader.WebReaderError("Không đọc được link (HTTP 403).")

    monkeypatch.setattr(web_reader, "_get_public", fake_get_public)
    monkeypatch.setattr(web_reader, "read_url", fake_read_url)

    text = await article_reader.maybe_handle("https://vnexpress.net/a.html")

    assert text.startswith("❌ Không đọc được bài này (Không đọc được link (HTTP 403).)")
    assert text.endswith("🔗 https://vnexpress.net/a.html")


async def test_tin_thuong_khong_xu_ly():
    assert await article_reader.maybe_handle("hôm nay em khoẻ không") is None
