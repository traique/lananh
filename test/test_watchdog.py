"""Watchdog luật cố định: RAM (working set, memory.events), dung lượng DB, inbox
ứ đọng, job hằng ngày quá hạn, nhịp tim + phân loại khởi động lại."""

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest

from services import watchdog

VN = watchdog.VN_TZ


@pytest.fixture
def store(monkeypatch):
    data: dict[str, str] = {}

    async def get_setting(key):
        return data.get(key)

    async def set_setting(key, value):
        data[key] = value

    monkeypatch.setattr(watchdog.db, "get_setting", get_setting)
    monkeypatch.setattr(watchdog.db, "set_setting", set_setting)
    return data


@pytest.fixture
def sent(monkeypatch):
    texts: list[tuple[str, str | None]] = []

    def send(text, *, key=None, cooldown=None):
        texts.append((text, key))
        return True

    monkeypatch.setattr(watchdog.alerts, "send", send)
    return texts


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(watchdog, "_high_cycles", 0)
    monkeypatch.setattr(watchdog, "_last_events", None)
    monkeypatch.setattr(watchdog, "_settled", set())
    monkeypatch.delenv("WATCHDOG_ENABLED", raising=False)
    monkeypatch.setenv("RENDER", "true")


def _memory(monkeypatch, percent, events=None):
    monkeypatch.setattr(
        watchdog.memory_stats,
        "working_set",
        lambda: {
            "working_set_mib": None if percent is None else percent * 5.12,
            "limit_mib": 512.0,
            "percent": percent,
        },
    )
    monkeypatch.setattr(watchdog.memory_stats, "memory_events", lambda: dict(events or {}))


# ─── RAM ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ram_cao_phai_2_chu_ky_lien_moi_bao(monkeypatch, sent):
    _memory(monkeypatch, 90)
    await watchdog.check_memory()
    assert sent == []
    await watchdog.check_memory()
    assert len(sent) == 1 and sent[0][1] == "watchdog:memory" and "90%" in sent[0][0]
    _memory(monkeypatch, 60)
    await watchdog.check_memory()  # hạ xuống: đếm lại từ đầu
    _memory(monkeypatch, 90)
    await watchdog.check_memory()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_oom_kill_tang_thi_bao_ngay(monkeypatch, sent):
    _memory(monkeypatch, 40, {"oom_kill": 1, "max": 5})
    await watchdog.check_memory()  # lần đầu chỉ lấy mốc
    _memory(monkeypatch, 40, {"oom_kill": 2, "max": 9})
    await watchdog.check_memory()
    assert [key for _, key in sent] == ["watchdog:oom_kill"]


@pytest.mark.asyncio
async def test_cham_tran_chi_bao_khi_ram_that_da_cao(monkeypatch, sent):
    """Page cache cũng làm memory.events max tăng khi RAM thật còn thấp."""
    _memory(monkeypatch, 40, {"max": 1})
    await watchdog.check_memory()
    _memory(monkeypatch, 40, {"max": 50})
    await watchdog.check_memory()
    assert sent == []
    _memory(monkeypatch, 80, {"max": 60})
    await watchdog.check_memory()
    assert [key for _, key in sent] == ["watchdog:memory_max"]


@pytest.mark.asyncio
async def test_khong_doc_duoc_cgroup_thi_im_lang(monkeypatch, sent):
    _memory(monkeypatch, None, {})
    for _ in range(3):
        await watchdog.check_memory()
    assert sent == []


# ─── DB ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dung_luong_db_bao_1_lan_moi_moc_moi_ngay(monkeypatch, store, sent):
    size = [300]

    async def database_bytes():
        return size[0] * watchdog.MIB

    monkeypatch.setattr(watchdog.db, "database_bytes", database_bytes)
    monkeypatch.setenv("DB_SIZE_LIMIT_MB", "500")
    day = datetime(2026, 10, 26, 10, 0, tzinfo=VN)
    await watchdog.check_db_size(day)
    assert sent == []
    size[0] = 410  # 82%
    await watchdog.check_db_size(day)
    await watchdog.check_db_size(day + timedelta(hours=1))
    assert len(sent) == 1 and "82%" in sent[0][0] and "TRUNG BÌNH" in sent[0][0]
    size[0] = 460  # 92%: mốc cao hơn trong cùng ngày vẫn báo
    await watchdog.check_db_size(day)
    assert len(sent) == 2
    await watchdog.check_db_size(day + timedelta(days=1))  # ngày mới báo lại
    assert len(sent) == 3


