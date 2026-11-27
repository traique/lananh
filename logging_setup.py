"""Cấu hình logging dùng chung cho main.py và web.py.

Gồm 1 logging.Filter để che các secret (API key 9Router, webhook secret,
diagnose secret) nếu chúng vô tình lọt vào message log - 1 regex gộp mọi secret,
quét chuỗi 1 lần -, 1 filter gắn mã lượt (core/trace.py: "[telegram:123] ..." - gom
mọi dòng log của 1 câu trả lời), và handler gom lỗi theo chữ ký
(services/error_digest.py: báo chủ bot lỗi mới + tổng hợp hằng ngày; tắt bằng
ERROR_DIGEST_ENABLED=0).

Ghi log qua hàng đợi (LOG_QUEUE_ENABLED, mặc định bật): khi tiến trình chưa ai cấu
hình logging (uvicorn chạy web.py, main.py), handler ghi stderr của basicConfig
được dời ra sau 1 QueueListener (luồng riêng); root chỉ còn 1 QueueHandler - event
loop không bao giờ chờ ghi stderr (pipe supervisord nghẽn = cả bot đứng). Mã lượt
gắn và secret được che Ở LUỒNG GỬI, trên chuỗi đã format (gồm cả traceback), trước
khi vào hàng đợi: secret không bao giờ nằm trong hàng đợi. Hàng đợi có trần
(``_QUEUE_MAX`` dòng): đầy thì bỏ dòng và báo số dòng đã bỏ, không chặn, không phình
RAM. Luồng ghi dừng lúc thoát tiến trình (atexit, ghi nốt dòng còn lại).
"""

import atexit
import logging
import logging.handlers
import os
import queue
import re
from typing import Optional

from core import config, trace

REDACTED = "***REDACTED***"
# Trần số dòng log chờ ghi (~1-2 KB/dòng): stderr nghẽn thì bỏ dòng thay vì phình RAM.
_QUEUE_MAX = 1000
# Lúc tắt: chờ luồng ghi xả hàng đợi tối đa chừng này giây rồi thôi (không treo thoát).
_STOP_TIMEOUT_SEC = 5.0
_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(turn_tag)s%(message)s"


def _db_password() -> str:
    """Mật khẩu trong DATABASE_URL (postgresql://user:PASS@host/...) - phần hay
    lọt vào log nhất khi asyncpg/httpx in URL kết nối lỗi."""
    from urllib.parse import unquote, urlsplit

    try:
        parsed = urlsplit(config.DATABASE_URL)
    except ValueError:
        return ""
    return unquote(parsed.password or "")


def _collect_secrets() -> list[str]:
    """Mọi secret cấu hình tĩnh; secret phát sinh lúc chạy (session Zalo, token
    Facebook theo page) đăng ký thêm qua add_redacted_secret()."""
    import os

    values = [
        config.TELEGRAM_TOKEN,
        config.ROUTER9_API_KEY,
        config.WEBHOOK_SECRET,
        config.DIAGNOSE_SECRET,
        config.ZOOM_CLIENT_SECRET,
        config.ZOOM_SECRET_TOKEN,
        config.ZOOM_VERIFICATION_TOKEN,
        config.GOOGLE_AI_STUDIO_API_KEY_1,
        config.GOOGLE_AI_STUDIO_API_KEY_2,
        config.GROQ_API_KEY,
        config.OPENROUTER_API_KEY,
        config.TAVILY_API_KEY,
        config.AGNES_API_KEY,
        config.ADMIN_PASS,
        config.SETTINGS_ENC_KEY,
        config.SETTINGS_ENC_KEY_OLD,
        _db_password(),
        os.getenv("ZALO_BRIDGE_SECRET", "").strip(),
        os.getenv("GITHUB_RAG_TOKEN", "").strip(),
    ]
    # Mọi FACEBOOK_PAGE_ACCESS_TOKEN* (page mặc định, _2, _MARKET...).
    values.extend(
        v.strip() for k, v in os.environ.items() if k.startswith("FACEBOOK_PAGE_ACCESS_TOKEN")
    )
    # Secret quá ngắn (<8 ký tự) che sẽ phá log bình thường ("1", "true"...).
    return [v for v in values if v and len(v) >= 8]


