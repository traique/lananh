"""Entrypoint dùng để deploy lên Render bằng Telegram webhook."""

import asyncio
import hmac
import io
import logging
from contextlib import asynccontextmanager, redirect_stdout

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application

import bot_app
import logging_setup
import messages
from channels import group_commands, zalo_repository, zalo_scheduler, zoom
from channels.router import router as zalo_router
from core import config, database as db, idempotency
from diagnose_router9 import main as diagnose_main
from services.background_tasks import stop_tracked_tasks
from services.channel_chat_service import handle_channel_text, split_for_zalo
from services.concurrency import assistant_turn

logging_setup.configure_logging()
logger = logging.getLogger(__name__)
application: Application | None = None
_background_tasks: set[asyncio.Task] = set()
_diagnose_lock = asyncio.Lock()


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
        yield
    finally:
        logger.info("Đang tắt bot...")
        await _safe_shutdown("Zalo scheduler", zalo_scheduler.stop())
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
    token = request.headers.get("X-Diagnose-Token", "")
    if not config.DIAGNOSE_SECRET or not hmac.compare_digest(
        token,
        config.DIAGNOSE_SECRET,
    ):
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
                else:
                    result = await handle_channel_text(config.ALLOWED_USER_ID, event.text.strip())
                    reply_texts = _zoom_chunks(result.messages)
                    provider = result.provider
                await idempotency.save_zalo_response(
                    event.account_id or "zoom-bot",
                    event.event_id,
                    "zoom-text",
                    {"messages": reply_texts, "provider": provider},
                )

            for chunk in reply_texts:
                await zoom.send_message(
                    event.reply_jid, chunk, user_jid=event.sender_jid, account_id=event.account_id
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
