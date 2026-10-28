"""Test services/vn_daily_chat.py: giá vàng/tỷ giá/xổ số trả lời bằng code, grounding."""

import datetime as dt

import pytest

from _vn_data_samples import (
    FUEL_HTML,
    FX_XML,
    SJC_JSON,
    VANG_TODAY_JSON,
    XSMB_RSS,
    XSMN_RSS,
    XSTN_RSS,
)
from services import fuel_prices
from services import vn_daily_chat as daily
from services import vn_market_data as market

TODAY = dt.date(2026, 10, 26)


@pytest.fixture
def sources(monkeypatch):
    """Nguồn giả: đếm lượt gọi; ``down`` chứa tên nguồn đang lỗi."""
    state = {"calls": [], "down": set()}

    def make(name, value):
        async def fake(*args):
            state["calls"].append((name, *args))
            if name in state["down"]:
                raise market.MarketDataError(f"{name} lỗi")
            return value(*args) if callable(value) else value

        return fake

    lottery = {
        "mn": market.parse_lottery_rss(XSMN_RSS, "mn", TODAY),
        "mb": market.parse_lottery_rss(XSMB_RSS, "mb", TODAY),
    }
    tay_ninh = market.parse_lottery_rss(XSTN_RSS, "mn", TODAY, name="Tây Ninh")

    def province_feed(province):
        # Chỉ có mẫu feed riêng Tây Ninh; đài khác giả lập feed tỉnh lỗi -> lọc feed miền.
        if province.key != "tay-ninh":
            raise market.MarketDataError("feed tỉnh lỗi")
        return tay_ninh

    monkeypatch.setattr(market, "get_gold", make("gold", market.parse_sjc_json(SJC_JSON)))
    monkeypatch.setattr(
        market, "get_gold_live", make("gold_live", market.parse_vang_today_json(VANG_TODAY_JSON))
    )
    monkeypatch.setattr(market, "get_fx", make("fx", market.parse_fx_xml(FX_XML)))
    monkeypatch.setattr(market, "get_lottery", make("xs", lambda region: lottery[region]))
    monkeypatch.setattr(market, "get_province_lottery", make("xst", province_feed))
    monkeypatch.setattr(
        fuel_prices, "get_fuel", make("fuel", fuel_prices.parse_fuel_html(FUEL_HTML))
    )
    monkeypatch.setattr(daily, "_today", lambda: TODAY)
    return state


@pytest.mark.parametrize(
    "text",
    [
        "giá vàng hôm nay",
        "giá vàng SJC bao nhiêu vậy em",
        "vàng nhẫn bao nhiêu 1 chỉ",
    ],
)
async def test_gia_vang_gon_tra_thang(sources, text):
    reply = await daily.maybe_handle(text)
    assert reply.startswith("🪙 Giá vàng") and "vang.today" in reply
    assert "bán 141,7" in reply or "bán 139" in reply  # SJC miếng hoặc nhẫn SJC
    assert sources["calls"][0] == ("gold_live",) and ("gold",) not in sources["calls"]


async def test_vang_hoi_theo_chi(sources):
    reply = await daily.maybe_handle("vàng nhẫn bao nhiêu 1 chỉ")
    assert "1 chỉ = 1/10 lượng: Nhẫn SJC 9999 bán ≈ 13,9 triệu/chỉ." in reply
    assert "DOJI" not in reply  # hỏi vàng nhẫn: chỉ dòng nhẫn


@pytest.mark.parametrize(
    "text,expected",
    [
        ("tỷ giá đô hôm nay", "• USD: mua TM 25.620"),
        ("ty gia usd", "• USD:"),
        ("100 đô bằng bao nhiêu tiền việt", "💱 100 USD ≈ 2.565.000 đ"),
        ("1 triệu đồng đổi được bao nhiêu đô", "💱 1.000.000 đ ≈ 38,42 USD"),
        ("tỷ giá hôm nay", "• EUR:"),
    ],
)
async def test_ty_gia_gon_tra_thang(sources, text, expected):
    reply = await daily.maybe_handle(text)
    assert reply is not None and expected in reply


