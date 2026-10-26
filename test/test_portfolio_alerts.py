import json
from datetime import date, datetime, timedelta

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


class _Env:
    """Settings giả + nguồn giá giả cho run_once (ngày thật của máy chạy test)."""

    def __init__(self, monkeypatch, holdings, quotes, *, index_date="today"):
        self.saved: dict[str, str] = {}
        self.sent: list[str] = []
        self.fail_send = False
        self.today = datetime.now(pa._VN_TZ).date()
        self.holdings = holdings
        self.quotes = quotes
        self.index_date = self.today.isoformat() if index_date == "today" else index_date
        self.final = False

        async def get_setting(key):
            return self.saved.get(key)

        async def set_setting(key, value):
            self.saved[key] = value

        async def notify(uid, text):
            if self.fail_send:
                raise RuntimeError("Telegram lỗi mạng")
            self.sent.append(text)

        async def list_holdings(uid):
            return self.holdings

        async def fetch_quote(symbol):
            return self.quotes.get(symbol)

        async def fetch_ohlcv(symbol, days=90):
            if symbol == "VNINDEX":
                return providers.OhlcvSeries(
                    symbol="VNINDEX",
                    closes=[1.0] if self.index_date else [],
                    highs=[1.0] if self.index_date else [],
                    lows=[1.0] if self.index_date else [],
                    volumes=[1.0] if self.index_date else [],
                    dates=[self.index_date] if self.index_date else [],
                )
            return providers.OhlcvSeries(symbol=symbol)

        async def not_logged(*args, **kwargs):
            return False

        monkeypatch.setattr(pa.db, "get_setting", get_setting)
        monkeypatch.setattr(pa.db, "set_setting", set_setting)
        monkeypatch.setattr(pa.db, "portfolio_alert_logged", not_logged)
        monkeypatch.setattr(pa, "is_due", lambda now, last: last != self.today.isoformat())
        monkeypatch.setattr(pa, "is_final_poll", lambda now: self.final)
        monkeypatch.setattr(pa.portfolio, "list_holdings", list_holdings)
        monkeypatch.setattr(pa.providers, "fetch_quote", fetch_quote)
        monkeypatch.setattr(pa.providers, "fetch_ohlcv", fetch_ohlcv)
        pa.set_notify_callback(notify)

    def quote(self, symbol, price, *, day=None, realtime=True):
        return providers.Quote(
            symbol, price, price, 0.0, 0.0, (day or self.today).isoformat(), is_realtime=realtime
        )

    @property
    def marked(self) -> bool:
        return self.saved.get(pa._LAST_RUN_KEY) == self.today.isoformat()


@pytest.mark.asyncio
async def test_run_once_khong_co_su_kien_van_danh_dau_ngay(monkeypatch):
    env = _Env(monkeypatch, [_holding()], {})
    env.quotes["FPT"] = env.quote("FPT", 101_000.0)
    assert await pa.run_once(1) is False
    assert env.marked and env.sent == []


@pytest.mark.asyncio
async def test_run_once_gui_loi_khong_ghi_guard_va_thu_lai(monkeypatch):
    env = _Env(monkeypatch, [_holding(stop_price=95_000.0)], {})
    env.quotes["FPT"] = env.quote("FPT", 90_000.0)
    env.fail_send = True
    assert await pa.run_once(1) is False
    assert not env.marked  # Telegram lỗi: không mất cảnh báo hôm nay
    env.fail_send = False
    assert await pa.run_once(1) is True
    assert env.marked and "chạm/thủng stop" in env.sent[0]
    assert await pa.run_once(1) is False  # cùng ngày: guard chặn
    assert len(env.sent) == 1


@pytest.mark.asyncio
async def test_run_once_mat_du_lieu_thu_lai_roi_bao_o_vong_cuoi(monkeypatch):
    env = _Env(monkeypatch, [_holding(), _holding(symbol="HPG")], {})
    assert await pa.run_once(1) is False
    assert env.sent == [] and not env.marked  # vòng sau thử lại
    env.final = True
    assert await pa.run_once(1) is True
    assert "không lấy được dữ liệu giá cuối phiên" in env.sent[0]
    assert "FPT, HPG" in env.sent[0] and env.marked


@pytest.mark.asyncio
async def test_tick_loi_dung_nen_hom_qua_bi_tinh_la_thieu_du_lieu(monkeypatch):
    env = _Env(monkeypatch, [_holding(stop_price=95_000.0)], {})
    yesterday = env.today - timedelta(days=1)
    env.quotes["FPT"] = env.quote("FPT", 90_000.0, day=yesterday, realtime=False)
    assert await pa.run_once(1) is False
    assert env.sent == []  # không báo stop trên giá phiên trước
    env.final = True
    assert await pa.run_once(1) is True
    assert "chạm/thủng stop" not in env.sent[0]
    assert "không lấy được dữ liệu" in env.sent[0]


