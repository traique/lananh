import pytest

from services import portfolio_service
from stock import portfolio


def test_parse_number_supports_vietnamese_price_formats():
    assert portfolio_service.parse_number("120k") == 120_000
    assert portfolio_service.parse_number("120.000") == 120_000
    assert portfolio_service.parse_number("1.000") == 1_000
    assert portfolio_service.parse_number("1,5tr") == 1_500_000


@pytest.mark.asyncio
async def test_themcp_passes_quantity_cost_and_alerts(monkeypatch):
    calls = []

    async def fake_add(user_id, symbol, quantity, price, **kwargs):
        calls.append((user_id, symbol, quantity, price, kwargs))
        return "ok"

    monkeypatch.setattr(portfolio_service, "add_holding", fake_add)

    result = await portfolio_service.handle_command(7, "/themcp FPT 100 120k 110k 145k")

    assert result == "ok"
    assert calls == [
        (
            7,
            "FPT",
            100,
            120_000,
            {"stop_price": 110_000, "target_price": 145_000, "replace": False},
        )
    ]


@pytest.mark.asyncio
async def test_natural_language_adds_holding_and_alerts(monkeypatch):
    calls = []

    async def fake_add(user_id, symbol, quantity, price, **kwargs):
        calls.append((user_id, symbol, quantity, price, kwargs))
        return "đã thêm"

    monkeypatch.setattr(portfolio_service, "add_holding", fake_add)

    result = await portfolio_service.maybe_handle_natural_language(
        1,
        "Mua FPT 100 cp giá vốn 120k, stop 110k, target 145k",
    )

    assert result == "đã thêm"
    assert calls[0][0:4] == (1, "FPT", 100, 120_000)
    assert calls[0][4] == {"stop_price": 110_000, "target_price": 145_000}


@pytest.mark.asyncio
async def test_natural_language_lists_structured_portfolio(monkeypatch):
    async def fake_report(user_id):
        return f"portfolio:{user_id}"

    monkeypatch.setattr(portfolio, "build_report", fake_report)

    result = await portfolio_service.maybe_handle_natural_language(
        9,
        "Xem danh mục của anh đang lãi lỗ thế nào",
    )

    assert result == "portfolio:9"


@pytest.mark.asyncio
async def test_natural_language_partial_sell(monkeypatch):
    calls = []

    async def fake_sell(user_id, symbol, quantity):
        calls.append((user_id, symbol, quantity))
        return "đã bán"

    monkeypatch.setattr(portfolio_service, "sell_holding", fake_sell)

    result = await portfolio_service.maybe_handle_natural_language(2, "Bán FPT 50")

    assert result == "đã bán"
    assert calls == [(2, "FPT", 50)]
