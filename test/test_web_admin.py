import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

import web_admin

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings_store(monkeypatch):
    """Bảng settings giả trong RAM cho epoch phiên admin."""
    store: dict[str, str] = {}

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    monkeypatch.setattr(web_admin.db, "get_setting", get_setting)
    monkeypatch.setattr(web_admin.db, "set_setting", set_setting)
    return store


@pytest.fixture
def sleeps(monkeypatch):
    """Ghi lại các lần làm chậm đăng nhập thay vì ngủ thật."""
    recorded: list[float] = []

    async def fake_sleep(delay):
        recorded.append(delay)

    monkeypatch.setattr(web_admin.asyncio, "sleep", fake_sleep)
    return recorded


@pytest.fixture
async def client(monkeypatch, settings_store, sleeps):
    monkeypatch.setattr(web_admin.config, "ADMIN_USER", "admin")
    monkeypatch.setattr(web_admin.config, "ADMIN_PASS", "pass")
    monkeypatch.setattr(web_admin, "_login_limiter", web_admin.LoginRateLimiter(max_failures=3))
    monkeypatch.setattr(web_admin, "_login_slowdown", web_admin.LoginSlowdown())
    monkeypatch.setattr(web_admin, "_session_epoch", web_admin.SessionEpoch())
    app = FastAPI()
    app.include_router(web_admin.router)
    app.include_router(web_admin.api_router)
    # Dùng httpx.ASGITransport: TestClient của starlette 0.36 không tương thích httpx 0.28.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


async def _login(client, **kwargs):
    return await client.post(
        "/admin/login", data={"username": "admin", "password": "pass"}, **kwargs
    )


async def test_admin_api_requires_session(client):
    assert (await client.get("/admin/api/state")).status_code == 403


async def test_login_is_locked_after_repeated_failures(client):
    for _ in range(3):
        r = await client.post("/admin/login", data={"username": "admin", "password": "x"})
        assert r.status_code == 401
    r = await _login(client)
    assert r.status_code == 429  # cùng IP: đúng mật khẩu cũng bị chặn khi đang khoá


async def test_login_success_sets_cookie_and_unlocks_api(client, monkeypatch):
    monkeypatch.setattr(web_admin.orchestrator, "get_provider_state_snapshot", lambda: {"ok": 1})
    r = await _login(client)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert (await client.get("/admin/api/state")).json() == {"ok": 1}


async def test_non_ascii_credentials_do_not_crash(client):
    r = await client.post("/admin/login", data={"username": "quản trị", "password": "mật khẩu"})
    assert r.status_code == 401


async def test_global_failures_slow_down_but_never_lock_out_owner(client, sleeps):
    # Kẻ tấn công xoay nhiều IP thật: mỗi IP dưới ngưỡng khoá, tổng số lần sai lớn.
    for i in range(30):
        r = await client.post(
            "/admin/login",
            data={"username": "a", "password": "b"},
            headers={"x-forwarded-for": f"10.0.{i // 2}.{i}"},
        )
        assert r.status_code == 401
    r = await _login(client, headers={"x-forwarded-for": "198.51.100.7"})
    assert r.status_code == 303  # mật khẩu đúng vẫn vào được
    assert sleeps and max(sleeps) <= web_admin._login_slowdown.max_delay_sec
    assert sleeps[-1] == web_admin._login_slowdown.max_delay_sec


async def test_spoofed_forwarded_prefix_does_not_bypass_ip_lock(client):
    # Client tự thêm IP giả ở đầu X-Forwarded-For; proxy Render nối IP thật vào cuối.
    for i in range(3):
        await client.post(
            "/admin/login",
            data={"username": "a", "password": "b"},
            headers={"x-forwarded-for": f"1.2.3.{i}, 203.0.113.9"},
        )
    r = await _login(client, headers={"x-forwarded-for": "1.2.3.99, 203.0.113.9"})
    assert r.status_code == 429


def _request(headers: list[tuple[bytes, bytes]], client=("192.0.2.1", 1234)) -> Request:
    return Request({"type": "http", "headers": headers, "client": client})


def test_client_ip_respects_trusted_proxy_hops_and_ip_header(monkeypatch):
    chain = [(b"x-forwarded-for", b"6.6.6.6, 1.1.1.1, 10.0.0.5")]
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_HOPS", "2")
    assert web_admin._client_ip(_request(chain)) == "1.1.1.1"
    # Chuỗi ngắn hơn số hop tin cậy: lấy phần tử đầu (do proxy gần nhất ghi).
    assert web_admin._client_ip(_request([(b"x-forwarded-for", b"1.1.1.1")])) == "1.1.1.1"
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_HOPS", "abc")
    assert web_admin._client_ip(_request(chain)) == "10.0.0.5"
    monkeypatch.setenv("ADMIN_CLIENT_IP_HEADER", "True-Client-IP")
    headers = [*chain, (b"true-client-ip", b"203.0.113.50")]
    assert web_admin._client_ip(_request(headers)) == "203.0.113.50"
    # Header cấu hình nhưng vắng mặt -> quay về X-Forwarded-For.
    assert web_admin._client_ip(_request(chain)) == "10.0.0.5"


