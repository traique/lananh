"""Entrypoint dùng để deploy lên Render bằng Telegram webhook."""

import asyncio
import hmac
import io
import logging
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from telegram import Update
from telegram.ext import Application

import bot_app
import web_admin
from handlers import turn_feedback
import web_site
import logging_setup
from channels import (
    facebook_commands,
    group_commands,
    zalo_repository,
    zalo_scheduler,
    zalo_session,
    zoom,
)
from channels.router import router as zalo_router
from core import config, database as db, idempotency, webhook_inbox
from diagnose_router9 import main as diagnose_main
from services import db_maintenance, market_page, morning_news
from services import channel_dispatch, turn_control
from services.channel_chat_service import handle_channel_text, split_for_zalo
from services.concurrency import OWNER_TURN_KEY, assistant_turn, cancel_turn, is_busy
from services.reminder_delivery import NotificationTarget, notification_target
from services.telegram_processing import update_error
from services.request_limits import RequestLimitsMiddleware
from services.security_headers import SecurityHeadersMiddleware

logging_setup.configure_logging()
logger = logging.getLogger(__name__)
application: Application | None = None
_diagnose_lock = asyncio.Lock()
_PRIVACY_PATH = Path(__file__).resolve().parent / "templates" / "privacy.html"
_TERMS_PATH = Path(__file__).resolve().parent / "templates" / "terms.html"
_DATA_DELETION_PATH = Path(__file__).resolve().parent / "templates" / "data_deletion.html"


def _diagnose_token_valid(request: Request) -> bool:
    token = request.headers.get("X-Diagnose-Token", "")
    # So sánh bytes: header chứa ký tự non-ASCII không làm compare_digest TypeError (500).
    return bool(config.DIAGNOSE_SECRET) and hmac.compare_digest(
        token.encode(), config.DIAGNOSE_SECRET.encode()
    )


async def _safe_shutdown(label: str, awaitable) -> None:
    try:
        await awaitable
    except Exception:
        logger.exception("Shutdown step lỗi: %s", label)


async def _process_update(update: Update, *, locked: bool = True) -> None:
    """Preserve one cross-channel conversation order for the single owner.

    locked=False: lối nhanh của lệnh chỉ đọc khi khoá lượt đang bận
    (handlers/turn_feedback.maybe_fast_path) - chạy ngoài assistant_turn."""
    if application is None:
        raise RuntimeError("Telegram application is not ready")
    token = update_error.set(None)
    try:
        with notification_target(
            NotificationTarget(
                "telegram",
                str(update.effective_chat.id if update.effective_chat else config.ALLOWED_USER_ID),
                event_key=f"telegram:{update.update_id}",
            )
        ):
            message = getattr(update, "effective_message", None)
            command = (getattr(message, "text", None) or "").strip().lower().split(maxsplit=1)
            boundary = (
                nullcontext()
                if not locked or (command and command[0].startswith("/fb_"))
                else assistant_turn(OWNER_TURN_KEY)
            )
            async with boundary:
                await application.process_update(update)
        if update_error.get() is not None:
            raise update_error.get()
    except Exception as exc:
        # Tin đã mang 👀 (phải chờ lượt trước): hỏng hẳn mới đổi 😢, còn được
        # inbox thử lại thì giữ 👀.
        attempt = webhook_inbox.current_attempt() or 1
        final = webhook_inbox.is_non_retriable(exc) or attempt >= webhook_inbox.max_attempts()
        bot = getattr(application, "bot", None)
        turn_feedback.finish_queued(update.update_id, bot, ok=False, final=final)
        raise
    else:
        turn_feedback.finish_queued(update.update_id, getattr(application, "bot", None), ok=True)
    finally:
        update_error.reset(token)


async def _process_update_unlocked(update: Update) -> None:
    await _process_update(update, locked=False)


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
        await _safe_shutdown("Telegram turn feedback", turn_feedback.stop())
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


