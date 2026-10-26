"""Gom lỗi theo chữ ký: chỉ lưu loại + vị trí code (không message), báo ngay lỗi
ERROR mới (giới hạn/giờ, chuyển về event loop an toàn từ thread khác), WARNING có
exc_info chỉ vào tổng hợp, bỏ qua đường gửi cảnh báo, trần 200 chữ ký/ngày, lưu
và gộp qua restart, gửi tổng hợp hôm qua 1 lần/ngày."""

import asyncio
import json
import logging
import threading
from datetime import datetime

import pytest

from services import error_digest as ed

VN = ed.VN_TZ
SECRET = "mat-khau-trong-message-khong-duoc-luu"


@pytest.fixture
def digest(monkeypatch):
    """Store + logger riêng cho từng test (không đụng handler toàn cục)."""
    store = ed.DigestStore()
    handler = ed.ErrorDigestHandler(store)
    log = logging.getLogger("services.fake_job")
    log.addHandler(handler)
    # Không lan lên root: handler toàn cục (gắn khi test khác import web) không đếm trùng.
    monkeypatch.setattr(log, "propagate", False)
    monkeypatch.setattr(ed, "_loop", None)
    monkeypatch.setattr(ed, "_alert_times", ed.deque(maxlen=64))
    monkeypatch.setattr(ed, "store", store)
    monkeypatch.delenv("ERROR_ALERT_IMMEDIATE", raising=False)
    monkeypatch.delenv("ERROR_ALERT_MAX_PER_HOUR", raising=False)
    yield store, log
    log.removeHandler(handler)


@pytest.fixture
def sent(monkeypatch):
    texts = []

    def send(text, *, key=None, cooldown=None):
        texts.append((text, key))
        return True

    monkeypatch.setattr(ed.alerts, "send", send)
    return texts


def _boom(log, exc=KeyError, level="exception"):
    try:
        raise exc(SECRET)
    except Exception:
        getattr(log, level)("Job lỗi khi xử lý %s", SECRET, exc_info=True)


def _today():
    return datetime.now(VN).date().isoformat()


def test_chu_ky_gom_theo_loai_va_vi_tri_khong_luu_message(digest):
    store, log = digest
    for _ in range(3):
        _boom(log)
    _boom(log, ValueError)
    entries = store.entries(_today())
    assert [(e.exc_type, e.count) for e in entries] == [("KeyError", 3), ("ValueError", 1)]
    entry = entries[0]
    assert entry.where == "test/test_error_digest.py:_boom" and entry.level == "ERROR"
    assert entry.logger == "services.fake_job"
    assert SECRET not in store.to_json()


def test_warning_khong_exc_info_bi_bo_qua_warning_co_exc_info_vao_tong_hop(digest):
    store, log = digest
    log.warning("chỉ là cảnh báo")
    log.info("thông tin")
    _boom(log, level="warning")
    entries = store.entries(_today())
    assert len(entries) == 1 and entries[0].level == "WARNING"


def test_bo_qua_logger_cua_duong_gui_canh_bao():
    store = ed.DigestStore()
    handler = ed.ErrorDigestHandler(store)
    for name in ("core.alerts", "services.owner_alerts", "httpx", "telegram.ext.Application"):
        record = logging.LogRecord(name, logging.ERROR, __file__, 1, "x", None, None)
        handler.emit(record)
    record = logging.LogRecord("telegramx", logging.ERROR, __file__, 1, "x", None, None)
    handler.emit(record)  # chỉ khớp đúng tiền tố logger, không khớp chuỗi con
    assert [e.logger for e in store.entries(_today())] == ["telegramx"]


def test_tran_chu_ky_moi_ngay_bo_cai_lau_khong_gap_nhat():
    store = ed.DigestStore(max_signatures=3)
    for i in range(5):
        store.add("2026-10-26", ed.Entry(f"l{i}", "E", "a.py:f", 1, "ERROR", 1, i, i))
    assert sorted(e.logger for e in store.entries("2026-10-26")) == ["l2", "l3", "l4"]
    store.add("2026-10-24", ed.Entry("x", "E", "a.py:f", 1, "ERROR", 1, 0, 0))
    store.add("2026-10-25", ed.Entry("y", "E", "a.py:f", 1, "ERROR", 1, 0, 0))
    assert store.entries("2026-10-24") == []  # chỉ giữ 2 ngày gần nhất


