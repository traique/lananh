"""Cảnh báo giá trong phiên: ngưỡng người dùng tự đặt + stop/target danh mục.

Trạng thái vượt ngưỡng nằm trong DB (ở đây là dict giả) nên chạy lại run_once
(= restart/redeploy) không báo lại; chỉ dùng tick hôm nay, chỉ trong giờ giao dịch.
"""

from datetime import date, datetime, timedelta

import pytest

from services import portfolio_alerts as pa
from services import price_alerts as alerts
from stock import portfolio

_TZ = alerts._VN_TZ
IN_SESSION = datetime(2026, 10, 26, 10, 35, tzinfo=_TZ)  # thứ Hai


def test_evaluate_trang_thai_va_rearm():
    B, A = alerts.BELOW, alerts.ABOVE
    assert alerts.evaluate(B, 25_000, 24_900, "unknown") == (True, "crossed")
    assert alerts.evaluate(B, 25_000, 24_800, "crossed") == (False, "crossed")
    assert alerts.evaluate(B, 25_000, 25_100, "crossed") == (False, "crossed")  # chưa quá 1%
    assert alerts.evaluate(B, 25_000, 25_300, "crossed") == (False, "armed")
    assert alerts.evaluate(B, 25_000, 25_300, "unknown") == (False, "armed")
    assert alerts.evaluate(B, 25_000, 25_000, "armed") == (True, "crossed")
    assert alerts.evaluate(A, 30_000, 30_000, "armed") == (True, "crossed")
    assert alerts.evaluate(A, 30_000, 29_800, "crossed") == (False, "crossed")
    assert alerts.evaluate(A, 30_000, 29_600, "crossed") == (False, "armed")


class _Db:
    def __init__(self, monkeypatch, rows, ticks):
        self.rows = rows
        self.ticks = ticks
        self.sent: list[tuple[int, str, str]] = []
        self.deliver_ok = True
        self.logged: set[tuple] = set()

        async def active(limit):
            return [dict(r) for r in self.rows]

        async def set_state(alert_id, state, *, notified):
            for row in self.rows:
                if row["id"] == alert_id:
                    row["last_state"] = state
                    if notified:
                        row["notified_at"] = IN_SESSION

        async def tick(symbol):
            return self.ticks.get(symbol)

        async def deliver(user_id, text, *, kind, ref_key, today):
            if not self.deliver_ok:
                return False
            self.sent.append((user_id, kind, text))
            self.logged.add((user_id, kind, ref_key, today))
            return True

        async def already_sent(user_id, kind, ref_key, *, on=None):
            return (user_id, kind, ref_key, on) in self.logged

        async def no_holdings(owner_id):
            return []

        monkeypatch.setattr(alerts.db, "portfolio_active_price_alerts", active)
        monkeypatch.setattr(alerts.db, "portfolio_set_price_alert_state", set_state)
        monkeypatch.setattr(alerts.providers, "fetch_realtime_tick", tick)
        monkeypatch.setattr(alerts.portfolio_push, "deliver", deliver)
        monkeypatch.setattr(alerts.portfolio_push, "already_sent", already_sent)
        monkeypatch.setattr(alerts, "_owner_holdings", no_holdings)


def _row(alert_id=1, user=42, symbol="HPG", direction="BELOW", level=25_000, state="armed"):
    return dict(
        id=alert_id,
        telegram_user_id=user,
        symbol=symbol,
        direction=direction,
        level=level,
        last_state=state,
        notified_at=None,
    )


@pytest.mark.asyncio
async def test_ngoai_gio_giao_dich_khong_lam_gi(monkeypatch):
    fake = _Db(monkeypatch, [_row()], {"HPG": 24_000.0})
    assert await alerts.run_once(42, datetime(2026, 10, 26, 15, 30, tzinfo=_TZ)) == 0
    assert await alerts.run_once(42, datetime(2026, 10, 25, 10, 0, tzinfo=_TZ)) == 0  # CN
    assert fake.sent == []


