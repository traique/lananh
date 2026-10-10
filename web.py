"""Entrypoint dùng để deploy lên Render bằng Telegram webhook."""

import asyncio
import hmac
import io
import logging
from contextlib import asynccontextmanager, nullcontext, redirect_stdout
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from telegram import Update
from telegram.ext import Application

import bot_app
import web_admin
import logging_setup
import messages
from channels import facebook_commands, facebook_repository, group_commands, zalo_repository, zalo_scheduler, zalo_session, zoom
from channels.router import router as zalo_router
from core import config, database as db, idempotency, webhook_inbox
from diagnose_router9 import main as diagnose_main
from services import db_maintenance, market_page, morning_news
from services.background_tasks import stop_tracked_tasks
from services.channel_chat_service import handle_channel_text, split_for_zalo
from services.concurrency import OWNER_TURN_KEY, assistant_turn
from services.reminder_delivery import NotificationTarget, notification_target
from services.telegram_processing import update_error
from services.request_limits import RequestLimitsMiddleware

logging_setup.configure_logging()
logger = logging.getLogger(__name__)
application: Application | None = None
_background_tasks: set[asyncio.Task] = set()
_diagnose_lock = asyncio.Lock()
_PRIVACY_PATH = Path(__file__).resolve().parent / "templates" / "privacy.html"
_TERMS_PATH = Path(__file__).resolve().parent / "templates" / "terms.html"
_DATA_DELETION_PATH = Path(__file__).resolve().parent / "templates" / "data_deletion.html"


def _diagnose_token_valid(request: Request) -> bool:
    token = request.headers.get("X-Diagnose-Token", "")
    return bool(config.DIAGNOSE_SECRET) and hmac.compare_digest(token, config.DIAGNOSE_SECRET)


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
        raise RuntimeError("Telegram application is not ready")
    token = update_error.set(None)
    try:
        with notification_target(NotificationTarget(
            "telegram", str(update.effective_chat.id if update.effective_chat else config.ALLOWED_USER_ID),
            event_key=f"telegram:{update.update_id}",
        )):
            message = getattr(update, "effective_message", None)
            command = (getattr(message, "text", None) or "").strip().lower().split(maxsplit=1)
            boundary = (
                nullcontext()
                if command and command[0].startswith("/fb_")
                else assistant_turn(OWNER_TURN_KEY)
            )
            async with boundary:
                await application.process_update(update)
        if update_error.get() is not None:
            raise update_error.get()
    finally:
        update_error.reset(token)


async def _process_inbox_event(channel: str, payload: dict) -> None:
    if channel == "telegram":
        if application is None:
            raise RuntimeError("Telegram application is not ready")
        await _process_update(Update.de_json(payload, application.bot))
    elif channel == "zoom":
        event = zoom.parse_event(payload)
        if event is None:
            raise ValueError("Invalid Zoom inbox event")
        await _process_zoom_event(event)
    else:
        raise ValueError("Unsupported inbox channel")


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
        webhook_inbox.start(_process_inbox_event)
        webhook_url = config.WEBHOOK_BASE_URL.rstrip("/") + config.WEBHOOK_PATH
        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=config.WEBHOOK_SECRET,
            allowed_updates=["message"],
        )
        logger.info("Webhook đã set tới: %s", webhook_url)
        zalo_scheduler.start()
        morning_news.start()
        market_page.start()
        db_maintenance.start()
        yield
    finally:
        logger.info("Đang tắt bot...")
        await _safe_shutdown("Zalo scheduler", zalo_scheduler.stop())
        await _safe_shutdown("Morning news scheduler", morning_news.stop())
        await _safe_shutdown("Market page scheduler", market_page.stop())
        await _safe_shutdown("DB maintenance", db_maintenance.stop())
        await _safe_shutdown("webhook inbox", webhook_inbox.stop())
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
api.add_middleware(RequestLimitsMiddleware)
api.include_router(zalo_router)
api.include_router(web_admin.router)
api.include_router(web_admin.api_router)


@api.get("/r/{token}", include_in_schema=False)
async def affiliate_redirect(token: str):
    target = await facebook_repository.resolve_short_link(token)
    if not target:
        raise HTTPException(status_code=404, detail="Short link not found")
    return RedirectResponse(target, status_code=302)


@api.api_route("/", methods=["GET", "HEAD"])
async def health() -> dict:
    return {"status": "ok"}


@api.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_policy() -> HTMLResponse:
    return HTMLResponse(_PRIVACY_PATH.read_text(encoding="utf-8"))


