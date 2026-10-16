"""Bridge /internal/zalo: chỉ nhận gateway gọi qua loopback + báo lỗi outbox."""

import httpx
import pytest
from fastapi import FastAPI

from channels import router as zalo_router

SECRET = "s3cr3t"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(zalo_router.router)
    return app


async def _call(
    method: str,
    path: str,
    *,
    host: str = "127.0.0.1",
    headers: dict | None = None,
    json: dict | None = None,
):
    transport = httpx.ASGITransport(app=_app(), client=(host, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        return await client.request(
            method,
            path,
            headers={"x-zalo-bridge-secret": SECRET, **(headers or {})},
            json=json,
        )


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(zalo_router, "_secret", lambda: SECRET)
    monkeypatch.delenv("ZALO_BRIDGE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("ZALO_BRIDGE_ALLOW_FORWARDED", raising=False)
    calls = {"outbox": [], "failed": []}

    async def fake_outbox(account_id, limit=20):
        calls["outbox"].append(account_id)
        return [{"id": 1, "recipient_id": "u", "content": "hi", "chunks_sent": 0}]

    async def fake_failed(item_id, error, chunks_sent=0):
        calls["failed"].append((item_id, error, chunks_sent))
        return calls.get("failed_row")

    monkeypatch.setattr(zalo_router.zalo_repository, "get_account_outbox", fake_outbox)
    monkeypatch.setattr(zalo_router.zalo_repository, "mark_outbox_failed", fake_failed)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
async def test_loopback_gateway_is_allowed(bridge, host):
    response = await _call("GET", "/internal/zalo/outbox/acc", host=host)
    assert response.status_code == 200
    assert response.json()[0]["chunks_sent"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["203.0.113.7", "10.1.2.3", "testclient", ""])
async def test_non_loopback_caller_is_rejected_even_with_secret(bridge, host):
    response = await _call("GET", "/internal/zalo/outbox/acc", host=host)
    assert response.status_code == 403
    assert bridge["outbox"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        {"x-forwarded-for": "127.0.0.1"},
        {"forwarded": "for=127.0.0.1"},
        {"x-real-ip": "127.0.0.1"},
    ],
)
async def test_proxied_request_is_rejected_by_default(bridge, header):
    # uvicorn tin proxy (FORWARDED_ALLOW_IPS) sẽ đổi client.host thành 127.0.0.1,
    # nhưng header proxy vẫn còn -> vẫn phải chặn.
    response = await _call("GET", "/internal/zalo/outbox/acc", headers=header)
    assert response.status_code == 403
    assert bridge["outbox"] == []


@pytest.mark.asyncio
async def test_allowed_hosts_and_forwarded_are_configurable(bridge, monkeypatch):
    monkeypatch.setenv("ZALO_BRIDGE_ALLOWED_HOSTS", "testclient, 10.0.0.0/8")
    assert (await _call("GET", "/internal/zalo/outbox/a", host="testclient")).status_code == 200
    assert (await _call("GET", "/internal/zalo/outbox/a", host="10.9.8.7")).status_code == 200
    # Danh sách mới THAY mặc định: loopback không còn tự được phép.
    assert (await _call("GET", "/internal/zalo/outbox/a", host="127.0.0.1")).status_code == 403
    proxied = {"x-forwarded-for": "1.2.3.4"}
    assert (
        await _call("GET", "/internal/zalo/outbox/a", host="10.9.8.7", headers=proxied)
    ).status_code == 403
    monkeypatch.setenv("ZALO_BRIDGE_ALLOW_FORWARDED", "1")
    assert (
        await _call("GET", "/internal/zalo/outbox/a", host="10.9.8.7", headers=proxied)
    ).status_code == 200


@pytest.mark.asyncio
async def test_blank_allowed_hosts_falls_back_to_loopback(bridge, monkeypatch):
    monkeypatch.setenv("ZALO_BRIDGE_ALLOWED_HOSTS", "  ")
    assert (await _call("GET", "/internal/zalo/outbox/a")).status_code == 200
    assert (await _call("GET", "/internal/zalo/outbox/a", host="8.8.8.8")).status_code == 403


@pytest.mark.asyncio
async def test_loopback_still_needs_secret(bridge):
    response = await _call(
        "GET", "/internal/zalo/outbox/acc", headers={"x-zalo-bridge-secret": "wrong"}
    )
    assert response.status_code == 403
    assert bridge["outbox"] == []


@pytest.mark.asyncio
async def test_outbox_failure_schedules_retry(bridge):
    bridge["failed_row"] = {"id": 7, "recipient_id": "u", "attempts": 2, "failed": False}
    response = await _call(
        "POST",
        "/internal/zalo/outbox/7/failed",
        json={"error": "  user\nblocked   bot ", "chunks_sent": 1},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "retry", "attempts": 2}
    assert bridge["failed"] == [(7, "user blocked bot", 1)]


@pytest.mark.asyncio
async def test_outbox_failure_gives_up_and_alerts_owner_once(bridge, monkeypatch):
    alerts = []
    monkeypatch.setattr(zalo_router.alerts, "send", lambda text: alerts.append(text) or True)
    bridge["failed_row"] = {"id": 7, "recipient_id": "u9", "attempts": 5, "failed": True}
    response = await _call(
        "POST", "/internal/zalo/outbox/7/failed", json={"error": "x" * 5000, "chunks_sent": 0}
    )
    assert response.json() == {"status": "failed", "attempts": 5}
    item_id, error, _ = bridge["failed"][0]
    assert item_id == 7 and len(error) == zalo_router.zalo_repository.OUTBOX_ERROR_MAX_CHARS
    assert len(alerts) == 1 and "#7" in alerts[0] and "u9" in alerts[0]


@pytest.mark.asyncio
async def test_outbox_failure_for_unknown_or_done_item_is_ignored(bridge):
    response = await _call("POST", "/internal/zalo/outbox/99/failed", json={})
    assert response.json() == {"status": "ignored"}
    assert bridge["failed"] == [(99, "lỗi không rõ", 0)]


@pytest.mark.asyncio
async def test_outbox_failure_rejects_negative_chunks(bridge):
    response = await _call(
        "POST", "/internal/zalo/outbox/1/failed", json={"error": "e", "chunks_sent": -1}
    )
    assert response.status_code == 422
    assert bridge["failed"] == []


# ─── /gateway-state: gateway báo mất kết nối/kết nối lại ─────────────────────


@pytest.mark.asyncio
async def test_gateway_state_alerts_owner_once_per_state_change(bridge, monkeypatch):
    sent = []
    monkeypatch.setattr(zalo_router.alerts, "send", lambda text: sent.append(text) or True)
    zalo_router._last_reported_gateway_state.clear()

    async def post(state, detail=""):
        return await _call(
            "POST",
            "/internal/zalo/gateway-state",
            json={"account_id": "acc", "state": state, "detail": detail},
        )

    # connected lúc khởi động bình thường: không báo.
    assert (await post("connected", "khởi động")).status_code == 204
    assert sent == []
    # closed -> báo 1 lần dù gateway gửi lặp.
    assert (await post("closed", "code=1006")).status_code == 204
    assert (await post("closed", "code=1006")).status_code == 204
    assert len(sent) == 1 and "mất kết nối" in sent[0]
    # reconnecting không bao giờ báo.
    assert (await post("reconnecting")).status_code == 204
    assert len(sent) == 1
    # kết nối lại sau khi đã báo mất -> báo "đã kết nối lại".
    assert (await post("connected", "sau 2 lần thử")).status_code == 204
    assert len(sent) == 2 and "kết nối lại" in sent[1]
    # send_failed báo mỗi lần (mỗi tin khác nhau).
    assert (await post("send_failed", "tin 1")).status_code == 204
    assert (await post("send_failed", "tin 2")).status_code == 204
    assert len(sent) == 4
    # login_required -> hướng dẫn /zalo.
    assert (await post("login_required", "hết 10 lần")).status_code == 204
    assert "/zalo" in sent[-1]


@pytest.mark.asyncio
async def test_gateway_state_rejects_unknown_state_and_bad_secret(bridge):
    response = await _call(
        "POST", "/internal/zalo/gateway-state", json={"account_id": "acc", "state": "boom"}
    )
    assert response.status_code == 422
    response = await _call(
        "POST",
        "/internal/zalo/gateway-state",
        headers={"x-zalo-bridge-secret": "sai"},
        json={"account_id": "acc", "state": "closed"},
    )
    assert response.status_code == 403