@pytest.mark.parametrize(
    "text",
    [
        "giá vàng hôm nay có nên mua không",  # hỏi ý kiến -> AI + grounding
        "bitcoin bao nhiêu usd",
        "nhiệt độ hôm nay bao nhiêu độ",
        "anh yên tâm nha",
        "xin chào em",
        "em nói về số liệu kinh tế đi",  # bỏ dấu "về số" = "vé số"
        "nước lũ đổ về số đông hộ dân",
    ],
)
async def test_khong_phai_cau_hoi_gon_thi_none(sources, text):
    assert await daily.maybe_handle(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Vâng, bây giờ ạ",  # bỏ dấu "vâng" = "vang"
        "vâng hôm nay em bận",
        "Vâng nhắn em nha",  # "nhắn" bỏ dấu = "nhẫn"
        "lớp vắng hôm nay nhiều quá",
        "rượu vang bao nhiêu tiền 1 chai",
        "ruou vang bao nhieu",
        "vâng, bây giờ tin tức về bão thế nào",
    ],
)
def test_vang_vang_vang_khong_phai_hoi_gia_vang(text):
    assert daily.wants_gold(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "giá vàng hôm nay",
        "vàng nhẫn bao nhiêu 1 chỉ",
        "vang hom nay",  # gõ không dấu cả câu
        "gia vang sjc",
        "giá vàng SJC bao nhiêu vậy em",
        "vàng miếng bây giờ",
    ],
)
def test_hoi_gia_vang_van_nhan_ra(text):
    assert daily.wants_gold(text) is True


async def test_vang_vang_khong_tra_bang_gia_vang(sources):
    assert await daily.maybe_handle("Vâng, bây giờ ạ") is None
    assert await daily.maybe_handle("vâng hôm nay em bận") is None
    g = await daily.grounding_for("rượu vang bao nhiêu tiền 1 chai")
    assert g == daily.DailyGrounding()
    assert sources["calls"] == []


@pytest.mark.parametrize(
    "text",
    [
        "anh chuyển về 500000 cho em rồi nha",  # bỏ dấu "về" = "vé"
        "tiền điện tháng này về 350000 à",
        "em giải thích về số hôm qua anh gửi đi",
        "con về 12345 lần rồi",
        "vé 200000 đồng một người",  # giá vé, không phải số vé
    ],
)
async def test_ve_so_khong_nham_ve(sources, text):
    assert daily.parse_lottery(text) is None
    assert await daily.maybe_handle(text) is None
    assert sources["calls"] == []


@pytest.mark.parametrize(
    "text",
    [
        "dò vé 729510",
        "do ve so 729510 giup anh",
        "ve so mien nam",
        "vé 30328 có trúng không",
        "xổ số miền nam",
        "vé số hôm qua",
        "dò vé 123456 đồng nai",
        "vé 123456 đồng tháp",
    ],
)
def test_ve_so_van_nhan_ra(text):
    assert daily.parse_lottery(text) is not None


async def test_nguon_vang_loi_thi_none_de_tra_web(sources):
    sources["down"].add("gold_live")
    # vang.today lỗi: rơi về bảng SJC cũ.
    assert (await daily.maybe_handle("giá vàng hôm nay")).startswith("🪙 Giá vàng SJC")
    # SJC không có DOJI/thế giới: để tra web ("tham khảo"), không trả bảng thiếu.
    assert await daily.maybe_handle("giá vàng doji hôm nay") is None
    assert await daily.maybe_handle("vàng thế giới hôm nay") is None
    sources["down"].add("gold")
    assert await daily.maybe_handle("giá vàng hôm nay") is None


async def test_xo_so_ket_qua_va_loc_tinh(sources):
    reply = await daily.maybe_handle("kết quả xổ số miền nam")
    assert reply.startswith("🎰 Xổ số miền Nam - Chủ Nhật 25/10/2026")
    assert "[Kiên Giang]" in reply and "[Tiền Giang]" in reply
    only = await daily.maybe_handle("xổ số Tiền Giang hôm qua")
    assert "[Tiền Giang]" in only and "Kiên Giang" not in only
    assert sources["calls"][-1] == ("xs", "mn")


async def test_xo_so_hom_nay_chua_quay(sources):
    reply = await daily.maybe_handle("xổ số miền nam hôm nay")
    assert reply.startswith("⏳ Hôm nay xổ số miền Nam chưa có kết quả (quay lúc 16h15")
    assert "Kết quả gần nhất ngày 25/10" in reply and "[Kiên Giang]" in reply
    # Dò vé đúng hôm nay mà chưa có kết quả: không dò nhầm kỳ trước.
    ticket = await daily.maybe_handle("dò vé 729510 hôm nay")
    assert "chưa có kết quả" in ticket and "TRÚNG" not in ticket


