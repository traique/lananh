"""Test services/vn_daily_chat.py: giá vàng/tỷ giá/xổ số trả lời bằng code, grounding."""

import datetime as dt

import pytest

from _vn_data_samples import FX_XML, SJC_JSON, XSMB_RSS, XSMN_RSS
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
    monkeypatch.setattr(market, "get_gold", make("gold", market.parse_sjc_json(SJC_JSON)))
    monkeypatch.setattr(market, "get_fx", make("fx", market.parse_fx_xml(FX_XML)))
    monkeypatch.setattr(market, "get_lottery", make("xs", lambda region: lottery[region]))
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
    assert reply.startswith("🪙 Giá vàng SJC") and "150.000" in reply
    assert sources["calls"] == [("gold",)]


async def test_vang_hoi_theo_chi(sources):
    reply = await daily.maybe_handle("vàng nhẫn bao nhiêu 1 chỉ")
    assert "1 chỉ = 1/10 lượng: Vàng SJC 1L, 10L, 1KG bán ≈ 15.000 nghìn/chỉ." in reply


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
        "giá vàng thế giới hôm nay",  # SJC không có giá thế giới
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


async def test_nguon_vang_loi_thi_none_de_tra_web(sources):
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


async def test_do_ve_nguon_loi_bao_that_khong_doan(sources):
    sources["down"].add("xs")
    assert await daily.maybe_handle("dò vé 729510") == daily._LOTTERY_DOWN
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


# ─── grounding_for ──────────────────────────────────────────────────────────


async def test_grounding_vang_du_so_lieu_bo_tra_web(sources):
    g = await daily.grounding_for("giá vàng hôm nay có nên mua không")
    assert g.skip_search and not g.force_real_search
    assert "Giá vàng SJC thật vừa tra bằng code" in g.text and "150.000" in g.text


async def test_grounding_con_hoi_thu_khac_van_tra_web(sources):
    g = await daily.grounding_for("giá vàng với giá bitcoin hôm nay thế nào")
    assert not g.skip_search and "Giá vàng SJC" in g.text
    g = await daily.grounding_for("giá vàng thế giới tăng không")
    assert not g.skip_search


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

    async def fake_search(text, user_id=None):
        seen["search"] += 1
        return "WEB"

    async def fake_chat(user_id, text, *, grounding, memory_context, require_real_search, **kw):
        seen.update(grounding=grounding, require_real_search=require_real_search, **kw)
        return _Resp()

    monkeypatch.setattr(ccs.telemetry, "start", fake_start)
    monkeypatch.setattr(ccs.telemetry, "success", noop)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", noop)
    monkeypatch.setattr(ccs.web_search, "maybe_search", fake_search)
    monkeypatch.setattr(ccs.memory_service, "build_memory_context", noop)
    monkeypatch.setattr(ccs.memory_service, "update_memory", noop)
    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.db, "add_chat_message", noop)
    return ccs, seen


async def test_run_chat_turn_dung_so_lieu_code_bo_web_va_forced_search(sources, chat_spy):
    ccs, seen = chat_spy
    await ccs.run_chat_turn(1, "giá vàng hôm nay có nên mua không", channel="zalo")
    await ccs.stop_background_tasks()
    assert seen["search"] == 0 and seen["require_real_search"] is False
    assert "Giá vàng SJC thật" in seen["grounding"] and seen["web_grounded"] is False


async def test_run_chat_turn_nguon_loi_giu_duong_tra_web(sources, chat_spy):
    ccs, seen = chat_spy
    sources["down"].add("gold")
    await ccs.run_chat_turn(1, "giá vàng hôm nay có nên mua không", channel="zalo")
    await ccs.stop_background_tasks()
    assert seen["search"] == 1 and seen["require_real_search"] is True
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

    assert result.messages[0].startswith("🪙 Giá vàng SJC")
