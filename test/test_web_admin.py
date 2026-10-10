import pytest
from fastapi import FastAPI
import httpx

import web_admin


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(web_admin.config, "ADMIN_USER", "admin")
    monkeypatch.setattr(web_admin.config, "ADMIN_PASS", "pass")
    monkeypatch.setattr(web_admin, "_login_limiter", web_admin.LoginRateLimiter(max_failures=3))
    monkeypatch.setattr(
        web_admin, "_global_login_limiter", web_admin.LoginRateLimiter(max_failures=100)
    )
    app = FastAPI()
    app.include_router(web_admin.router)
    app.include_router(web_admin.api_router)
    # Dùng httpx.ASGITransport: TestClient của starlette 0.36 không tương thích httpx 0.28.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


async def test_admin_api_requires_session(client):
    assert (await client.get("/admin/api/state")).status_code == 403


async def test_login_is_locked_after_repeated_failures(client):
    for _ in range(3):
        r = await client.post("/admin/login", data={"username": "admin", "password": "x"})
        assert r.status_code == 401
    r = await client.post("/admin/login", data={"username": "admin", "password": "pass"})
    assert r.status_code == 429  # đúng mật khẩu cũng bị chặn khi đang khoá


async def test_login_success_sets_cookie_and_unlocks_api(client, monkeypatch):
    monkeypatch.setattr(web_admin.orchestrator, "get_provider_state_snapshot", lambda: {"ok": 1})
    r = await client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass"},
    )
    assert r.status_code == 303
    assert (await client.get("/admin/api/state")).json() == {"ok": 1}


async def test_non_ascii_credentials_do_not_crash(client):
    r = await client.post("/admin/login", data={"username": "quản trị", "password": "mật khẩu"})
    assert r.status_code == 401


async def test_global_limiter_blocks_rotating_forwarded_ips(client, monkeypatch):
    monkeypatch.setattr(
        web_admin, "_global_login_limiter", web_admin.LoginRateLimiter(max_failures=2)
    )
    for i in range(2):
        await client.post(
            "/admin/login",
            data={"username": "a", "password": "b"},
            headers={"x-forwarded-for": f"10.0.0.{i}"},
        )
    r = await client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass"},
        headers={"x-forwarded-for": "10.0.0.99"},
    )
    assert r.status_code == 429


def test_limiter_is_bounded_in_memory():
    limiter = web_admin.LoginRateLimiter(max_tracked=3)
    for i in range(10):
        limiter.record_failure(f"ip{i}")
    assert len(limiter._failures) <= 3


async def test_memory_usage_endpoint_requires_login_and_returns_json(client):
    assert (await client.get("/admin/api/memory-usage")).status_code == 403
    await client.post("/admin/login", data={"username": "admin", "password": "pass"})
    body = (await client.get("/admin/api/memory-usage")).json()
    assert "container" in body and "python" in body