async def test_do_ve_bang_code(sources):
    reply = await daily.maybe_handle("dò vé 729510")
    assert "Kiên Giang: 🎉 TRÚNG Giải Đặc biệt" in reply
    assert "TRÚNG" in await daily.maybe_handle("do ve so 729510 giup anh")  # gõ không dấu
    assert "[Kiên Giang]" in await daily.maybe_handle("ve so mien nam")
    mb = await daily.maybe_handle("vé 30328 có trúng không")  # 5 số -> miền Bắc
    assert "Giải Đặc biệt" in mb and sources["calls"][-1] == ("xs", "mb")


async def test_do_ve_dai_duoc_nhac_tim_dung_ky(sources):
    # Kỳ mới nhất (25/10) không có đài HCM: tìm kỳ 24/10 có đài đó, kể cả tên khác.
    reply = await daily.maybe_handle("dò vé 698440 sài gòn")
    assert "24/10/2026" in reply and "Hồ Chí Minh: 🎉 TRÚNG Giải Đặc biệt" in reply
    assert "Kiên Giang" not in reply
    only = await daily.maybe_handle("xổ số hồ chí minh")
    assert "[Hồ Chí Minh]" in only and "Kiên Giang" not in only


@pytest.mark.parametrize("text", ["dò vé 123456 vĩnh long", "dò vé 123456 vĩnh long hôm qua"])
async def test_do_ve_dai_khong_co_trong_ky_khong_phan_trung_truot(sources, text):
    reply = await daily.maybe_handle(text)
    assert "Vĩnh Long" in reply
    assert "chưa trúng" not in reply and "không trúng" not in reply and "TRÚNG" not in reply


async def test_ky_hom_nay_chua_du_dai_thi_khong_ket_luan_truot(sources, monkeypatch):
    def draw(day, *names):
        provinces = tuple(market.ProvinceResult(n, {"ĐB": ("111111",)}) for n in names)
        return market.LotteryDraw("mn", day, provinces)

    draws = [
        draw(TODAY, "TP. HCM"),  # đang quay: mới có 1 đài
        draw(TODAY - dt.timedelta(days=7), "TP. HCM", "Đồng Tháp", "Cà Mau"),
    ]

    async def fake(region):
        return draws

    monkeypatch.setattr(market, "get_lottery", fake)
    reply = await daily.maybe_handle("dò vé 123456 hôm nay")
    assert "TP. HCM: không trúng" in reply and "Tiếc ghê" not in reply
    assert "1/3 đài" in reply
    result = await daily.maybe_handle("xổ số miền nam hôm nay")
    assert "1/3 đài" in result and "[TP. HCM]" in result
    # Đài được nhắc chưa có trong kỳ hôm nay: báo chưa có kết quả, không phán.
    reply = await daily.maybe_handle("dò vé 123456 đồng tháp hôm nay")
    assert "Đồng Tháp" in reply and "chưa có kết quả" in reply and "trúng" not in reply
    # Tên kiểu "TP. HCM" trên nguồn vẫn khớp "sài gòn".
    assert "TP. HCM: không trúng" in await daily.maybe_handle("dò vé 123456 sài gòn hôm nay")


async def test_do_ve_nguon_loi_bao_that_khong_doan(sources):
    sources["down"].add("xs")
    assert await daily.maybe_handle("dò vé 729510") == daily.lottery_down()
    # Hỏi kết quả (không dò vé) -> None để đi tra web thật.
    assert await daily.maybe_handle("xổ số miền bắc hôm qua") is None


async def test_xo_so_hoi_kien_thuc_de_ai(sources):
    assert await daily.maybe_handle("xổ số kiến thiết là gì") is None
    assert sources["calls"] == []


async def test_cau_khai_bao_noi_o_luu_thanh_pho(monkeypatch, sources):
    saved = {}

    async def fake_set(key, value):
        saved[key] = value

    from services import weather_home

    weather_home.clear_cache()
    monkeypatch.setattr(weather_home.db, "set_setting", fake_set)
    reply = await daily.maybe_handle("nhà anh ở Hà Nội nha", user_id=-3)
    assert reply.startswith("📍 Dạ em nhớ rồi: thời tiết mặc định của anh là Hà Nội")
    assert saved == {"weather_home:-3": "Hà Nội"}
    weather_home.clear_cache()