def _compile(secrets: list[str]) -> "re.Pattern[str] | None":
    """1 regex gộp mọi secret (dài trước: secret này là tiền tố của secret kia thì
    vẫn che trọn chuỗi dài) - quét message 1 lần thay vì ~20 lần ``in``/replace."""
    if not secrets:
        return None
    ordered = sorted(set(secrets), key=len, reverse=True)
    return re.compile("|".join(re.escape(value) for value in ordered))


class _RedactFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._secrets = [s for s in _collect_secrets() if s]
        self._pattern = _compile(self._secrets)

    def add_secret(self, value: str) -> None:
        """Đăng ký thêm 1 secret phát sinh lúc runtime để các log về sau cũng
        được che, không chỉ các giá trị gốc lúc khởi động từ env."""
        if value and value not in self._secrets:
            self._secrets.append(value)
            # Gán 1 lần (nguyên tử): luồng khác đang che dùng regex cũ hoặc mới, không lỗi.
            self._pattern = _compile(self._secrets)

    def redact(self, text: str) -> str:
        pattern = self._pattern
        if pattern is None or not text:
            return text
        return pattern.sub(REDACTED, text)

    def filter(self, record: logging.LogRecord) -> bool:
        pattern = self._pattern
        if pattern is None:
            return True
        redacted, count = pattern.subn(REDACTED, record.getMessage())
        if count:
            record.msg = redacted
            record.args = ()
        return True


class _QueueHandler(logging.handlers.QueueHandler):
    """Root chỉ đẩy record vào hàng đợi (luồng QueueListener ghi stderr). ``prepare``
    format sẵn message + traceback rồi che secret TRÊN CHUỖI ĐÃ FORMAT - bản cũ chỉ
    che message, không che traceback. Hàng đợi đầy thì bỏ dòng (đếm, báo lại khi
    có chỗ); đã dừng luồng ghi (tắt app) thì ghi thẳng, không mất dòng cuối."""

    def __init__(self, log_queue: queue.Queue, targets: list[logging.Handler]) -> None:
        super().__init__(log_queue)
        self.targets = targets
        self.dropped = 0
        self.stopped = False

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        prepared = super().prepare(record)
        redactor = _active_filter
        if redactor is not None:
            prepared.msg = prepared.message = redactor.redact(prepared.msg)
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        if self.stopped:
            for target in self.targets:
                if record.levelno >= target.level:
                    target.handle(record)
            return
        try:
            if self.dropped:
                self.queue.put_nowait(self._dropped_notice())
                self.dropped = 0
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def _dropped_notice(self) -> logging.LogRecord:
        return logging.makeLogRecord(
            {
                "name": __name__,
                "levelno": logging.WARNING,
                "levelname": "WARNING",
                "msg": f"Hàng đợi log đầy (stderr ghi chậm): đã bỏ {self.dropped} dòng log.",
                "turn_id": "-",
                "turn_tag": "",
            }
        )


class _QueueListener(logging.handlers.QueueListener):
    """Dừng có hạn: hàng đợi đầy hay stderr nghẽn lúc tắt thì sau _STOP_TIMEOUT_SEC
    bỏ phần còn lại thay vì treo tiến trình (bản gốc put_nowait/join không hạn)."""

    def enqueue_sentinel(self) -> None:
        self.queue.put(self._sentinel, timeout=_STOP_TIMEOUT_SEC)

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        try:
            self.enqueue_sentinel()
        except queue.Full:
            return
        thread.join(_STOP_TIMEOUT_SEC)
        self._thread = None


