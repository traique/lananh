"""Test lệnh /vang, /tygia, /xoso, /thoitiet, /bantin (services/daily_commands.py)."""

import datetime as dt
import re

import pytest

from _vn_data_samples import FUEL_HTML, FX_XML, SJC_JSON, VANG_TODAY_JSON, XSMB_RSS, XSMN_RSS
from _vn_data_samples import XSTN_RSS
from core import config
from core import database as db
from services import channel_command_service as ccs
from services import daily_commands, fuel_prices, morning_brief, vn_daily_chat, weather_chat
from services import weather_home
from services import vn_market_data as market

TODAY = dt.date(2026, 10, 26)


@pytest.fixture
def env(monkeypatch):
    state = {"down": set(), "settings": {}, "weather": []}

    def source(name, value):
        async def fake(*args):
            if name in state["down"]:
                raise market.MarketDataError(name)
            return value(*args) if callable(value) else value

        return fake

    draws = {
        "mn": market.parse_lottery_rss(XSMN_RSS, "mn", TODAY),
        "mb": market.parse_lottery_rss(XSMB_RSS, "mb", TODAY),
    }
    tay_ninh = market.parse_lottery_rss(XSTN_RSS, "mn", TODAY, name="Tây Ninh")

    def province_feed(province):
        if province.key != "tay-ninh":
            raise market.MarketDataError("feed tỉnh lỗi")
        return tay_ninh

    monkeypatch.setattr(market, "get_gold", source("gold", market.parse_sjc_json(SJC_JSON)))
    monkeypatch.setattr(
        market, "get_gold_live", source("gold_live", market.parse_vang_today_json(VANG_TODAY_JSON))
    )
    monkeypatch.setattr(market, "get_fx", source("fx", market.parse_fx_xml(FX_XML)))
    monkeypatch.setattr(market, "get_lottery", source("xs", lambda region: draws[region]))
    monkeypatch.setattr(market, "get_province_lottery", source("xst", province_feed))
    monkeypatch.setattr(
        fuel_prices, "get_fuel", source("fuel", fuel_prices.parse_fuel_html(FUEL_HTML))
    )
    monkeypatch.setattr(vn_daily_chat, "_today", lambda: TODAY)

    async def get_setting(key):
        return state["settings"].get(key)

    async def set_setting(key, value):
        state["settings"][key] = value

    async def list_prefix(prefix, limit=100):
        return sorted((k, v) for k, v in state["settings"].items() if k.startswith(prefix))

    async def fake_answer(text, today=None, *, user_id=None, now=None):
        state["weather"].append((text, user_id))
        return None if "Xyz" in text else f"🌤 {text}"

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(db, "list_settings_with_prefix", list_prefix)
    monkeypatch.setattr(weather_chat, "answer", fake_answer)
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    weather_home.clear_cache()
    yield state
    weather_home.clear_cache()


async def test_vang_va_nguon_loi(env):
    text = await daily_commands.vang()
    assert text.startswith("🪙 Giá vàng hôm nay (cập nhật 07:30 27/10/2026)")
    assert "• SJC miếng 9999: mua 138,7 (▲0,5) · bán 141,7 (▲0,5)" in text
    assert "• Nhẫn SJC 9999: mua 135 (▼0,3) · bán 139 (▼0,3)" in text
    assert "DOJI Hà Nội" in text and "PNJ 24K" in text and "Bảo Tín 9999" in text
    assert "• Thế giới XAU/USD: 4.015,1 USD/oz (▲9)" in text
    assert "≈ 126,01 triệu/lượng" in text and "chưa gồm thuế" in text
    pnj = await daily_commands.vang("pnj")
    assert "PNJ Hà Nội" in pnj and "SJC miếng" not in pnj and "XAU/USD" not in pnj
    world = await daily_commands.vang("thế giới")
    assert "XAU/USD" in world and "DOJI" not in world
    env["down"].add("gold_live")
    assert (await daily_commands.vang()).startswith("🪙 Giá vàng SJC")  # dự phòng SJC
    env["down"].add("gold")
    assert "tra web tham khảo" in await daily_commands.vang()


async def test_xang(env):
    text = await daily_commands.xang()
    assert text.startswith("⛽ Giá xăng dầu Petrolimex (cập nhật 00:00 27/10/2026)")
    assert "• Xăng E5 RON 92: vùng 1 25.060 · vùng 2 25.560 (kỳ trước ▼1.610)" in text
    assert "PVOIL:" in text and "giaxanghomnay.com" in text
    diesel = await daily_commands.xang("diesel")
    assert "DO 0,05S Mức 2" in diesel and "Xăng E5" not in diesel
    env["down"].add("fuel")
    assert (await daily_commands.xang()).startswith("❌") and "tham khảo" in (
        await daily_commands.xang()
    )


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("", "• EUR:"),
        ("usd", "• USD: mua TM 25.620"),
        ("100 usd", "💱 100 USD ≈ 2.565.000 đ"),
        ("yên nhật", "• JPY:"),
    ],
)
async def test_tygia(env, arg, expected):
    assert expected in await daily_commands.tygia(arg)