async def test_cau_code_viet_theo_cach_xung_ho_rieng(sources, monkeypatch):
    from services import reply_audience, weather_home

    async def fake_set(key, value):
        return None

    weather_home.clear_cache()
    monkeypatch.setattr(weather_home.db, "set_setting", fake_set)
    with reply_audience.use(reply_audience.ReplyAudience(address_form="cô")):
        reply = await daily.maybe_handle("dò vé 123456")
        assert "vé chưa trúng rồi cô" in reply and "cháu dò bằng code" in reply
        assert " anh" not in reply and " em " not in reply
        home = await daily.maybe_handle("mình ở Hà Nội", user_id=-3)
        assert home.startswith("📍 Dạ cháu nhớ rồi: thời tiết mặc định của cô là Hà Nội")
        sources["down"].add("xs")
        down = await daily.maybe_handle("dò vé 123456")
        assert "cô thử lại" in down and " anh" not in down and "Cháu không đoán" in down
    weather_home.clear_cache()


# ─── grounding_for ──────────────────────────────────────────────────────────


async def test_grounding_vang_du_so_lieu_bo_tra_web(sources):
    g = await daily.grounding_for("giá vàng hôm nay có nên mua không")
    assert g.skip_search and not g.force_real_search
    assert "Giá vàng thật vừa tra bằng code" in g.text and "141,7" in g.text


async def test_grounding_con_hoi_thu_khac_van_tra_web(sources):
    g = await daily.grounding_for("giá vàng với giá bitcoin hôm nay thế nào")
    assert not g.skip_search and "Giá vàng thật" in g.text
    # vang.today có XAU/USD: câu hỏi vàng thế giới đủ số liệu code, bỏ tra web.
    g = await daily.grounding_for("giá vàng thế giới tăng không")
    assert g.skip_search and "XAU/USD" in g.text
    sources["down"].add("gold_live")  # chỉ còn bảng SJC: phần thế giới vẫn phải tra web
    g = await daily.grounding_for("giá vàng thế giới tăng không")
    assert not g.skip_search


@pytest.mark.parametrize(
    "text",
    [
        "tin tức thủ đô hôm nay có gì",  # "đô" trong "thủ đô" không phải USD
        "giá nhà ở thủ đô bao nhiêu",
        "đô thị bây giờ đông quá",
        "bánh kinh đô bao nhiêu",
    ],
)
async def test_thu_do_do_thi_khong_phai_ty_gia(sources, text):
    assert daily.parse_fx(text) is None
    assert await daily.grounding_for(text) == daily.DailyGrounding()


async def test_grounding_cau_hon_hop_van_tra_web(sources):
    # Phần bão không có trong số liệu code -> giữ tra web, vẫn kèm giá SJC thật.
    g = await daily.grounding_for("giá vàng hôm nay và tình hình bão ở miền Trung thế nào")
    assert not g.skip_search and "Giá vàng thật" in g.text
    # Câu chỉ hỏi ý kiến/xu hướng quanh con số đã có -> bỏ tra web như cũ.
    g = await daily.grounding_for("tỷ giá usd hôm nay tăng hay giảm")
    assert g.skip_search and "Tỷ giá Vietcombank thật" in g.text
    g = await daily.grounding_for("vàng nhẫn bây giờ có nên đầu tư tiếp không")
    assert g.skip_search


async def test_grounding_nguon_loi_ghi_tham_khao(sources):
    sources["down"].add("fx")
    g = await daily.grounding_for("tỷ giá usd hôm nay tăng hay giảm")
    assert not g.skip_search and not g.force_real_search
    assert "THAM KHẢO" in g.text and "Vietcombank" in g.text


async def test_grounding_xo_so_loi_bat_tra_web_that(sources):
    sources["down"].add("xs")
    g = await daily.grounding_for("xổ số miền nam hôm qua")
    assert g.force_real_search and not g.skip_search
    assert "TUYỆT ĐỐI không nêu" in g.text and "THAM KHẢO" in g.text


async def test_grounding_xo_so_hoi_kien_thuc_cam_neu_so(sources):
    g = await daily.grounding_for("có nên mua vé số không")
    assert "TUYỆT ĐỐI không nêu" in g.text and not g.force_real_search
    assert sources["calls"] == []