# Không mở /docs, /openapi.json: handler nhận Request thô nên schema sinh ra
# rỗng (hợp đồng thật ở docstring web_site.py + README) và danh sách route
# nội bộ (/internal/zalo/*, /webhook) không cần lộ cho người lạ.
api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
api.add_middleware(RequestLimitsMiddleware)
# Thêm SAU (= bọc NGOÀI cùng) để cả response 413/503 của RequestLimits cũng có header.
api.add_middleware(SecurityHeadersMiddleware)


@api.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """Dưới /api/v1 mọi lỗi HTTP (404, 405...) cùng dạng {"error": {code, message}}
    như lỗi nhập liệu 422; các đường dẫn khác giữ dạng mặc định của FastAPI."""
    if request.url.path.startswith("/api/v1/"):
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return JSONResponse(
            {
                "error": {
                    "code": codes.get(exc.status_code, "HTTP_ERROR"),
                    "message": str(exc.detail),
                }
            },
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )
    return await http_exception_handler(request, exc)


api.include_router(zalo_router)
api.include_router(web_admin.router)
api.include_router(web_admin.api_router)
api.include_router(web_site.static_router)
api.include_router(web_site.router)
api.include_router(web_site.api_router)


@api.api_route("/", methods=["GET", "HEAD"])
async def health(request: Request) -> Response:
    """Trình duyệt (Accept: text/html) nhận trang chủ; Render health check,
    UptimeRobot và mọi client khác vẫn nhận JSON {"status": "ok"} như cũ."""
    if request.method == "GET" and web_site.wants_html(request):
        return await web_site.home(request)
    return JSONResponse({"status": "ok"})


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
        # Ghi vào buffer riêng của request (không redirect_stdout: thao tác đó đổi
        # sys.stdout của CẢ process trong lúc await mạng, nuốt log của request khác).
        buf = io.StringIO()
        try:
            await diagnose_main(buf)
        except Exception as exc:
            print(f"Lỗi ngoài dự kiến: {type(exc).__name__}: {exc}", file=buf)
        return Response(content=buf.getvalue(), media_type="text/plain; charset=utf-8")


@api.get("/healthz")
async def healthz() -> Response:
    """Health sâu: kiểm tra DB (SELECT 1, trần 3s). Render vẫn dùng GET / (nhẹ,
    không chạm DB) để không restart service chỉ vì Supabase chập chờn; endpoint
    này để UptimeRobot/chủ bot soi trạng thái thật."""
    try:
        pool = await asyncio.wait_for(db.get_pool(), timeout=3.0)
        await asyncio.wait_for(pool.fetchval("SELECT 1"), timeout=3.0)
    except Exception as exc:
        logger.warning("healthz: DB không phản hồi: %r", exc)
        return Response(
            content='{"status":"degraded","db":"down"}',
            media_type="application/json",
            status_code=503,
        )
    return Response(content='{"status":"ok","db":"ok"}', media_type="application/json")


