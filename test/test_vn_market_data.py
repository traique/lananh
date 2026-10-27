"""Test services/vn_market_data.py với dữ liệu mẫu ghi lại từ nguồn thật, không gọi mạng."""

import datetime as dt

import httpx
import pytest

from services import fuel_prices, http_client
from services import lottery_provinces as provinces
from services import vn_market_data as market
from _vn_data_samples import FX_XML, SJC_JSON, XSMB_RSS, XSMN_RSS  # noqa: E402
from _vn_data_samples import FUEL_HTML, VANG_TODAY_JSON, XSTN_RSS  # noqa: E402

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


async def test_get_province_lottery_dung_feed_rieng(mock_http):
    sent, routes = mock_http
    routes["xskt.com.vn"] = lambda r: httpx.Response(200, content=XSTN_RSS)
    draws = await market.get_province_lottery(provinces.BY_KEY["tay-ninh"])
    assert sent[0].url.path == "/rss-feed/tay-ninh-xstn.rss"
    assert [d.date for d in draws] == [dt.date(2026, 10, 22), dt.date(2026, 10, 15)]
    assert draws[0].provinces[0].name == "Tây Ninh"
    await market.get_province_lottery(provinces.BY_KEY["tay-ninh"])
    assert len(sent) == 1  # cache theo đài
    # Miền Bắc: 1 kỳ chung -> feed miền.
    routes["xskt.com.vn"] = lambda r: httpx.Response(200, content=XSMB_RSS)
    await market.get_province_lottery(provinces.BY_KEY["mien-bac"])
    assert sent[-1].url.path == "/rss-feed/mien-bac-xsmb.rss"
    fake = provinces.Province("x", "X", "mn", (0,), "evil-xs")
    with pytest.raises(market.MarketDataError):
        await market.get_province_lottery(fake)  # chỉ đài trong danh mục


async def test_cache_co_tran_so_khoa(mock_http, monkeypatch):
    _sent, routes = mock_http
    routes["xskt.com.vn"] = lambda r: httpx.Response(200, content=XSTN_RSS)
    monkeypatch.setattr(market, "_MAX_CACHE_KEYS", 3)
    for p in provinces.PROVINCES[:6]:
        await market.get_province_lottery(p)
    assert len(market._cache) == 3 and len(market._locks) <= 3


def test_feed_tinh_dinh_dang_nhu_mien_bac_va_bo_ky_ngay_la():
    draws = market.parse_lottery_rss(XSTN_RSS, "mn", dt.date(2026, 10, 27), name="Tây Ninh")
    assert len(draws) == 2  # kỳ "26-1-2007" lẫn trong feed bị bỏ
    prizes = draws[0].provinces[0].prizes
    assert prizes["ĐB"] == ("058734",) and prizes["7"] == ("133",) and prizes["8"] == ("59",)
    assert market.check_ticket("000059", prizes, "mn") == ["8"]
    assert market.format_draw(draws[0]).startswith("🎰 Xổ số Tây Ninh (miền Nam) - Thứ Năm")


def test_danh_muc_dai_va_lich_quay():
    assert len(provinces.PROVINCES) == 36 and len(provinces.in_region("mt")) == 14
    feeds = {p.feed for p in provinces.PROVINCES}
    assert "tay-ninh-xstn" in feeds and "thua-thien-hue-xstth" in feeds and len(feeds) == 36
    assert provinces.match("xổ số Đà Lạt").key == "lam-dong"
    assert (
        provinces.match("Sài Gòn") is provinces.match("TP. HCM") is provinces.BY_KEY["ho-chi-minh"]
    )
    assert provinces.match("vũng tàu").key == "vung-tau" and provinces.match("huế").region == "mt"
    assert provinces.match("xskt hôm nay") is None  # "xổ số kiến thiết", không phải Kon Tum
    monday = dt.date(2026, 10, 26)
    names = {p.name for p in provinces.drawing_on(monday, "mn")}
    assert names == {"TP.HCM", "Đồng Tháp", "Cà Mau"}
    assert {p.name for p in provinces.drawing_on(dt.date(2026, 10, 25), "mt")} == {
        "Thừa Thiên Huế",
        "Khánh Hòa",
        "Kon Tum",
    }
    assert provinces.schedule_text(provinces.BY_KEY["thua-thien-hue"]) == "thứ hai và Chủ Nhật"
    assert provinces.same("Lâm Đồng", provinces.BY_KEY["lam-dong"])
    assert provinces.get("tay-ninh") is provinces.get("Tây Ninh") is provinces.BY_KEY["tay-ninh"]
    # Mỗi ngày miền Nam 3-4 đài, miền Trung 2-3 đài.
    for offset in range(7):
        day = monday + dt.timedelta(days=offset)
        assert 3 <= len(provinces.drawing_on(day, "mn")) <= 4
        assert 2 <= len(provinces.drawing_on(day, "mt")) <= 3


# ─── Giá vàng vang.today ────────────────────────────────────────────────────


