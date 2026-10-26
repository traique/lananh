"""monitor_service: báo 1 lần khi TOÀN BỘ provider down và 1 lần khi hồi phục;
vòng lặp chạy thêm watchdog; khởi động phân loại restart; tắt ghi nhịp tim."""

import asyncio

import pytest

from services import monitor_service


@pytest.fixture
def state(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor_service, "send_alert", sent.append)
    monkeypatch.setattr(monitor_service, "_all_down_since", None)
    monkeypatch.setattr(monitor_service.config, "PROVIDER_ORDER", ["router9", "groq"])
    ps = monitor_service.provider_state

    async def ensure_loaded():
        return None

    monkeypatch.setattr(ps, "ensure_loaded", ensure_loaded)
    monkeypatch.setattr(ps, "router9_enabled", True)
    monkeypatch.setattr(ps, "router9_dead_since", None)
    monkeypatch.setattr(ps, "api_exhausted_until", {})
    return sent, ps


@pytest.mark.asyncio
async def test_bao_khi_toan_bo_down_va_khi_hoi_phuc_moi_lan_1_tin(state, monkeypatch):
    sent, ps = state
    await monitor_service._check_all_providers_down()
    assert sent == []
    monkeypatch.setattr(ps, "router9_dead_since", 1.0)
    monkeypatch.setattr(ps, "api_exhausted_until", {"groq": 9e18})
    await monitor_service._check_all_providers_down()
    await monitor_service._check_all_providers_down()  # vẫn down: không spam
    assert sent == [monitor_service.messages.ALL_PROVIDERS_DOWN_ALERT]
    monkeypatch.setattr(ps, "api_exhausted_until", {})
    await monitor_service._check_all_providers_down()
    await monitor_service._check_all_providers_down()
    assert sent[1:] == [monitor_service.messages.ALL_PROVIDERS_RECOVERED_ALERT]


@pytest.mark.asyncio
async def test_router9_tat_tay_cung_tinh_la_down(state, monkeypatch):
    sent, ps = state
    monkeypatch.setattr(ps, "router9_enabled", False)
    monkeypatch.setattr(ps, "api_exhausted_until", {"groq": 9e18})
    await monitor_service._check_all_providers_down()
    assert sent == [monitor_service.messages.ALL_PROVIDERS_DOWN_ALERT]


@pytest.mark.asyncio
async def test_log_down_duoc_danh_dau_da_bao(state, monkeypatch, caplog):
    _, ps = state
    monkeypatch.setattr(ps, "router9_dead_since", 1.0)
    monkeypatch.setattr(ps, "api_exhausted_until", {"groq": 9e18})
    await monitor_service._check_all_providers_down()
    record = next(r for r in caplog.records if r.levelname == "ERROR")
    assert getattr(record, "alerted", False) is True  # error digest không báo trùng


@pytest.mark.asyncio
async def test_start_chay_kiem_tra_khoi_dong_va_vong_watchdog(monkeypatch):
    calls = []

    async def detect_restart():
        calls.append("detect")

    async def run_checks():
        calls.append("checks")

    async def providers():
        calls.append("providers")

    async def heartbeat(*, clean=False):
        calls.append(f"heartbeat:{clean}")

    async def digest_step(*args):
        calls.append("digest")

    monkeypatch.setattr(monitor_service.error_digest, "_loop", None)  # start() bind loop test
    monkeypatch.setattr(monitor_service.error_digest, "load", digest_step)
    monkeypatch.setattr(monitor_service.error_digest, "persist", digest_step)
    monkeypatch.setattr(monitor_service.error_digest, "send_daily_digest_if_due", digest_step)
    monkeypatch.setattr(monitor_service.watchdog, "detect_restart", detect_restart)
    monkeypatch.setattr(monitor_service.watchdog, "run_checks", run_checks)
    monkeypatch.setattr(monitor_service.watchdog, "write_heartbeat", heartbeat)
    monkeypatch.setattr(monitor_service, "_check_all_providers_down", providers)
    monkeypatch.setattr(monitor_service.config, "MONITOR_INTERVAL_SEC", 0)
    monkeypatch.setattr(monitor_service, "_monitor_task", None)
    monkeypatch.setattr(monitor_service, "_startup_task", None)
    monitor_service.start()
    for _ in range(20):
        await asyncio.sleep(0)
    await monitor_service.stop()
    assert "detect" in calls and "providers" in calls and "checks" in calls
    assert calls.count("digest") >= 3  # load lúc khởi động + persist/tổng hợp mỗi vòng
    assert calls[-2:] == ["heartbeat:True", "digest"]  # tắt: nhịp tim rồi lưu bộ đếm lỗi
    assert monitor_service._monitor_task is None and monitor_service._startup_task is None
