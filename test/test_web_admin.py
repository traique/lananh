import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

import web_admin

ROOT = Path(__file__).resolve().parent.parent
# Bí mật server cho khoá ký cookie phiên (HKDF cùng ADMIN_PASS); dạng như Fernet key.
ENC_KEY = "dGVzdC1vbmx5LWtleS1mb3ItYWRtaW4tc2Vzc2lvbnM="


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
    monkeypatch.setattr(web_admin.config, "SETTINGS_ENC_KEY", ENC_KEY)
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


async def test_login_quay_lai_trang_dang_mo_va_chan_open_redirect(client):
    page = (await client.get("/admin", params={"next": '/lich?x=1&y="<b>'})).text
    assert 'name="next" value="/lich?x=1&amp;y=&quot;&lt;b&gt;"' in page  # escape trong HTML
    r = await client.post(
        "/admin/login", data={"username": "admin", "password": "pass", "next": "/thoi-tiet?noi=Huế"}
    )
    assert r.status_code == 303 and r.headers["location"] == "/thoi-tiet?noi=Hu%E1%BA%BF"
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "lich", "/a\nb", "/" + "x" * 400):
        assert web_admin.safe_next(bad) == "/admin", bad
    r = await client.post(
        "/admin/login", data={"username": "admin", "password": "pass", "next": "//evil.com"}
    )
    assert r.headers["location"] == "/admin"
    # Sai mật khẩu vẫn giữ trang cần quay lại.
    r = await client.post(
        "/admin/login", data={"username": "admin", "password": "x", "next": "/tu-vi"}
    )
    assert r.status_code == 401 and 'name="next" value="/tu-vi"' in r.text


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


async def test_usage_daily_buckets_by_hour_or_day_and_zero_fills(client, monkeypatch):
    from datetime import datetime, timedelta

    class FakePool:
        def __init__(self):
            self.queries: list[tuple[str, tuple]] = []

        async def fetch(self, sql, *args):
            self.queries.append((sql, args))
            now_vn = datetime.now(web_admin._VN_TZ).replace(tzinfo=None)
            bucket = "hour" if "date_trunc('hour'" in sql else "day"
            point = now_vn.replace(minute=0, second=0, microsecond=0)
            if bucket == "day":
                point = point.replace(hour=0) - timedelta(days=2)
            return [{"bucket": point, "calls": 7}]

    pool = FakePool()

    async def get_pool():
        return pool

    monkeypatch.setattr(web_admin.db, "get_pool", get_pool)
    assert (await client.get("/admin/api/usage/daily")).status_code == 403
    await _login(client)

    body = (await client.get("/admin/api/usage/daily?hours=24")).json()
    assert body["bucket"] == "hour" and body["timezone"] == "Asia/Ho_Chi_Minh"
    assert len(body["points"]) == 25  # 24 giờ trước ... giờ hiện tại, điền 0 cho mốc trống
    assert [p["calls"] for p in body["points"]][-1] == 7
    assert all(p["calls"] == 0 for p in body["points"][:-1])
    assert pool.queries[-1][1] == ("24",)

    body = (await client.get("/admin/api/usage/daily?hours=168")).json()
    assert body["bucket"] == "day" and len(body["points"]) == 8
    assert body["points"][-3]["calls"] == 7 and sum(p["calls"] for p in body["points"]) == 7
    assert all(p["at"].endswith("T00:00") for p in body["points"])
    # Khoảng 1 năm vẫn có trần số điểm (theo ngày), không phình RAM.
    body = (await client.get("/admin/api/usage/daily?hours=99999")).json()
    assert body["bucket"] == "day" and len(body["points"]) <= 367

    r = await client.get("/admin/api/usage/daily?hours=abc")
    assert r.status_code == 400


def _templates():
    return {p.name: p.read_text(encoding="utf-8") for p in (ROOT / "templates").glob("*.html")}


