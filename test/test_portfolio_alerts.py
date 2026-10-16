from datetime import datetime

import pytest

from services import portfolio_alerts as pa
from stock import portfolio, providers


def _holding(**kw):
    base = dict(user_id=1, symbol="FPT", quantity=100, average_price=100_000.0)
    base.update(kw)
    return portfolio.Holding(**base)


def _series(closes, highs=None, lows=None):
    return providers.OhlcvSeries(
        symbol="FPT",
        closes=closes,
        highs=highs or [c * 1.01 for c in closes],
        lows=lows or [c * 0.99 for c in closes],
        volumes=[1.0] * len(closes),
        dates=[f"2026-01-{i + 1:02d}" for i in range(len(closes))],
    )


def test_is_due_chi_sau_gio_dong_cua_ngay_giao_dich():
    tz = pa._VN_TZ
    assert pa.is_due(datetime(2026, 3, 12, 14, 59, tzinfo=tz), None) is False
    assert pa.is_due(datetime(2026, 3, 12, 15, 5, tzinfo=tz), None) is True
    assert pa.is_due(datetime(2026, 3, 12, 17, 30, tzinfo=tz), None) is True
    assert pa.is_due(datetime(2026, 3, 12, 18, 6, tzinfo=tz), None) is False
    assert pa.is_due(datetime(2026, 3, 12, 15, 30, tzinfo=tz), "2026-03-12") is False
    assert pa.is_due(datetime(2026, 3, 14, 15, 30, tzinfo=tz), None) is False  # thứ Bảy
    assert pa.is_due(datetime(2026, 4, 30, 15, 30, tzinfo=tz), None) is False  # lễ


def test_detect_events_stop_target_breakout_va_lo_sau():
    quote = providers.Quote("FPT", 90_000.0, 95_000.0, -5_000.0, -5.3, "2026-03-12")
    events = pa.detect_events(_holding(stop_price=92_000.0), quote, None)
    assert any("stop" in e for e in events)
    assert any("lỗ -10" in e for e in events)

    quote_up = providers.Quote("FPT", 130_000.0, 120_000.0, 10_000.0, 8.3, "2026-03-12")
    closes = [100_000.0] * 25 + [130_000.0]
    events = pa.detect_events(_holding(target_price=125_000.0), quote_up, _series(closes))
    assert any("target" in e for e in events)
    assert any("breakout" in e for e in events)

    quiet = providers.Quote("FPT", 101_000.0, 100_000.0, 1_000.0, 1.0, "2026-03-12")
    assert pa.detect_events(_holding(), quiet, _series([100_000.0] * 30)) == []


@pytest.mark.asyncio
async def test_run_once_guard_ngay_va_chi_gui_khi_co_su_kien(monkeypatch):
    saved = {}

    async def get_setting(key):
        return saved.get(key)

    async def set_setting(key, value):
        saved[key] = value

    sent = []

    async def notify(uid, text):
        sent.append(text)

    monkeypatch.setattr(pa.db, "get_setting", get_setting)
    monkeypatch.setattr(pa.db, "set_setting", set_setting)
    monkeypatch.setattr(pa, "is_due", lambda now, last: last is None)
    pa.set_notify_callback(notify)

    async def no_alert(uid):
        return None

    monkeypatch.setattr(pa, "build_alert_text", no_alert)
    assert await pa.run_once(1) is False
    assert pa._LAST_RUN_KEY in saved and sent == []  # đánh dấu ngày dù không có sự kiện

    saved.clear()

    async def alert(uid):
        return "🔔 cảnh báo"

    monkeypatch.setattr(pa, "build_alert_text", alert)
    assert await pa.run_once(1) is True
    assert sent == ["🔔 cảnh báo"]
    assert await pa.run_once(1) is False  # cùng ngày: guard chặn