@pytest.mark.asyncio
async def test_khong_co_phien_hom_nay_im_lang_danh_dau_o_vong_cuoi(monkeypatch):
    yesterday = (datetime.now(pa._VN_TZ).date() - timedelta(days=1)).isoformat()
    env = _Env(monkeypatch, [_holding(stop_price=95_000.0)], {}, index_date=yesterday)
    env.quotes["FPT"] = env.quote(
        "FPT", 90_000.0, day=env.today - timedelta(days=1), realtime=False
    )
    assert await pa.run_once(1) is False
    assert not env.marked
    env.final = True
    assert await pa.run_once(1) is False
    assert env.marked and env.sent == []  # ngày lễ ngoài bảng: không báo mất dữ liệu


def test_stop_lap_lai_rut_gon_ngay_thu_n():
    holding = _holding(stop_price=95_000.0)
    quote = providers.Quote("FPT", 94_000.0, 95_000.0, 0.0, 0.0, "2026-03-12", True)
    signals = pa.detect_signals(holding, quote, None)
    day1, state = pa.apply_repeat_rules(signals, None, date(2026, 3, 12))
    assert any("chạm/thủng stop" in line for line in day1)
    day2, state = pa.apply_repeat_rules(signals, state, date(2026, 3, 13))
    assert any("ngày thứ 2 dưới stop 95.000" in line for line in day2)
    # Qua cuối tuần vẫn là phiên liên tiếp.
    day3, state = pa.apply_repeat_rules(signals, state, date(2026, 3, 16))
    assert any("ngày thứ 3 dưới stop" in line for line in day3)
    # Hồi lên trên stop thì trạng thái xoá; lần sau thủng lại báo đầy đủ.
    _, cleared = pa.apply_repeat_rules([], state, date(2026, 3, 17))
    assert "stop" not in cleared


def test_dong_lo_8_phan_tram_chi_lap_khi_sau_them_hoac_sau_mot_tuan():
    holding = _holding()

    def signals(price):
        quote = providers.Quote("FPT", price, price, 0.0, 0.0, "2026-03-12", True)
        return [s for s in pa.detect_signals(holding, quote, None) if s.kind == "loss"]

    lines, state = pa.apply_repeat_rules(signals(90_000.0), None, date(2026, 3, 12))
    assert lines and "lỗ -10" in lines[0]
    lines, state = pa.apply_repeat_rules(signals(89_000.0), state, date(2026, 3, 13))
    assert lines == []  # lỗ -11%: chưa sâu thêm 3 điểm
    lines, state = pa.apply_repeat_rules(signals(86_500.0), state, date(2026, 3, 16))
    assert lines and "lỗ -13,5" in lines[0]  # sâu thêm >= 3 điểm
    lines, state = pa.apply_repeat_rules(signals(86_500.0), state, date(2026, 3, 20))
    assert lines == []
    lines, state = pa.apply_repeat_rules(signals(86_500.0), state, date(2026, 3, 23))
    assert lines  # đủ 7 ngày: nhắc lại


@pytest.mark.asyncio
async def test_run_once_luu_trang_thai_sau_khi_gui(monkeypatch):
    env = _Env(monkeypatch, [_holding(stop_price=95_000.0)], {})
    env.quotes["FPT"] = env.quote("FPT", 90_000.0)
    assert await pa.run_once(1) is True
    state = json.loads(env.saved[pa._STATE_KEY.format(user_id=1)])
    assert state["FPT"]["stop"]["days"] == 1
    assert state["FPT"]["loss"]["pct"] == -10.0


@pytest.mark.asyncio
async def test_nhac_cap_nhat_lich_giao_dich_toi_da_moi_tuan(monkeypatch):
    saved = {}

    async def get_setting(key):
        return saved.get(key)

    async def set_setting(key, value):
        saved[key] = value

    sent = []
    monkeypatch.setattr(pa.db, "get_setting", get_setting)
    monkeypatch.setattr(pa.db, "set_setting", set_setting)
    monkeypatch.setattr(pa.alerts, "send", lambda text: sent.append(text) or True)
    tz = pa._VN_TZ

    assert await pa.maybe_remind_calendar(datetime(2026, 11, 3, 9, tzinfo=tz)) is False  # có 2027
    assert await pa.maybe_remind_calendar(datetime(2027, 11, 1, 9, tzinfo=tz)) is True
    assert "2028" in sent[0]
    assert await pa.maybe_remind_calendar(datetime(2027, 11, 5, 9, tzinfo=tz)) is False
    assert await pa.maybe_remind_calendar(datetime(2027, 11, 8, 9, tzinfo=tz)) is True
    assert len(sent) == 2