def test_templates_have_theme_tokens_and_no_inline_handlers():
    for name, html in _templates().items():
        assert ":root{" in html.replace(" ", "") or ":root {" in html, name
        assert "prefers-color-scheme" in html and ':root[data-theme="dark"]' in html, name
        assert ':root:not([data-theme="light"])' in html, name
        # Không có handler inline (onclick="...") - mọi sự kiện gắn bằng addEventListener.
        assert re.search(r"<[^>]+\son\w+\s*=", html) is None, name
        # Không nạp script ngoài (trang phải chạy được không cần CDN, hợp CSP).
        assert re.search(r"<script[^>]+src=", html) is None, name
        assert 'lang="vi"' in html, name


def test_admin_template_sections_and_controls():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    for section in ("overview", "ai", "providers", "users", "journal", "market", "system"):
        assert f'<section id="{section}"' in html
    for control in ("refresh-btn", "auto-refresh", "range", "chart", "chart-table"):
        assert f'id="{control}"' in html
    assert "/admin/api/usage/daily" in html and "localStorage" in html
    # Truy cập localStorage luôn trong try/catch.
    assert "try { localStorage" in html or "try {\n      var saved = localStorage" in html


def test_admin_template_la_dang_tab_khong_cuon_dai():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    # Mỗi lần chỉ hiện 1 mục: ẩn các section khác, đổi theo hash (Back/Forward được).
    assert "function showTab" in html and "sec.hidden = sec.id !== id" in html
    assert "main > section[hidden]{display:none}" in html
    assert "popstate" in html and "aria-current" in html
    assert "IntersectionObserver" not in html  # bỏ scroll-spy của trang dài
    # Có lối sang các trang công cụ của web công khai; link ngoài không bị coi là tab.
    for href in ("/lich", "/thoi-tiet", "/tu-vi", "/than-so-hoc"):
        assert (
            html.count(f'class="ext" href="{href}"') == 1
        )  # chỉ menu bên (điện thoại: bảng danh mục)
    assert "#side-nav a[href^=" in html
    # Điện thoại: thanh bên gập thành bảng danh mục mở bằng nút, Esc đóng.
    assert 'id="menu-btn"' in html and 'aria-controls="side-sheet"' in html and "Escape" in html


def test_admin_dieu_khien_theo_chuan():
    """Khoảng thời gian là segmented (radiogroup), mọi Bật/Tắt là công tắc role=switch,
    ô nhập + nút Lưu dính liền; chỉ một kiểu nút chính."""
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert 'id="range" role="radiogroup"' in html and html.count('role="radio"') == 3
    assert 'role="switch"' in html and "function toggle(" in html
    assert (
        ">${p.enabled ? 'Tắt' : 'Bật'}<" not in html
        and ">${t.enabled ? 'Tắt' : 'Bật'}<" not in html
    )
    assert html.count("btn sm primary") == 0  # một nút chính mỗi màn (Thử lại 9Router)


def test_login_page_renders_error_block(client):
    html = web_admin._login_page("Sai tài khoản hoặc mật khẩu.", 401).body.decode()
    assert '<div class="err">Sai tài khoản hoặc mật khẩu.</div>' in html
    assert "<!--ERROR-->" not in html