async def test_grounding_cau_thuong_rong(sources):
    assert await daily.grounding_for("hôm nay em khoẻ không") == daily.DailyGrounding()
    assert sources["calls"] == []


# ─── Tích hợp pipeline chat ─────────────────────────────────────────────────


class _Resp:
    text = "dạ"
    used_fallback = False


@pytest.fixture
def chat_spy(monkeypatch):
    from services import channel_chat_service as ccs

    seen = {"search": 0}

    async def fake_start(*args, **kwargs):
        return 1

    async def noop(*args, **kwargs):
        return None

    async def empty_memory(*args, **kwargs):
        return ccs.memory_service.TurnMemory()

    async def fake_plan(text, user_id=None):
        seen["search"] += 1
        return ccs.web_search.SearchPlan("WEB", ccs.orchestrator.SEARCH_OFF, True)

    async def fake_chat(user_id, text, *, grounding, memory_context, require_real_search, **kw):
        seen.update(grounding=grounding, require_real_search=require_real_search, **kw)
        return _Resp()

    monkeypatch.setattr(ccs.telemetry, "start", fake_start)
    monkeypatch.setattr(ccs.telemetry, "success", noop)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", noop)
    monkeypatch.setattr(ccs.web_search, "plan_search", fake_plan)
    monkeypatch.setattr(ccs.memory_service, "build_turn_memory", empty_memory)
    monkeypatch.setattr(ccs.memory_service, "after_exchange", lambda *a, **k: None)
    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.conversation_log.db, "add_chat_turn", noop)
    return ccs, seen


async def test_run_chat_turn_dung_so_lieu_code_bo_web_va_forced_search(sources, chat_spy):
    ccs, seen = chat_spy
    await ccs.run_chat_turn(1, "giá vàng hôm nay có nên mua không", channel="zalo")
    await ccs.stop_background_tasks()
    assert seen["search"] == 0 and seen["require_real_search"] is False
    # Số liệu code đã đủ: provider cũng không tự tra web chồng lên.
    assert "Giá vàng thật" in seen["grounding"] and seen["search_mode"] == "off"


async def test_run_chat_turn_nguon_loi_giu_duong_tra_web(sources, chat_spy):
    ccs, seen = chat_spy
    sources["down"].update({"gold", "gold_live"})
    await ccs.run_chat_turn(1, "giá vàng hôm nay có nên mua không", channel="zalo")
    await ccs.stop_background_tasks()
    assert seen["search"] == 1 and seen["require_real_search"] is True
    assert seen["search_mode"] == "forced"
    assert "THAM KHẢO" in seen["grounding"] and "WEB" in seen["grounding"]


async def test_run_chat_turn_xo_so_loi_ep_tra_web_that(sources, chat_spy):
    ccs, seen = chat_spy
    sources["down"].add("xs")
    await ccs.run_chat_turn(1, "xổ số miền nam hôm qua", channel="zalo")
    await ccs.stop_background_tasks()
    assert seen["require_real_search"] is True


async def test_kenh_zalo_gia_vang_khong_goi_ai(sources, monkeypatch):
    from services import channel_chat_service as ccs

    async def no_command(*args, **kwargs):
        return None

    async def boom(*args, **kwargs):
        raise AssertionError("không được gọi AI cho câu giá vàng gọn")

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", no_command)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", no_command)
    monkeypatch.setattr(ccs.orchestrator, "chat", boom)
    monkeypatch.setattr(ccs, "_handle_stock", boom)

    result = await ccs.handle_channel_text(-5, "giá vàng hôm nay", is_admin=False)

    assert result.messages[0].startswith("🪙 Giá vàng hôm nay")


# ─── Xổ số theo tỉnh (feed riêng từng đài) ──────────────────────────────────


@pytest.mark.parametrize(
    "text,key,day",
    [
        ("xổ số tây ninh hôm nay", "tay-ninh", TODAY),
        ("kết quả đà lạt chủ nhật", "lam-dong", dt.date(2026, 10, 25)),
        ("kết quả xổ số lâm đồng", "lam-dong", None),
        ("dò vé 123456 đồng nai", "dong-nai", None),
        ("xstn thứ 5", "tay-ninh", dt.date(2026, 10, 22)),
        ("xổ số huế chủ nhật tuần trước", "thua-thien-hue", dt.date(2026, 10, 18)),
        ("xổ số mn đà nẵng", "da-nang", None),  # tên đài thắng tên miền
        ("kq sài gòn hôm qua", "ho-chi-minh", dt.date(2026, 10, 25)),
    ],
)
def test_nhan_dien_dai_va_ngay(text, key, day):
    q = daily.parse_lottery(text, TODAY)
    assert q is not None and q.province == key and q.day == day


