"""Factory đăng ký handler dùng chung cho long polling và webhook."""

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

import scheduler
import tg_format
from ai import agnes_client, orchestrator, router9_client, groq_client, openrouter_client, tavily_client
from channels import zalo_users
from core import config, database as db, idempotency
from handlers import chat_router, commands, media_handler, portfolio_commands, zalo_login
from services import channel_chat_service
from services.background_tasks import stop_tracked_tasks
from stock import portfolio
from stock import providers as stock_providers

logger = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("start", "Bắt đầu"),
    BotCommand("help", "Xem hướng dẫn"),
    BotCommand("danhmuc", "Xem danh mục cổ phiếu"),
    BotCommand("themcp", "Thêm giao dịch mua"),
    BotCommand("capnhatcp", "Cập nhật vị thế"),
    BotCommand("bancp", "Bán bớt hoặc bán hết"),
    BotCommand("xoacp", "Xóa mã khỏi danh mục"),
    BotCommand("zalo", "Đăng nhập hoặc xem trạng thái Zalo B"),
    BotCommand("zalologout", "Đăng xuất Zalo B"),
    BotCommand("prompt", "Viết prompt tạo ảnh"),
    BotCommand("gia", "Tìm giá sản phẩm"),
    BotCommand("dich", "Dịch chat Nhật-Việt"),
    BotCommand("reset", "Xoá ngữ cảnh chat"),
    BotCommand("history", "Xem lịch sử"),
    BotCommand("memory", "Xem trí nhớ dài hạn"),
    BotCommand("forget", "Xoá trí nhớ"),
    BotCommand("notes", "Xem ghi chú"),
    BotCommand("model", "Xem/đổi model"),
    BotCommand("status", "Xem provider"),
    BotCommand("userouter9", "Thử lại 9Router"),
    BotCommand("router9", "Bật/tắt 9Router (on|off)"),
    BotCommand("tavily", "Bật/tắt tra web Tavily (on|off)"),
    BotCommand("anh", "Tạo ảnh thật từ mô tả (Agnes AI)"),
    BotCommand("zoompair", "Cấp quyền 1 jid Zoom"),
    BotCommand("zoomxoa", "Gỡ pairing Zoom"),
    BotCommand("zoomstatus", "Xem jid Zoom đã pair"),
    BotCommand("nhom", "Xem danh sách nhóm Zalo"),
    BotCommand("themnhom", "Thêm nhóm Zalo theo dõi"),
    BotCommand("xoanhom", "Ngừng theo dõi 1 nhóm Zalo"),
    BotCommand("tongket", "Tổng kết nhóm Zalo (AI)"),
    BotCommand("dangnoi", "Xem nguyên văn thảo luận hôm nay"),
    BotCommand("zalopair", "Cấp quyền thành viên Zalo"),
    BotCommand("zaloadmin", "Cấp quyền admin Zalo"),
    BotCommand("zalohaquyen", "Hạ quyền admin Zalo về thành viên"),
    BotCommand("zalokhoa", "Khóa 1 user Zalo"),
    BotCommand("zalomokhoa", "Mở khóa 1 user Zalo"),
    BotCommand("zaloxoa", "Xoá pairing 1 user Zalo"),
    BotCommand("zalodanhsach", "Xem danh sách user Zalo"),
]


async def _post_init(app):
    await db.init_db()
    await idempotency.ensure_schema()
    await portfolio.ensure_schema()
    await zalo_users.ensure_schema()
    await app.bot.set_my_commands(COMMANDS)
    await orchestrator.init_provider_state()
    orchestrator.start_background_tasks()
    scheduler.start(config.ALLOWED_USER_ID)


async def _post_shutdown(app):
    async def run_step(label, awaitable):
        try:
            await awaitable
        except Exception:
            logger.exception("Shutdown step lỗi: %s", label)

    await run_step("Telegram scheduler", scheduler.stop())
    await run_step("Telegram memory tasks", chat_router.stop_background_tasks())
    await run_step("channel memory tasks", channel_chat_service.stop_background_tasks())

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
    await run_step("Agnes AI client", agnes_client.close())

    await run_step("stock HTTP client", stock_providers.close_http_client())
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
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    command_handlers = [
        ("start", commands.start_cmd),
        ("help", commands.help_cmd),
        ("danhmuc", portfolio_commands.portfolio_cmd),
        ("themcp", portfolio_commands.portfolio_cmd),
        ("capnhatcp", portfolio_commands.portfolio_cmd),
        ("bancp", portfolio_commands.portfolio_cmd),
        ("xoacp", portfolio_commands.portfolio_cmd),
        ("zalo", zalo_login.zalo_cmd),
        ("zalologout", zalo_login.zalologout_cmd),
        ("prompt", commands.prompt_cmd),
        ("gia", commands.price_cmd),
        ("dich", commands.dich_cmd),
        ("reset", commands.reset_chat_cmd),
        ("history", commands.history_cmd),
        ("memory", commands.memory_cmd),
        ("forget", commands.forget_cmd),
        ("notes", commands.notes_cmd),
        ("model", commands.model_cmd),
        ("status", commands.status_cmd),
        ("userouter9", commands.userouter9_cmd),
        ("router9", commands.router9_toggle_cmd),
        ("tavily", commands.tavily_toggle_cmd),
        ("anh", commands.anh_cmd),
        ("zoompair", commands.zoompair_cmd),
        ("zoomxoa", commands.zoomxoa_cmd),
        ("zoomstatus", commands.zoomstatus_cmd),
        ("nhom", commands.nhom_cmd),
        ("themnhom", commands.themnhom_cmd),
        ("xoanhom", commands.xoanhom_cmd),
        ("tongket", commands.tongket_cmd),
        ("dangnoi", commands.dangnoi_cmd),
        ("zalopair", commands.zalopair_cmd),
        ("zaloadmin", commands.zaloadmin_cmd),
        ("zalohaquyen", commands.zalohaquyen_cmd),
        ("zalokhoa", commands.zalokhoa_cmd),
        ("zalomokhoa", commands.zalomokhoa_cmd),
        ("zaloxoa", commands.zaloxoa_cmd),
        ("zalodanhsach", commands.zalodanhsach_cmd),
    ]
    for name, handler in command_handlers:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(MessageHandler(filters.PHOTO, media_handler.photo_msg))
    app.add_handler(MessageHandler(filters.Document.IMAGE, media_handler.photo_msg))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_router.chat_msg))
    app.add_handler(MessageHandler(filters.COMMAND, commands.unknown_cmd))
    app.add_error_handler(commands.error_handler)

    async def alert(text):
        try:
            await app.bot.send_message(chat_id=config.ALLOWED_USER_ID, text=text)
        except Exception:
            logger.warning("Không gửi được cảnh báo.", exc_info=True)

    orchestrator.set_alert_callback(alert)
    zalo_users.set_alert_callback(alert)

    async def notify(uid, text):
        try:
            await tg_format.send_rich(app.bot, uid, text)
        except Exception:
            logger.warning("Không gửi được thông báo.", exc_info=True)

    scheduler.set_notify_callback(notify)
    return app
