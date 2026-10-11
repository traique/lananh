"""Trang /admin và admin API (tách khỏi web.py).

- ``router``: trang /admin, /admin/login, /admin/logout.
- ``api_router``: mọi endpoint /admin/api/*, tự kiểm tra session qua
  dependency ``require_admin`` (không còn lặp lại check trong từng hàm).
- Đăng nhập sai bị khoá theo IP (chống brute-force). IP lấy từ phần tử CUỐI
  của X-Forwarded-For (do proxy Render nối vào, client không giả được).
- Không khoá cứng toàn cục: tổng số lần sai gần đây chỉ làm CHẬM mọi lượt đăng
  nhập (tối đa vài giây), để người lạ không thể khoá luôn chủ bot ra ngoài.
  State nằm trong RAM vì service chạy 1 worker; restart thì bộ đếm reset.
- Cookie phiên ký HMAC (khoá ADMIN_PASS) kèm "epoch" lưu ở bảng settings;
  đăng xuất đổi epoch => mọi phiên đang mở hết hiệu lực ngay.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import deque
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
# Key bảng settings giữ epoch phiên admin (xem SessionEpoch).
_SESSION_EPOCH_KEY = "admin_session_epoch"
_MAX_CLIENT_KEY_LEN = 64


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


class LoginSlowdown:
    """Làm chậm MỌI lượt đăng nhập khi gần đây có nhiều lần sai (từ bất kỳ IP nào).

    Thay cho khoá cứng toàn cục cũ: khoá cứng cho phép bất kỳ ai gửi đủ số lần
    sai là chặn luôn cả mật khẩu đúng của chủ bot (DoS). Làm chậm vẫn giữ
    brute-force ở tốc độ thấp nhưng mật khẩu đúng luôn đăng nhập được. Chỉ giữ
    tối đa ``max_tracked`` mốc thời gian (deque có trần) để không phình RAM.
    """

    def __init__(
        self,
        window_sec: int = 900,
        free_failures: int = 10,
        step_sec: float = 0.25,
        max_delay_sec: float = 3.0,
        max_tracked: int = 500,
    ):
        self.window_sec = window_sec
        self.free_failures = free_failures
        self.step_sec = step_sec
        self.max_delay_sec = max_delay_sec
        self._failures: deque[float] = deque(maxlen=max_tracked)

    def _recent_count(self, now: float) -> int:
        while self._failures and now - self._failures[0] >= self.window_sec:
            self._failures.popleft()
        return len(self._failures)

    def delay(self) -> float:
        extra = self._recent_count(time.monotonic()) - self.free_failures
        return min(self.max_delay_sec, max(0, extra) * self.step_sec)

    def record_failure(self) -> None:
        self._failures.append(time.monotonic())


class SessionEpoch:
    """Epoch phiên admin: lưu ở bảng settings, cache trong RAM (1 worker).

    Epoch được đưa vào chữ ký cookie; đổi epoch (đăng xuất) là thu hồi mọi
    cookie đã phát. Epoch KHÔNG phải secret (khoá ký vẫn là ADMIN_PASS) nên lưu
    thẳng qua db.get_setting/set_setting. Không đọc được DB thì trả None =>
    coi như chưa đăng nhập (fail closed).
    """

    def __init__(self) -> None:
        self._value: str | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> str | None:
        if self._value is not None:
            return self._value
        async with self._lock:
            if self._value is not None:
                return self._value
            try:
                value = await db.get_setting(_SESSION_EPOCH_KEY)
                if not value:
                    await db.set_setting(_SESSION_EPOCH_KEY, secrets.token_urlsafe(18))
                    # Đọc lại: lúc deploy 2 instance chạy chồng nhau, dùng chung
                    # giá trị của bên ghi sau thay vì mỗi bên giữ 1 epoch riêng.
                    value = await db.get_setting(_SESSION_EPOCH_KEY)
            except Exception:
                logger.warning(
                    "Không đọc được epoch phiên admin từ DB, từ chối phiên.", exc_info=True
                )
                return None
            self._value = value or None
            return self._value

    async def rotate(self) -> bool:
        """Đổi epoch (thu hồi mọi phiên). False nếu không lưu được xuống DB."""
        new_value = secrets.token_urlsafe(18)
        async with self._lock:
            # Đổi trong RAM trước: dù DB lỗi, process này vẫn thu hồi ngay phiên cũ.
            self._value = new_value
            try:
                await db.set_setting(_SESSION_EPOCH_KEY, new_value)
            except Exception:
                logger.error(
                    "Không lưu được epoch phiên admin mới; phiên cũ có thể sống lại "
                    "sau khi restart (tới khi cookie hết hạn).",
                    exc_info=True,
                )
                return False
        return True


_login_limiter = LoginRateLimiter()
_login_slowdown = LoginSlowdown()
_session_epoch = SessionEpoch()


def _trusted_proxy_hops() -> int:
    """Số proxy tin cậy đứng trước app (mặc định 1 = proxy của Render). Nếu
    trước Render còn 1 proxy khác cũng nối IP vào X-Forwarded-For (vd
    Cloudflare -> Render) thì đặt ADMIN_TRUSTED_PROXY_HOPS=2."""
    try:
        return max(1, min(5, int(os.getenv("ADMIN_TRUSTED_PROXY_HOPS", "1"))))
    except ValueError:
        return 1


def _client_ip(request: Request) -> str:
    # Header IP riêng do proxy tin cậy đặt (vd "true-client-ip", "cf-connecting-ip")
    # - chỉ cấu hình khi đã kiểm tra log production rằng proxy LUÔN ghi đè header
    # này, nếu không client tự gửi được.
    header = os.getenv("ADMIN_CLIENT_IP_HEADER", "").strip().lower()
    if header:
        value = (request.headers.get(header) or "").strip()
        if value:
            return value[:_MAX_CLIENT_KEY_LEN]
    # Render đứng sau proxy: proxy NỐI IP thật của client vào CUỐI X-Forwarded-For.
    # Các phần tử phía trước do client tự gửi nên giả được => chỉ tin phần tử thứ
    # N tính từ cuối (N = số proxy tin cậy). Gộp mọi header XFF (getlist) phòng
    # trường hợp proxy thêm dòng header riêng.
    forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
    # Giữ cả phần tử rỗng: phần tử proxy lẽ ra nối vào mà rỗng (header dị dạng)
    # thì quay về request.client.host, KHÔNG lùi sang phần tử client tự gửi.
    entries = [part.strip() for part in forwarded.split(",")] if forwarded else []
    if entries:
        hops = _trusted_proxy_hops()
        chosen = entries[-hops] if len(entries) >= hops else entries[0]
        if chosen:
            return chosen[:_MAX_CLIENT_KEY_LEN]
    return request.client.host if request.client else "unknown"


def _hours_param(request: Request, default: int = 168) -> int:
    try:
        hours = int(request.query_params.get("hours", str(default)))
    except ValueError:
        raise HTTPException(status_code=400, detail="hours phải là số nguyên")
    return min(max(hours, 1), 24 * 365)


def _admin_session_sig(expiry: int, epoch: str) -> str:
    return hmac.new(
        config.ADMIN_PASS.encode(),
        f"{config.ADMIN_USER}:{expiry}:{epoch}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _admin_session_token(epoch: str) -> str:
    expiry = int(time.time()) + config.ADMIN_SESSION_TTL_SEC
    return f"{expiry}.{_admin_session_sig(expiry, epoch)}"


async def _admin_session_valid(request: Request) -> bool:
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
    epoch = await _session_epoch.get()
    if epoch is None:
        return False
    # So sánh bytes: cookie có thể chứa ký tự non-ASCII, compare_digest(str) sẽ TypeError.
    return hmac.compare_digest(sig.encode(), _admin_session_sig(expiry, epoch).encode())


async def require_admin(request: Request) -> None:
    if not await _admin_session_valid(request):
        raise HTTPException(status_code=403)


router = APIRouter(include_in_schema=False)
api_router = APIRouter(
    prefix="/admin/api", dependencies=[Depends(require_admin)], include_in_schema=False
)


def _login_page(error: str, status_code: int) -> HTMLResponse:
    login_html = _ADMIN_LOGIN_PATH.read_text(encoding="utf-8").replace(
        "<!--ERROR-->", f'<div class="err">{error}</div>'
    )
    return HTMLResponse(login_html, status_code=status_code)


@router.get("/admin")
async def admin_page(request: Request) -> Response:
    if not await _admin_session_valid(request):
        return HTMLResponse(_ADMIN_LOGIN_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(_ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8"))


@router.post("/admin/login")
async def admin_login(request: Request) -> Response:
    client_ip = _client_ip(request)
    too_many = "Sai quá nhiều lần. Thử lại sau ít phút."
    if _login_limiter.is_blocked(client_ip):
        return _login_page(too_many, 429)
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    # Làm chậm mọi lượt (kể cả đúng mật khẩu) theo tổng số lần sai gần đây từ
    # mọi IP, nhưng KHÔNG chặn: chủ bot luôn đăng nhập được, chỉ phải chờ thêm.
    delay = _login_slowdown.delay()
    if delay > 0:
        await asyncio.sleep(delay)
    # Kiểm tra lại sau các await ở trên: nhiều request song song cùng IP không
    # được lách khoá. Từ đây tới record_failure không có await nên không bị chen.
    if _login_limiter.is_blocked(client_ip):
        return _login_page(too_many, 429)
    valid = (
        config.ADMIN_USER
        and config.ADMIN_PASS
        # So sánh bytes: compare_digest với str chứa ký tự non-ASCII sẽ TypeError.
        and hmac.compare_digest(username.encode(), config.ADMIN_USER.encode())
        and hmac.compare_digest(password.encode(), config.ADMIN_PASS.encode())
    )
    if not valid:
        _login_limiter.record_failure(client_ip)
        _login_slowdown.record_failure()
        # Ghi kèm chuỗi X-Forwarded-For để kiểm tra trên production rằng IP chọn
        # ra đúng là IP client (xem ADMIN_TRUSTED_PROXY_HOPS).
        logger.warning(
            "Đăng nhập /admin thất bại từ %s (X-Forwarded-For: %s).",
            client_ip,
            ",".join(request.headers.getlist("x-forwarded-for"))[:200] or "-",
        )
        return _login_page("Sai tài khoản hoặc mật khẩu.", 401)
    _login_limiter.reset(client_ip)
    epoch = await _session_epoch.get()
    if epoch is None:
        # Fail closed: không có epoch thì không phát cookie được.
        return _login_page("Không kết nối được cơ sở dữ liệu. Thử lại sau.", 503)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        _ADMIN_SESSION_COOKIE,
        _admin_session_token(epoch),
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=config.ADMIN_SESSION_TTL_SEC,
    )
    return response


@router.post("/admin/logout")
async def admin_logout(request: Request) -> Response:
    # Chỉ phiên hợp lệ mới được đổi epoch: nếu không, ai cũng có thể POST liên
    # tục vào đây để đá chủ bot ra khỏi trang admin.
    if await _admin_session_valid(request):
        await _session_epoch.rotate()
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(_ADMIN_SESSION_COOKIE, httponly=True, samesite="lax", secure=True)
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
    return JSONResponse(
        {
            "database_mib": round(usage.database_bytes / mib, 1),
            "media_table_mib": round(usage.total_bytes / mib, 1),
            "media_live_mib": round(usage.live_bytes / mib, 1),
            "media_wasted_mib": round(usage.wasted_bytes / mib, 1),
            "auto_vacuum_enabled": db_maintenance.enabled(),
            "vacuum_would_run": ok,
            "reason": reason,
        }
    )