@pytest.mark.parametrize(
    "text",
    ["kết quả bóng đá đà nẵng tối qua", "kết quả thi tây ninh", "đà lạt hôm nay đẹp không"],
)
def test_ten_tinh_khong_kem_xo_so_khong_bat_nham(text):
    assert daily.parse_lottery(text, TODAY) is None


def test_mien_bac_ha_noi_van_la_ky_chung():
    q = daily.parse_lottery("xổ số hà nội hôm qua", TODAY)
    assert q.region == "mb" and q.province == ""


async def test_xo_so_tay_ninh_feed_rieng(sources):
    reply = await daily.maybe_handle("xổ số tây ninh")
    assert reply.startswith("🎰 Xổ số Tây Ninh (miền Nam) - Thứ Năm 22/10/2026")
    assert ("xst", daily.provinces.BY_KEY["tay-ninh"]) in sources["calls"]
    assert ("xs", "mn") not in sources["calls"]  # không đọc feed miền
    # Hôm nay đài không quay: nói lịch quay, đưa kỳ gần nhất, dò vé đúng kỳ đó.
    today = await daily.maybe_handle("dò vé 058734 tây ninh hôm nay")
    assert "Hôm nay đài Tây Ninh không quay. Đài Tây Ninh quay thứ năm lúc 16h15." in today
    assert "dò theo kỳ gần nhất (22/10)" in today and "TRÚNG Giải Đặc biệt" in today
    lost = await daily.maybe_handle("dò vé 000000 tây ninh 15/10")
    assert "15/10/2026" in lost and "Tây Ninh: không trúng" in lost
    old = await daily.maybe_handle("xổ số tây ninh 1/10")
    assert "chưa có kết quả đài Tây Ninh ngày 01/10/2026" in old


async def test_dai_quay_hom_nay_nhung_chua_co_ket_qua(sources, monkeypatch):
    thursday = dt.date(2026, 10, 29)
    monkeypatch.setattr(daily, "_today", lambda: thursday)
    reply = await daily.maybe_handle("dò vé 123456 tây ninh hôm nay")
    assert "Đài Tây Ninh hôm nay quay lúc 16h15, nguồn chưa có kết quả" in reply
    assert "trúng" not in reply.lower().replace("chưa có", "")
    result = await daily.maybe_handle("xổ số tây ninh hôm nay")
    assert "Kỳ gần nhất ngày 22/10" in result and "058734" in result


async def test_feed_tinh_loi_loc_tu_feed_mien(sources):
    # Feed riêng Kiên Giang "lỗi" trong fixture -> lấy đúng đài đó từ feed miền Nam.
    reply = await daily.maybe_handle("xổ số kiên giang")
    assert "Kiên Giang (miền Nam) - Chủ Nhật 25/10/2026" in reply and "Tiền Giang" not in reply
    sources["down"].add("xs")
    assert await daily.maybe_handle("xổ số kiên giang") is None  # cả 2 lỗi -> tra web thật
    assert await daily.maybe_handle("dò vé 729510 kiên giang") == daily.lottery_down()


def test_do_ve_ky_khong_co_dai_duoc_nhac_khong_ket_luan():
    draw = market.parse_lottery_rss(XSMN_RSS, "mn", TODAY)[0]  # Kiên Giang + Tiền Giang
    text = market.format_ticket("123456", draw, "dò vé 123456 đồng nai")
    assert "không có đài Đồng Nai" in text and "trúng" not in text


# ─── Giá vàng nhiều thương hiệu ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,brands",
    [
        ("giá vàng doji hôm nay", {"doji"}),
        ("vàng pnj bao nhiêu", {"pnj"}),
        ("giá vàng bảo tín minh châu", {"baotin"}),
        ("vàng thế giới hôm nay", {"world"}),
        ("giá vàng nhẫn sjc", {"nhan"}),
        ("giá vàng sjc", {"sjc"}),
        ("giá vàng hôm nay", set()),
    ],
)
def test_loc_thuong_hieu_vang(text, brands):
    assert daily.wants_gold(text) and daily.gold_brands(text) == brands