@pytest.mark.asyncio
async def test_loi_moi_bao_ngay_qua_event_loop_ke_ca_tu_thread_khac(digest, sent):
    store, log = digest
    ed.bind_loop()
    await asyncio.to_thread(_boom, log)  # emit() chạy trên thread của to_thread
    _boom(log)  # cùng chữ ký trong ngày: không báo lại
    await asyncio.sleep(0.01)
    assert len(sent) == 1
    text, key = sent[0]
    assert "KeyError" in text and "test/test_error_digest.py:_boom" in text
    assert SECRET not in text and key.startswith("err:")
    assert store.entries(_today())[0].count == 2


@pytest.mark.asyncio
async def test_khong_bao_ngay_khi_da_co_canh_bao_rieng_hoac_warning(digest, sent):
    _, log = digest
    ed.bind_loop()
    log.error("toàn bộ provider down", extra={"alerted": True})
    _boom(log, level="warning")
    await asyncio.sleep(0.01)
    assert sent == []


@pytest.mark.asyncio
async def test_gioi_han_so_canh_bao_moi_gio_va_tat_bang_env(digest, sent, monkeypatch):
    _, log = digest
    ed.bind_loop()
    monkeypatch.setenv("ERROR_ALERT_MAX_PER_HOUR", "2")
    for exc in (KeyError, ValueError, TypeError, IndexError):
        _boom(log, exc)
    await asyncio.sleep(0.01)
    assert len(sent) == 2
    monkeypatch.setenv("ERROR_ALERT_MAX_PER_HOUR", "10")
    monkeypatch.setenv("ERROR_ALERT_IMMEDIATE", "0")
    _boom(log, ZeroDivisionError)
    await asyncio.sleep(0.01)
    assert len(sent) == 2


def test_emit_khong_bao_gio_raise(digest, monkeypatch):
    store, log = digest

    def broken(day, entry):
        raise RuntimeError("store hỏng")

    monkeypatch.setattr(store, "add", broken)
    _boom(log)  # không raise ra code gọi logger


@pytest.mark.asyncio
async def test_luu_va_gop_qua_restart(digest, monkeypatch):
    store, log = digest
    data = {}

    async def get_setting(key):
        return data.get(key)

    async def set_setting(key, value):
        data[key] = value

    monkeypatch.setattr(ed.db, "get_setting", get_setting)
    monkeypatch.setattr(ed.db, "set_setting", set_setting)
    monkeypatch.setattr(ed, "_loaded", False)
    _boom(log)
    _boom(log)
    await ed.persist()
    assert not store.dirty and SECRET not in data[ed.SETTINGS_KEY]
    # "Restart": store mới, lỗi mới trước khi load, rồi gộp số liệu cũ.
    fresh = ed.DigestStore()
    monkeypatch.setattr(ed, "store", fresh)
    monkeypatch.setattr(ed, "_loaded", False)
    fresh.add(_today(), _entry())
    await ed.load()
    counts = {e.exc_type: e.count for e in fresh.entries(_today())}
    assert counts["KeyError"] == 2 and counts["RuntimeError"] == 1
    # Ngày quá cũ trong dữ liệu đã lưu bị bỏ.
    payload = json.loads(data[ed.SETTINGS_KEY])
    payload["days"]["2020-01-01"] = payload["days"][_today()]
    fresh2 = ed.DigestStore()
    fresh2.merge_json(json.dumps(payload), today=_today())
    assert fresh2.entries("2020-01-01") == []


def _entry():
    now = datetime.now(VN).timestamp()
    return ed.Entry("services.fake_job", "RuntimeError", "x.py:f", 1, "ERROR", 1, now, now)


