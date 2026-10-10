"""Trang /admin và admin API (tách khỏi web.py).

- ``router``: trang /admin, /admin/login, /admin/logout.
- ``api_router``: mọi endpoint /admin/api/*, tự kiểm tra session qua
  dependency ``require_admin`` (không còn lặp lại check trong từng hàm).
- Đăng nhập bị giới hạn số lần sai theo IP (chống brute-force), state nằm
  trong RAM vì service chạy 1 worker; restart thì bộ đếm reset.
"""

import asyncio
import hashlib
import hmac
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ai import (
    agnes_client,
    groq_client,
    official_client,
    openrouter_client,
    orchestrator,
    provider_overrides,
    router9_client,
    tavily_client,
)
from ai.provider_state import provider_state
from channels import zalo_users
from core import config, database as db
from services import db_maintenance, market_page, memory_service, memory_stats

logger = logging.getLogger(__name__)

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_ADMIN_TEMPLATE_PATH = _TEMPLATES / "admin.html"
_ADMIN_LOGIN_PATH = _TEMPLATES / "admin_login.html"
_ADMIN_COOLDOWN_PROVIDERS = ("groq", "openrouter", "api1", "api2")
_ADMIN_SESSION_COOKIE = "admin_session"
_CHANNEL_LABELS = {"telegram": "Telegram", "zoom": "Zoom", "zalo": "Zalo"}


class LoginRateLimiter:
    """Khoá IP sau ``max_failures`` lần sai trong ``window_sec`` giây."""

    def __init__(self, max_failures: int = 5, window_sec: int = 900, max_tracked: int = 1000):
        self.max_failures = max_failures
        self.window_sec = window_sec
        self.max_tracked = max_tracked
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window_sec]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def is_blocked(self, key: str) -> bool:
        return len(self._recent(key, time.monotonic())) >= self.max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        if key not in self._failures and len(self._failures) >= self.max_tracked:
            # Giới hạn RAM: bỏ entry cũ nhất thay vì để dict phình vô hạn.
            self._failures.pop(next(iter(self._failures)))
        self._failures[key] = [*self._recent(key, now), now]

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


_login_limiter = LoginRateLimiter()
_global_login_limiter = LoginRateLimiter(max_failures=30)


def _client_ip(request: Request) -> str:
    # Render đứng sau proxy: IP thật là phần tử đầu của X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _hours_param(request: Request, default: int = 168) -> int:
    try:
        hours = int(request.query_params.get("hours", str(default)))
    except ValueError:
        raise HTTPException(status_code=400, detail="hours phải là số nguyên")
    return min(max(hours, 1), 24 * 365)