@api.post(config.WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    # So sánh bytes: header non-ASCII không làm compare_digest TypeError (500 thay vì 403).
    if not config.WEBHOOK_SECRET or not hmac.compare_digest(
        secret.encode(), config.WEBHOOK_SECRET.encode()
    ):
        return Response(status_code=403)
    if application is None:
        return Response(status_code=503)

    try:
        payload = await request.json()
        update = Update.de_json(payload, application.bot)
        if update.update_id is None:
            return Response(status_code=400)
        # /huy và lệnh chỉ đọc lúc khoá lượt bận: xử lý ngay, không qua inbox
        # (cả 2 worker inbox có thể đang kẹt sau khoá lượt của chủ bot).
        if turn_feedback.maybe_fast_path(update, application.bot, _process_update_unlocked):
            return Response(status_code=200)
        await webhook_inbox.enqueue("telegram", str(update.update_id), payload)
        turn_feedback.mark_queued(update, application.bot)
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
    with notification_target(
        NotificationTarget(
            "zoom",
            event.reply_jid,
            event.account_id,
            event.sender_jid,
            event_key=f"zoom:{event.account_id}:{event.event_id}",
        )
    ):
        await _deliver_zoom_event(event)


async def _deliver_zoom_event(event: "zoom.ZoomEvent") -> None:
    try:
        pairing = await db.zoom_get_pairing()
        if pairing is None or pairing[0] != event.sender_jid:
            logger.info(
                "Zoom: tin nhắn từ jid chưa pair (%s), bỏ qua + báo owner.", event.sender_jid
            )
            zoom.notify_unpaired(event.sender_jid)
            return

        # Zoom admin dùng chung lệnh quản lý nhóm Zalo và /fb_* với Zalo admin.
        # Request Zoom không có account_id Zalo nên ưu tiên lấy từ session đang
        # đăng nhập; fallback resolver cũ để tương thích session cũ.
        zalo_account_id = (
            await zalo_session.load_account_id()
            or await zalo_repository.resolve_default_account_id()
        )
        payload = await channel_dispatch.dispatch(
            channel_dispatch.DispatchRequest(
                account_id=event.account_id or "zoom-bot",
                event_id=event.event_id,
                cache_kind="zoom-text",
                user_id=config.ALLOWED_USER_ID,
                is_admin=True,
                channel="zoom",
                text=event.text,
                turn_key=OWNER_TURN_KEY,
                command_account_id=zalo_account_id or None,
            ),
            channel_dispatch.DispatchHandlers(
                chat=lambda user_id, text, is_admin: handle_channel_text(
                    user_id, text, channel="zoom"
                ),
                get_cached=idempotency.get_zalo_response,
                save_cached=idempotency.save_zalo_response,
                facebook=facebook_commands.maybe_handle_facebook_command,
                group=group_commands.maybe_handle_group_command,
                chunk=_zoom_chunks,
                turn=assistant_turn,
            ),
        )
        reply_texts = payload.get("messages", [])
        image_url = payload.get("image_url")

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


async def _zoom_fast_path(event: "zoom.ZoomEvent") -> bool:
    """/huy và lệnh chỉ đọc (lúc khoá lượt chủ bot bận) của Zoom: xử lý ngay ở
    webhook như Telegram. True = đã xử lý, không đưa vào inbox."""
    cancel = turn_control.is_cancel(event.text)
    readonly = turn_control.is_readonly(event.text) and is_busy(OWNER_TURN_KEY)
    if not (cancel or readonly):
        return False
    pairing = await db.zoom_get_pairing()
    if pairing is None or pairing[0] != event.sender_jid:
        return False  # inbox lo báo chủ bot như mọi tin chưa pair
    if turn_feedback.seen_once(f"zoom:{event.event_id}"):
        return True
    if readonly:
        turn_feedback.spawn(_process_zoom_event(event))
        return True
    if not cancel_turn(OWNER_TURN_KEY):
        # Có việc bị huỷ thì chính lượt đó trả lời "em dừng rồi".
        turn_feedback.spawn(
            zoom.send_message(
                event.reply_jid,
                turn_control.nothing_to_cancel_text(owner=True),
                user_jid=event.sender_jid,
                account_id=event.account_id,
            )
        )
    return True


@api.post(config.ZOOM_WEBHOOK_PATH)
async def zoom_webhook(request: Request) -> Response:
    if not config.ZOOM_ENABLED:
        return Response(status_code=404)

    raw_body = await request.body()
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=400)
    if not isinstance(payload, dict):
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
        if await _zoom_fast_path(event):
            return Response(status_code=200)
        await webhook_inbox.enqueue("zoom", event.event_id, payload)
    except Exception:
        logger.warning("Không lưu được Zoom webhook vào inbox.", exc_info=True)
        return Response(status_code=503)
    return Response(status_code=200)