@pytest.mark.asyncio
async def test_bao_mot_lan_moi_lan_cham_ke_ca_khi_restart(monkeypatch):
    fake = _Db(monkeypatch, [_row()], {"HPG": 24_950.0})
    assert await alerts.run_once(42, IN_SESSION) == 1
    user_id, kind, text = fake.sent[0]
    assert (user_id, kind) == (42, "price")
    assert "HPG chạm 25.000đ" in text and "không phải khuyến nghị" in text
    assert fake.rows[0]["last_state"] == "crossed"
    # Vòng sau / sau restart: trạng thái crossed trong DB -> không báo lại.
    assert await alerts.run_once(42, IN_SESSION + timedelta(minutes=5)) == 0
    # Giá quay lại quá ngưỡng 1% -> armed; chạm lại cùng ngày: không quá 1 tin/ngày.
    fake.ticks["HPG"] = 25_400.0
    await alerts.run_once(42, IN_SESSION + timedelta(minutes=10))
    assert fake.rows[0]["last_state"] == "armed"
    fake.ticks["HPG"] = 24_900.0
    assert await alerts.run_once(42, IN_SESSION + timedelta(minutes=15)) == 0
    assert fake.rows[0]["last_state"] == "crossed" and len(fake.sent) == 1


@pytest.mark.asyncio
async def test_khong_co_tick_hom_nay_khong_bao(monkeypatch):
    fake = _Db(monkeypatch, [_row()], {})
    assert await alerts.run_once(42, IN_SESSION) == 0
    assert fake.rows[0]["last_state"] == "armed" and fake.sent == []


@pytest.mark.asyncio
async def test_gui_loi_giu_trang_thai_de_thu_lai(monkeypatch):
    fake = _Db(monkeypatch, [_row()], {"HPG": 24_000.0})
    fake.deliver_ok = False
    assert await alerts.run_once(42, IN_SESSION) == 0
    assert fake.rows[0]["last_state"] == "armed"
    fake.deliver_ok = True
    assert await alerts.run_once(42, IN_SESSION + timedelta(minutes=5)) == 1


@pytest.mark.asyncio
async def test_tran_so_ma_moi_vong(monkeypatch):
    rows = [_row(alert_id=i, symbol=f"M{i:02d}") for i in range(5)]
    fake = _Db(monkeypatch, rows, {f"M{i:02d}": 1.0 for i in range(5)})
    asked = []
    original = alerts.providers.fetch_realtime_tick

    async def counting(symbol):
        asked.append(symbol)
        return await original(symbol)

    monkeypatch.setattr(alerts.providers, "fetch_realtime_tick", counting)
    monkeypatch.setenv("PRICE_ALERT_MAX_SYMBOLS", "3")
    assert await alerts.run_once(42, IN_SESSION) == 3
    assert asked == ["M00", "M01", "M02"] and len(fake.sent) == 3


@pytest.mark.asyncio
async def test_stop_danh_muc_bao_trong_phien_mot_lan_moi_ngay(monkeypatch):
    fake = _Db(monkeypatch, [], {"VNM": 54_000.0})
    holding = portfolio.Holding(42, "VNM", 100, 60_000.0, stop_price=55_000.0)

    async def holdings(owner_id):
        return [holding]

    async def state(owner_id):
        return {}

    monkeypatch.setattr(alerts, "_owner_holdings", holdings)
    monkeypatch.setattr(alerts.portfolio_alerts, "load_state", state)
    assert await alerts.run_once(42, IN_SESSION) == 1
    assert (
        fake.sent[0][1] == "stop" and "VNM chạm/thủng stop 55.000đ trong phiên" in fake.sent[0][2]
    )
    assert (42, "stop", "VNM:55000", "2026-10-26") in fake.logged
    assert await alerts.run_once(42, IN_SESSION + timedelta(minutes=5)) == 0


