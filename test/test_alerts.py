"""core.alerts: fire-and-forget có trần MAX_PENDING, chống trùng theo key +
cooldown (bảng key có trần MAX_KEYS)."""

import asyncio

import pytest

from core import alerts


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(alerts, "_last_sent", alerts.OrderedDict())
    monkeypatch.setattr(alerts, "tasks", set())
    yield
    alerts.set_callback(None)


@pytest.mark.asyncio
async def test_khong_co_callback_thi_bo_qua():
    alerts.set_callback(None)
    assert alerts.send("x") is False


@pytest.mark.asyncio
async def test_gui_nen_va_drain_task():
    sent = []

    async def callback(text):
        sent.append(text)

    alerts.set_callback(callback)
    assert alerts.send("a") is True
    await asyncio.gather(*alerts.tasks)
    assert sent == ["a"] and not alerts.tasks


@pytest.mark.asyncio
async def test_tran_max_pending(monkeypatch):
    gate = asyncio.Event()

    async def slow(text):
        await gate.wait()

    monkeypatch.setattr(alerts, "MAX_PENDING", 3)
    alerts.set_callback(slow)
    results = [alerts.send(str(i)) for i in range(5)]
    assert results == [True, True, True, False, False]
    gate.set()
    await asyncio.gather(*alerts.tasks)


@pytest.mark.asyncio
async def test_callback_loi_khong_lan_ra_ngoai(caplog):
    async def broken(text):
        raise RuntimeError("telegram down")

    alerts.set_callback(broken)
    assert alerts.send("x") is True
    await asyncio.gather(*alerts.tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert any("telegram down" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cung_key_chi_gui_1_lan_trong_cooldown(monkeypatch):
    sent = []

    async def callback(text):
        sent.append(text)

    clock = [1000.0]
    monkeypatch.setattr(alerts.time, "monotonic", lambda: clock[0])
    alerts.set_callback(callback)
    assert alerts.send("ram cao", key="mem", cooldown=600) is True
    assert alerts.send("ram cao lần 2", key="mem", cooldown=600) is False
    assert alerts.send("khác key", key="db", cooldown=600) is True
    assert alerts.send("không key") is True and alerts.send("không key") is True
    clock[0] += 601
    assert alerts.send("ram cao lần 3", key="mem", cooldown=600) is True
    await asyncio.gather(*alerts.tasks)
    assert sent == ["ram cao", "khác key", "không key", "không key", "ram cao lần 3"]


@pytest.mark.asyncio
async def test_bang_key_co_tran(monkeypatch):
    async def callback(text):
        return None

    monkeypatch.setattr(alerts, "MAX_KEYS", 4)
    alerts.set_callback(callback)
    for i in range(10):
        assert alerts.send("x", key=f"k{i}")
    assert list(alerts._last_sent) == ["k6", "k7", "k8", "k9"]
    await asyncio.gather(*alerts.tasks)


@pytest.mark.asyncio
async def test_bi_tu_choi_vi_qua_tai_thi_khong_ghi_nho_key(monkeypatch):
    gate = asyncio.Event()

    async def slow(text):
        await gate.wait()

    monkeypatch.setattr(alerts, "MAX_PENDING", 1)
    alerts.set_callback(slow)
    assert alerts.send("a")
    assert alerts.send("b", key="k") is False  # quá tải: chưa gửi
    assert not alerts.in_cooldown("k")  # lần sau còn được thử
    gate.set()
    await asyncio.gather(*alerts.tasks)
