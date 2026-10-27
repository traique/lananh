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
from core import config, database as db, idempotency
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
from services import channel_chat_service, memory_service, proactive_service
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

COMMANDS = [
    BotCommand("start", "Bắt đầu"),
    BotCommand("help", "Xem hướng dẫn"),
    BotCommand("huy", "Dừng câu trả lời đang chạy dở"),
    BotCommand("sai", "Báo câu trả lời vừa rồi chưa ổn (kèm lý do)"),
    BotCommand("danhmuc", "Xem danh mục cổ phiếu"),
    BotCommand("themcp", "Thêm giao dịch mua"),
    BotCommand("capnhatcp", "Cập nhật vị thế"),
    BotCommand("bancp", "Bán bớt hoặc bán hết"),
    BotCommand("xoacp", "Xóa mã khỏi danh mục"),
    BotCommand("lichsucp", "Sổ giao dịch cổ phiếu"),
    BotCommand("laicp", "Lãi/lỗ đã chốt theo tháng"),
    BotCommand("theodoi", "Danh sách theo dõi và ngưỡng giá"),
    BotCommand("canhbaogia", "Báo khi giá chạm ngưỡng anh đặt"),
    BotCommand("zalo", "Đăng nhập hoặc xem trạng thái Zalo B"),
    BotCommand("zalologout", "Đăng xuất Zalo B"),
    BotCommand("lich", "Tra lịch âm, ngày lễ, ngày tốt, tuổi, sao hạn"),
    BotCommand("tsh", "Thần số học theo ngày sinh"),
    BotCommand("tsh_chitiet", "Thần số học + AI diễn giải"),
    BotCommand("tuvi", "An lá số tử vi"),
    BotCommand("tuvi_chitiet", "Lá số tử vi + AI luận giải"),
    BotCommand("kymon", "Lập bàn Kỳ Môn Độn Giáp (mặc định lúc này)"),
    BotCommand("kymon_chitiet", "Bàn Kỳ Môn + AI luận giải việc cần xem"),
    BotCommand("vang", "Giá vàng SJC"),
    BotCommand("tygia", "Tỷ giá Vietcombank, đổi tiền"),
    BotCommand("xoso", "Kết quả xổ số, dò vé"),
    BotCommand("thoitiet", "Thời tiết; /thoitiet nha <tỉnh> đặt nơi ở"),
    BotCommand("bantin", "Bản tin sáng cá nhân (on|off|giờ|xem)"),
    BotCommand("chuy", "Có gì cần anh chú ý hôm nay"),
    BotCommand("yenlang", "Giờ yên lặng, gom thông báo (on|off|22-7)"),
    BotCommand("viec", "Giao việc chạy nền; xem, huỷ việc"),
    BotCommand("lichtacvu", "Tác vụ định kỳ: em tự chạy yêu cầu theo lịch"),
    BotCommand("nhac", "Nhắc việc: xem, đặt, huỷ, hoãn"),
    BotCommand("ngaynho", "Ngày nhớ: giỗ, sinh nhật, kỷ niệm"),
    BotCommand("chitieu", "Chi tiêu: tổng theo danh mục, ghi khoản"),
    BotCommand("chitieu_xoa", "Xoá 1 khoản chi theo số #"),
    BotCommand("an", "Ghi bữa ăn, xem kcal ước lượng hôm nay/tuần"),
    BotCommand("noi", "Trả lời kèm giọng nói (on|off|auto)"),
    BotCommand("prompt", "Viết prompt tạo ảnh"),
    BotCommand("gia", "Tìm giá sản phẩm"),
    BotCommand("dich", "Dịch chat Nhật-Việt"),
    BotCommand("reset", "Xoá ngữ cảnh chat"),
    BotCommand("history", "Xem lịch sử"),
    BotCommand("memory", "Xem trí nhớ dài hạn (đánh số), on|off"),
    BotCommand("quen", "Quên 1 mục trí nhớ (số hoặc từ khoá)"),
    BotCommand("nho", "Nhờ em nhớ (ghim) 1 điều"),
    BotCommand("ghim", "Ghim 1 mục trí nhớ theo số"),
    BotCommand("boghim", "Bỏ ghim 1 mục trí nhớ"),
    BotCommand("forget", "Xoá hết trí nhớ dài hạn"),
    BotCommand("notes", "Xem/tìm ghi chú"),
    BotCommand("xoaghichu", "Xoá 1 ghi chú theo số"),
    BotCommand("xuatdulieu", "Tải file dữ liệu em đang lưu"),
    BotCommand("xoadulieu", "Xoá trí nhớ, ghi chú, lịch sử chat"),
    BotCommand("hoithan", "Bật/tắt em chủ động hỏi thăm việc anh kể (on|off)"),
    BotCommand("hoc", "Duyệt cách trả lời em tự rút ra (ok|bo)"),
    BotCommand("model", "Xem/đổi model"),
    BotCommand("status", "Xem provider"),
    BotCommand("loi", "Xem lỗi gom nhóm hôm nay"),
    BotCommand("thongke", "Thống kê lượt gọi theo user/model"),
    BotCommand("danhgia", "Đánh giá 👍/👎 câu trả lời theo provider"),
    BotCommand("xuateval", "Xuất câu bị 👎 thành case đánh giá (YAML)"),
    BotCommand("agent", "Hỏi agent tự tra cứu nhiều bước (thử nghiệm)"),
    BotCommand("rag", "Tra cứu kiến thức trong thư mục rag/"),
    BotCommand("ragxuly", "Dọn file md OCR trong rag/ (AI thêm heading)"),
    BotCommand("bangtinsang", "Gửi ngay Bản tin sáng tới Zalo + Zoom"),
    BotCommand("tintuc", "Đọc Điểm tin mới ngay trong Telegram"),
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
    BotCommand("fb_nhom", "Xem nhóm nguồn Facebook"),
    BotCommand("fb_pages", "Xem các Facebook Page đã cấu hình"),
    BotCommand("fb_market", "Xem thử/đăng bài Page chứng khoán"),
    BotCommand("fb_themnhom", "Thêm nhóm nguồn Facebook"),
    BotCommand("fb_xoanhom", "Bỏ nhóm nguồn Facebook"),
    BotCommand("fb_xem", "Xem bài Facebook chờ duyệt"),
    BotCommand("fb_sua", "Sửa bài Facebook chờ duyệt"),
    BotCommand("fb_link", "Nhập link Shopee affiliate cho bài chờ"),
    BotCommand("fb_ok", "Duyệt và đăng Facebook"),
    BotCommand("fb_check", "Kiểm tra bài Facebook đã công khai"),
    BotCommand("fb_reconcile", "Đối soát lượt đăng Facebook bị gián đoạn"),
    BotCommand("fb_boqua", "Bỏ bài Facebook chờ"),
    BotCommand("fb_reset", "Xóa bài Facebook đã lưu (cần gõ kèm xacnhan)"),
    BotCommand("fb_boloc", "Thống kê bộ lọc Zalo → Facebook"),
    BotCommand("fb_loclai", "Lọc lại các bài đang chờ duyệt"),
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
    command_handlers = [
        ("start", commands.start_cmd),
        ("help", commands.help_cmd),
        ("huy", commands.huy_cmd),
        ("sai", feedback_commands.sai_cmd),
        ("danhgia", feedback_commands.danhgia_cmd),
        ("xuateval", feedback_commands.xuateval_cmd),
        ("danhmuc", portfolio_commands.portfolio_cmd),
        ("themcp", portfolio_commands.portfolio_cmd),
        ("capnhatcp", portfolio_commands.portfolio_cmd),
        ("bancp", portfolio_commands.portfolio_cmd),
        ("xoacp", portfolio_commands.portfolio_cmd),
        ("lichsucp", portfolio_commands.portfolio_cmd),
        ("laicp", portfolio_commands.portfolio_cmd),
        ("giaban", portfolio_commands.portfolio_cmd),
        ("theodoi", portfolio_commands.portfolio_cmd),
        ("botheodoi", portfolio_commands.portfolio_cmd),
        ("canhbaogia", portfolio_commands.portfolio_cmd),
        ("xoacanhbao", portfolio_commands.portfolio_cmd),
        ("zalo", zalo_login.zalo_cmd),
        ("zalologout", zalo_login.zalologout_cmd),
        ("prompt", commands.prompt_cmd),
        ("gia", commands.price_cmd),
        ("dich", commands.dich_cmd),
        ("reset", commands.reset_chat_cmd),
        ("history", commands.history_cmd),
        ("memory", commands.memory_family_cmd),
        ("quen", commands.memory_family_cmd),
        ("nho", commands.memory_family_cmd),
        ("ghim", commands.memory_family_cmd),
        ("boghim", commands.memory_family_cmd),
        ("forget", commands.memory_family_cmd),
        ("notes", commands.memory_family_cmd),
        ("xoaghichu", commands.memory_family_cmd),
        ("xuatdulieu", commands.memory_family_cmd),
        ("xoadulieu", commands.memory_family_cmd),
        ("hoithan", commands.memory_family_cmd),
        ("hoc", commands.memory_family_cmd),
        ("model", provider_commands.model_cmd),
        ("status", provider_commands.status_cmd),
        ("loi", provider_commands.loi_cmd),
        ("thongke", commands.thongke_cmd),
        ("lich", commands.lich_cmd),
        ("tsh", commands.tsh_cmd),
        ("tsh_chitiet", commands.tsh_chitiet_cmd),
        ("ths_chitiet", commands.tsh_chitiet_cmd),
        ("tuvi", commands.tuvi_cmd),
        ("tuvi_chitiet", commands.tuvi_chitiet_cmd),
        ("kymon", commands.kymon_cmd),
        ("kymon_chitiet", commands.kymon_chitiet_cmd),
        ("vang", daily_data_commands.vang_cmd),
        ("tygia", daily_data_commands.tygia_cmd),
        ("xoso", daily_data_commands.xoso_cmd),
        ("thoitiet", daily_data_commands.thoitiet_cmd),
        ("bantin", daily_data_commands.bantin_cmd),
        ("chuy", daily_data_commands.chuy_cmd),
        ("yenlang", daily_data_commands.yenlang_cmd),
        ("viec", job_commands.viec_cmd),
        ("lichtacvu", job_commands.lichtacvu_cmd),
        ("nhac", reminder_commands.nhac_cmd),
        ("ngaynho", reminder_commands.ngaynho_cmd),
        ("chitieu", expense_commands.chitieu_cmd),
        ("chitieu_xoa", expense_commands.chitieu_xoa_cmd),
        ("an", personal_commands.an_cmd),
        ("noi", personal_commands.noi_cmd),
        ("agent", commands.agent_cmd),
        ("rag", commands.rag_cmd),
        ("ragxuly", commands.ragxuly_cmd),
        ("bangtinsang", commands.bangtinsang_cmd),
        ("bantinsang", commands.bangtinsang_cmd),
        ("tintuc", commands.tintuc_cmd),
        ("userouter9", provider_commands.userouter9_cmd),
        ("router9", provider_commands.router9_toggle_cmd),
        ("tavily", provider_commands.tavily_toggle_cmd),
        ("anh", provider_commands.anh_cmd),
        ("zoompair", pairing_commands.zoompair_cmd),
        ("zoomxoa", pairing_commands.zoomxoa_cmd),
        ("zoomstatus", pairing_commands.zoomstatus_cmd),
        ("nhom", zalo_bridge_commands.nhom_cmd),
        ("themnhom", zalo_bridge_commands.themnhom_cmd),
        ("xoanhom", zalo_bridge_commands.xoanhom_cmd),
        ("tongket", zalo_bridge_commands.tongket_cmd),
        ("dangnoi", zalo_bridge_commands.dangnoi_cmd),
        ("fb_nhom", zalo_bridge_commands.fb_nhom_cmd),
        ("fb_pages", zalo_bridge_commands.fb_pages_cmd),
        ("fb_market", zalo_bridge_commands.fb_market_cmd),
        ("fb_themnhom", zalo_bridge_commands.fb_themnhom_cmd),
        ("fb_xoanhom", zalo_bridge_commands.fb_xoanhom_cmd),
        ("fb_xem", zalo_bridge_commands.fb_xem_cmd),
        ("fb_sua", zalo_bridge_commands.fb_sua_cmd),
        ("fb_link", zalo_bridge_commands.fb_link_cmd),
        ("fb_ok", zalo_bridge_commands.fb_ok_cmd),
        ("fb_check", zalo_bridge_commands.fb_check_cmd),
        ("fb_reconcile", zalo_bridge_commands.fb_reconcile_cmd),
        ("fb_boqua", zalo_bridge_commands.fb_boqua_cmd),
        ("fb_reset", zalo_bridge_commands.fb_reset_cmd),
        ("fb_boloc", zalo_bridge_commands.fb_boloc_cmd),
        ("fb_loclai", zalo_bridge_commands.fb_loclai_cmd),
        ("zalopair", pairing_commands.zalopair_cmd),
        ("zaloadmin", pairing_commands.zaloadmin_cmd),
        ("zalohaquyen", pairing_commands.zalohaquyen_cmd),
        ("zalokhoa", pairing_commands.zalokhoa_cmd),
        ("zalomokhoa", pairing_commands.zalomokhoa_cmd),
        ("zaloxoa", pairing_commands.zaloxoa_cmd),
        ("zalodanhsach", pairing_commands.zalodanhsach_cmd),
    ]
    for name, handler in command_handlers:
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
