"""Entrypoint chạy LOCAL bằng long polling - test nhanh, không cần webhook/HTTPS.
Deploy lên Render thì dùng web.py (webhook) thay vì file này.
"""
import logging

import bot_app
import logging_setup
from core import config

logging_setup.configure_logging()
logger = logging.getLogger(__name__)


def main() -> None:
    config.validate()
    config.ensure_media_dir()

    app = bot_app.build_application()

    # Khởi tạo DB / dọn dẹp DB+HTTP client được thực hiện trong
    # bot_app._post_init / _post_shutdown, chạy bên trong Event Loop mà
    # run_polling() tự tạo và quản lý - KHÔNG dùng asyncio.run() riêng ở
    # đây (sẽ tạo 1 Event Loop khác, gây RuntimeError "Future attached to
    # a different loop" khi Pool DB được dùng lại trong loop của bot).
    logger.info("Bot đang khởi động (long polling, local)... Nhấn Ctrl+C để dừng.")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
