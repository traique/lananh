"""Entrypoint dùng để deploy lên Render bằng Telegram webhook."""

import asyncio
import hashlib
import hmac
import io
import logging
import time
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from telegram import Update
from telegram.ext import Application

import bot_app
import logging_setup
import messages
from ai import orchestrator, provider_overrides
from ai import groq_client, official_client, openrouter_client, router9_client, tavily_client
from ai import agnes_client
from ai.provider_state import provider_state
from channels import group_commands, zalo_repository, zalo_scheduler, zalo_users, zoom
from channels.router import router as zalo_router
from core import config, database as db, idempotency
from diagnose_router9 import main as diagnose_main
from services import memory_service
from services import morning_news
from services.background_tasks import stop_tracked_tasks
from services.channel_chat_service import handle_channel_text, split_for_zalo
from services.concurrency import assistant_turn

logging_setup.configure_logging()
logger = logging.getLogger(__name__)
application: Application | None = None
_background_tasks: set[asyncio.Task] = set()
_diagnose_lock = asyncio.Lock()
_ADMIN_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "admin.html"
_ADMIN_LOGIN_PATH = Path(__file__).resolve().parent / "templates" / "admin_login.html"
_ADMIN_COOLDOWN_PROVIDERS = ("groq", "openrouter", "api1", "api2")
_ADMIN_SESSION_COOKIE = "admin_session"


def _diagnose_token_valid(request: Request) -> bool:
    token = request.headers.get("X-Diagnose-Token", "")
    return bool(config.DIAGNOSE_SECRET) and hmac.compare_digest(token, config.DIAGNOSE_SECRET)


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


async def _stop_webhook_tasks() -> None:
    """Drain request handlers, rồi huỷ lượt treo trước khi đóng app/DB."""
    await stop_tracked_tasks(
        _background_tasks,
        timeout=30.0,
        logger=logger,
        label="Telegram webhook",
    )


async def _safe_shutdown(label: str, awaitable) -> None:
    try:
        await awaitable
    except Exception:
        logger.exception("Shutdown step lỗi: %s", label)


async def _process_update(update: Update) -> None:
    """Preserve one cross-channel conversation order for the single owner."""
    if application is None:
        return
    async with assistant_turn():
        await application.process_update(update)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global application
    config.validate(require_webhook=True)
    config.ensure_media_dir()
    application = bot_app.build_application()
    initialized = False
    app_started = False
    app_resources_started = False
    try:
        await application.initialize()
        initialized = True
        app_resources_started = True
        await bot_app._post_init(application)
        await application.start()
        app_started = True
        webhook_url = config.WEBHOOK_BASE_URL.rstrip("/") + config.WEBHOOK_PATH
        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=config.WEBHOOK_SECRET,
            allowed_updates=["message"],
        )
        logger.info("Webhook đã set tới: %s", webhook_url)
        zalo_scheduler.start()
        morning_news.start()
        yield
    finally:
        logger.info("Đang tắt bot...")
        await _safe_shutdown("Zalo scheduler", zalo_scheduler.stop())
        await _safe_shutdown("Morning news scheduler", morning_news.stop())
        await _safe_shutdown("webhook tasks", _stop_webhook_tasks())
        if app_started:
            await _safe_shutdown("Telegram application stop", application.stop())
        if app_resources_started:
            await _safe_shutdown(
                "application resources",
                bot_app._post_shutdown(application),
            )
        if initialized:
            await _safe_shutdown("Telegram application shutdown", application.shutdown())
        application = None


api = FastAPI(lifespan=lifespan)
api.include_router(zalo_router)


@api.api_route("/", methods=["GET", "HEAD"])
async def health() -> dict:
    return {"status": "ok"}


