"""Sổ giao dịch danh mục: lệnh bán kèm giá, lãi/lỗ đã chốt theo tháng (không DB).

Phần SQL (transaction, phí/thuế ghi DB) kiểm ở test/test_delivery_sql.py (PGlite).
"""

from datetime import datetime

import pytest

from services import portfolio_service
from stock import portfolio

_TZ = portfolio._VN_TZ


def _tx(side, symbol="HPG", qty=100, price=None, fee=0.0, tax=0.0, avg=None, pnl=None, day=1):
    return portfolio.Transaction(
        id=day,
        symbol=symbol,
        side=side,
        quantity=qty,
        price=price,
        fee=fee,
        tax=tax,
        average_cost=avg,
        realized_pnl=pnl,
        note="",
        traded_at=datetime(2026, 10, day, 10, tzinfo=_TZ),
    )


@pytest.fixture
def sells(monkeypatch):
    calls = []

    async def fake_get(user_id, symbol):
        return portfolio.Holding(user_id, symbol.upper(), 1000, 25_000)

    async def fake_sell(user_id, symbol, quantity, price=None):
        calls.append((symbol, quantity, price))
        return "sold"

    monkeypatch.setattr(portfolio, "get_holding", fake_get)
    monkeypatch.setattr(portfolio_service, "sell_holding", fake_sell)
    return calls


@pytest.mark.asyncio
async def test_bancp_kem_gia_ban(sells):
    assert await portfolio_service.handle_command(1, "/bancp HPG 1000 27.5") == "sold"
    assert sells == [("HPG", 1000, 27_500)]
    await portfolio_service.handle_command(1, "/bancp HPG hết 27")
    assert sells[-1] == ("HPG", None, 27_000)
    assert "Giá bán không hợp lệ" in await portfolio_service.handle_command(1, "/bancp HPG 10 abc")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected",
    [
        ("đã bán HPG 1000 giá 27", ("HPG", 1000, 27_000)),
        ("vừa bán HPG 500 cp @ 27.5", ("HPG", 500, 27_500)),
        ("bán HPG 1000 giá 26 rồi", ("HPG", 1000, 26_000)),
        ("đã bán hết HPG giá 27", ("HPG", None, 27_000)),
        ("đã bán HPG 1000", ("HPG", 1000, None)),
    ],
)
async def test_cau_ke_da_ban_kem_gia(sells, text, expected):
    await portfolio_service.maybe_handle_natural_language(1, text)
    assert sells == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["bán HPG 1000 giá 27", "có nên bán HPG 1000 giá 27 không?", "bán 1000 HPG giá 27"],
)
async def test_lenh_ban_kem_gia_khong_phai_cau_ke_khong_ghi(sells, text):
    """Giữ quy tắc an toàn: chỉ câu kể ĐÃ bán mới ghi vào danh mục."""
    await portfolio_service.maybe_handle_natural_language(1, text)
    assert sells == []


def test_format_sale_co_va_khong_co_gia():
    priced = portfolio.SaleResult(
        holding=portfolio.Holding(1, "HPG", 500, 26_000),
        quantity=500,
        price=30_000,
        fee=22_500,
        tax=15_000,
        average_cost=26_000,
        realized_pnl=1_962_500,
    )
    text = portfolio_service.format_sale("HPG", priced)
    assert text.startswith("✅ Đã giảm HPG, còn 500 cp.")
    assert "+1.962.500đ" in text and "phí 22.500đ" in text and "thuế 15.000đ" in text
    closed = portfolio.SaleResult(None, 500, None, 0, 0, 26_000, None)
    text = portfolio_service.format_sale("HPG", closed)
    assert text.startswith("✅ Đã bán hết và xóa HPG")
    assert "/giaban HPG" in text


def test_summarize_realized_theo_thang_va_ti_le_thang():
    txs = [
        _tx("BUY", qty=1000, price=25_000, fee=37_500, avg=25_000, day=1),
        _tx(
            "SELL", qty=500, price=30_000, fee=22_500, tax=15_000, avg=25_000, pnl=2_462_500, day=5
        ),
        _tx("SELL", qty=200, price=24_000, fee=7_200, tax=4_800, avg=25_000, pnl=-212_000, day=6),
        _tx("SELL", qty=300, day=7, avg=25_000),  # chưa có giá
        _tx("ADJUST", qty=0, day=8),
    ]
    (month,) = portfolio.summarize_realized(txs)
    assert month.month == "2026-10"
    assert month.priced_sells == 2 and month.wins == 1
    assert month.realized == 2_462_500 - 212_000
    assert month.gross == 5_000 * 500 - 1_000 * 200
    assert month.sell_fee == 29_700 and month.tax == 19_800 and month.buy_fee == 37_500
    assert month.unpriced == [("HPG", "07/10")]
    text = portfolio.format_realized([month])
    assert "thắng 1/2 (50,0%)" in text
    assert "Đã chốt ròng: *+2.250.500đ*" in text
    assert "/giaban HPG" in text


def test_tham_so_thang_cua_laicp():
    assert portfolio_service._parse_month_arg("2026-09") == ("2026-09", None)
    assert portfolio_service._parse_month_arg("9/2026") == ("2026-09", None)
    assert portfolio_service._parse_month_arg("12") == (None, 12)
    for bad in ("13/2026", "0", "99", "abc"):
        with pytest.raises(ValueError):
            portfolio_service._parse_month_arg(bad)


def test_phi_va_thue_doc_tu_env(monkeypatch):
    monkeypatch.setenv("STOCK_FEE_RATE", "0.001")
    monkeypatch.setenv("STOCK_SELL_TAX_RATE", "0,001")
    assert portfolio.fee_rate() == 0.001 and portfolio.sell_tax_rate() == 0.001
    monkeypatch.setenv("STOCK_FEE_RATE", "0.5")  # vô lý -> mặc định
    assert portfolio.fee_rate() == 0.0015


@pytest.mark.asyncio
async def test_laicp_va_lichsucp_goi_dung_ham(monkeypatch):
    seen = {}

    async def fake_realized(user_id, *, months, month):
        seen["realized"] = (user_id, months, month)
        return []

    async def fake_history(user_id, *, symbol=None, limit=15):
        seen["history"] = (user_id, symbol)
        return "sổ"

    monkeypatch.setattr(portfolio, "realized_by_month", fake_realized)
    monkeypatch.setattr(portfolio, "build_history_text", fake_history)
    text = await portfolio_service.handle_command(-3, "/laicp 9/2026")
    assert seen["realized"] == (-3, 6, "2026-09") and "Chưa có lệnh" in text
    assert await portfolio_service.handle_command(-3, "/lichsucp fpt") == "sổ"
    assert seen["history"] == (-3, "FPT")