async def test_tygia_nguon_loi(env):
    env["down"].add("fx")
    assert (await daily_commands.tygia("")).startswith("❌")


async def test_xoso(env):
    assert "[Kiên Giang]" in await daily_commands.xoso("")
    assert "miền Bắc" in await daily_commands.xoso("mb")
    assert "TRÚNG Giải Đặc biệt" in await daily_commands.xoso("729510")
    assert "TRÚNG" in await daily_commands.xoso("mb 30328")
    assert await daily_commands.xoso("help") == daily_commands.XOSO_USAGE
    env["down"].add("xs")
    assert "không đoán số" in await daily_commands.xoso("123456")


async def test_xoso_theo_tinh_va_danh_sach(env):
    text = await daily_commands.xoso("tây ninh")
    assert text.startswith("🎰 Xổ số Tây Ninh (miền Nam) - Thứ Năm 22/10/2026")
    assert "ĐB: 058734" in text and "G7: 133" in text and "G8: 59" in text
    # Hôm nay (thứ Hai) Tây Ninh không quay: nói lịch + đưa kỳ gần nhất.
    today = await daily_commands.xoso("tây ninh hôm nay")
    assert "Hôm nay đài Tây Ninh không quay" in today and "quay thứ năm lúc 16h15" in today
    assert "ĐB: 058734" in today
    win = await daily_commands.xoso("tây ninh 058734")
    assert "Tây Ninh: 🎉 TRÚNG Giải Đặc biệt" in win
    old = await daily_commands.xoso("tây ninh 15/10 636638")
    assert "15/10/2026" in old and "TRÚNG Giải Đặc biệt" in old
    listing = await daily_commands.xoso("tinh")
    assert "⭐ TP.HCM (T2/T7)" in listing and "Tây Ninh (T5)" in listing
    assert "⭐ Tây Ninh" not in listing and "⭐ Thừa Thiên Huế (T2/CN)" in listing
    assert "Miền Bắc (18h15): quay hằng ngày" in listing


async def test_xoso_khong_tham_so_zalo_co_goi_y_telegram_co_nut(env):
    zalo = await daily_commands.xoso("", "zalo")
    assert "[Kiên Giang]" in zalo and "/xoso <tên tỉnh>" in zalo
    assert "hôm nay quay: TP.HCM, Đồng Tháp, Cà Mau, Thừa Thiên Huế, Phú Yên" in zalo
    telegram = await daily_commands.xoso("", "telegram")
    assert "/xoso <tên tỉnh>" not in telegram
    rows = daily_commands.today_rows()
    flat = [b for row in rows for b in row]
    assert ("🎰 TP.HCM", "xs:0") in flat and len(flat) == 5 and all(len(r) <= 3 for r in rows)
    from services import action_buttons

    assert action_buttons.parse("xs:9") == action_buttons.Action("xs", "9", "")
    assert action_buttons.parse("xs:999") is None and action_buttons.parse("xs:a") is None
    assert not action_buttons.needs_turn_lock("xs:0")
    tay_ninh = await daily_commands.xoso_province(9)
    assert "Tây Ninh" in tay_ninh and "058734" in tay_ninh
    assert "không còn trong danh mục" in await daily_commands.xoso_province(99)


async def test_thoitiet_dat_xem_xoa_thanh_pho(env):
    assert "chưa đặt" in await daily_commands.thoitiet(-5, "nhà")
    assert "Hà Nội" in await daily_commands.thoitiet(-5, "nha Hà Nội")
    assert env["settings"]["weather_home:-5"] == "Hà Nội"
    assert "Hà Nội" in await daily_commands.thoitiet(-5, "nha xem")
    assert "TP.HCM" in await daily_commands.thoitiet(-5, "nha xoá")
    assert env["settings"]["weather_home:-5"] == ""
    # Zalo của chủ bot (-5) và Telegram (42) tách biệt: Telegram chưa đặt gì.
    assert "weather_home:42" not in env["settings"]