def test_client_ip_uses_rightmost_forwarded_entry():
    assert web_admin._client_ip(_request([(b"x-forwarded-for", b"6.6.6.6, 1.1.1.1")])) == "1.1.1.1"
    # Proxy thêm dòng header riêng: vẫn lấy phần tử cuối cùng.
    headers = [(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", b"2.2.2.2")]
    assert web_admin._client_ip(_request(headers)) == "2.2.2.2"
    assert web_admin._client_ip(_request([])) == "192.0.2.1"
    assert web_admin._client_ip(_request([(b"x-forwarded-for", b"6.6.6.6, ")])) == "192.0.2.1"


async def test_logout_revokes_existing_sessions(client, monkeypatch, settings_store):
    monkeypatch.setattr(web_admin.orchestrator, "get_provider_state_snapshot", lambda: {"ok": 1})
    await _login(client)
    old_cookie = client.cookies.get("admin_session")
    assert old_cookie
    old_epoch = settings_store[web_admin._SESSION_EPOCH_KEY]

    r = await client.post("/admin/logout")
    assert r.status_code == 303
    assert settings_store[web_admin._SESSION_EPOCH_KEY] != old_epoch
    client.cookies.clear()
    # Cookie cũ (vd bị lộ) không dùng lại được sau khi đăng xuất.
    replay = await client.get("/admin/api/state", headers={"cookie": f"admin_session={old_cookie}"})
    assert replay.status_code == 403

    await _login(client)
    assert (await client.get("/admin/api/state")).status_code == 200


async def test_logout_without_session_does_not_rotate_epoch(client, settings_store):
    await _login(client)
    epoch = settings_store[web_admin._SESSION_EPOCH_KEY]
    client.cookies.clear()
    await client.post("/admin/logout")
    assert settings_store[web_admin._SESSION_EPOCH_KEY] == epoch


async def test_epoch_survives_restart_via_settings(client, monkeypatch):
    monkeypatch.setattr(web_admin.orchestrator, "get_provider_state_snapshot", lambda: {"ok": 1})
    await _login(client)
    # Process mới: cache RAM rỗng, epoch đọc lại từ bảng settings => phiên vẫn hợp lệ.
    monkeypatch.setattr(web_admin, "_session_epoch", web_admin.SessionEpoch())
    assert (await client.get("/admin/api/state")).status_code == 200


async def test_session_fails_closed_when_db_unavailable(client, monkeypatch):
    await _login(client)
    cookie = client.cookies.get("admin_session")

    async def broken(*args):
        raise OSError("db down")

    monkeypatch.setattr(web_admin.db, "get_setting", broken)
    monkeypatch.setattr(web_admin.db, "set_setting", broken)
    monkeypatch.setattr(web_admin, "_session_epoch", web_admin.SessionEpoch())
    client.cookies.clear()
    r = await client.get("/admin/api/state", headers={"cookie": f"admin_session={cookie}"})
    assert r.status_code == 403
    r = await _login(client)
    assert r.status_code == 503
    assert "set-cookie" not in r.headers


async def test_forged_or_garbage_cookie_is_rejected(client):
    for value in ("9999999999.deadbeef", "abc.def", "9999999999.éé", "."):
        cookie = f"admin_session={value}".encode()  # bytes: cho phép ký tự non-ASCII
        r = await client.get("/admin/api/state", headers={"cookie": cookie})
        assert r.status_code == 403


def test_limiter_is_bounded_in_memory():
    limiter = web_admin.LoginRateLimiter(max_tracked=3)
    for i in range(10):
        limiter.record_failure(f"ip{i}")
    assert len(limiter._failures) <= 3


def test_slowdown_is_bounded_and_capped():
    slowdown = web_admin.LoginSlowdown(
        free_failures=2, step_sec=1.0, max_delay_sec=3.0, max_tracked=5
    )
    assert slowdown.delay() == 0
    for _ in range(100):
        slowdown.record_failure()
    assert len(slowdown._failures) == 5
    assert slowdown.delay() == 3.0


async def test_memory_usage_endpoint_requires_login_and_returns_json(client):
    assert (await client.get("/admin/api/memory-usage")).status_code == 403
    await _login(client)
    body = (await client.get("/admin/api/memory-usage")).json()
    assert "container" in body and "python" in body


async def test_admin_page_serves_merged_template_with_market_panel(client):
    await _login(client)
    html = (await client.get("/admin")).text
    assert 'id="market-box"' in html and "/admin/api/market_page" in html
    assert not (ROOT / "admin.html").exists()  # chỉ còn 1 bản ở templates/


def test_admin_template_escapes_server_values():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "function escapeHtml" in html
    # Không còn chèn thẳng field từ server (u.label, r.model, p.provider...) vào HTML.
    raw = re.findall(r"\$\{\s*[uprtam]\.\w+\s*\}", html)
    assert raw == []
    for field in ("u.label", "r.label", "r.model", "p.model", "p.base_url", "a.model"):
        assert f"esc({field})" in html