def test_trang_admin_dung_logo_co_phien_ban_va_chung_lua_chon_giao_dien():
    """/static cache 1 năm (immutable) nên logo trên admin phải có ?v=<hash>;
    admin và web công khai dùng chung key giao diện lcta.theme."""
    import web_admin

    page = web_admin._with_logo_version(web_admin._ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8"))
    v = web_admin._logo_version()
    assert f"/static/logo-light.webp?v={v}" in page and f"/static/logo-dark.webp?v={v}" in page
    assert '/static/logo-light.webp"' not in page
    login = web_admin._login_page("", 200).body.decode()
    assert f"logo-dark.webp?v={v}" in login and "lcta.theme" in login
    assert "savePref('lcta.theme'" in page


async def test_admin_sua_tri_nho_can_dang_nhap_va_kiem_tham_so(client, monkeypatch):
    calls = []

    async def items(user_id):
        return {"user_id": user_id, "facts": [], "highlights": [], "enabled": True}

    async def edit(user_id, action, kind, item_id):
        calls.append((user_id, action, kind, item_id))
        return item_id == 7

    monkeypatch.setattr(web_admin.memory_control, "admin_items", items)
    monkeypatch.setattr(web_admin.memory_control, "admin_edit", edit)
    assert (await client.get("/admin/api/memory/items?user_id=1")).status_code == 403
    await _login(client)
    assert (await client.get("/admin/api/memory/items?user_id=abc")).status_code == 400
    assert (await client.get("/admin/api/memory/items?user_id=-3")).json()["user_id"] == -3
    body = {"user_id": -3, "kind": "fact", "id": 7, "action": "forget"}
    assert (await client.post("/admin/api/memory/item", json=body)).json() == {"changed": True}
    bad = {**body, "action": "drop"}
    assert (await client.post("/admin/api/memory/item", json=bad)).status_code == 400
    missing = {**body, "id": 8}
    assert (await client.post("/admin/api/memory/item", json=missing)).status_code == 404
    assert calls == [(-3, "forget", "fact", 7), (-3, "forget", "fact", 8)]


# ─── Tổng quan 1 request (/admin/api/overview) ───────────────────────────────


@pytest.fixture
def overview_sources(monkeypatch):
    """Nguồn số liệu giả của từng phần tổng quan, đếm số lần đọc."""
    import collections

    from services import background_jobs

    calls: collections.Counter = collections.Counter()

    def fake(name, value):
        async def load(*args):
            calls[name] += 1
            return value

        return load

    for attr, name, value in (
        ("_usage_rows", "usage", [{"label": "Chủ bot", "calls": 3}]),
        ("_usage_model_rows", "models", []),
        ("_outcome_rows", "outcomes", []),
        ("_burn_data", "burn", {"alert_pct": 80, "providers": []}),
        ("_turns_data", "turns", {"recent": [], "summary": []}),
        ("_usage_timeline", "daily", {"bucket": "day", "points": []}),
        ("_db_usage_data", "db", {"database_mib": 12.5}),
        ("_watchdog_data", "watchdog", {"errors": {"today_total": 0}}),
    ):
        monkeypatch.setattr(web_admin, attr, fake(name, value))
    monkeypatch.setattr(web_admin.market_page, "status", fake("market", {"busy": False}))
    monkeypatch.setattr(background_jobs, "admin_overview", fake("jobs", {"jobs": []}))
    monkeypatch.setattr(
        web_admin.orchestrator, "get_provider_state_snapshot", lambda: {"active_provider": "groq"}
    )
    monkeypatch.setattr(web_admin.memory_stats, "snapshot", lambda: {"python": {"rss_mib": 90}})
    monkeypatch.setattr(web_admin, "_overview_cache", collections.OrderedDict())
    return calls


def _age_cache(seconds: float) -> None:
    """Lùi mốc đọc của mọi phần đang cache (giả lập thời gian trôi)."""
    for key, (at, value) in list(web_admin._overview_cache.items()):
        web_admin._overview_cache[key] = (at - seconds, value)


async def test_overview_mot_request_du_moi_phan_va_cache_theo_phan(client, overview_sources):
    calls = overview_sources
    assert (await client.get("/admin/api/overview")).status_code == 403
    await _login(client)
    body = (await client.get("/admin/api/overview?hours=168")).json()
    sections = ("state", "market", "usage", "models", "outcomes", "burn", "turns", "daily")
    for name in (*sections, "ram", "db", "watchdog", "jobs"):
        assert body[name] is not None, name
    assert body["errors"] == {} and body["hours"] == 168
    assert body["state"] == {"active_provider": "groq"} and body["db"] == {"database_mib": 12.5}
    # Lượt tự làm mới kế tiếp: thống kê/DB/giám sát lấy cache, phần trong RAM đọc mới.
    again = (await client.get("/admin/api/overview?hours=168")).json()
    assert again["usage"] == body["usage"]
    assert calls["usage"] == calls["db"] == calls["watchdog"] == calls["jobs"] == 1
    assert calls["market"] == 2
    # Đổi khoảng thời gian: chỉ phần theo ?hours= đọc lại.
    await client.get("/admin/api/overview?hours=24")
    assert calls["usage"] == calls["daily"] == 2 and calls["db"] == 1
    # Sau 61 giây: thống kê đọc lại, DB/giám sát/việc nền (5 phút) vẫn cache.
    _age_cache(61)
    aged = (await client.get("/admin/api/overview?hours=168")).json()
    assert calls["usage"] == 3 and calls["burn"] == 2 and calls["db"] == 1
    assert aged["ages"]["db"] >= 61 and "usage" not in aged["ages"]
    _age_cache(301)
    await client.get("/admin/api/overview?hours=168")
    assert calls["db"] == calls["watchdog"] == calls["jobs"] == 2
    # Nút Làm mới (?fresh=1): đo lại mọi phần.
    await client.get("/admin/api/overview?hours=168&fresh=1")
    assert calls["db"] == 3 and calls["usage"] == 5
    # Cache có trần dù gọi đủ kiểu ?hours=.
    for hours in range(1, 60):
        await client.get(f"/admin/api/overview?hours={hours}")
    assert len(web_admin._overview_cache) <= web_admin._OVERVIEW_CACHE_MAX


async def test_overview_mot_phan_loi_khong_hong_ca_trang(client, overview_sources, monkeypatch):
    async def broken():
        raise RuntimeError("quét bảng ảnh lỗi")

    monkeypatch.setattr(web_admin, "_db_usage_data", broken)
    await _login(client)
    body = (await client.get("/admin/api/overview")).json()
    assert body["db"] is None and body["errors"] == {"db": "RuntimeError"}
    assert body["usage"] == [{"label": "Chủ bot", "calls": 3}]
    assert not any(key[0] == "db" for key in web_admin._overview_cache)  # lỗi không bị cache


async def test_xoay_khoa_xoa_cache_giam_sat_cua_tong_quan(client, overview_sources, monkeypatch):
    async def rotate_all():
        return web_admin.key_rotation.RotationReport(1, 1, 0, 0, 0, 0, (), ())

    monkeypatch.setattr(web_admin.key_rotation, "rotate_all", rotate_all)
    await _login(client)
    await client.get("/admin/api/overview")
    assert overview_sources["watchdog"] == 1
    assert (await client.post("/admin/api/key-rotation/run", json={})).status_code == 200
    await client.get("/admin/api/overview")
    assert overview_sources["watchdog"] == 2 and overview_sources["db"] == 1


def test_trang_admin_tu_lam_moi_bang_overview_mac_dinh_60_giay():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "/admin/api/overview?hours=" in html and "&fresh=1" in html
    assert '<option value="60000" selected>' in html
    assert "loadPref('admin.autoRefresh', '60000')" in html
    loaders = html[html.index("const LIVE_LOADERS") : html.index("const CONFIG_LOADERS")]
    assert loaders.count(", render") == 12 and "await api(" not in loaders


# ─── Chống CSRF ──────────────────────────────────────────────────────────────


async def test_api_admin_doi_du_lieu_chi_nhan_json_cung_trang(client, monkeypatch):
    calls = []

    async def set_enabled(on):
        calls.append(on)

    monkeypatch.setattr(web_admin.orchestrator, "set_router9_enabled", set_enabled)
    monkeypatch.setattr(web_admin.orchestrator, "get_provider_state_snapshot", lambda: {})
    await _login(client)
    url = "/admin/api/router9"
    plain = {"content-type": "text/plain"}
    assert (await client.post(url, content=b'{"action": "on"}', headers=plain)).status_code == 415
    assert (await client.post(url, data={"action": "on"})).status_code == 415
    for site in ("cross-site", "same-site"):
        r = await client.post(url, json={"action": "on"}, headers={"Sec-Fetch-Site": site})
        assert r.status_code == 403, site
    assert calls == []
    ok = await client.post(url, json={"action": "on"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200 and calls == [True]
    # GET (chỉ đọc) từ trang khác vẫn theo phiên như cũ.
    get = await client.get("/admin/api/state", headers={"Sec-Fetch-Site": "cross-site"})
    assert get.status_code == 200


async def test_dang_nhap_dang_xuat_tu_trang_khac_bi_tu_choi(client, settings_store, monkeypatch):
    monkeypatch.setattr(web_admin.orchestrator, "get_provider_state_snapshot", lambda: {})
    r = await client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403 and "set-cookie" not in r.headers
    assert web_admin._login_limiter._failures == {}  # không tính là lần sai
    await _login(client, headers={"Sec-Fetch-Site": "same-origin"})
    epoch = settings_store[web_admin._SESSION_EPOCH_KEY]
    out = await client.post("/admin/logout", headers={"Sec-Fetch-Site": "cross-site"})
    assert out.status_code == 403 and settings_store[web_admin._SESSION_EPOCH_KEY] == epoch
    assert (await client.get("/admin/api/state")).status_code == 200


# ─── Khoá ký cookie phiên (HKDF) ─────────────────────────────────────────────


async def test_cookie_phien_khong_ky_bang_chinh_mat_khau(client, monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setattr(web_admin.orchestrator, "get_provider_state_snapshot", lambda: {"ok": 1})
    await _login(client)
    expiry, sig = client.cookies.get("admin_session").split(".")
    epoch = await web_admin._session_epoch.get()
    old = hmac.new(b"pass", f"admin:{expiry}:{epoch}".encode(), hashlib.sha256).hexdigest()
    assert sig != old  # cookie lộ không còn dò được ADMIN_PASS offline
    assert (await client.get("/admin/api/state")).status_code == 200
    # Đổi SETTINGS_ENC_KEY (hoặc ADMIN_PASS): mọi phiên cũ hết hiệu lực.
    monkeypatch.setattr(web_admin.config, "SETTINGS_ENC_KEY", ENC_KEY[::-1])
    assert (await client.get("/admin/api/state")).status_code == 403


async def test_thieu_settings_enc_key_thi_khong_co_phien(client, monkeypatch):
    monkeypatch.setattr(web_admin.config, "SETTINGS_ENC_KEY", None)
    r = await _login(client)
    assert r.status_code == 503 and "set-cookie" not in r.headers
    assert "SETTINGS_ENC_KEY" in r.text
    assert (await client.get("/admin/api/state")).status_code == 403


# ─── Trang admin đọc 1 lần, API no-store ─────────────────────────────────────


async def test_trang_admin_doc_template_mot_lan(client):
    web_admin._admin_page_html.cache_clear()
    await _login(client)
    first = await client.get("/admin")
    second = await client.get("/admin")
    assert first.text == second.text and 'id="market-box"' in first.text
    info = web_admin._admin_page_html.cache_info()
    assert info.misses == 1 and info.hits >= 1
    assert f"logo-dark.webp?v={web_admin._logo_version()}" in first.text


async def test_api_admin_tra_no_store(monkeypatch, settings_store, sleeps):
    from services.security_headers import SecurityHeadersMiddleware

    monkeypatch.setattr(web_admin.config, "ADMIN_USER", "admin")
    monkeypatch.setattr(web_admin.config, "ADMIN_PASS", "pass")
    monkeypatch.setattr(web_admin.config, "SETTINGS_ENC_KEY", ENC_KEY)
    monkeypatch.setattr(web_admin, "_session_epoch", web_admin.SessionEpoch())
    monkeypatch.setattr(web_admin.orchestrator, "get_provider_state_snapshot", lambda: {"ok": 1})
    app = FastAPI()
    app.include_router(web_admin.router)
    app.include_router(web_admin.api_router)
    app.add_middleware(SecurityHeadersMiddleware)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        await _login(c)
        r = await c.get("/admin/api/state")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
