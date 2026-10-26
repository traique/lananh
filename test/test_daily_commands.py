"""Test lệnh /vang, /tygia, /xoso, /thoitiet, /bantin (services/daily_commands.py)."""

import datetime as dt

import pytest

from _vn_data_samples import FX_XML, SJC_JSON, XSMB_RSS, XSMN_RSS
from core import config
from core import database as db
from services import channel_command_service as ccs
from services import daily_commands, morning_brief, vn_daily_chat, weather_chat, weather_home
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
    monkeypatch.setattr(market, "get_gold", source("gold", market.parse_sjc_json(SJC_JSON)))
    monkeypatch.setattr(market, "get_fx", source("fx", market.parse_fx_xml(FX_XML)))
    monkeypatch.setattr(market, "get_lottery", source("xs", lambda region: draws[region]))
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
    assert (await daily_commands.vang()).startswith("🪙 Giá vàng SJC")
    env["down"].add("gold")
    assert "tra web tham khảo" in await daily_commands.vang()


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


async def test_thoitiet_dat_xem_xoa_thanh_pho(env):
    assert "chưa đặt" in await daily_commands.thoitiet(-5, "nhà")
    assert "Hà Nội" in await daily_commands.thoitiet(-5, "nha Hà Nội")
    assert env["settings"]["weather_home:-5"] == "Hà Nội"
    assert "Hà Nội" in await daily_commands.thoitiet(-5, "nha xem")
    assert "TP.HCM" in await daily_commands.thoitiet(-5, "nha xoá")
    assert env["settings"]["weather_home:-5"] == ""
    # Zalo của chủ bot (-5) và Telegram (42) tách biệt: Telegram chưa đặt gì.
    assert "weather_home:42" not in env["settings"]


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
    assert "tin đáng chú ý" not in status
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


def test_help_va_menu_co_lenh_moi():
    import bot_app
    from handlers import commands

    names = {c.command for c in bot_app.COMMANDS}
    assert {"vang", "tygia", "xoso", "thoitiet", "bantin"} <= names
    for cmd in ("/vang", "/tygia", "/xoso", "/thoitiet", "/bantin"):
        assert cmd in commands.HELP_TEXT and cmd in ccs.HELP
