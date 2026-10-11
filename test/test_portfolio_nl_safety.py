"""Câu hỏi/xin ý kiến KHÔNG BAO GIỜ được ghi vào danh mục.

Lỗi thật: "Có nên bán FPT không?" từng xoá sạch vị thế FPT vì regex bán chỉ
cần thấy "bán FPT"; "hom nay ban ngay nong" bị bắt thành lệnh bán mã NGAY.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import portfolio_service  # noqa: E402
from stock import portfolio  # noqa: E402


@pytest.fixture
def recorder(monkeypatch):
    calls = {"sell": [], "add": [], "alerts": [], "report": 0}

    async def fake_get_holding(user_id, symbol):
        if symbol.upper() in {"FPT", "HPG", "VNM"}:
            return portfolio.Holding(user_id, symbol.upper(), 1000, 25_000)
        return None

    async def fake_sell(user_id, symbol, quantity):
        calls["sell"].append((symbol, quantity))
        return f"sold {symbol} {quantity}"

    async def fake_add(user_id, symbol, quantity, price, **kwargs):
        calls["add"].append((symbol, quantity, price, kwargs))
        return f"added {symbol}"

    async def fake_update_alerts(user_id, symbol, *, stop_price, target_price):
        calls["alerts"].append((symbol, stop_price, target_price))
        return portfolio.Holding(user_id, symbol, 1000, 25_000, stop_price, target_price)

    monkeypatch.setattr(portfolio, "get_holding", fake_get_holding)
    monkeypatch.setattr(portfolio, "update_alerts", fake_update_alerts)
    monkeypatch.setattr(portfolio_service, "sell_holding", fake_sell)
    monkeypatch.setattr(portfolio_service, "add_holding", fake_add)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Có nên bán FPT không?",
        "giữ hay bán HPG",
        "co nen ban VNM khong",
        "nên mua FPT 1000 giá 25 không",
        "FPT nên bán hay giữ anh",
        "bán FPT được không",
        "đặt stop FPT 24k được không?",
    ],
)
async def test_cau_hoi_khong_bao_gio_ghi_danh_muc(recorder, text):
    result = await portfolio_service.maybe_handle_natural_language(1, text)
    assert result is None
    assert recorder["sell"] == [] and recorder["add"] == [] and recorder["alerts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["hom nay ban ngay nong", "Urban area is nice", "đã bán cho anh ấy rồi"]
)
async def test_cau_chat_thuong_khong_bi_bat_lam_lenh(recorder, text):
    assert await portfolio_service.maybe_handle_natural_language(1, text) is None
    assert recorder["sell"] == []


@pytest.mark.asyncio
async def test_cau_ke_da_ban_co_so_luong_moi_ghi(recorder):
    assert (await portfolio_service.maybe_handle_natural_language(1, "vừa bán FPT 200")).startswith(
        "sold FPT"
    )
    assert (await portfolio_service.maybe_handle_natural_language(1, "bán HPG 100 rồi")).startswith(
        "sold HPG"
    )
    assert recorder["sell"] == [("FPT", 200), ("HPG", 100)]


@pytest.mark.asyncio
async def test_ban_khong_so_luong_khong_xoa_vi_the(recorder):
    result = await portfolio_service.maybe_handle_natural_language(1, "đã bán FPT")
    assert recorder["sell"] == []
    assert "/bancp FPT" in result and "đã bán hết FPT" in result


@pytest.mark.asyncio
async def test_da_ban_het_moi_xoa_toan_bo(recorder):
    assert (
        await portfolio_service.maybe_handle_natural_language(1, "đã bán hết FPT")
        == "sold FPT None"
    )
    assert (
        await portfolio_service.maybe_handle_natural_language(1, "mới bán VNM hết")
        == "sold VNM None"
    )


@pytest.mark.asyncio
async def test_lenh_ban_tran_chi_huong_dan(recorder):
    result = await portfolio_service.maybe_handle_natural_language(1, "Bán FPT 50")
    assert recorder["sell"] == []
    assert "đã bán FPT 50" in result


@pytest.mark.asyncio
async def test_mua_tran_chi_huong_dan_cau_ke_moi_ghi(recorder):
    hint = await portfolio_service.maybe_handle_natural_language(1, "mua FPT 100 cp giá 120")
    assert recorder["add"] == [] and "đã mua FPT" in hint
    await portfolio_service.maybe_handle_natural_language(1, "đã mua FPT 100 cp giá 120")
    assert recorder["add"][0][:3] == ("FPT", 100, 120_000)


def test_parse_price_quy_uoc_nghin_dong():
    assert portfolio_service.parse_price("25.5") == 25_500
    assert portfolio_service.parse_price("25,5") == 25_500
    assert portfolio_service.parse_price("120") == 120_000
    assert portfolio_service.parse_price("120k") == 120_000
    assert portfolio_service.parse_price("120.000") == 120_000
    assert portfolio_service.parse_price("1,5tr") == 1_500_000
    # Khối lượng không bị nhân.
    assert portfolio_service.parse_number("120") == 120


@pytest.mark.asyncio
async def test_themcp_gia_nghin_dong_nhung_so_luong_giu_nguyen(monkeypatch):
    calls = []

    async def fake_add(user_id, symbol, quantity, price, **kwargs):
        calls.append((quantity, price, kwargs))
        return "ok"

    monkeypatch.setattr(portfolio_service, "add_holding", fake_add)
    await portfolio_service.handle_command(7, "/themcp FPT 100 25.5 24 30")
    assert calls == [
        (100, 25_500, {"stop_price": 24_000, "target_price": 30_000, "replace": False})
    ]