class _HealthCheckFilter(logging.Filter):
    """Ẩn access-log của uvicorn cho GET / (Render health check gọi liên
    tục mỗi vài giây, không mang thông tin gì hữu ích, chỉ làm loãng log)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return '"GET / HTTP/' not in record.getMessage()


# Tham chiếu tới filter đang chạy, để module khác có thể đăng ký thêm secret
# mới phát sinh lúc runtime qua add_redacted_secret().
_active_filter: Optional[_RedactFilter] = None
_listener: Optional[_QueueListener] = None
_queue_handler: Optional[_QueueHandler] = None
_atexit_registered = False


def add_redacted_secret(value: str) -> None:
    if _active_filter is not None:
        _active_filter.add_secret(value)


def _queue_enabled() -> bool:
    return os.getenv("LOG_QUEUE_ENABLED", "1").strip() != "0"


def _start_queue(root: logging.Logger, turn_filter: logging.Filter) -> None:
    """Dời handler hiện có của root (stderr của basicConfig) ra sau QueueListener."""
    global _listener, _queue_handler, _atexit_registered
    stop_logging()
    targets = list(root.handlers)
    log_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
    handler = _QueueHandler(log_queue, targets)
    # Mã lượt là ContextVar: chỉ đọc được ở luồng gửi, không phải luồng ghi.
    handler.addFilter(turn_filter)
    for target in targets:
        root.removeHandler(target)
    root.addHandler(handler)
    listener = _QueueListener(log_queue, *targets, respect_handler_level=True)
    listener.start()
    _listener, _queue_handler = listener, handler
    if not _atexit_registered:
        atexit.register(stop_logging)
        _atexit_registered = True


def stop_logging() -> None:
    """Ghi nốt các dòng còn trong hàng đợi rồi dừng luồng ghi (atexit tự gọi; gọi
    thêm lúc tắt app cũng được). Dòng log sau đó ghi thẳng stderr như cũ. Không raise."""
    global _listener, _queue_handler
    listener, handler = _listener, _queue_handler
    _listener = _queue_handler = None
    if handler is not None:
        handler.stopped = True
    if listener is not None:
        try:
            listener.stop()
        except Exception:  # pragma: no cover - tắt app không được hỏng vì log
            pass


def configure_logging() -> None:
    global _active_filter
    root = logging.getLogger()
    # Chưa ai gắn handler vào root (uvicorn/python chạy web.py, main.py): logging do
    # module này quản, được dời ra sau hàng đợi. Root đã có handler (pytest, nơi gọi
    # tự cấu hình) thì giữ nguyên handler đó, chỉ gắn filter như trước.
    fresh = not root.handlers
    logging.basicConfig(format=_FORMAT, level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())
    _active_filter = _RedactFilter()
    # QUAN TRỌNG: gắn filter vào HANDLER của root logger, không phải vào root
    # logger. logger.addFilter() chỉ chạy khi log trực tiếp qua logger đó -
    # mọi module trong codebase này dùng logging.getLogger(__name__) (logger
    # con), record của chúng propagate thẳng tới handler mà KHÔNG đi qua
    # filter của root logger. Gắn nhầm ở logger khiến redact im lặng không
    # chạy cho bất kỳ log thực tế nào trước đây.
    # Mã lượt (core/trace.py) gắn cùng chỗ: format trên dùng %(turn_tag)s nên
    # filter này phải chạy trên MỌI handler gốc (luôn đặt giá trị mặc định).
    turn_filter = trace.TurnIdFilter()
    if fresh and _queue_enabled():
        # QueueHandler che secret trong prepare() (cả traceback), gắn mã lượt ở luồng gửi.
        _start_queue(root, turn_filter)
    else:
        for handler in root.handlers:
            if isinstance(handler, _QueueHandler):
                continue  # gọi lại lần 2: hàng đợi đã che secret + gắn mã lượt
            handler.addFilter(_active_filter)
            handler.addFilter(turn_filter)
    if os.getenv("ERROR_DIGEST_ENABLED", "1").strip() != "0":
        from services import error_digest

        digest = error_digest.handler()
        root = logging.getLogger()
        if digest not in root.handlers:
            # Handler chỉ lưu tên loại lỗi + vị trí code, nhưng vẫn gắn filter che
            # secret cho chắc nếu sau này có trường nào đọc message.
            digest.addFilter(_active_filter)
            root.addHandler(digest)
        # uvicorn CLI áp LOGGING_CONFIG (logger "uvicorn" propagate=False) TRƯỚC khi
        # import web.py, nên "Exception in ASGI application" (route HTTP lỗi không
        # bắt) từ uvicorn.error không bao giờ tới root. Chỉ gắn thêm khi chuỗi đó
        # không tới root - chạy uvicorn với log_config=None thì không đếm 2 lần.
        uv_error = logging.getLogger("uvicorn.error")
        if not _reaches_root(uv_error) and digest not in uv_error.handlers:
            uv_error.addHandler(digest)


def _reaches_root(logger: logging.Logger) -> bool:
    """Record của ``logger`` có lan tới handler của root không (mọi tầng propagate)."""
    current: Optional[logging.Logger] = logger
    while current is not None and current is not logging.getLogger():
        if not current.propagate:
            return False
        current = current.parent
    return True