# ─── Inbox ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbox_u_dong_bao_kem_tuoi(monkeypatch, sent):
    seen = []

    async def backlog(stale_after):
        seen.append(stale_after)
        return 3, 25 * 60.0

    monkeypatch.setattr(watchdog.db, "webhook_backlog", backlog)
    await watchdog.check_inbox()
    assert seen == [timedelta(minutes=10)]
    assert sent[0][1] == "watchdog:inbox" and "3 tin" in sent[0][0] and "25 phút" in sent[0][0]


@pytest.mark.asyncio
async def test_inbox_sach_thi_im_lang(monkeypatch, sent):
    async def backlog(stale_after):
        return 0, None

    monkeypatch.setattr(watchdog.db, "webhook_backlog", backlog)
    await watchdog.check_inbox()
    assert sent == []


# ─── Job quá hạn ────────────────────────────────────────────────────────────


def _jobs(monkeypatch, *checks):
    monkeypatch.setattr(watchdog, "daily_job_checks", lambda now: list(checks))


def _check(name, deadline, key, done_value="2026-10-26", ever_key=None):
    return watchdog.JobCheck(name, name, deadline, key, done_value, ever_key)


@pytest.mark.asyncio
async def test_job_qua_han_bao_1_lan_moi_ngay(monkeypatch, store, sent):
    deadline = datetime(2026, 10, 26, 11, 0, tzinfo=VN)
    _jobs(monkeypatch, _check("digest", deadline, "digest:last_sent_date"))
    store["digest:last_sent_date"] = "2026-10-25"  # đã từng chạy, hôm nay chưa
    assert await watchdog.check_missed_jobs(deadline - timedelta(minutes=1)) == []
    assert await watchdog.check_missed_jobs(deadline + timedelta(minutes=5)) == ["digest"]
    assert "digest" in sent[0][0] and "11:00" in sent[0][0]
    watchdog._settled.clear()  # mô phỏng restart: dedup vẫn còn trong settings
    assert await watchdog.check_missed_jobs(deadline + timedelta(minutes=10)) == []
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_job_da_chay_hoac_chua_tung_chay_thi_khong_bao(monkeypatch, store, sent):
    deadline = datetime(2026, 10, 26, 11, 0, tzinfo=VN)
    _jobs(
        monkeypatch,
        _check("digest", deadline, "digest:last_sent_date"),
        _check("morning_news", deadline, "morning_news:last_sent_date"),
    )
    store["digest:last_sent_date"] = "2026-10-26"  # xong hôm nay
    # morning_news chưa từng gửi lần nào (chưa pair Zalo/Zoom): không báo giả.
    assert await watchdog.check_missed_jobs(deadline + timedelta(hours=1)) == []
    assert sent == []


@pytest.mark.asyncio
async def test_slot_page_chung_khoan_dung_key_slot_va_key_tung_chay(monkeypatch, store, sent):
    deadline = datetime(2026, 10, 26, 17, 20, tzinfo=VN)
    check = _check(
        "market:stock:1520",
        deadline,
        "market_page:stock:2026-10-26T1520",
        done_value=None,
        ever_key="market_page:last:stock",
    )
    _jobs(monkeypatch, check)
    assert await watchdog.check_missed_jobs(deadline + timedelta(minutes=1)) == []
    store["market_page:last:stock"] = "26/10 15:50 — lỗi RuntimeError"
    assert await watchdog.check_missed_jobs(deadline + timedelta(minutes=2)) == [
        "market:stock:1520"
    ]
    watchdog._settled.clear()
    store["market_page:stock:2026-10-26T1520"] = "1"
    assert await watchdog.check_missed_jobs(deadline + timedelta(minutes=3)) == []


