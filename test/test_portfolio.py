import pytest

from stock import portfolio, providers


def _quote(symbol: str, price: float) -> providers.Quote:
    return providers.Quote(
        symbol=symbol,
        price=price,
        prev_close=price - 1_000,
        change=1_000,
        change_pct=1.0,
        date="2026-08-04",
        is_realtime=True,
    )


@pytest.mark.asyncio
async def test_report_calculates_pnl_allocation_and_alerts(monkeypatch):
    holdings = [
        portfolio.Holding(
            user_id=1,
            symbol="FPT",
            quantity=100,
            average_price=100_000,
            stop_price=95_000,
            target_price=120_000,
        ),
        portfolio.Holding(
            user_id=1,
            symbol="HPG",
            quantity=200,
            average_price=25_000,
            stop_price=24_000,
            target_price=30_000,
        ),
    ]

    async def fake_list(user_id):
        assert user_id == 1
        return holdings

    async def fake_quote(symbol):
        return _quote(symbol, 120_000 if symbol == "FPT" else 23_000)

    monkeypatch.setattr(portfolio, "list_holdings", fake_list)
    monkeypatch.setattr(portfolio.providers, "fetch_quote", fake_quote)

    report = await portfolio.build_report(1)

    assert "FPT" in report and "+20.00%" in report
    assert "HPG" in report and "-8.00%" in report
    assert "đã đạt/vượt mục tiêu" in report
    assert "đã chạm/cắt xuống stop" in report
    assert "Lãi/lỗ tạm tính" in report


@pytest.mark.asyncio
async def test_report_excludes_unpriced_holding_from_totals(monkeypatch):
    holdings = [
        portfolio.Holding(1, "FPT", 100, 100_000),
        portfolio.Holding(1, "HPG", 1_000, 25_000),
    ]

    async def fake_list(user_id):
        return holdings

    async def fake_quote(symbol):
        return _quote(symbol, 110_000) if symbol == "FPT" else None

    monkeypatch.setattr(portfolio, "list_holdings", fake_list)
    monkeypatch.setattr(portfolio.providers, "fetch_quote", fake_quote)

    report = await portfolio.build_report(1)

    assert "không tính vào tổng lãi/lỗ" in report
    assert "Tổng giá vốn đã định giá: 10.000.000đ" in report
    assert "1.000.000đ (+10.00%)" in report


@pytest.mark.asyncio
async def test_empty_portfolio_message(monkeypatch):
    async def fake_list(user_id):
        return []

    monkeypatch.setattr(portfolio, "list_holdings", fake_list)

    assert "Chưa có cổ phiếu" in await portfolio.build_report(1)
