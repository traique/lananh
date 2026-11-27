"""Nén gzip, ETag/304, Cache-Control của web công khai (web.py, web_site.py), cache trang
lễ hội, trang chính sách, header bảo mật theo đường dẫn, Zoom cắt tin theo trần 4.096
ký tự và nút AI luận giải báo bận thay vì chờ khoá lượt vô hạn."""

import asyncio
import datetime as dt
import re
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

import web
import web_site
from services import concurrency
from services.security_headers import SecurityHeadersMiddleware, permissions_policy

VN = ZoneInfo("Asia/Ho_Chi_Minh")


async def _logged_in(request):
    return True


async def _logged_out(request):
    return False


@pytest.fixture
async def client(monkeypatch):
    """Toàn bộ web.api (đủ middleware: nén, giới hạn body, header bảo mật)."""
    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_out)
    web_site._festival_pages.clear()
    transport = httpx.ASGITransport(app=web.api)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c
    web_site._festival_pages.clear()


def _fix_now(monkeypatch, when: dt.datetime) -> None:
    monkeypatch.setattr(web_site.site_data, "now", lambda: when)


# ─── Nén gzip ────────────────────────────────────────────────────────────────


async def test_html_css_nen_gzip_con_noi_bo_va_file_nho_thi_khong(client):
    page = await client.get("/lich?thang=2027-01", headers={"Accept-Encoding": "gzip"})
    assert page.headers["content-encoding"] == "gzip" and "Accept-Encoding" in page.headers["vary"]
    assert "Lịch vạn niên tháng 1/2027" in page.text  # httpx tự giải nén
    css = await client.get("/static/site.css", headers={"Accept-Encoding": "gzip"})
    assert css.headers["content-encoding"] == "gzip"
    assert "immutable" in css.headers["cache-control"]
    raw = await client.get("/static/site.css", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in raw.headers and raw.content == css.content
    # Số byte thật đi qua mạng: bản nén nhỏ hơn 1/3 bản thô (~64 KB -> ~16 KB).
    assert raw.num_bytes_downloaded > 3 * css.num_bytes_downloaded
    # Ảnh đã nén sẵn, JSON nhỏ: giữ nguyên.
    icon = await client.get("/static/icon.png", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in icon.headers
    health = await client.get(
        "/", headers={"Accept": "application/json", "Accept-Encoding": "gzip"}
    )
    assert "content-encoding" not in health.headers and health.json() == {"status": "ok"}


async def test_duong_noi_bo_internal_khong_nen():
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * 5000})

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "path": "/internal/zalo/message",
        "headers": [(b"accept-encoding", b"gzip")],
    }
    await web.CompressionMiddleware(app)(scope, None, send)
    assert dict(sent[0]["headers"]).get(b"content-encoding") is None
    assert sent[1]["body"] == b"x" * 5000


async def test_luong_su_kien_text_event_stream_khong_nen_va_chay_tung_doan():
    """GZipMiddleware của Starlette cài đặt bỏ qua text/event-stream: luồng chat của
    /admin/avatar vẫn tới trình duyệt từng đoạn, không bị gom lại để nén."""
    release = asyncio.Event()

    async def events():
        yield "data: một\n\n" + " " * 2000
        await release.wait()
        yield "data: hai\n\n"

    inner = FastAPI()

    @inner.get("/chat")
    async def chat():
        return StreamingResponse(events(), media_type="text/event-stream")

    app = web.CompressionMiddleware(inner)
    messages: list[dict] = []
    first_chunk = asyncio.Event()

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/chat",
        "raw_path": b"/chat",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"accept-encoding", b"gzip"), (b"host", b"testserver")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 443),
    }
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(first_chunk.wait(), 2)
    start = messages[0]
    assert start["type"] == "http.response.start"
    headers = dict(start["headers"])
    assert headers[b"content-type"].startswith(b"text/event-stream")
    assert b"content-encoding" not in headers
    # Đoạn đầu đã tới tay client trong khi đoạn sau còn chưa sinh ra.
    assert messages[1]["body"].startswith("data: một".encode())
    release.set()
    await asyncio.wait_for(task, 2)
    assert b"".join(m.get("body", b"") for m in messages[1:]).endswith("data: hai\n\n".encode())