@pytest.mark.asyncio
async def test_tong_hop_hom_qua_gui_1_lan_moi_ngay_tu_7h(monkeypatch, sent):
    data = {}

    async def get_setting(key):
        return data.get(key)

    async def set_setting(key, value):
        data[key] = value

    store = ed.DigestStore()
    monkeypatch.setattr(ed, "store", store)
    monkeypatch.setattr(ed.db, "get_setting", get_setting)
    monkeypatch.setattr(ed.db, "set_setting", set_setting)
    monkeypatch.delenv("ERROR_DIGEST_HOUR_VN", raising=False)
    ts = datetime(2026, 10, 25, 14, 0, tzinfo=VN).timestamp()
    store.add("2026-10-25", ed.Entry("a", "KeyError", "x.py:f", 3, "ERROR", 4, ts, ts))
    store.add("2026-10-25", ed.Entry("b", "OSError", "y.py:g", 9, "WARNING", 2, ts, ts))
    early = datetime(2026, 10, 26, 6, 59, tzinfo=VN)
    assert await ed.send_daily_digest_if_due(early) is None
    text = await ed.send_daily_digest_if_due(early.replace(hour=7, minute=5))
    assert text is not None and "25/10" in text and "6 lần (4 lỗi, 2 cảnh báo)" in text
    assert "4× KeyError ở x.py:f:3" in text
    assert await ed.send_daily_digest_if_due(early.replace(hour=9)) is None  # đã gửi
    assert len(sent) == 1
    monkeypatch.setenv("ERROR_DIGEST_HOUR_VN", "-1")
    data.clear()
    assert await ed.send_daily_digest_if_due(early.replace(hour=9)) is None


@pytest.mark.asyncio
async def test_build_report_cho_lenh_loi(monkeypatch):
    store = ed.DigestStore()
    monkeypatch.setattr(ed, "store", store)

    async def restart_summary():
        return {"counts": {"deploy": 2, "oom": 1}}

    from services import watchdog

    monkeypatch.setattr(watchdog, "restart_summary", restart_summary)
    now = datetime(2026, 10, 26, 10, 0, tzinfo=VN)
    text = await ed.build_report(now)
    assert "Chưa ghi nhận lỗi nào" in text and "deploy 2" in text and "hết RAM (OOM) 1" in text
    store.add("2026-10-26", ed.Entry("a", "KeyError", "x.py:f", 3, "ERROR", 1, 0, 0))
    text = await ed.build_report(now)
    assert "1× KeyError ở x.py:f:3" in text


def test_handler_toan_cuc_chi_gan_1_lan(monkeypatch):
    import logging_setup

    root = logging.getLogger()
    before = list(root.handlers)
    try:
        monkeypatch.delenv("ERROR_DIGEST_ENABLED", raising=False)
        logging_setup.configure_logging()
        logging_setup.configure_logging()
        assert sum(isinstance(h, ed.ErrorDigestHandler) for h in root.handlers) == 1
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_thread_safe_dem_dong_thoi():
    store = ed.DigestStore()

    def worker():
        for _ in range(200):
            store.add("2026-10-26", ed.Entry("a", "E", "x.py:f", 1, "ERROR", 1, 0, 0))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.entries("2026-10-26")[0].count == 800


def _raise_record(name: str) -> logging.LogRecord:
    try:
        raise KeyError("x")
    except KeyError:
        import sys

        return logging.getLogger(name).makeRecord(
            name, logging.ERROR, __file__, 1, "Exception in ASGI application", (), sys.exc_info()
        )


@pytest.mark.parametrize("uvicorn_propagate", [False, True])
def test_loi_uvicorn_error_duoc_dem_dung_1_lan(monkeypatch, uvicorn_propagate):
    """uvicorn CLI áp LOGGING_CONFIG (logger "uvicorn" propagate=False) trước khi import
    web.py: "Exception in ASGI application" từ uvicorn.error không tới root - handler
    phải gắn thêm vào uvicorn.error; còn khi chuỗi đã tới root thì không đếm 2 lần."""
    import logging_setup

    store = ed.DigestStore()
    monkeypatch.setattr(ed, "store", store)
    monkeypatch.setattr(ed, "_handler", None)
    monkeypatch.setattr(ed, "_loop", None)
    monkeypatch.delenv("ERROR_DIGEST_ENABLED", raising=False)
    root = logging.getLogger()
    uv = logging.getLogger("uvicorn")
    uv_error = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(uv, "handlers", [logging.NullHandler()])
    monkeypatch.setattr(uv, "propagate", uvicorn_propagate)
    monkeypatch.setattr(uv_error, "handlers", [])
    monkeypatch.setattr(uv_error, "disabled", False)
    logging_setup.configure_logging()
    logging_setup.configure_logging()
    uv_error.handle(_raise_record("uvicorn.error"))
    entries = store.entries(_today_vn())
    assert [(e.logger, e.exc_type, e.count) for e in entries] == [("uvicorn.error", "KeyError", 1)]
    assert entries[0].where.startswith("test/test_error_digest.py:")


def _today_vn() -> str:
    return datetime.now(VN).date().isoformat()
