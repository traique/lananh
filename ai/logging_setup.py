"""Cấu hình logging dùng chung cho main.py và web.py.

Gồm 1 logging.Filter để che các secret (API key 9Router, webhook secret,
diagnose secret) nếu chúng vô tình lọt vào message log.
"""
import logging
from typing import Optional

from core import config


class _RedactFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._secrets = [
            s
            for s in (
                config.ROUTER9_API_KEY,
                config.WEBHOOK_SECRET,
                config.DIAGNOSE_SECRET,
                config.ZOOM_CLIENT_SECRET,
                config.ZOOM_SECRET_TOKEN,
                config.ZOOM_VERIFICATION_TOKEN,
            )
            if s
        ]

    def add_secret(self, value: str) -> None:
        """Đăng ký thêm 1 secret phát sinh lúc runtime để các log về sau cũng
        được che, không chỉ các giá trị gốc lúc khởi động từ env."""
        if value and value not in self._secrets:
            self._secrets.append(value)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for s in self._secrets:
            if s and s in redacted:
                redacted = redacted.replace(s, "***REDACTED***")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


# Tham chiếu tới filter đang chạy, để module khác có thể đăng ký thêm secret
# mới phát sinh lúc runtime qua add_redacted_secret().
_active_filter: Optional[_RedactFilter] = None


def add_redacted_secret(value: str) -> None:
    if _active_filter is not None:
        _active_filter.add_secret(value)


def configure_logging() -> None:
    global _active_filter
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _active_filter = _RedactFilter()
    # QUAN TRỌNG: gắn filter vào HANDLER của root logger, không phải vào root
    # logger. logger.addFilter() chỉ chạy khi log trực tiếp qua logger đó -
    # mọi module trong codebase này dùng logging.getLogger(__name__) (logger
    # con), record của chúng propagate thẳng tới handler mà KHÔNG đi qua
    # filter của root logger. Gắn nhầm ở logger khiến redact im lặng không
    # chạy cho bất kỳ log thực tế nào trước đây.
    for handler in logging.getLogger().handlers:
        handler.addFilter(_active_filter)