def _admin_session_sig(expiry: int) -> str:
    return hmac.new(
        config.ADMIN_PASS.encode(),
        f"{config.ADMIN_USER}:{expiry}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _admin_session_token() -> str:
    expiry = int(time.time()) + config.ADMIN_SESSION_TTL_SEC
    return f"{expiry}.{_admin_session_sig(expiry)}"


def _admin_session_valid(request: Request) -> bool:
    if not config.ADMIN_USER or not config.ADMIN_PASS:
        return False
    expiry_str, _, sig = request.cookies.get(_ADMIN_SESSION_COOKIE, "").partition(".")
    if not expiry_str or not sig:
        return False
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if time.time() > expiry:
        return False
    return hmac.compare_digest(sig, _admin_session_sig(expiry))


def require_admin(request: Request) -> None:
    if not _admin_session_valid(request):
        raise HTTPException(status_code=403)


router = APIRouter(include_in_schema=False)
api_router = APIRouter(
    prefix="/admin/api", dependencies=[Depends(require_admin)], include_in_schema=False
)


@router.get("/admin")
async def admin_page(request: Request) -> Response:
    if not _admin_session_valid(request):
        return HTMLResponse(_ADMIN_LOGIN_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(_ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8"))


@router.post("/admin/login")
async def admin_login(request: Request) -> Response:
    client_ip = _client_ip(request)
    # Khoá theo IP + khoá toàn cục: X-Forwarded-For có thể bị giả mạo để đổi
    # "IP" mỗi lần thử, nên vẫn cần 1 trần chung cho mọi lượt đăng nhập sai.
    if _login_limiter.is_blocked(client_ip) or _global_login_limiter.is_blocked("*"):
        login_html = _ADMIN_LOGIN_PATH.read_text(encoding="utf-8").replace(
            "<!--ERROR-->",
            '<div class="err">Sai quá nhiều lần. Thử lại sau ít phút.</div>',
        )
        return HTMLResponse(login_html, status_code=429)
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    valid = (
        config.ADMIN_USER
        and config.ADMIN_PASS
        # So sánh bytes: compare_digest với str chứa ký tự non-ASCII sẽ TypeError.
        and hmac.compare_digest(username.encode(), config.ADMIN_USER.encode())
        and hmac.compare_digest(password.encode(), config.ADMIN_PASS.encode())
    )
    if not valid:
        _login_limiter.record_failure(client_ip)
        _global_login_limiter.record_failure("*")
        logger.warning("Đăng nhập /admin thất bại từ %s.", client_ip)
        login_html = _ADMIN_LOGIN_PATH.read_text(encoding="utf-8").replace(
            "<!--ERROR-->", '<div class="err">Sai tài khoản hoặc mật khẩu.</div>'
        )
        return HTMLResponse(login_html, status_code=401)
    _login_limiter.reset(client_ip)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        _ADMIN_SESSION_COOKIE,
        _admin_session_token(),
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=config.ADMIN_SESSION_TTL_SEC,
    )
    return response


@router.post("/admin/logout")
async def admin_logout() -> Response:
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(_ADMIN_SESSION_COOKIE)
    return response


@api_router.get("/state")
async def admin_state(request: Request) -> Response:
    return JSONResponse(orchestrator.get_provider_state_snapshot())


@api_router.post("/router9")
async def admin_router9(request: Request) -> Response:
    """Body: {"action": "on"|"off"|"retry"}. "retry" ping 9Router ngay,
    chuyển active_provider về router9 nếu sống - xem orchestrator.try_router9_now."""
    action = (await request.json()).get("action")
    if action == "retry":
        ok, detail = await orchestrator.try_router9_now()
        return JSONResponse(
            {"ok": ok, "detail": detail, **orchestrator.get_provider_state_snapshot()}
        )
    if action in ("on", "off"):
        await orchestrator.set_router9_enabled(action == "on")
        return JSONResponse(orchestrator.get_provider_state_snapshot())
    return Response(status_code=400)


@api_router.post("/cooldown/reset")
async def admin_reset_cooldown(request: Request) -> Response:
    provider = (await request.json()).get("provider")
    if provider not in _ADMIN_COOLDOWN_PROVIDERS:
        return Response(status_code=400)
    await orchestrator.reset_api_cooldown(provider)
    return JSONResponse(orchestrator.get_provider_state_snapshot())


def _mask_api_key(key: str) -> str:
    if not key:
        return ""
    return f"···{key[-4:]}" if len(key) > 4 else "···"


async def _provider_info(provider: str) -> dict:
    base_url_info = {}
    if provider == "router9":
        model_override = await router9_client.get_preferred_model_name()
        effective_model = model_override or config.ROUTER9_MODEL
        enabled = provider_state.router9_enabled
        base_url_override = await router9_client.get_base_url_override()
        base_url_info = {
            "base_url": base_url_override or config.ROUTER9_BASE_URL,
            "base_url_override": base_url_override or "",
            "base_url_overridden": bool(base_url_override),
        }
    else:
        model_override = await provider_overrides.get_model_override(provider)
        effective_model = await {
            "groq": groq_client.model_name,
            "openrouter": openrouter_client.model_name,
            "api1": lambda: official_client.model_for(1),
            "api2": lambda: official_client.model_for(2),
        }[provider]()
        enabled = await provider_overrides.is_enabled(provider)
    api_key = await {
        "router9": router9_client.api_key,
        "groq": groq_client.api_key,
        "openrouter": openrouter_client.api_key,
        "api1": lambda: official_client.api_key_for(1),
        "api2": lambda: official_client.api_key_for(2),
    }[provider]()
    return {
        "provider": provider,
        "model": effective_model,
        "model_overridden": bool(model_override),
        "api_key_configured": bool(api_key),
        "api_key_masked": _mask_api_key(api_key or ""),
        "api_key_overridden": bool(await provider_overrides.get_api_key_override(provider)),
        "enabled": enabled,
        "enabled_editable": provider in provider_overrides.ENABLE_OVERRIDABLE,
        **base_url_info,
    }


@api_router.get("/providers")
async def admin_providers(request: Request) -> Response:
    return JSONResponse([await _provider_info(p) for p in provider_overrides.PROVIDERS])


@api_router.post("/providers/update")
async def admin_providers_update(request: Request) -> Response:
    """Body: {"provider": "router9"|"groq"|"openrouter"|"api1"|"api2",
    "model"?: str (rỗng = xoá override, dùng lại mặc định env),
    "api_key"?: str (rỗng = xoá override),
    "base_url"?: str (chỉ router9; rỗng/sai định dạng = dùng ROUTER9_BASE_URL env),
    "enabled"?: bool}."""
    body = await request.json()
    provider = body.get("provider")
    if provider not in provider_overrides.PROVIDERS:
        return Response(status_code=400)

    if "model" in body:
        model = body["model"]
        if provider == "router9":
            await router9_client.set_preferred_model_name(model or None)
        else:
            await provider_overrides.set_model_override(provider, model)
    if "api_key" in body:
        try:
            await provider_overrides.set_api_key_override(provider, body["api_key"])
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    warning = None
    if "base_url" in body:
        if provider != "router9":
            return JSONResponse(
                {"error": "Base URL chỉ được cấu hình cho router9."}, status_code=400
            )
        base_url = body["base_url"]
        if isinstance(base_url, str):
            valid = await router9_client.set_base_url_override(base_url)
        else:
            await router9_client.set_base_url_override(None)
            valid = False
        if not valid:
            warning = (
                "Base URL không đúng định dạng. Đã bỏ override và dùng "
                "ROUTER9_BASE_URL từ biến môi trường trên Render."
            )
    if "enabled" in body:
        enabled = bool(body["enabled"])
        if provider == "router9":
            await orchestrator.set_router9_enabled(enabled)
        elif provider in provider_overrides.ENABLE_OVERRIDABLE:
            await provider_overrides.set_enabled(provider, enabled)

    result = await _provider_info(provider)
    if warning:
        result["warning"] = warning
    return JSONResponse(result)


@api_router.get("/tavily")
async def admin_tavily(request: Request) -> Response:
    api_key = await tavily_client.api_key()
    return JSONResponse(
        {
            "enabled": await tavily_client.get_enabled(),
            "api_key_configured": bool(api_key),
            "api_key_masked": _mask_api_key(api_key or ""),
            "api_key_overridden": bool(await provider_overrides.get_api_key_override("tavily")),
        }
    )


@api_router.post("/tavily/update")
async def admin_tavily_update(request: Request) -> Response:
    """Body: {"enabled"?: bool, "api_key"?: str (rỗng = xoá override)}."""
    body = await request.json()
    if "enabled" in body:
        await tavily_client.set_enabled(bool(body["enabled"]))
    if "api_key" in body:
        try:
            await provider_overrides.set_api_key_override("tavily", body["api_key"])
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    api_key = await tavily_client.api_key()
    return JSONResponse(
        {
            "enabled": await tavily_client.get_enabled(),
            "api_key_configured": bool(api_key),
            "api_key_masked": _mask_api_key(api_key or ""),
            "api_key_overridden": bool(await provider_overrides.get_api_key_override("tavily")),
        }
    )


@api_router.get("/agnes")
async def admin_agnes(request: Request) -> Response:
    api_key = await agnes_client.api_key()
    return JSONResponse(
        {
            "enabled": await agnes_client.get_enabled(),
            "model": await provider_overrides.get_model_override("agnes")
            or config.AGNES_IMAGE_MODEL,
            "model_overridden": bool(await provider_overrides.get_model_override("agnes")),
            "api_key_configured": bool(api_key),
            "api_key_masked": _mask_api_key(api_key or ""),
            "api_key_overridden": bool(await provider_overrides.get_api_key_override("agnes")),
        }
    )


@api_router.post("/agnes/update")
async def admin_agnes_update(request: Request) -> Response:
    """Body: {"enabled"?: bool, "api_key"?: str, "model"?: str (rỗng = xoá override)}."""
    body = await request.json()
    if "enabled" in body:
        await agnes_client.set_enabled(bool(body["enabled"]))
    if "model" in body:
        await provider_overrides.set_model_override("agnes", body["model"])
    if "api_key" in body:
        try:
            await provider_overrides.set_api_key_override("agnes", body["api_key"])
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    api_key = await agnes_client.api_key()
    return JSONResponse(
        {
            "enabled": await agnes_client.get_enabled(),
            "model": await provider_overrides.get_model_override("agnes")
            or config.AGNES_IMAGE_MODEL,
            "model_overridden": bool(await provider_overrides.get_model_override("agnes")),
            "api_key_configured": bool(api_key),
            "api_key_masked": _mask_api_key(api_key or ""),
            "api_key_overridden": bool(await provider_overrides.get_api_key_override("agnes")),
        }
    )


@api_router.get("/market_page")
async def admin_market_page(request: Request) -> Response:
    return JSONResponse(await market_page.status())


@api_router.post("/market_page/run")
async def admin_market_page_run(request: Request) -> Response:
    """Body: {"job": "stock"|"news", "publish"?: bool}. Không có publish = chỉ tạo nội dung xem thử."""
    body = await request.json()
    job, publish = body.get("job"), bool(body.get("publish"))
    if job not in ("stock", "news"):
        return Response(status_code=400)
    try:
        text = await market_page.run_manual(job, publish=publish)
    except market_page.MarketPageError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        logger.warning("Admin market_page %s lỗi (%s).", job, type(exc).__name__, exc_info=True)
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=502)
    return JSONResponse({"published": publish and bool(text), "text": text})


async def _memory_user_entries() -> list[dict]:
    entries = [{"user_id": config.ALLOWED_USER_ID, "label": "Chủ bot (Telegram/Zoom)"}]
    for zuser in await zalo_users.list_users():
        entries.append(
            {"user_id": zuser.internal_user_id, "label": zuser.display_name or zuser.external_id}
        )
    return entries


@api_router.get("/memory")
async def admin_memory(request: Request) -> Response:
    result = []
    for entry in await _memory_user_entries():
        result.append({**entry, "enabled": await memory_service.is_enabled(entry["user_id"])})
    return JSONResponse(result)


@api_router.post("/memory/update")
async def admin_memory_update(request: Request) -> Response:
    """Body: {"user_id": int, "enabled": bool}."""
    body = await request.json()
    user_id = body.get("user_id")
    if not isinstance(user_id, int):
        return Response(status_code=400)
    await memory_service.set_enabled(user_id, bool(body.get("enabled", True)))
    return JSONResponse({"user_id": user_id, "enabled": await memory_service.is_enabled(user_id)})


@api_router.get("/usage")
async def admin_usage(request: Request) -> Response:
    since_hours = _hours_param(request)
    rows = await db.usage_by_user(since_hours)
    zalo_by_id = {u.internal_user_id: u for u in await zalo_users.list_users()}

    result = []
    for row in rows:
        channel, uid = row["channel"], row["telegram_user_id"]
        if channel == "zalo" and uid in zalo_by_id:
            zuser = zalo_by_id[uid]
            label = zuser.display_name or zuser.external_id
        elif uid == config.ALLOWED_USER_ID:
            label = "Chủ bot"
        else:
            label = str(uid)
        result.append(
            {
                "channel": channel,
                "channel_label": _CHANNEL_LABELS.get(channel, channel),
                "user_id": uid,
                "label": label,
                "calls": row["calls"],
                "last_call_at": row["last_call_at"].isoformat() if row["last_call_at"] else None,
            }
        )
    return JSONResponse(result)


@api_router.get("/usage/models")
async def admin_usage_models(request: Request) -> Response:
    """Lượt gọi thành công theo (provider, model) - xem
    ai/orchestrator.py::_record_provider_call, ghi mỗi khi 1 provider trong
    provider-chain trả lời thành công (router9/groq/openrouter/api1/api2)."""
    since_hours = _hours_param(request)
    rows = await db.usage_by_model(since_hours)
    return JSONResponse(
        [
            {
                "provider": row["provider"],
                "model": row["model"],
                "calls": row["calls"],
                "last_call_at": row["last_call_at"].isoformat() if row["last_call_at"] else None,
            }
            for row in rows
        ]
    )


@api_router.get("/memory-usage")
async def admin_memory_usage() -> Response:
    """RAM của container (giới hạn Render free 512 MB), từng tiến trình và các
    thư viện nặng đã nạp. Mở trực tiếp trên trình duyệt khi đã đăng nhập /admin."""
    return JSONResponse(await asyncio.to_thread(memory_stats.snapshot))


@api_router.get("/db-usage")
async def admin_db_usage() -> Response:
    """Dung lượng bảng ảnh Facebook và job VACUUM FULL tuần tới có chạy không."""
    usage = await db_maintenance.measure()
    ok, reason = db_maintenance.should_vacuum(usage)
    mib = db_maintenance.MIB
    return JSONResponse({
        "database_mib": round(usage.database_bytes / mib, 1),
        "media_table_mib": round(usage.total_bytes / mib, 1),
        "media_live_mib": round(usage.live_bytes / mib, 1),
        "media_wasted_mib": round(usage.wasted_bytes / mib, 1),
        "auto_vacuum_enabled": db_maintenance.enabled(),
        "vacuum_would_run": ok,
        "reason": reason,
    })
