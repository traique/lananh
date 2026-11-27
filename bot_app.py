"""Factory đăng ký handler dùng chung cho long polling và webhook."""

import asyncio
import logging

from telegram import BotCommand, LinkPreviewOptions
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    Defaults,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import scheduler
import tg_format
from ai import (
    agnes_client,
    orchestrator,
    router9_client,
    groq_client,
    openrouter_client,
    tavily_client,
)
from channels import facebook_commands, zalo_users, zoom
from core import config, database as db, idempotency, memtrim
from handlers import (
    callbacks,
    chat_router,
    commands,
    daily_data_commands,
    expense_commands,
    document_handler,
    feedback_commands,
    job_commands,
    media_handler,
    voice_handler,
    pairing_commands,
    personal_commands,
    portfolio_commands,
    provider_commands,
    reminder_commands,
    unsupported,
    zalo_bridge_commands,
    zalo_login,
)
from services import channel_chat_service, command_catalog, memory_service, proactive_service
from services import morning_brief
from services import expense_service  # noqa: F401 - đăng ký mục "expenses" của bản tin sáng
from services import ngay_nho  # noqa: F401 - đăng ký mục "ngay_nho" của bản tin sáng
from services import portfolio_alerts, rag_clean_service
from services import portfolio_events, portfolio_push, price_alerts
from services import monitor_service
from services import notify_hub
from services import background_jobs
from services import routines  # noqa: F401 - đăng ký vòng xếp tác vụ định kỳ + mục /chuy
from services import owner_alerts
from services import db_maintenance
from services import http_client
from services import web_reader
from services.background_tasks import stop_tracked_tasks
from stock import portfolio
from stock import providers as stock_providers

logger = logging.getLogger(__name__)

# Bảng lệnh Telegram sinh từ danh bạ lệnh (services/command_catalog.py - nguồn duy nhất,
# chung với Zalo/Zoom). ALL_COMMANDS: mô tả các lệnh có ``menu`` (tối đa 100 như giới hạn
# setMyCommands) - tài liệu/test; menu thật (COMMANDS) chỉ ~18 lệnh hay dùng: người dùng
# cứ nhắn tự nhiên (services/nl_commands/), bỏ khỏi menu không có nghĩa là bỏ lệnh -
# handler vẫn đăng ký đủ trong build_application().
ALL_COMMANDS = [BotCommand(name, text) for name, text in command_catalog.telegram_descriptions()]
# Menu "/" của Telegram - thứ tự = thứ tự hiện trong menu.
MENU_COMMANDS = command_catalog.TELEGRAM_MENU
_DESCRIPTIONS = {c.command: c.description for c in ALL_COMMANDS}
COMMANDS = [BotCommand(name, _DESCRIPTIONS[name]) for name in MENU_COMMANDS]
# Module handler theo tên ngắn: danh bạ trỏ handler Telegram bằng "<module>.<hàm>".
_HANDLER_MODULES = {
    module.__name__.rpartition(".")[2]: module
    for module in (
        commands,
        daily_data_commands,
        expense_commands,
        feedback_commands,
        job_commands,
        pairing_commands,
        personal_commands,
        portfolio_commands,
        provider_commands,
        reminder_commands,
        zalo_bridge_commands,
        zalo_login,
    )
}


def telegram_command_handlers() -> list[tuple[str, object]]:
    """(tên lệnh, handler) cho mọi CommandHandler - sinh từ danh bạ lệnh."""
    handlers = []
    for name, ref in command_catalog.telegram_commands():
        module, _, func = ref.partition(".")
        handlers.append((name, getattr(_HANDLER_MODULES[module], func)))
    return handlers


