"""logging_setup: che secret bằng 1 regex gộp, ghi log qua hàng đợi (QueueHandler ->
QueueListener) để event loop không chờ ghi stderr, secret được che TRƯỚC khi vào hàng
đợi (cả traceback), mã lượt gắn ở luồng gửi, hàng đợi có trần."""

import io
import logging
import queue
from contextlib import contextmanager

import pytest

import logging_setup
from core import config, trace

SECRET = "gsk-test-secret-0123456789"


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", SECRET)
    monkeypatch.setattr(config, "ADMIN_PASS", "AdminPass999")
    monkeypatch.setattr(logging_setup, "_active_filter", logging_setup._RedactFilter())
    return logging_setup._active_filter


@contextmanager
def _fresh_root(monkeypatch):
    """Root chưa có handler như lúc uvicorn nạp web.py. Làm TRONG thân test (pytest
    gắn handler bắt log của nó vào root sau khi fixture chạy) và trả lại đúng các
    handler đó sau test; dọn luồng ghi."""
    monkeypatch.setenv("ERROR_DIGEST_ENABLED", "0")
    monkeypatch.delenv("LOG_QUEUE_ENABLED", raising=False)
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers.clear()
    try:
        yield root
    finally:
        logging_setup.stop_logging()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _record(msg, args=(), exc_info=None, name="t"):
    return logging.LogRecord(name, logging.ERROR, __file__, 1, msg, args, exc_info)


def _exc_info():
    try:
        raise RuntimeError(f"kết nối lỗi key={SECRET}")
    except RuntimeError:
        import sys

        return sys.exc_info()


def test_regex_gop_che_moi_secret_ke_ca_secret_la_tien_to_cua_nhau(secrets):
    secrets.add_secret("short-secret")
    secrets.add_secret("short-secret-but-longer")
    record = _record("a=%s b=%s c=AdminPass999", (SECRET, "short-secret-but-longer"))
    assert secrets.filter(record) is True
    msg = record.getMessage()
    assert SECRET not in msg and "AdminPass999" not in msg and "but-longer" not in msg
    assert msg.count(logging_setup.REDACTED) == 3
    plain = _record("không có gì bí mật %s", ("cả",))
    secrets.filter(plain)
    assert plain.args == ("cả",)  # không đụng record không chứa secret
    assert secrets.redact("x" + SECRET) == "x" + logging_setup.REDACTED


def test_khong_co_secret_nao_thi_bo_qua(monkeypatch):
    monkeypatch.setattr(logging_setup, "_collect_secrets", lambda: [])
    filt = logging_setup._RedactFilter()
    record = _record("giữ nguyên %s", ("x",))
    assert filt.filter(record) is True and record.getMessage() == "giữ nguyên x"


def test_secret_khong_bao_gio_vao_hang_doi_chua_che(secrets):
    """Che trên chuỗi đã format ở luồng gửi: cả message lẫn traceback (bản cũ chỉ
    che message)."""
    log_queue: queue.Queue = queue.Queue()
    handler = logging_setup._QueueHandler(log_queue, [])
    handler.addFilter(trace.TurnIdFilter())
    with trace.turn("telegram:42"):
        handler.handle(_record("gọi Groq key=%s", (SECRET,), _exc_info()))
    queued = log_queue.get_nowait()
    assert SECRET not in queued.msg and "RuntimeError" in queued.msg
    assert queued.msg.count(logging_setup.REDACTED) == 2
    assert queued.exc_info is None and queued.args is None
    assert queued.turn_tag == "[telegram:42] "


def test_hang_doi_day_thi_bo_dong_va_bao_lai(secrets):
    log_queue: queue.Queue = queue.Queue(maxsize=2)
    handler = logging_setup._QueueHandler(log_queue, [])
    for i in range(4):
        handler.handle(_record(f"dòng {i}"))  # không chặn dù hàng đợi đầy
    assert handler.dropped == 2
    assert [log_queue.get_nowait().msg for _ in range(2)] == ["dòng 0", "dòng 1"]
    handler.handle(_record("dòng 4"))
    notice, line = log_queue.get_nowait(), log_queue.get_nowait()
    assert "bỏ 2 dòng log" in notice.getMessage() and notice.turn_tag == ""
    assert line.msg == "dòng 4" and handler.dropped == 0


def test_configure_logging_dua_stderr_ra_sau_hang_doi(monkeypatch):
    with _fresh_root(monkeypatch) as root:
        _check_queue_path(root)


def _check_queue_path(root):
    logging_setup.configure_logging()
    handlers = root.handlers
    assert len(handlers) == 1 and isinstance(handlers[0], logging_setup._QueueHandler)
    target = handlers[0].targets[0]
    assert isinstance(target, logging.StreamHandler) and target.filters == []
    out = io.StringIO()
    target.setStream(out)
    logging_setup.add_redacted_secret("runtime-zalo-session-abc")
    log = logging.getLogger("test.queue")
    with trace.turn("zalo:acc:m1"):
        log.warning("phiên %s", "runtime-zalo-session-abc")
    try:
        raise ValueError("lỗi ngoài lượt")
    except ValueError:
        log.exception("có lỗi")
    logging_setup.stop_logging()  # xả hàng đợi
    text = out.getvalue()
    assert "- WARNING - [zalo:acc:m1] phiên ***REDACTED***" in text
    assert "runtime-zalo-session-abc" not in text
    assert "- ERROR - có lỗi\nTraceback" in text and "ValueError: lỗi ngoài lượt" in text
    # Đã dừng luồng ghi (tắt app): dòng sau đó ghi thẳng, không mất.
    log.warning("sau khi dừng")
    assert text.count("\n") < out.getvalue().count("\n")
    assert "sau khi dừng" in out.getvalue()


def test_configure_logging_goi_lai_khong_tao_them_luong(monkeypatch):
    with _fresh_root(monkeypatch) as root:
        logging_setup.configure_logging()
        first = logging_setup._listener
        logging_setup.configure_logging()
        assert logging_setup._listener is first and len(root.handlers) == 1
        filters = root.handlers[0].filters
        assert len(filters) == 1 and isinstance(filters[0], trace.TurnIdFilter)


def test_tat_hang_doi_bang_env_thi_ghi_dong_bo_nhu_cu(monkeypatch):
    with _fresh_root(monkeypatch) as root:
        monkeypatch.setenv("LOG_QUEUE_ENABLED", "0")
        logging_setup.configure_logging()
        handlers = root.handlers
        assert len(handlers) == 1 and not isinstance(handlers[0], logging_setup._QueueHandler)
        assert any(isinstance(f, logging_setup._RedactFilter) for f in handlers[0].filters)
        assert logging_setup._listener is None
