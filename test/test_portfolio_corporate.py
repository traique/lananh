"""Vị thế quy đổi theo sự kiện quyền: ngày GDKHQ cổ tức cổ phiếu 20% không còn
báo "thủng stop"/"lỗ" giả; sự kiện chưa được giá xác nhận chỉ được ghi chú."""

from datetime import date, timedelta

import pytest

from services import portfolio_alerts as pa
from stock import corporate_actions as ca
from stock import portfolio, providers


def _business_days(n, start=date(2026, 6, 1)):
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


DATES = _business_days(90)
EX_INDEX = 85
EX_DATE = DATES[EX_INDEX]
CLOSES = [60_000.0] * EX_INDEX + [50_000.0] * (90 - EX_INDEX)  # 60.000 / 1,2


def _series():
    return providers.OhlcvSeries(
        symbol="VNM",
        closes=list(CLOSES),
        highs=[c * 1.01 for c in CLOSES],
        lows=[c * 0.99 for c in CLOSES],
        volumes=[1_000.0] * 90,
        dates=list(DATES),
    )


def _holding(**kw):
    base = dict(
        user_id=1,
        symbol="VNM",
        quantity=1005,
        average_price=60_000.0,
        stop_price=55_000.0,
        target_price=72_000.0,
        cost_basis_as_of=DATES[0],
    )
    base.update(kw)
    return portfolio.Holding(**base)


@pytest.fixture
def events(monkeypatch):
    state = {"stock": [ca.StockEvent(ex_date=EX_DATE, ratio=0.2, label="Cổ tức CP")]}

    async def fake_events(symbol):
        return [], state["stock"]

    async def fake_ohlcv(symbol, days=90):
        return _series()

    monkeypatch.setattr(portfolio.corporate_actions, "fetch_corporate_events_cached", fake_events)
    monkeypatch.setattr(portfolio.providers, "fetch_ohlcv", fake_ohlcv)
    return state


def test_effective_holding_lam_tron_xuong_giu_tong_gia_von():
    event = ca.StockEvent(ex_date=EX_DATE, ratio=0.2)
    eff = portfolio.effective_holding(_holding(), [event])
    assert eff.quantity == 1206  # floor(1005 x 1,2) - cp lẻ bị huỷ
    assert round(eff.average_price * eff.quantity) == 1005 * 60_000
    assert eff.stop_price == round(55_000 / 1.2, 2)
    assert eff.target_price == 60_000.0
    assert eff.cost_basis_as_of == EX_DATE


@pytest.mark.asyncio
async def test_corporate_view_chi_quy_doi_su_kien_duoc_gia_xac_nhan(events):
    view = await portfolio.corporate_view(_holding())
    assert view.applied and not view.unconfirmed
    assert view.effective.quantity == 1206
    assert view.rebase().as_of == EX_DATE and view.rebase().factor == pytest.approx(1.2)
    assert "Đã quy đổi" in view.notes()[0]

    events["stock"] = [ca.StockEvent(ex_date=EX_DATE, ratio=1.0)]  # tỷ lệ sai
    view = await portfolio.corporate_view(_holding())
    assert not view.applied and view.unconfirmed
    assert view.effective == view.raw
    assert view.rebase() is None
    assert "giá chưa xác nhận" in view.notes()[0]


@pytest.mark.asyncio
async def test_corporate_view_bo_qua_su_kien_truoc_moc_gia_von(events):
    view = await portfolio.corporate_view(_holding(cost_basis_as_of=EX_DATE))
    assert view.effective == view.raw and not view.applied and not view.unconfirmed


@pytest.mark.asyncio
async def test_bao_cao_khong_lo_gia_ngay_gdkhq(events, monkeypatch):
    async def fake_list(user_id):
        return [_holding()]

    async def fake_quote(symbol):
        # Ngày GDKHQ: giá đóng cửa phiên trước 60.000 (thô), giá nay 50.000.
        return providers.Quote("VNM", 50_000.0, 60_000.0, -10_000.0, -16.67, EX_DATE)

    monkeypatch.setattr(portfolio, "list_holdings", fake_list)
    monkeypatch.setattr(portfolio.providers, "fetch_quote", fake_quote)
    report = await portfolio.build_report(1)
    assert "1.206 cp" in report
    assert "đã chạm/cắt xuống stop" not in report
    assert "-16" not in report  # không lỗ giả tổng/lẫn trong ngày
    assert "Đã quy đổi theo cổ phiếu 20%" in report