async def _post_init(app):
    await db.init_db()
    await idempotency.ensure_schema()
    await idempotency.cleanup_expired()
    idempotency.start_cleanup_task()
    await portfolio.ensure_schema()
    await zalo_users.ensure_schema()
    await app.bot.set_my_commands(COMMANDS)
    await orchestrator.init_provider_state()
    orchestrator.start_background_tasks()
    # Cửa gom thông báo chủ động: bật TRƯỚC các vòng nền để tin đầu tiên đã đi qua hub.
    notify_hub.start()
    monitor_service.start()
    scheduler.start(config.ALLOWED_USER_ID)
    portfolio_alerts.start(config.ALLOWED_USER_ID)
    portfolio_events.start()
    price_alerts.start(config.ALLOWED_USER_ID)
    morning_brief.start()
    memory_service.start()
    proactive_service.start()
    # Việc nền /viec + tác vụ định kỳ (routines đăng ký vòng xếp lịch vào bộ chạy nền).
    background_jobs.start()
    # CUỐI CÙNG: heap khởi động (module, bảng tra, cache dựng sẵn) không đổi nữa - đóng
    # băng (gc.freeze) để gc.collect() sau mỗi lượt (core/memtrim.py) khỏi quét lại.
    # getattr: bản memtrim chưa có hàm này thì bỏ qua.
    freeze_startup_heap = getattr(memtrim, "freeze_startup_heap", None)
    if freeze_startup_heap is not None:
        freeze_startup_heap()


async def _post_shutdown(app):
    async def run_step(label, awaitable):
        try:
            await awaitable
        except Exception:
            logger.exception("Shutdown step lỗi: %s", label)

    await run_step("background jobs", background_jobs.stop())
    await run_step("notify hub", notify_hub.stop())
    await run_step("Telegram scheduler", scheduler.stop())
    await run_step("portfolio alerts", portfolio_alerts.stop())
    await run_step("portfolio events", portfolio_events.stop())
    await run_step("price alerts", price_alerts.stop())
    await run_step("morning brief", morning_brief.stop())
    await run_step("idempotency cleanup task", idempotency.stop_cleanup_task())
    await run_step("provider monitor task", monitor_service.stop())
    await run_step("proactive follow-ups", proactive_service.stop())
    await run_step("memory sweep", memory_service.stop())
    await run_step("chat memory tasks", channel_chat_service.stop_background_tasks())
    await run_step("ragxuly tasks", rag_clean_service.stop_background_tasks())

    probe_task = orchestrator._probe_task
    orchestrator._probe_task = None
    if probe_task is not None and not probe_task.done():
        probe_task.cancel()
        await asyncio.gather(probe_task, return_exceptions=True)

    await run_step(
        "provider alert tasks",
        stop_tracked_tasks(
            orchestrator.provider_state_module._background_tasks,
            timeout=5.0,
            logger=logger,
            label="provider alert",
        ),
    )

    await run_step("9Router client", router9_client.close())
    await run_step("Groq client", groq_client.close())
    await run_step("OpenRouter client", openrouter_client.close())
    await run_step("Tavily client", tavily_client.close())
    await run_step("Web reader client", web_reader.close())
    await run_step("Agnes AI client", agnes_client.close())

    await run_step("stock HTTP client", stock_providers.close_http_client())
    await run_step("shared HTTP client", http_client.close())
    await run_step("database pool", db.close_pool())