@pytest.mark.parametrize("text", ["FPT xuất hiện nến doji", "PNJ có nên mua không"])
def test_doji_pnj_co_phieu_khong_phai_gia_vang(text):
    assert daily.wants_gold(text) is False


async def test_vang_doji_va_the_gioi_tra_thang(sources):
    doji = await daily.maybe_handle("giá vàng doji hôm nay")
    assert "DOJI Hà Nội" in doji and "DOJI nữ trang" in doji and "PNJ" not in doji
    world = await daily.maybe_handle("vàng thế giới hôm nay")
    assert "XAU/USD: 4.015,1 USD/oz" in world and "≈ 126,01 triệu/lượng" in world


# ─── Xăng dầu ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,kind",
    [
        ("giá xăng hôm nay", "xang"),
        ("dầu diesel bao nhiêu", "diesel"),
        ("gia xang hom nay bao nhieu", "xang"),
        ("giá xăng dầu hôm nay", None),
        ("xăng e5 bao nhiêu 1 lít", "xang"),
        ("giá dầu hỏa hôm nay", "dau_hoa"),
        ("xăng tăng giá chưa em", "xang"),
    ],
)
def test_nhan_dien_gia_xang(text, kind):
    q = daily.parse_fuel(text)
    assert q is not None and q.kind == kind and q.narrow


@pytest.mark.parametrize(
    "text",
    [
        "đổ xăng 100k",
        "hết xăng rồi",
        "giá dầu hôm nay",  # mơ hồ: dầu thô thế giới hay dầu ăn
        "dầu ăn bao nhiêu",
        "anh đi đổ xăng đây",
        "cái đầu đó bao nhiêu tuổi",
    ],
)
def test_khong_bat_nham_xang(text):
    assert daily.parse_fuel(text) is None


async def test_gia_xang_tra_thang_khong_ai(sources):
    reply = await daily.maybe_handle("giá xăng hôm nay")
    assert reply.startswith("⛽ Giá xăng dầu Petrolimex") and "25.060" in reply
    assert "Dầu hỏa" not in reply  # hỏi xăng: chỉ dòng xăng
    diesel = await daily.maybe_handle("dầu diesel bao nhiêu")
    assert "DO 0,05S Mức 2: vùng 1 29.250" in diesel and "Xăng" not in diesel
    # Giá xăng thế giới: code không có -> để AI kèm grounding + tra web.
    assert await daily.maybe_handle("giá xăng thế giới hôm nay") is None
    sources["down"].add("fuel")
    assert await daily.maybe_handle("giá xăng hôm nay") is None


async def test_grounding_xang(sources):
    g = await daily.grounding_for("giá xăng hôm nay tăng hay giảm")
    assert g.skip_search and "Giá xăng dầu bán lẻ trong nước thật" in g.text
    g = await daily.grounding_for("giá xăng thế giới hôm nay thế nào")
    assert not g.skip_search and "25.060" in g.text
    sources["down"].add("fuel")
    g = await daily.grounding_for("giá xăng hôm nay có tăng không")
    assert not g.skip_search and "THAM KHẢO" in g.text


async def test_hoi_lich_quay_cua_dai(sources):
    reply = await daily.maybe_handle("xổ số tây ninh quay thứ mấy")
    assert reply.startswith("📅 Đài Tây Ninh quay thứ năm lúc 16h15.") and "058734" in reply
    g = await daily.grounding_for("lịch quay xổ số tây ninh")
    assert g.skip_search and "Đài Tây Ninh quay thứ năm" in g.text


def test_hom_nay_dai_nao_quay_theo_lich_co_dinh():
    reply = daily.stations_text("hôm nay đài nào quay", TODAY)  # Thứ Hai 26/10
    assert reply.startswith("🎰 Hôm nay (Thứ Hai 26/10) quay:")
    assert "• Miền Nam (16h15): TP.HCM, Đồng Tháp, Cà Mau" in reply
    assert "• Miền Bắc (18h15): quay hằng ngày" in reply
    thu5 = daily.stations_text("thứ 5 miền nam đài nào quay", TODAY)
    assert thu5.startswith("🎰 Thứ Năm 29/10 quay:") and "Tây Ninh" in thu5
    assert "Miền Trung" not in thu5
    assert daily.stations_text("đài nào hay nhất", TODAY) is None