@pytest.mark.asyncio
async def test_da_dong_cua_duoi_stop_phien_truoc_thi_khong_bao_lai_moi_sang(monkeypatch):
    fake = _Db(monkeypatch, [], {"VNM": 54_000.0})
    holding = portfolio.Holding(42, "VNM", 100, 60_000.0, stop_price=55_000.0)

    async def holdings(owner_id):
        return [holding]

    async def state(owner_id):
        # Job cuối phiên thứ Sáu 23/10 đã ghi "dưới stop".
        return {"VNM": {"stop": {"level": 55_000.0, "days": 2, "last": "2026-10-23"}}}

    monkeypatch.setattr(alerts, "_owner_holdings", holdings)
    monkeypatch.setattr(alerts.portfolio_alerts, "load_state", state)
    assert await alerts.run_once(42, IN_SESSION) == 0
    assert fake.sent == []


def test_canh_bao_cuoi_phien_bo_qua_stop_da_bao_trong_phien():
    holding = portfolio.Holding(42, "VNM", 100, 60_000.0, stop_price=58_000.0)
    quote = type("Q", (), {"price": 57_000.0, "is_realtime": True, "date": "2026-10-26"})()
    scan = pa.PortfolioScan([pa.HoldingScan(holding, quote, None, True)], True)
    day = date(2026, 10, 26)
    text, state = pa.compose_alert(scan, {}, day, outage=False, reported_intraday={"VNM": {"stop"}})
    assert text is None  # lỗ -5% chưa tới ngưỡng, stop đã báo trong phiên
    assert state["VNM"]["stop"]["days"] == 1
    # Hôm sau vẫn dưới stop: dòng rút gọn "ngày thứ 2" vẫn có.
    text, _ = pa.compose_alert(scan, state, day + timedelta(days=1), outage=False)
    assert "ngày thứ 2 dưới stop" in text


@pytest.mark.asyncio
async def test_cache_quy_doi_quyen_khong_giu_stop_target_cu(monkeypatch):
    """Cache chỉ giữ sự kiện quyền đã xác nhận theo (mã, mốc giá vốn): dời stop
    (không đổi mốc/số cp) phải có hiệu lực ngay ở vòng quét kế tiếp."""
    from stock import corporate_actions as ca

    monkeypatch.setenv("PRICE_ALERTS_HOLDINGS_INTRADAY", "1")
    monkeypatch.setattr(alerts, "_view_cache", {})
    row = {"symbol": "HPG", "quantity": 1000, "stop": 20_000.0, "events": []}
    lookups = []

    async def list_holdings(owner_id):
        return [
            portfolio.Holding(
                42,
                row["symbol"],
                row["quantity"],
                24_000.0,
                stop_price=row["stop"],
                cost_basis_as_of="2026-10-01",
            )
        ]

    async def views(holdings, series_list=None, *, limit_sec=8.0):
        lookups.append([h.symbol for h in holdings])
        applied = tuple(row["events"])
        return [
            portfolio.CorporateView(h, portfolio.effective_holding(h, applied), applied)
            for h in holdings
        ]

    monkeypatch.setattr(alerts.portfolio, "list_holdings", list_holdings)
    monkeypatch.setattr(alerts.portfolio, "corporate_views", views)
    (first,) = await alerts._owner_holdings(42)
    assert first.stop_price == 20_000
    row["stop"] = 24_000.0  # 10h anh dời stop lên 24.000 (không đổi mốc giá vốn/số cp)
    (second,) = await alerts._owner_holdings(42)
    assert second.stop_price == 24_000
    assert len(lookups) == 1  # vẫn dùng cache, không tra lại lịch quyền

    # Mã có sự kiện đã xác nhận: quy đổi áp lên dòng vị thế MỚI mỗi vòng (bán bớt
    # cũng không làm lệch số cp đã quy đổi).
    row.update(symbol="VNM", events=[ca.StockEvent(ex_date="2026-10-20", ratio=0.2)])
    (third,) = await alerts._owner_holdings(42)
    assert third.quantity == 1200 and third.stop_price == pytest.approx(20_000, abs=0.01)
    row.update(quantity=500, stop=30_000.0)
    (fourth,) = await alerts._owner_holdings(42)
    assert fourth.quantity == 600 and fourth.stop_price == pytest.approx(25_000, abs=0.01)
    assert len(lookups) == 2