@api.get(config.DIAGNOSE_PATH)
async def diagnose(request: Request) -> Response:
    if not _diagnose_token_valid(request):
        return Response(status_code=403)
    async with _diagnose_lock:
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                await diagnose_main()
        except Exception as exc:
            print(f"Lỗi ngoài dự kiến: {type(exc).__name__}: {exc}")
        return Response(content=buf.getvalue(), media_type="text/plain; charset=utf-8")


@api.get("/admin")
async def admin_page(request: Request) -> Response:
    if not _admin_session_valid(request):
        return HTMLResponse(_ADMIN_LOGIN_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(_ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8"))


@api.post("/admin/login")
async def admin_login(request: Request) -> Response:
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    valid = (
        config.ADMIN_USER
        and config.ADMIN_PASS
        and hmac.compare_digest(username, config.ADMIN_USER)
        and hmac.compare_digest(password, config.ADMIN_PASS)
    )
    if not valid:
        login_html = _ADMIN_LOGIN_PATH.read_text(encoding="utf-8").replace(
            "<!--ERROR-->", '<div class="err">Sai tài khoản hoặc mật khẩu.</div>'
        )
        return HTMLResponse(login_html, status_code=401)
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


@api.post("/admin/logout")
async def admin_logout() -> Response:
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(_ADMIN_SESSION_COOKIE)
    return response


@api.get("/admin/api/state")
async def admin_state(request: Request) -> Response:
    if not _admin_session_valid(request):
        return Response(status_code=403)
    return JSONResponse(orchestrator.get_provider_state_snapshot())


@api.post("/admin/api/router9")
async def admin_router9(request: Request) -> Response:
    """Body: {"action": "on"|"off"|"retry"}. "retry" ping 9Router ngay,
    chuyển active_provider về router9 nếu sống - xem orchestrator.try_router9_now."""
    if not _admin_session_valid(request):
        return Response(status_code=403)
    action = (await request.json()).get("action")
    if action == "retry":
        ok, detail = await orchestrator.try_router9_now()
        return JSONResponse({"ok": ok, "detail": detail, **orchestrator.get_provider_state_snapshot()})
    if action in ("on", "off"):
        await orchestrator.set_router9_enabled(action == "on")
        return JSONResponse(orchestrator.get_provider_state_snapshot())
    return Response(status_code=400)


@api.post("/admin/api/cooldown/reset")
async def admin_reset_cooldown(request: Request) -> Response:
    if not _admin_session_valid(request):
        return Response(status_code=403)
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
    if provider == "router9":
        model_override = await router9_client.get_preferred_model_name()
        effective_model = model_override or config.ROUTER9_MODEL
        enabled = provider_state.router9_enabled
    else:
        model_override = await provider_overrides.get_model_override(provider)
        effective_model = await {
            "groq": groq_client._model,
            "openrouter": openrouter_client._model,
            "api1": lambda: official_client._model_for(1),
            "api2": lambda: official_client._model_for(2),
        }[provider]()
        enabled = await provider_overrides.is_enabled(provider)
    api_key = await {
        "router9": router9_client._api_key,
        "groq": groq_client._api_key,
        "openrouter": openrouter_client._api_key,
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
    }


@api.get("/admin/api/providers")
async def admin_providers(request: Request) -> Response:
    if not _admin_session_valid(request):
        return Response(status_code=403)
    return JSONResponse([await _provider_info(p) for p in provider_overrides.PROVIDERS])


@api.post("/admin/api/providers/update")
async def admin_providers_update(request: Request) -> Response:
    """Body: {"provider": "router9"|"groq"|"openrouter"|"api1"|"api2",
    "model"?: str (rỗng = xoá override, dùng lại mặc định env),
    "api_key"?: str (rỗng = xoá override),
    "enabled"?: bool}."""
    if not _admin_session_valid(request):
        return Response(status_code=403)
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
    if "enabled" in body:
        enabled = bool(body["enabled"])
        if provider == "router9":
            await orchestrator.set_router9_enabled(enabled)
        elif provider in provider_overrides.ENABLE_OVERRIDABLE:
            await provider_overrides.set_enabled(provider, enabled)

    return JSONResponse(await _provider_info(provider))


_CHANNEL_LABELS = {"telegram": "Telegram", "zoom": "Zoom", "zalo": "Zalo"}


@api.get("/admin/api/tavily")
async def admin_tavily(request: Request) -> Response:
    if not _admin_session_valid(request):
        return Response(status_code=403)
    api_key = await tavily_client._api_key()
    return JSONResponse({
        "enabled": await tavily_client.get_enabled(),
        "api_key_configured": bool(api_key),
        "api_key_masked": _mask_api_key(api_key or ""),
        "api_key_overridden": bool(await provider_overrides.get_api_key_override("tavily")),
    })


@api.post("/admin/api/tavily/update")
async def admin_tavily_update(request: Request) -> Response:
    """Body: {"enabled"?: bool, "api_key"?: str (rỗng = xoá override)}."""
    if not _admin_session_valid(request):
        return Response(status_code=403)
    body = await request.json()
    if "enabled" in body:
        await tavily_client.set_enabled(bool(body["enabled"]))
    if "api_key" in body:
        try:
            await provider_overrides.set_api_key_override("tavily", body["api_key"])
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    api_key = await tavily_client._api_key()
    return JSONResponse({
        "enabled": await tavily_client.get_enabled(),
        "api_key_configured": bool(api_key),
        "api_key_masked": _mask_api_key(api_key or ""),
        "api_key_overridden": bool(await provider_overrides.get_api_key_override("tavily")),
    })


@api.get("/admin/api/agnes")
async def admin_agnes(request: Request) -> Response:
    if not _admin_session_valid(request):
        return Response(status_code=403)
    api_key = await agnes_client._api_key()
    return JSONResponse({
        "enabled": await agnes_client.get_enabled(),
        "model": await provider_overrides.get_model_override("agnes") or config.AGNES_IMAGE_MODEL,
        "model_overridden": bool(await provider_overrides.get_model_override("agnes")),
        "api_key_configured": bool(api_key),
        "api_key_masked": _mask_api_key(api_key or ""),
        "api_key_overridden": bool(await provider_overrides.get_api_key_override("agnes")),
    })


@api.post("/admin/api/agnes/update")
async def admin_agnes_update(request: Request) -> Response:
    """Body: {"enabled"?: bool, "api_key"?: str, "model"?: str (rỗng = xoá override)}."""
    if not _admin_session_valid(request):
        return Response(status_code=403)
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
    api_key = await agnes_client._api_key()
    return JSONResponse({
        "enabled": await agnes_client.get_enabled(),
        "model": await provider_overrides.get_model_override("agnes") or config.AGNES_IMAGE_MODEL,
        "model_overridden": bool(await provider_overrides.get_model_override("agnes")),
        "api_key_configured": bool(api_key),
        "api_key_masked": _mask_api_key(api_key or ""),
        "api_key_overridden": bool(await provider_overrides.get_api_key_override("agnes")),
    })


async def _memory_user_entries() -> list[dict]:
    entries = [{"user_id": config.ALLOWED_USER_ID, "label": "Chủ bot (Telegram/Zoom)"}]
    for zuser in await zalo_users.list_users():
        entries.append({"user_id": zuser.internal_user_id, "label": zuser.display_name or zuser.external_id})
    return entries


@api.get("/admin/api/memory")
async def admin_memory(request: Request) -> Response:
    if not _admin_session_valid(request):
        return Response(status_code=403)
    result = []
    for entry in await _memory_user_entries():
        result.append({**entry, "enabled": await memory_service.is_enabled(entry["user_id"])})
    return JSONResponse(result)


@api.post("/admin/api/memory/update")
async def admin_memory_update(request: Request) -> Response:
    """Body: {"user_id": int, "enabled": bool}."""
    if not _admin_session_valid(request):
        return Response(status_code=403)
    body = await request.json()
    user_id = body.get("user_id")
    if not isinstance(user_id, int):
        return Response(status_code=400)
    await memory_service.set_enabled(user_id, bool(body.get("enabled", True)))
    return JSONResponse({"user_id": user_id, "enabled": await memory_service.is_enabled(user_id)})


@api.get("/admin/api/usage")
async def admin_usage(request: Request) -> Response:
    if not _admin_session_valid(request):
        return Response(status_code=403)
    since_hours = int(request.query_params.get("hours", "168"))
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
        result.append({
            "channel": channel,
            "channel_label": _CHANNEL_LABELS.get(channel, channel),
            "user_id": uid,
            "label": label,
            "calls": row["calls"],
            "last_call_at": row["last_call_at"].isoformat() if row["last_call_at"] else None,
        })
    return JSONResponse(result)


@api.get("/admin/api/usage/models")
async def admin_usage_models(request: Request) -> Response:
    """Lượt gọi thành công theo (provider, model) - xem
    ai/orchestrator.py::_record_provider_call, ghi mỗi khi 1 provider trong
    provider-chain trả lời thành công (router9/groq/openrouter/api1/api2)."""
    if not _admin_session_valid(request):
        return Response(status_code=403)
    since_hours = int(request.query_params.get("hours", "168"))
    rows = await db.usage_by_model(since_hours)
    return JSONResponse([
        {
            "provider": row["provider"],
            "model": row["model"],
            "calls": row["calls"],
            "last_call_at": row["last_call_at"].isoformat() if row["last_call_at"] else None,
        }
        for row in rows
    ])


@api.post(config.WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not config.WEBHOOK_SECRET or not hmac.compare_digest(secret, config.WEBHOOK_SECRET):
        return Response(status_code=403)
    if application is None:
        return Response(status_code=503)

    update = Update.de_json(await request.json(), application.bot)
    if update.update_id is not None:
        if not await idempotency.claim_telegram_update(update.update_id):
            return Response(status_code=200)

    task = asyncio.create_task(_process_update(update))
    _background_tasks.add(task)

    def done(completed: asyncio.Task) -> None:
        _background_tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            logger.exception("Background task xử lý update lỗi không bắt được")

    task.add_done_callback(done)
    return Response(status_code=200)


# ─── Zoom Team Chat webhook ──────────────────────────────────────────────────
def _zoom_chunks(outputs: list[str]) -> list[str]:
    """Cắt tin theo cùng ngưỡng ký tự với Zalo; markdown->Zoom-dialect được
    xử lý riêng trong channels.zoom.send_message (khác GFM Zalo lọc sạch)."""
    return [chunk for message in outputs for chunk in split_for_zalo(message)]


async def _process_zoom_event(event: "zoom.ZoomEvent") -> None:
    async with assistant_turn():
        try:
            pairing = await db.zoom_get_pairing()
            if pairing is None or pairing[0] != event.sender_jid:
                logger.info("Zoom: tin nhắn từ jid chưa pair (%s), bỏ qua + báo owner.", event.sender_jid)
                if application is not None:
                    try:
                        await application.bot.send_message(
                            chat_id=config.ALLOWED_USER_ID,
                            text=messages.ZOOM_UNPAIRED_ALERT.format(jid=event.sender_jid),
                        )
                    except Exception:
                        logger.warning("Không gửi được cảnh báo Zoom chưa pair.", exc_info=True)
                return

            cached = await idempotency.get_zalo_response(
                event.account_id or "zoom-bot", event.event_id, "zoom-text"
            )
            if cached is not None:
                reply_texts = cached.get("messages", [])
                image_url = cached.get("image_url")
            else:
                # Lệnh quản lý/xem lại nhóm Zalo (/nhom, /themnhom, /xoanhom, /tongket,
                # /dangnoi) hoạt động GIỐNG HỆT từ Zoom như từ Zalo (xem README mục
                # "Zoom Team Chat") - dữ liệu nhóm luôn là dữ liệu Zalo (thu thập qua
                # zalo-gateway), Zoom chỉ là 1 kênh khác để TRUY VẤN dữ liệu đó. Vì
                # request tới đây không có sẵn account_id Zalo (khác channels/router.py
                # nhận trực tiếp từ payload bridge), phải tự suy ra qua
                # zalo_repository.resolve_default_account_id().
                zalo_account_id = await zalo_repository.resolve_default_account_id()
                group_result = (
                    await group_commands.maybe_handle_group_command(zalo_account_id, event.text.strip())
                    if zalo_account_id
                    else None
                )
                if group_result is not None:
                    reply_texts = _zoom_chunks(group_result.messages)
                    provider = None
                    image_url = None
                else:
                    result = await handle_channel_text(config.ALLOWED_USER_ID, event.text.strip(), channel="zoom")
                    reply_texts = _zoom_chunks(result.messages)
                    provider = result.provider
                    image_url = result.image_url
                await idempotency.save_zalo_response(
                    event.account_id or "zoom-bot",
                    event.event_id,
                    "zoom-text",
                    {"messages": reply_texts, "provider": provider, "image_url": image_url},
                )

            for chunk in reply_texts:
                await zoom.send_message(
                    event.reply_jid, chunk, user_jid=event.sender_jid, account_id=event.account_id
                )
            if image_url:
                try:
                    await zoom.send_image_message(
                        event.reply_jid,
                        image_url,
                        user_jid=event.sender_jid,
                        account_id=event.account_id,
                    )
                except Exception:
                    logger.warning(
                        "Không gửi được ảnh qua Zoom (event_id=%s), text đã gửi bình thường.",
                        event.event_id,
                        exc_info=True,
                    )
        except Exception:
            logger.exception("Lỗi xử lý sự kiện Zoom event_id=%s", event.event_id)


@api.post(config.ZOOM_WEBHOOK_PATH)
async def zoom_webhook(request: Request) -> Response:
    if not config.ZOOM_ENABLED:
        return Response(status_code=404)

    raw_body = await request.body()
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=400)

    event_type = payload.get("event", "")

    # Bước xác thực challenge-response khi bấm "Validate" trên Marketplace -
    # KHÔNG cần verify chữ ký/token ở request này (xem docstring hàm build_url_validation_response).
    if event_type == "endpoint.url_validation":
        plain_token = (payload.get("payload") or {}).get("plainToken", "")
        if not plain_token or not config.ZOOM_SECRET_TOKEN:
            return Response(status_code=400)
        return zoom.build_url_validation_response(plain_token)

    # Xác thực request thật: ưu tiên chữ ký HMAC (cơ chế mới), fallback về
    # Verification Token cũ nếu app không dùng Secret Token.
    signature = request.headers.get("x-zm-signature", "")
    timestamp = request.headers.get("x-zm-request-timestamp", "")
    authorized = False
    if config.ZOOM_SECRET_TOKEN:
        authorized = zoom.verify_webhook_signature(signature, timestamp, raw_body)
    elif config.ZOOM_VERIFICATION_TOKEN:
        authorized = zoom.verify_webhook_token(request.headers.get("authorization", ""))
    if not authorized:
        return Response(status_code=403)

    event = zoom.parse_event(payload)
    if event is None:
        # Không phải sự kiện tin nhắn text hiểu được (vd reaction, join...) - vẫn trả 200
        # để Zoom không coi là lỗi và retry vô ích.
        return Response(status_code=200)

    if not await idempotency.claim_zoom_event(event.event_id):
        return Response(status_code=200)

    task = asyncio.create_task(_process_zoom_event(event))
    _background_tasks.add(task)

    def done(completed: asyncio.Task) -> None:
        _background_tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            logger.exception("Background task xử lý sự kiện Zoom lỗi không bắt được")

    task.add_done_callback(done)
    return Response(status_code=200)