async def test_luong_chat_avatar_qua_middleware_nen_van_la_su_kien(monkeypatch):
    """Route /admin/api/avatar/chat thật đi qua CompressionMiddleware với trình duyệt
    nhận gzip: vẫn text/event-stream, không Content-Encoding."""
    import web_admin
    import web_avatar

    async def answer(text, sink):
        sink("Em đây")
        return {"text": "Em đây nè", "mood": "happy"}

    monkeypatch.setattr(web_avatar, "_answer", answer)
    app = FastAPI()
    app.include_router(web_avatar.api_router)
    app.dependency_overrides[web_admin.require_admin] = lambda: None
    app.add_middleware(web.CompressionMiddleware)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        r = await c.post(
            "/admin/api/avatar/chat", json={"text": "chào em"}, headers={"Accept-Encoding": "gzip"}
        )
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in r.headers
    assert '"text":"Em đây nè"' in r.text


# ─── ETag, 304, Cache-Control ────────────────────────────────────────────────


async def test_trang_theo_url_cache_cong_khai_etag_va_304(client, monkeypatch):
    _fix_now(monkeypatch, dt.datetime(2026, 11, 27, 10, 0, tzinfo=VN))
    r = await client.get("/lich?thang=2027-01")
    assert r.headers["cache-control"] == "public, max-age=3600"
    assert "Cookie" in r.headers["vary"]
    etag = r.headers["etag"]
    assert etag.startswith('W/"')
    again = await client.get("/lich?thang=2027-01", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    assert (
        again.headers["etag"] == etag and again.headers["cache-control"] == "public, max-age=3600"
    )
    assert "Cookie" in again.headers["vary"]
    # Danh sách nhiều ETag / bản không có W/ vẫn khớp; ETag khác thì 200 đủ trang.
    assert (
        await client.get("/lich?thang=2027-01", headers={"If-None-Match": f'"x", {etag[2:]}'})
    ).status_code == 304
    other = await client.get("/lich?thang=2027-01", headers={"If-None-Match": 'W/"khac"'})
    assert other.status_code == 200 and "Lịch vạn niên" in other.text
    for path in (
        "/lich?ngay=2027-02-18",
        "/tuoi/1990",
        "/lich/ngay-tot?thang=2027-01",
        "/tu-vi",
        "/than-so-hoc",
        "/lich/le-hoi?nam=2027",
        "/lich/hoi",
        "/tuoi",
    ):
        resp = await client.get(path)
        assert resp.headers["cache-control"] == "public, max-age=3600", path
        assert resp.headers["etag"].startswith('W/"'), path


async def test_trang_theo_hom_nay_no_cache_nhung_van_304(client, monkeypatch):
    _fix_now(monkeypatch, dt.datetime(2026, 11, 27, 10, 0, tzinfo=VN))
    for path in (
        "/lich",
        "/lich?thang=2026-11",
        "/lich?ngay=2026-11-03",
        "/lich/ngay-tot",
        "/lich/ngay-le",
        "/lich/ngay-le/tet-nguyen-dan",
    ):
        r = await client.get(path, headers={"Accept": "text/html"})
        assert r.headers["cache-control"] == "no-cache", path
        again = await client.get(path, headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304, path
    home = await client.get("/", headers={"Accept": "text/html"})
    assert home.headers["cache-control"] == "no-cache" and home.headers["etag"]
    same = await client.get(
        "/", headers={"Accept": "text/html", "If-None-Match": home.headers["etag"]}
    )
    assert same.status_code == 304
    # Trang lỗi nhập liệu (noindex): không cho bộ đệm công khai giữ.
    bad = await client.get("/lich?thang=abc")
    assert bad.headers["cache-control"] == "no-cache" and "noindex" in bad.text


async def test_cache_khong_qua_nua_dem_gio_viet_nam(client, monkeypatch):
    _fix_now(monkeypatch, dt.datetime(2026, 11, 27, 23, 30, tzinfo=VN))
    r = await client.get("/tuoi/1990")
    assert r.headers["cache-control"] == "public, max-age=1800"
    assert web_site._until_midnight(dt.datetime(2026, 12, 31, 23, 59, 59, tzinfo=VN)) == 1


async def test_da_dang_nhap_chi_cache_rieng(client, monkeypatch):
    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)
    r = await client.get("/lich?thang=2027-01")
    assert r.headers["cache-control"] == "private, no-cache"
    assert ">Quản trị</a>" in r.text and "Cookie" in r.headers["vary"]


async def test_api_lich_theo_ngay_thang_cache_va_304(client):
    for url in ("/api/v1/calendar/days/2026-11-27", "/api/v1/calendar/months/2027-01"):
        r = await client.get(url)
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=3600"
        again = await client.get(url, headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304 and again.headers["etag"] == r.headers["etag"]
    bad = await client.get("/api/v1/calendar/days/2026-13-45")
    assert bad.status_code == 422 and "etag" not in bad.headers


def test_so_khop_if_none_match():
    tag = web_site.weak_etag(b"abc")
    assert web_site.etag_matches(tag, tag) and web_site.etag_matches("*", tag)
    assert web_site.etag_matches(f'W/"zzz" , {tag}', tag)
    assert web_site.etag_matches(tag.removeprefix("W/"), tag)
    assert not web_site.etag_matches("", tag) and not web_site.etag_matches('W/"zzz"', tag)


def test_template_chi_doc_lai_khi_chay_may_dev(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    assert web_site._template_reload() is False
    monkeypatch.delenv("RENDER")
    assert web_site._template_reload() is True


# ─── Trang lễ hội: LRU bản đã nén ────────────────────────────────────────────


async def test_le_hoi_giu_ban_nen_san_khong_render_lai(client, monkeypatch):
    calls = []
    real = web_site._festival_page

    def counting(*args):
        calls.append(args[1:3])
        return real(*args)

    monkeypatch.setattr(web_site, "_festival_page", counting)
    first = await client.get("/lich/le-hoi?nam=2027", headers={"Accept-Encoding": "gzip"})
    second = await client.get("/lich/le-hoi?nam=2027", headers={"Accept-Encoding": "gzip"})
    assert calls == [(2027, "")]  # lượt 2 không render
    assert second.headers["content-encoding"] == "gzip" and second.text == first.text
    assert "Hội Lim" in second.text and second.headers["etag"] == first.headers["etag"]
    assert "Cookie" in second.headers["vary"] and "Accept-Encoding" in second.headers["vary"]
    # Client không nhận gzip: giải nén từ cùng bản cache, vẫn không render lại.
    plain = await client.get("/lich/le-hoi?nam=2027", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.text == first.text
    assert calls == [(2027, "")]
    assert (
        await client.get("/lich/le-hoi?nam=2027", headers={"If-None-Match": first.headers["etag"]})
    ).status_code == 304
    # Đã đăng nhập (thanh bên khác) là bản riêng.
    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)
    owner = await client.get("/lich/le-hoi?nam=2027")
    assert ">Quản trị</a>" in owner.text and owner.headers["cache-control"] == "private, no-cache"
    assert len(calls) == 2


async def test_le_hoi_lru_co_tran_va_html_gon(client, monkeypatch):
    monkeypatch.setattr(web_site, "_FESTIVAL_PAGES_MAX", 3)
    for year in range(2020, 2026):
        await client.get(f"/lich/le-hoi?nam={year}")
    assert len(web_site._festival_pages) == 3
    assert [key[0] for key in web_site._festival_pages] == [2023, 2024, 2025]
    html = (await client.get("/lich/le-hoi?nam=2026", headers={"Accept-Encoding": "identity"})).text
    # Mỗi lễ hội 1 dòng HTML, dòng phụ <small>: trang cả nước dưới 110 KB (trước 118 KB).
    assert len(html.encode()) < 110_000 and '<table class="fest">' in html
    assert re.search(r"\n<tr><td><a href=\"/lich\?ngay=2026-", html)
    css = (await client.get("/static/site.css")).text
    assert ".fest small" in css


# ─── Trang tĩnh, header bảo mật, Zoom, AI bận ────────────────────────────────


async def test_trang_chinh_sach_doc_mot_lan_cache_mot_ngay(client, monkeypatch):
    web._legal_page.cache_clear()
    reads = []
    real = web.Path.read_text

    def counting(self, *args, **kwargs):
        reads.append(self.name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(web.Path, "read_text", counting)
    for path in ("/privacy", "/terms", "/data-deletion", "/privacy"):
        r = await client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=86400"
        assert 'media="print" id="font-css"' in r.text and "<noscript>" in r.text
    assert reads.count("privacy.html") == 1


async def test_phong_chu_khong_chan_ve_trang(client):
    html = (await client.get("/lich")).text
    head = html[: html.index("</head>")]
    assert '<link rel="preload" as="style" href="https://fonts.googleapis.com/css2?' in head
    assert re.search(
        r'<link rel="stylesheet" href="https://fonts[^"]+" media="print" id="font-css">', head
    )
    assert '<noscript><link rel="stylesheet" href="https://fonts.googleapis.com' in head
    assert "display=swap" in head and "css.media = 'all'" in head
    # Không còn thẻ stylesheet phông chặn vẽ (không có media).
    assert not re.search(r'<link rel="stylesheet" href="https://fonts[^"]+">\s*<script', head)


async def test_header_bao_mat_theo_duong_dan(client):
    page = await client.get("/lich")
    assert "camera=()" in page.headers["permissions-policy"]
    assert "microphone=()" in page.headers["permissions-policy"]
    assert "geolocation=()" in page.headers["permissions-policy"]
    assert permissions_policy("/thoi-tiet").decode().count("geolocation=(self)") == 1
    assert b"microphone=(self)" in permissions_policy("/admin/avatar")
    assert b"microphone=()" in permissions_policy("/admin")
    assert b"camera=()" in permissions_policy("/admin/avatar")
    assert b"geolocation=()" in permissions_policy("/thoi-tietx")
    api = await client.get("/admin/api/state")
    assert api.status_code == 403 and api.headers["cache-control"] == "no-store"


async def test_admin_api_khong_ghi_de_cache_control_app_tu_dat():
    sent = []

    async def app(scope, receive, send):
        headers = [(b"cache-control", b"private, no-store")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"{}"})

    async def send(message):
        sent.append(message)

    await SecurityHeadersMiddleware(app)(
        {"type": "http", "path": "/admin/api/avatar/x"}, None, send
    )
    values = [v for k, v in sent[0]["headers"] if k == b"cache-control"]
    assert values == [b"private, no-store"]


def test_zoom_cat_tin_theo_tran_4096_ky_tu():
    paragraph = "Câu trả lời dài. " * 20
    text = "\n\n".join([paragraph] * 9)  # ~3.100 ký tự: Zalo cắt 2 đoạn, Zoom 1
    assert 1800 < len(text) < 4096
    assert web._zoom_chunks([text]) == [text.strip()]
    long = "\n\n".join([paragraph] * 15)
    chunks = web._zoom_chunks([long])
    assert len(chunks) == 2 and all(len(c) <= 4096 for c in chunks)


async def test_ai_luan_giai_ban_qua_lau_thi_bao_ban(client, monkeypatch):
    """Chủ bot đang có lượt chat dài giữ khoá: nút AI trên web chờ tối đa
    _AI_LOCK_WAIT_SEC rồi báo bận (vẫn hiện nút để bấm lại), không treo request."""
    monkeypatch.setattr(web_site, "_AI_LOCK_WAIT_SEC", 0.05)
    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)
    calls = []

    async def fake_ai_text(user_id, kind, channel, explain):
        calls.append(kind)
        return "Luận giải."

    monkeypatch.setattr(web_site.divination_commands, "ai_text", fake_ai_text)
    form = {
        "ngay": "15",
        "thang": "3",
        "nam": "1990",
        "gio": "5",
        "gioi": "male",
        "lich": "duong",
        "ai": "1",
    }
    holding, release = asyncio.Event(), asyncio.Event()

    async def long_turn():
        async with concurrency.assistant_turn(concurrency.OWNER_TURN_KEY):
            holding.set()
            await release.wait()

    task = asyncio.create_task(long_turn())
    await holding.wait()
    try:
        html = (await client.post("/tu-vi", data=form)).text
        assert calls == [] and web_site._AI_BUSY_TEXT in html
        assert 'name="ai" value="1"' in html  # nút bấm lại vẫn còn
    finally:
        release.set()
        await task
    assert not concurrency.is_busy(concurrency.OWNER_TURN_KEY)  # chờ bị huỷ không rò khoá
    html = (await client.post("/tu-vi", data=form)).text
    assert calls == ["tuvi_chitiet"] and "Luận giải." in html


async def test_form_tu_trang_khac_khong_duoc_coi_la_chu_trang(client, monkeypatch):
    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)
    calls = []

    async def fake_ai_text(*args):
        calls.append(args)
        return "x"

    monkeypatch.setattr(web_site.divination_commands, "ai_text", fake_ai_text)
    form = {"ngay": "1990-03-15", "ai": "1"}
    html = (
        await client.post("/than-so-hoc", data=form, headers={"Sec-Fetch-Site": "cross-site"})
    ).text
    assert calls == [] and ">Quản trị</a>" not in html
    await client.post("/than-so-hoc", data=form, headers={"Sec-Fetch-Site": "same-origin"})
    assert len(calls) == 1
