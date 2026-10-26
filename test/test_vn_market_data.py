"""Test services/vn_market_data.py với dữ liệu mẫu ghi lại từ nguồn thật, không gọi mạng."""

import datetime as dt

import httpx
import pytest

from services import http_client
from services import vn_market_data as market
from _vn_data_samples import FX_XML, SJC_JSON, XSMB_RSS, XSMN_RSS  # noqa: E402

TODAY = dt.date(2026, 10, 26)


def test_parse_fx_xml():
    table = market.parse_fx_xml(FX_XML)
    assert table.updated == "08:03 26/10/2026"
    usd = table.rates["USD"]
    assert (usd.buy, usd.transfer, usd.sell) == (25620.0, 25650.0, 26030.0)
    assert table.rates["KWD"].buy is None  # "-" = không niêm yết
    assert table.rates["EUR"].name == "EURO"
    text = market.format_fx(table, ["USD", "JPY"])
    assert "• USD: mua TM 25.620 · mua CK 25.650 · bán 26.030" in text
    assert "• JPY: mua TM 164,12 · mua CK 165,78 · bán 173,65" in text


def test_parse_fx_tu_choi_doctype_va_xml_la():
    with pytest.raises(market.MarketDataError):
        market.parse_fx_xml(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><ExrateList/>')
    with pytest.raises(market.MarketDataError):
        market.parse_fx_xml(b"<Other><Exrate CurrencyCode='USD'/></Other>")
    with pytest.raises(market.MarketDataError):
        market.parse_fx_xml(b"<ExrateList><DateTime>x</DateTime></ExrateList>")  # thiếu USD


def test_doi_tien_hai_chieu():
    table = market.parse_fx_xml(FX_XML)
    to_vnd = market.format_conversion(table, 100, "USD", to_vnd=True)
    assert to_vnd.startswith("💱 100 USD ≈ 2.565.000 đ")
    assert "bán tiền mặt cho ngân hàng được ≈ 2.562.000 đ" in to_vnd
    from_vnd = market.format_conversion(table, 1_000_000, "USD", to_vnd=False)
    assert from_vnd.startswith("💱 1.000.000 đ ≈ 38,42 USD")
    assert market.format_conversion(table, 100, "XYZ", to_vnd=True) is None


def test_parse_sjc_json():
    table = market.parse_sjc_json(SJC_JSON)
    assert table.updated == "08:30 26/10/2026" and len(table.rows) == 3
    text = market.format_gold(table)
    assert text.startswith("🪙 Giá vàng SJC (cập nhật 08:30 26/10/2026) - Hồ Chí Minh")
    assert "• Vàng SJC 1L, 10L, 1KG: mua 148.000 · bán 150.000" in text
    assert "Vàng nhẫn SJC 99,99" in text and "Hà Nội" not in text  # chỉ 1 chi nhánh
    assert market.gold_headline(table) == "Vàng SJC mua 148.000 · bán 150.000 nghìn/lượng"


def test_parse_sjc_loi():
    with pytest.raises(market.MarketDataError):
        market.parse_sjc_json(b'{"success":false}')
    with pytest.raises(market.MarketDataError):
        market.parse_sjc_json(b'{"success":true,"data":[]}')
    # Phòng khi trả nghìn đồng thay vì đồng.
    table = market.parse_sjc_json(
        b'{"success":true,"data":[{"TypeName":"SJC","BuyValue":148000,"SellValue":150000}]}'
    )
    assert table.rows[0].sell == 150_000_000


def test_parse_lottery_mien_nam_ke_ca_dong_dinh():
    draws = market.parse_lottery_rss(XSMN_RSS, "mn", TODAY)
    assert [d.date for d in draws] == [dt.date(2026, 10, 25), dt.date(2026, 10, 24)]
    kg, tg = draws[0].provinces
    assert kg.name == "Kiên Giang" and kg.prizes["ĐB"] == ("729510",)
    assert kg.prizes["4"][-1] == "21803" and len(kg.prizes["4"]) == 7
    assert kg.prizes["7"] == ("222",) and kg.prizes["8"] == ("37",)
    assert tg.prizes["8"] == ("16",)
    text = market.format_draw(draws[0])
    assert text.startswith("🎰 Xổ số miền Nam - Chủ Nhật 25/10/2026")
    assert "[Kiên Giang]" in text and "G7: 222" in text and "G8: 37" in text


def test_parse_lottery_mien_bac_khong_ten_tinh():
    draw = market.parse_lottery_rss(XSMB_RSS, "mb", TODAY)[0]
    assert draw.provinces[0].name == "Miền Bắc"
    assert draw.provinces[0].prizes["7"] == ("52", "94", "82", "67")
    assert "[" not in market.format_draw(draw)


def test_ngay_lay_tu_tieu_de_khi_link_khong_co_nam():
    rss = XSMB_RSS.replace(b"ngay-25-10-2026", b"latest").replace(b"25/10", b"31/12")
    draw = market.parse_lottery_rss(rss, "mb", dt.date(2027, 1, 1))[0]
    assert draw.date == dt.date(2026, 12, 31)


@pytest.mark.parametrize(
    "ticket,expected",
    [
        ("729510", ["ĐB"]),
        ("129510", ["PHU_DB"]),  # chỉ sai chữ số đầu
        ("723510", ["KK"]),  # đúng chữ số đầu, sai đúng 1 hàng
        ("999937", ["8"]),
        ("912222", ["7"]),  # 3 số cuối trùng giải 7 "222"
        ("021803", ["4"]),
        ("000000", []),
    ],
)
def test_do_ve_mien_nam(ticket, expected):
    kg = market.parse_lottery_rss(XSMN_RSS, "mn", TODAY)[0].provinces[0]
    assert market.check_ticket(ticket, kg.prizes, "mn") == expected


def test_do_ve_mien_bac():
    prizes = market.parse_lottery_rss(XSMB_RSS, "mb", TODAY)[0].provinces[0].prizes
    assert market.check_ticket("30328", prizes, "mb") == ["ĐB"]
    assert market.check_ticket("11128", prizes, "mb") == ["KK"]  # 2 số cuối ĐB
    assert market.check_ticket("00052", prizes, "mb") == ["7"]


def test_format_ticket():
    draw = market.parse_lottery_rss(XSMN_RSS, "mn", TODAY)[0]
    text = market.format_ticket("729510", draw)
    assert "• Kiên Giang: 🎉 TRÚNG Giải Đặc biệt (2 tỷ)" in text
    assert "• Tiền Giang: không trúng" in text and "tham khảo" in text
    only = market.format_ticket("000016", draw, "vé tiền giang 000016")
    assert "Kiên Giang" not in only and "Tiền Giang: 🎉 TRÚNG Giải Tám" in only
    assert "có 6 chữ số" in market.format_ticket("12345", draw)
    mb = market.parse_lottery_rss(XSMB_RSS, "mb", TODAY)[0]
    assert "cần khớp thêm ký hiệu vé" in market.format_ticket("30328", mb)


# ─── Tải qua HTTP giả lập + cache ───────────────────────────────────────────


@pytest.fixture
def mock_http(monkeypatch):
    market.clear_cache()
    sent: list[httpx.Request] = []
    routes: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return routes[request.url.host](request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_client, "get_client", lambda: client)
    yield sent, routes
    market.clear_cache()


async def test_get_fx_cache_trong_ttl(mock_http):
    sent, routes = mock_http
    routes["portal.vietcombank.com.vn"] = lambda r: httpx.Response(200, content=FX_XML)
    first = await market.get_fx()
    second = await market.get_fx()
    assert first is second and len(sent) == 1


async def test_get_gold_post_form(mock_http):
    sent, routes = mock_http
    routes["sjc.com.vn"] = lambda r: httpx.Response(200, content=SJC_JSON)
    table = await market.get_gold()
    assert table.rows[0].name.startswith("Vàng SJC")
    assert sent[0].method == "POST"
    assert b"method=GetSJCGoldPriceByDate" in sent[0].content


async def test_nguon_loi_cache_loi_ngan(mock_http):
    sent, routes = mock_http
    routes["sjc.com.vn"] = lambda r: httpx.Response(403)
    with pytest.raises(market.MarketDataError):
        await market.get_gold()
    with pytest.raises(market.MarketDataError):
        await market.get_gold()
    assert len(sent) == 1  # lỗi được nhớ 60s, không gọi dồn


async def test_phan_hoi_qua_lon_bi_chan(mock_http):
    _sent, routes = mock_http
    routes["xskt.com.vn"] = lambda r: httpx.Response(200, content=b"x" * (market._MAX_BYTES + 1))
    with pytest.raises(market.MarketDataError):
        await market.get_lottery("mn")


async def test_get_lottery(mock_http):
    sent, routes = mock_http
    routes["xskt.com.vn"] = lambda r: httpx.Response(200, content=XSMN_RSS)
    draws = await market.get_lottery("mn")
    assert market.pick_draw(draws, None).date == dt.date(2026, 10, 25)
    assert market.pick_draw(draws, dt.date(2026, 10, 24)).provinces[0].name == "Hồ Chí Minh"
    assert market.pick_draw(draws, dt.date(2026, 10, 1)) is None
    assert sent[0].url.path == "/rss-feed/mien-nam-xsmn.rss"
    with pytest.raises(market.MarketDataError):
        await market.get_lottery("xx")