@api.get("/terms", response_class=HTMLResponse, include_in_schema=False)
async def terms_of_service() -> HTMLResponse:
    return HTMLResponse(_TERMS_PATH.read_text(encoding="utf-8"))


@api.get("/data-deletion", response_class=HTMLResponse, include_in_schema=False)
async def data_deletion_instructions() -> HTMLResponse:
    return HTMLResponse(_DATA_DELETION_PATH.read_text(encoding="utf-8"))


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


@api.post(config.WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not config.WEBHOOK_SECRET or not hmac.compare_digest(secret, config.WEBHOOK_SECRET):
        return Response(status_code=403)
    if application is None:
        return Response(status_code=503)

    try:
        payload = await request.json()
        update = Update.de_json(payload, application.bot)
        if update.update_id is None:
            return Response(status_code=400)
        await webhook_inbox.enqueue("telegram", str(update.update_id), payload)
    except (ValueError, TypeError, KeyError):
        return Response(status_code=400)
    except Exception:
        logger.warning("Không lưu được Telegram webhook vào inbox.", exc_info=True)
        return Response(status_code=503)
    return Response(status_code=200)


# ─── Zoom Team Chat webhook ──────────────────────────────────────────────────
def _zoom_chunks(outputs: list[str]) -> list[str]:
    """Cắt tin theo cùng ngưỡng ký tự với Zalo; markdown->Zoom-dialect được
    xử lý riêng trong channels.zoom.send_message (khác GFM Zalo lọc sạch)."""
    return [chunk for message in outputs for chunk in split_for_zalo(message)]


async def _process_zoom_event(event: "zoom.ZoomEvent") -> None:
    with notification_target(NotificationTarget(
        "zoom", event.reply_jid, event.account_id, event.sender_jid,
        event_key=f"zoom:{event.account_id}:{event.event_id}",
    )):
        await _deliver_zoom_event(event)


async def _deliver_zoom_event(event: "zoom.ZoomEvent") -> None:
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
            # Zoom admin dùng chung lệnh quản lý nhóm Zalo và /fb_* với Zalo admin.
            # Request Zoom không có account_id Zalo nên ưu tiên lấy từ session đang
            # đăng nhập; fallback resolver cũ để tương thích session cũ.
            zalo_account_id = (
                await zalo_session.load_account_id()
                or await zalo_repository.resolve_default_account_id()
            )
            # /fb_* chạy NGOÀI assistant_turn() cố ý: /fb_ok gọi Facebook Graph API
            # có thể chậm, và nó đã có atomic riêng (claim_post). Giữ lock lượt
            # của owner suốt thời gian đó sẽ chặn chat của chính owner và chiếm
            # 1 slot MAX_CONCURRENT_TURNS.
            facebook_result = (
                await facebook_commands.maybe_handle_facebook_command(
                    zalo_account_id, event.text.strip()
                )
                if zalo_account_id
                else None
            )
            if facebook_result is not None:
                reply_texts = _zoom_chunks(facebook_result.messages)
                provider = None
                image_url = None
                await idempotency.save_zalo_response(
                    event.account_id or "zoom-bot",
                    event.event_id,
                    "zoom-text",
                    {"messages": reply_texts, "provider": provider, "image_url": image_url},
                )
            else:
                async with assistant_turn(OWNER_TURN_KEY):
                    # Re-check: một lượt gửi trùng của cùng event có thể đã
                    # được xử lý xong trong lúc ta chờ lock ở trên.
                    cached = await idempotency.get_zalo_response(
                        event.account_id or "zoom-bot", event.event_id, "zoom-text"
                    )
                    if cached is not None:
                        reply_texts = cached.get("messages", [])
                        image_url = cached.get("image_url")
                    else:
                        group_result = (
                            await group_commands.maybe_handle_group_command(
                                zalo_account_id, event.text.strip()
                            )
                            if zalo_account_id
                            else None
                        )
                        if group_result is not None:
                            reply_texts = _zoom_chunks(group_result.messages)
                            provider = None
                            image_url = None
                        else:
                            result = await handle_channel_text(
                                config.ALLOWED_USER_ID, event.text.strip(), channel="zoom"
                            )
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
        raise


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

    try:
        await webhook_inbox.enqueue("zoom", event.event_id, payload)
    except Exception:
        logger.warning("Không lưu được Zoom webhook vào inbox.", exc_info=True)
        return Response(status_code=503)
    return Response(status_code=200)