@pytest.mark.asyncio
async def test_daily_job_checks_chi_xet_job_dang_chay_va_tinh_han_tu_cau_hinh(monkeypatch):
    import scheduler
    from services import market_page, morning_news, portfolio_alerts

    async def forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(forever())
    try:
        monkeypatch.setattr(scheduler, "_digest_task", task)
        monkeypatch.setattr(morning_news, "_task", None)  # job không chạy: bỏ qua
        monkeypatch.setattr(portfolio_alerts, "_task", task)
        monkeypatch.setattr(market_page, "_task", task)
        monkeypatch.setattr(watchdog.config, "ENABLE_DAILY_DIGEST", True)
        monkeypatch.setattr(watchdog.config, "ENABLE_PORTFOLIO_ALERTS", True)
        monkeypatch.setattr(watchdog.config, "DAILY_DIGEST_HOUR_VN", 8)
        monkeypatch.setattr(watchdog.config, "MARKET_NEWS_TIMES_VN", "08:30")
        monkeypatch.setattr(watchdog.config, "MARKET_STOCK_TIMES_VN", "15:20")
        monkeypatch.setenv("MARKET_CATCHUP_MIN", "120")
        monday = datetime(2026, 10, 26, 9, 0, tzinfo=VN)  # thứ 2, ngày giao dịch
        checks = {c.name: c for c in watchdog.daily_job_checks(monday)}
        assert set(checks) == {
            "digest",
            "portfolio_alerts",
            "market:news:0830",
            "market:stock:1520",
        }
        assert checks["digest"].deadline.strftime("%H:%M") == "11:00"
        assert checks["portfolio_alerts"].deadline.strftime("%H:%M") == "18:05"
        assert checks["market:stock:1520"].deadline.strftime("%H:%M") == "17:20"
        assert checks["market:stock:1520"].setting_key == "market_page:stock:2026-10-26T1520"
        sunday = datetime(2026, 10, 25, 9, 0, tzinfo=VN)
        names = {c.name for c in watchdog.daily_job_checks(sunday)}
        assert "portfolio_alerts" not in names and "market:stock:1520" not in names
        assert "market:news:0830" in names
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ─── Nhịp tim + khởi động lại ───────────────────────────────────────────────


def _classify(prev, **kw):
    params = dict(now=10_000.0, commit="abc", oom_kill=0, pid1_offset=1.0, gap_alert_min=20)
    params.update(kw)
    return watchdog.classify_restart(prev, **params)


def test_phan_loai_khoi_dong_lai():
    beat = {"ts": 10_000 - 120, "commit": "abc", "oom_kill": 0}
    assert _classify(None).kind == "first"
    assert _classify(beat).kind == "restart" and not _classify(beat).alert
    assert _classify(beat, commit="def").kind == "deploy"
    assert not _classify(beat, commit="def").alert
    sleep = _classify({**beat, "ts": 10_000 - 45 * 60})
    assert sleep.kind == "sleep" and sleep.alert and round(sleep.gap_min) == 45
    # Cùng container (supervisord khởi động trước process này rất lâu).
    crash = _classify(beat, pid1_offset=3600.0)
    assert crash.kind == "crash" and crash.alert
    oom = _classify(beat, pid1_offset=3600.0, oom_kill=1)
    assert oom.kind == "oom" and oom.alert
    # Chạy local (PID 1 không phải supervisord) thì không bao giờ là crash.
    assert _classify(beat, pid1_offset=None).kind == "restart"


@pytest.mark.asyncio
async def test_detect_restart_ghi_nhat_ky_co_tran_va_bao_khi_gian_doan(monkeypatch, store, sent):
    _memory(monkeypatch, 50, {"oom_kill": 0})
    monkeypatch.setattr(watchdog, "container_start_offset", lambda: 0.5)
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567890")
    now = time.time()
    store[watchdog.HEARTBEAT_KEY] = json.dumps(
        {"ts": now - 50 * 60, "commit": "abcdef123456", "oom_kill": 0, "ws_pct": 91.0}
    )
    store[watchdog.RESTARTS_KEY] = json.dumps(
        [{"ts": now - 40 * 86400, "kind": "restart"}]  # quá 30 ngày: bị bỏ
        + [{"ts": now - 3600, "kind": "deploy"}] * 60
    )
    info = await watchdog.detect_restart(now)
    assert info is not None and info.kind == "sleep"
    log = json.loads(store[watchdog.RESTARTS_KEY])
    assert len(log) == watchdog.MAX_RESTART_LOG and log[-1]["kind"] == "sleep"
    assert "50 phút" in sent[0][0] and "91%" in sent[0][0]
    beat = json.loads(store[watchdog.HEARTBEAT_KEY])
    assert beat["inst"] == watchdog._INSTANCE and beat["commit"] == "abcdef123456"