def build_application():
    request = HTTPXRequest(
        connect_timeout=config.TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=config.TELEGRAM_READ_TIMEOUT,
        write_timeout=config.TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=config.TELEGRAM_POOL_TIMEOUT,
    )
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .request(request)
        # Tắt link preview cho MỌI tin bot gửi (reply/edit/send_message, scheduler,
        # bản tin...): Telegram tự tải URL trong tin để dựng preview mà không cần
        # ai bấm - 1 trang độc xúi model viết https://ke-xau/?q=<trí nhớ> là rò
        # rỉ dữ liệu "zero-click". Mọi đường gửi đều đi qua app.bot nên 1 Defaults
        # ở đây là đủ.
        .defaults(Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True)))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    for name, handler in telegram_command_handlers():
        app.add_handler(CommandHandler(name, handler))
    # Nút inline (👍/👎, thử lại, duyệt bài Facebook, cấp quyền Zalo, hoãn nhắc).
    app.add_handler(CallbackQueryHandler(callbacks.on_callback))
    # Thứ tự: lệnh -> nút -> vị trí -> thoại -> tài liệu -> ảnh -> chữ -> lệnh lạ -> catch-all.
    # Vị trí -> thời tiết tại toạ độ; UpdateType.MESSAGE: bỏ qua cập nhật live location.
    app.add_handler(
        MessageHandler(filters.LOCATION & filters.UpdateType.MESSAGE, unsupported.location_msg)
    )
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_handler.voice_msg))
    app.add_handler(
        MessageHandler(
            filters.Document.PDF
            | filters.Document.TXT
            | filters.Document.DOCX
            | filters.Document.FileExtension("md"),
            document_handler.document_msg,
        )
    )
    app.add_handler(MessageHandler(filters.PHOTO, media_handler.photo_msg))
    app.add_handler(MessageHandler(filters.Document.IMAGE, media_handler.photo_msg))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_router.chat_msg))
    app.add_handler(MessageHandler(filters.COMMAND, commands.unknown_cmd))
    # Đăng ký CUỐI: tin loại chưa hỗ trợ (sticker, video, file lạ...) nhận 1 câu giọng
    # Lan Anh thay vì im lặng; tin dịch vụ (vào nhóm, ghim...) và tin sửa bị bỏ qua.
    app.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            unsupported.unsupported_msg,
        )
    )
    app.add_error_handler(commands.error_handler)

    async def send_telegram_alert(text):
        await app.bot.send_message(chat_id=config.ALLOWED_USER_ID, text=text)

    # Telegram trước, Zoom dự phòng/nhân bản (ALERT_ZOOM_MIRROR) - xem services/owner_alerts.py.
    alert = owner_alerts.make_alert_callback(send_telegram_alert)
    orchestrator.set_alert_callback(alert)
    zalo_users.set_alert_callback(alert)

    async def send_with_buttons(uid, text, buttons):
        await tg_format.send_rich(app.bot, uid, text, reply_markup=callbacks.markup(buttons))

    async def zalo_pair_prompt(text, buttons):
        # Cảnh báo người Zalo lạ kèm nút cấp quyền; Zoom vẫn nhận bản chữ (nhân bản).
        await send_with_buttons(config.ALLOWED_USER_ID, text, buttons)
        await owner_alerts.mirror_to_zoom(text)

    zalo_users.set_pair_prompt_callback(zalo_pair_prompt)

    async def notify_facebook_admins(text, buttons=None):
        try:
            await tg_format.send_rich(
                app.bot, config.ALLOWED_USER_ID, text, reply_markup=callbacks.markup(buttons)
            )
        except Exception:
            logger.warning("Không gửi được preview Facebook tới Telegram owner.", exc_info=True)

        if not config.ZOOM_ENABLED:
            return
        try:
            pairing = await db.zoom_get_pairing()
            if pairing is not None:
                await zoom.send_message(to_jid=pairing[0], text=text)
        except Exception:
            logger.warning("Không gửi được preview Facebook tới Zoom admin.", exc_info=True)

    facebook_commands.set_admin_notification_callback(notify_facebook_admins)
    facebook_commands.set_admin_buttons_callback(notify_facebook_admins)
    db_maintenance.set_notifier(notify_facebook_admins)

    async def notify(uid, text):
        try:
            await tg_format.send_rich(app.bot, uid, text)
        except Exception:
            logger.warning("Không gửi được thông báo.", exc_info=True)
            raise

    scheduler.set_notify_callback(notify)

    async def notify_reminder(uid, text, buttons):
        try:
            await send_with_buttons(uid, text, buttons)
        except Exception:
            logger.warning("Không gửi được tin nhắc kèm nút.", exc_info=True)
            raise

    scheduler.set_reminder_notify_callback(notify_reminder)
    portfolio_alerts.set_notify_callback(notify)
    portfolio_push.set_notify_callback(notify)
    morning_brief.set_notify_callback(notify)
    proactive_service.set_notify_callback(notify)
    notify_hub.set_telegram_sender(notify)
    return app
