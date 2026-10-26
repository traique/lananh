"""Callback cảnh báo chủ bot: Telegram trước, Zoom dự phòng khi Telegram lỗi và
nhân bản cảnh báo 🚨 theo ALERT_ZOOM_MIRROR."""

import pytest

from services import owner_alerts


@pytest.fixture
def zoom_sent(monkeypatch):
    sent = []

    async def pairing():
        return ("owner@xmpp.zoom.us", "Chủ bot")

    async def send_message(to_jid, text, user_jid=None, account_id=None):
        sent.append((to_jid, text))

    monkeypatch.setattr(owner_alerts.config, "ZOOM_ENABLED", True)
    monkeypatch.setattr(owner_alerts.db, "zoom_get_pairing", pairing)
    monkeypatch.setattr(owner_alerts.zoom, "send_message", send_message)
    return sent


def _telegram(ok=True):
    sent = []

    async def send(text):
        if not ok:
            raise RuntimeError("Forbidden: bot was blocked")
        sent.append(text)

    return send, sent


@pytest.mark.asyncio
async def test_mac_dinh_chi_nhan_ban_canh_bao_nghiem_trong(monkeypatch, zoom_sent):
    monkeypatch.delenv("ALERT_ZOOM_MIRROR", raising=False)
    send, telegram = _telegram()
    alert = owner_alerts.make_alert_callback(send)
    await alert("⚠️ 9Router vừa lỗi")
    await alert("🚨 TOÀN BỘ provider đều không dùng được")
    assert telegram == ["⚠️ 9Router vừa lỗi", "🚨 TOÀN BỘ provider đều không dùng được"]
    assert zoom_sent == [("owner@xmpp.zoom.us", "🚨 TOÀN BỘ provider đều không dùng được")]


@pytest.mark.asyncio
async def test_telegram_loi_thi_du_phong_qua_zoom(monkeypatch, zoom_sent):
    monkeypatch.setenv("ALERT_ZOOM_MIRROR", "fallback")
    send, _ = _telegram(ok=False)
    alert = owner_alerts.make_alert_callback(send)
    await alert("⚠️ webhook DEAD")
    assert zoom_sent == [("owner@xmpp.zoom.us", "⚠️ webhook DEAD")]


@pytest.mark.asyncio
async def test_che_do_all_va_off(monkeypatch, zoom_sent):
    send, _ = _telegram()
    alert = owner_alerts.make_alert_callback(send)
    monkeypatch.setenv("ALERT_ZOOM_MIRROR", "all")
    await alert("ℹ️ thường")
    monkeypatch.setenv("ALERT_ZOOM_MIRROR", "off")
    await alert("🚨 nghiêm trọng")
    failing, _ = _telegram(ok=False)
    await owner_alerts.make_alert_callback(failing)("⚠️ telegram sập")
    assert zoom_sent == [("owner@xmpp.zoom.us", "ℹ️ thường")]


@pytest.mark.asyncio
async def test_khong_bao_gio_raise(monkeypatch, caplog):
    async def broken_zoom(text):
        raise RuntimeError("zoom down")

    monkeypatch.setenv("ALERT_ZOOM_MIRROR", "critical")
    monkeypatch.setattr(owner_alerts, "_send_zoom", broken_zoom)
    send, _ = _telegram(ok=False)
    await owner_alerts.make_alert_callback(send)("🚨 x")  # không raise
    assert any(r.name == "services.owner_alerts" for r in caplog.records)


@pytest.mark.asyncio
async def test_zoom_tat_hoac_chua_pair_thi_bo_qua(monkeypatch):
    monkeypatch.setattr(owner_alerts.config, "ZOOM_ENABLED", False)
    assert await owner_alerts._send_zoom("x") is False

    async def no_pairing():
        return None

    monkeypatch.setattr(owner_alerts.config, "ZOOM_ENABLED", True)
    monkeypatch.setattr(owner_alerts.db, "zoom_get_pairing", no_pairing)
    assert await owner_alerts._send_zoom("x") is False


def test_gia_tri_env_la_ve_mac_dinh(monkeypatch):
    monkeypatch.setenv("ALERT_ZOOM_MIRROR", "bừa")
    assert owner_alerts.mirror_mode() == "critical"