async def test_lenh_doi_song_theo_cach_xung_ho_rieng(env):
    from services import reply_audience

    # "cô" -> Lan Anh xưng "cháu" (core/address.py).
    with reply_audience.use(reply_audience.ReplyAudience(address_form="cô")):
        assert "Cô chưa đặt thành phố riêng, cháu đang dùng" in await daily_commands.thoitiet(
            -5, "nha xem"
        )
        saved = await daily_commands.thoitiet(-5, "nha Hà Nội")
        assert "Dạ cháu nhớ rồi: thời tiết mặc định của cô là Hà Nội" in saved
        assert "cháu xem đúng nơi cô ở" in saved
        assert "của cô: Hà Nội" in await daily_commands.thoitiet(-5, "nha xem")
        assert "tới 12h cô nha" in await daily_commands.bantin(-5, "20", "zalo")
        assert "cô gõ rõ tên" in await daily_commands.thoitiet(-5, "Xyz")
        env["down"].update({"gold", "gold_live", "fx", "xs"})
        for reply in (
            await daily_commands.vang(),
            await daily_commands.tygia(""),
            await daily_commands.xoso("123456"),
        ):
            assert reply.startswith("❌ Cháu chưa lấy được") and re.search(r"\b[Cc]ô\b", reply)
            assert not re.search(r"\b(?:anh|em)\b", reply, re.IGNORECASE)


async def test_thoitiet_tra_cuu(env):
    assert (await daily_commands.thoitiet(42, "")) == "🌤 thời tiết hôm nay"
    await daily_commands.thoitiet(42, "nha trang")  # địa danh, không phải đặt nơi ở
    await daily_commands.thoitiet(42, "Đà Lạt mai")
    assert env["weather"] == [
        ("thời tiết hôm nay", 42),
        ("thời tiết ở nha trang", 42),
        ("thời tiết ở Đà Lạt mai", 42),
    ]
    assert "không tìm thấy" in await daily_commands.thoitiet(42, "Xyz")


async def test_bantin_bat_tat_gio_tin(env):
    status = await daily_commands.bantin(-5, "", "zalo")
    assert "ĐANG TẮT" in status and "tin đáng chú ý" in status
    assert "ĐANG BẬT, gửi lúc 7h" in await daily_commands.bantin(-5, "on", "zalo")
    assert "gửi lúc 6h" in await daily_commands.bantin(-5, "6h", "zalo")
    assert "từ 4h tới 12h" in await daily_commands.bantin(-5, "20", "zalo")
    status = await daily_commands.bantin(-5, "tin tắt", "zalo")
    assert "tin đáng chú ý" not in status and "giá xăng dầu" not in status
    assert "giá xăng dầu" in await daily_commands.bantin(-5, "xang on", "zalo")
    assert (await morning_brief.get_settings("zalo", -5)).fuel is True
    assert "giá xăng dầu" not in await daily_commands.bantin(-5, "xăng off", "zalo")
    assert "ĐANG TẮT" in await daily_commands.bantin(-5, "off", "zalo")
    assert (await morning_brief.get_settings("zalo", -5)).hour == 6
    assert "/bantin on|off" in await daily_commands.bantin(-5, "linh tinh", "zalo")


async def test_bantin_telegram_mac_dinh_khong_tin_va_chi_chu_bot(env):
    status = await daily_commands.bantin(42, "on", "telegram")
    assert "tin đáng chú ý" not in status and "thay luôn digest" in status
    assert "chỉ dành cho chủ bot" in await daily_commands.bantin(7, "on", "zoom")


async def test_bantin_xem_thu(env, monkeypatch):
    async def fake_build(user_id, channel, *, preview=False, **kw):
        return f"PREVIEW {user_id} {channel} {preview}"

    monkeypatch.setattr(morning_brief, "build", fake_build)
    assert await daily_commands.bantin(-5, "xem", "zalo") == "PREVIEW -5 zalo True"


async def test_kenh_zalo_zoom_dung_lenh(env):
    result, provider = await ccs.maybe_handle_command(-5, "/vang", is_admin=False)
    assert result[0].startswith("🪙") and provider is None
    result, _ = await ccs.maybe_handle_command(-5, "/tygia 100 usd", is_admin=False)
    assert "2.565.000" in result[0]
    result, _ = await ccs.maybe_handle_command(-5, "/xang", is_admin=False)
    assert result[0].startswith("⛽ Giá xăng dầu Petrolimex")
    result, _ = await ccs.maybe_handle_command(-5, "/xoso", is_admin=False)
    assert "/xoso <tên tỉnh>" in result[0]  # Zalo/Zoom: dòng gợi ý thay cho nút bấm


def test_help_va_menu_co_lenh_moi():
    import bot_app
    from handlers import commands

    names = {c.command for c in bot_app.COMMANDS}
    assert {"vang", "tygia", "xang", "xoso", "thoitiet", "bantin"} <= names
    for cmd in ("/vang", "/tygia", "/xang", "/xoso", "/thoitiet", "/bantin"):
        assert cmd in commands.HELP_TEXT and cmd in ccs.HELP
    # Telegram nhận tối đa 100 lệnh menu; /userouter9 rời menu nhưng vẫn chạy.
    assert len(bot_app.COMMANDS) <= 100 and len(names) == len(bot_app.COMMANDS)
    assert "userouter9" not in names and "/userouter9" in commands.HELP_TEXT