@pytest.mark.asyncio
async def test_restart_don_dap_thi_bao(monkeypatch, store, sent):
    _memory(monkeypatch, 50, {})
    monkeypatch.setattr(watchdog, "container_start_offset", lambda: 0.5)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    now = time.time()
    store[watchdog.HEARTBEAT_KEY] = json.dumps({"ts": now - 60})
    store[watchdog.RESTARTS_KEY] = json.dumps(
        [{"ts": now - 7200, "kind": "restart"}, {"ts": now - 3600, "kind": "restart"}]
    )
    info = await watchdog.detect_restart(now)
    assert info is not None and info.kind == "restart"
    assert sent and "3 lần trong 24 giờ" in sent[0][0]


@pytest.mark.asyncio
async def test_khong_chay_ngoai_render_hoac_khi_tat(monkeypatch, store, sent):
    monkeypatch.delenv("RENDER", raising=False)
    assert await watchdog.detect_restart() is None
    await watchdog.write_heartbeat()
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("WATCHDOG_ENABLED", "0")
    assert await watchdog.detect_restart() is None
    assert store == {} and sent == []


@pytest.mark.asyncio
async def test_nhip_tim_luc_tat_khong_ghi_de_instance_moi(monkeypatch, store):
    _memory(monkeypatch, 50, {})
    store[watchdog.HEARTBEAT_KEY] = json.dumps({"ts": 1, "inst": "instance-moi"})
    await watchdog.write_heartbeat(clean=True)
    assert json.loads(store[watchdog.HEARTBEAT_KEY])["inst"] == "instance-moi"
    store[watchdog.HEARTBEAT_KEY] = json.dumps({"ts": 1, "inst": watchdog._INSTANCE})
    await watchdog.write_heartbeat(clean=True)
    assert json.loads(store[watchdog.HEARTBEAT_KEY])["clean"] is True


def test_container_start_offset_doc_proc(monkeypatch, tmp_path):
    for pid, cmd, start in (("1", b"/usr/bin/supervisord\x00-c", 1000), ("self", b"uvicorn", 7000)):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "cmdline").write_bytes(cmd)
        fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 5
        (tmp_path / pid / "stat").write_text(f"{pid} (tên (có) ngoặc) " + " ".join(fields))
    monkeypatch.setattr(watchdog, "PROC_ROOT", tmp_path)
    monkeypatch.setattr(watchdog.os, "sysconf", lambda name: 100)
    assert watchdog.container_start_offset() == 60.0
    (tmp_path / "1" / "cmdline").write_bytes(b"/sbin/init")
    assert watchdog.container_start_offset() is None


@pytest.mark.asyncio
async def test_run_checks_mot_luat_loi_khong_chan_luat_khac(monkeypatch, caplog):
    ran = []

    async def broken():
        raise RuntimeError("db down")

    async def ok(*args):
        ran.append(args)

    monkeypatch.setattr(watchdog, "check_memory", broken)
    monkeypatch.setattr(watchdog, "check_db_size", ok)
    monkeypatch.setattr(watchdog, "check_inbox", ok)
    monkeypatch.setattr(watchdog, "check_missed_jobs", ok)
    monkeypatch.setattr(watchdog, "write_heartbeat", ok)
    await watchdog.run_checks(datetime(2026, 10, 26, 9, 0, tzinfo=VN))
    assert len(ran) == 4
    assert any("RAM" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)
    monkeypatch.setenv("WATCHDOG_ENABLED", "0")
    ran.clear()
    await watchdog.run_checks()
    assert ran == []