def test_parse_vang_today_object_va_danh_sach():
    table = market.parse_vang_today_json(VANG_TODAY_JSON)
    assert table.updated == "07:30 27/10/2026"
    sjc = table.get("SJL1L10")
    assert (sjc.buy, sjc.sell, sjc.change_sell) == (138_700_000, 141_700_000, 500_000)
    world = table.get("XAUUSD")
    assert world.buy == 4015.1 and world.sell is None and world.currency == "USD"
    # Dạng danh sách prices[] có type_code (như tài liệu API).
    import json

    raw = json.loads(VANG_TODAY_JSON)
    listed = {**raw, "prices": [{"type_code": k, **v} for k, v in raw["prices"].items()]}
    again = market.parse_vang_today_json(json.dumps(listed).encode())
    assert [r.code for r in again.rows] == [r.code for r in table.rows]
    assert table.rows[0].code == "SJL1L10"  # theo thứ tự hiển thị, không theo nguồn


def test_parse_vang_today_loi():
    with pytest.raises(market.MarketDataError):
        market.parse_vang_today_json(b'{"success":false}')
    with pytest.raises(market.MarketDataError):
        market.parse_vang_today_json(b'{"success":true,"prices":[]}')
    with pytest.raises(market.MarketDataError):  # chỉ có thế giới, không có giá trong nước
        market.parse_vang_today_json(
            b'{"success":true,"prices":{"XAUUSD":{"buy":4000,"currency":"USD"}}}'
        )
    with pytest.raises(ValueError):
        market.parse_vang_today_json(b"<html>")


def test_dinh_dang_trieu_va_quy_doi_the_gioi():
    assert market.trieu(138_700_000) == "138,7" and market.trieu(135_000_000) == "135"
    assert market.trieu(141_750_000) == "141,75"
    # bảng giá web: luôn 2 số lẻ để các ô thẳng cột
    assert market.trieu(135_000_000, fixed=True) == "135,00"
    assert market.trieu(138_700_000, fixed=True) == "138,70"
    assert round(market.world_gold_vnd(4015.1, 26030) / 1e6, 2) == 126.01
    table = market.parse_vang_today_json(VANG_TODAY_JSON)
    head = market.gold_live_headline(table)
    assert head.startswith("Vàng SJC miếng 9999 mua 138,7 · bán 141,7 (▲0,5) triệu/lượng")
    assert "thế giới 4.015 USD/oz" in head


async def test_get_gold_live(mock_http):
    sent, routes = mock_http
    routes["www.vang.today"] = lambda r: httpx.Response(200, content=VANG_TODAY_JSON)
    table = await market.get_gold_live()
    assert table.get("PQHN24NTT").label == "PNJ 24K"
    assert str(sent[0].url) == "https://www.vang.today/api/prices"


# ─── Xăng dầu giaxanghomnay.com ─────────────────────────────────────────────


def test_parse_fuel_html_doc_theo_tieu_de_cot():
    table = fuel_prices.parse_fuel_html(FUEL_HTML)
    assert table.updated == "00:00 27/10/2026"
    names = [r.name for r in table.petrolimex]
    assert names[0] == "Xăng E5 RON 92" and "DO 0,001S Mức 5" in names and len(names) == 6
    e5 = table.petrolimex[0]
    assert (e5.change, e5.prev_change, e5.zone1, e5.zone2) == (0, -1610, 25060, 25560)
    assert table.petrolimex[4].prev_change == 450
    assert [r.zone1 for r in table.pvoil] == [24910, 29250, 30650]
    assert all(r.zone2 is None for r in table.pvoil)


def test_parse_fuel_ten_moi_va_cot_doi_cho():
    html = """<table><tr><th>Mặt hàng</th><th>Giá vùng 2</th><th>Giá vùng 1</th>
    <th>Tăng giảm kỳ trước</th></tr><tr><td>Xăng RON 100 Mới</td><td>27.500</td>
    <td>27.000</td><td>+1.200</td></tr><tr><td>Rác</td><td>abc</td><td>-</td><td>0</td></tr>
    </table><p>Giá cập nhật lúc 15:00 ngày 30/10/2026</p>""".encode()
    table = fuel_prices.parse_fuel_html(html)
    row = table.petrolimex[0]
    assert (row.name, row.zone1, row.zone2, row.prev_change) == (
        "Xăng RON 100 Mới",
        27000,
        27500,
        1200,
    )
    assert len(table.petrolimex) == 1 and table.updated == "15:00 30/10/2026"


def test_parse_fuel_loi():
    with pytest.raises(market.MarketDataError):
        fuel_prices.parse_fuel_html("<html><body>Bảo trì</body></html>".encode())
    # Chỉ có bảng thế giới (không có "kỳ trước") -> không nhận.
    with pytest.raises(market.MarketDataError):
        fuel_prices.parse_fuel_html(
            b"<table><tr><th>X\xc4\x83ng/d\xe1\xba\xa7u</th><th>Gi\xc3\xa1</th></tr>"
            b"<tr><td>WTI</td><td>25,000</td></tr></table>"
        )


async def test_get_fuel_cache_mot_gio(mock_http):
    sent, routes = mock_http
    routes["giaxanghomnay.com"] = lambda r: httpx.Response(200, content=FUEL_HTML)
    first = await fuel_prices.get_fuel()
    assert first is await fuel_prices.get_fuel() and len(sent) == 1
    routes["giaxanghomnay.com"] = lambda r: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
    market.clear_cache()
    with pytest.raises(market.MarketDataError):
        await fuel_prices.get_fuel()
