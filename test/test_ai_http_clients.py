"""Kết nối HTTP có trần: pool của Groq/OpenRouter/9Router có httpx.Limits; Tavily,
Agnes và kiểm link /gia dùng client chung services/http_client (không tạo client +
TLS context mới mỗi lượt), kiểm link vẫn qua lớp chống SSRF public_http."""

import asyncio

import httpx
import pytest

from ai import agnes_client, openai_compatible, tavily_client
from core import config
from services import http_client, price_search


async def test_pool_provider_co_tran_ket_noi():
    pool = openai_compatible.ClientPool(30, 4, 90)
    limits = pool.limits()
    assert limits.max_connections == 5 and limits.max_keepalive_connections == 2
    client = pool.get_client()
    try:
        transport_pool = client._transport._pool
        assert transport_pool._max_connections == 5
        assert transport_pool._max_keepalive_connections == 2
    finally:
        await pool.close()


async def test_tavily_dung_client_chung_voi_timeout_rieng():
    scoped = tavily_client._get_client()
    assert scoped._client is http_client.get_client()
    assert scoped._timeout == config.TAVILY_CALL_TIMEOUT_SEC
    await tavily_client.close()  # không đóng client chung
    assert not http_client.get_client().is_closed


async def test_agnes_tai_qua_client_chung(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, content=b"abc")

    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_client, "get_client", lambda: shared)
    try:
        data = await agnes_client._request_bytes("GET", "https://cdn.example/a.png", 10)
    finally:
        await shared.aclose()
    assert data == b"abc" and seen["url"] == "https://cdn.example/a.png"
    assert seen["timeout"]["read"] == config.AGNES_CALL_TIMEOUT_SEC


async def test_kiem_link_gia_dung_client_chung_co_tran_va_user_agent(monkeypatch):
    from services import web_reader

    monkeypatch.setattr(
        web_reader.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )
    active = {"now": 0, "max": 0}
    agents = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        agents.add(request.headers.get("user-agent"))
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.01)
        active["now"] -= 1
        return httpx.Response(404 if "het-hang" in str(request.url) else 200)

    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_client, "get_client", lambda: shared)
    links = " ".join(f"[Shop {i}](https://shop{i}.vn/p)" for i in range(8))
    text = f"{links} [Hết](https://shop9.vn/het-hang)"
    try:
        checked = await price_search.verify_links(text)
    finally:
        await shared.aclose()
    assert "[⚠️ Hết (link có thể đã đổi/hết hàng)](https://shop9.vn/het-hang)" in checked
    assert "[Shop 0](https://shop0.vn/p)" in checked
    assert agents == {price_search._LINK_CHECK_HEADERS["User-Agent"]}
    assert active["max"] <= price_search._LINK_CHECK_CONCURRENCY


async def test_kiem_link_gia_van_chan_ip_noi_bo(monkeypatch):
    from services import web_reader

    monkeypatch.setattr(
        web_reader.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 80))]
    )
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(404)

    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_client, "get_client", lambda: shared)
    text = "[Nội bộ](http://noi-bo.local/admin)"
    try:
        assert await price_search.verify_links(text) == text
    finally:
        await shared.aclose()
    assert requested == []


@pytest.fixture(autouse=True)
async def _close_shared_client():
    yield
    await http_client.close()