def test_canh_bao_khong_thung_stop_gia_khi_su_kien_da_xac_nhan():
    view = portfolio.CorporateView(
        _holding(),
        portfolio.effective_holding(_holding(), [ca.StockEvent(EX_DATE, 0.2)]),
        (ca.StockEvent(EX_DATE, 0.2),),
    )
    quote = providers.Quote("VNM", 50_000.0, 50_000.0, 0.0, 0.0, "2099-01-01", True)
    scan = pa.PortfolioScan([pa.HoldingScan(view.effective, quote, None, True, view)], True)
    text, _ = pa.compose_alert(scan, {}, date(2026, 10, 26), outage=False)
    assert text is None  # không còn "thủng stop" / "lỗ -16,7%" giả


def test_canh_bao_su_kien_chua_xac_nhan_van_bao_stop_kem_ghi_chu():
    wrong = ca.StockEvent(EX_DATE, 1.0)
    view = portfolio.CorporateView(_holding(), _holding(), (), (wrong,))
    quote = providers.Quote("VNM", 50_000.0, 50_000.0, 0.0, 0.0, "2099-01-01", True)
    scan = pa.PortfolioScan([pa.HoldingScan(view.effective, quote, None, True, view)], True)
    text, _ = pa.compose_alert(scan, {}, date(2026, 10, 26), outage=False)
    assert "chạm/thủng stop" in text  # không tắt cảnh báo stop thật
    assert "Có thể do GDKHQ cổ phiếu 100%" in text


@pytest.mark.asyncio
async def test_job_hang_ngay_luu_quy_doi_va_bao_nguoi_so_huu(monkeypatch):
    from services import portfolio_events

    before = _holding()
    after = portfolio.effective_holding(before, [ca.StockEvent(EX_DATE, 0.2)])
    rebase = portfolio.CorporateRebase(1.2, EX_DATE, "cổ phiếu 20% GDKHQ 01/10")
    sent = []

    async def fake_apply(holding):
        return (before, after, rebase) if holding.symbol == "VNM" else None

    async def fake_deliver(user_id, text, *, kind, ref_key, today):
        sent.append((user_id, kind, ref_key, text))
        return True

    monkeypatch.setattr(portfolio_events.portfolio, "apply_confirmed_corporate_actions", fake_apply)
    monkeypatch.setattr(portfolio_events.portfolio_push, "deliver", fake_deliver)
    holdings = [
        before,
        _holding(symbol="FPT"),
        _holding(symbol="HPG", cost_basis_as_of="2099-01-01"),
    ]
    assert await portfolio_events.persist_corporate_actions(holdings, "2026-10-26") == 1
    (user_id, kind, ref_key, text) = sent[0]
    assert (user_id, kind, ref_key) == (1, "corp", f"VNM:{EX_DATE}")
    assert "1.005 → 1.206 cp" in text and "/capnhatcp" in text


@pytest.mark.asyncio
async def test_khong_co_su_kien_sau_moc_thi_khong_tai_chuoi_gia(events, monkeypatch):
    events["stock"] = []
    calls = []

    async def counting_ohlcv(symbol, days=90):
        calls.append(symbol)
        return _series()

    monkeypatch.setattr(portfolio.providers, "fetch_ohlcv", counting_ohlcv)
    view = await portfolio.corporate_view(_holding())
    assert view.effective == view.raw and view.checked
    assert calls == []  # /danhmuc không tốn thêm request DNSE khi không có sự kiện


@pytest.mark.asyncio
async def test_mua_them_thu_lai_tra_lich_quyen_trong_han_chot_luot(monkeypatch):
    from ai import timeouts

    limits = []

    async def fake_get_holding(user_id, symbol):
        return _holding()

    async def unchecked(holdings, series_list=None, *, limit_sec=8.0):
        limits.append(limit_sec)
        return [portfolio.CorporateView(h, h, checked=False) for h in holdings]

    monkeypatch.setattr(portfolio, "get_holding", fake_get_holding)
    monkeypatch.setattr(portfolio, "corporate_views", unchecked)
    with timeouts.turn_deadline(20):
        with pytest.raises(portfolio.CorporateCheckUnavailable):
            await portfolio._purchase_view(1, "VNM")
    assert len(limits) == 2 and limits[0] == 8.0 and 10 < limits[1] <= 15
    limits.clear()
    with timeouts.turn_deadline(3):  # không còn đủ thời gian thử lại
        with pytest.raises(portfolio.CorporateCheckUnavailable):
            await portfolio._purchase_view(1, "VNM")
    assert limits == [8.0]
