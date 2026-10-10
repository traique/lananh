import httpx
import pytest

from services import http_client


@pytest.mark.asyncio
async def test_scoped_client_reuses_one_client_and_applies_timeout_and_headers(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_client, "_client", shared)

    async with http_client.scoped(timeout=7, headers={"User-Agent": "bot"}) as client:
        await client.get("https://graph.example/a", headers={"X-Extra": "1"})
    async with http_client.scoped() as client:
        await client.post("https://graph.example/b", json={})

    assert http_client.get_client() is shared and not shared.is_closed  # không bị đóng sau khối
    assert seen[0].headers["user-agent"] == "bot" and seen[0].headers["x-extra"] == "1"
    assert seen[0].extensions["timeout"]["read"] == 7
    assert seen[1].method == "POST"
    await http_client.close()
    assert http_client._client is None
