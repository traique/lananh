"""Test services/weather_chat.py: câu hỏi thời tiết trả lời thẳng, không gọi AI."""

import datetime as dt

import pytest

from services import weather, weather_chat

TODAY = dt.date(2026, 10, 22)  # Thứ Năm


def _fake_data(location="TP. Hồ Chí Minh"):
    daily = []
    for i in range(7):
        day = TODAY + dt.timedelta(days=i)
        daily.append(
            {
                "date": day.isoformat(),
                "weekday": weather.WEEKDAYS[day.weekday()],
                "description": "mưa rào" if i == 1 else "có mây",
                "min_c": 25,
                "max_c": 32 + i,
                "rain_chance_percent": 80 if i == 1 else 20,
                "uv_max": 8,
            }
        )
    return {
        "location": location,
        "source": "Open-Meteo",
        "now": {"description": "có mây", "temperature_c": 30.4, "feels_like_c": 35,
                "humidity_percent": 70, "wind_kmh": 9},
        "daily": daily,
    }  # fmt: skip


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def fake_get_weather(location="", days=1):
        seen.append((location, days))
        if location == "Xyz":
            return {"error": "Không tìm thấy địa điểm 'Xyz'"}
        return _fake_data(location or weather.DEFAULT_LABEL)

    monkeypatch.setattr(weather, "get_weather", fake_get_weather)
    return seen


@pytest.mark.parametrize(
    "text,place,days,expected",
    [
        ("thời tiết hôm nay thế nào", "", 1, "• Hôm nay 22/10: có mây, 25-32°C"),
        ("mai có mưa không em", "", 2, "• Ngày mai 23/10: mưa rào, 25-33°C, khả năng mưa 80% ☔"),
        ("Đà Lạt mai có mưa không", "Đà Lạt", 2, "Thời tiết Đà Lạt"),
        ("thoi tiet nha trang cuoi tuan", "Nha Trang", 4, "• Chủ Nhật 25/10"),
        ("thời tiết ở Bảo Lộc 3 ngày tới", "Bảo Lộc", 3, "• Thứ Bảy 24/10"),
        ("thời tiết Mai Châu hôm nay", "Mai Châu", 1, "Bây giờ: có mây, 30°C"),
        ("TP.HCM chiều nay có mưa không", "", 1, "Thời tiết TP. Hồ Chí Minh"),
        ("mai đi chơi được không, có mưa không", "", 2, "Ngày mai"),
    ],
)
async def test_tra_loi_thang(calls, text, place, days, expected):
    reply = await weather_chat.answer(text, TODAY)
    assert reply is not None and expected in reply
    assert calls == [(place, days)]


async def test_ngay_mai_khong_ke_hom_nay(calls):
    reply = await weather_chat.answer("mai có mưa không", TODAY)
    assert "Hôm nay" not in reply and "Bây giờ" not in reply


@pytest.mark.parametrize(
    "text",
    [
        "giá kem chống nắng bao nhiêu",  # không phải câu thời tiết
        "thời tiết Quận 7 hôm nay",  # chữ lạ không có "ở/tại": không chắc là địa danh
        "thời tiết ở Xyz mai",  # geocoding không tìm thấy
    ],
)
async def test_khong_chac_thi_de_ai(calls, text):
    assert await weather_chat.answer(text, TODAY) is None


async def test_nguon_loi_bao_that_khong_bia(monkeypatch):
    async def fail(location="", days=1):
        raise weather.WeatherError("down")

    monkeypatch.setattr(weather, "get_weather", fail)
    reply = await weather_chat.answer("thời tiết hôm nay", TODAY)
    assert reply.startswith("❌ Chưa lấy được dữ liệu thời tiết")


async def test_kenh_zalo_khong_goi_ai(calls, monkeypatch):
    from services import channel_chat_service as ccs

    async def no_command(*args, **kwargs):
        return None

    async def boom(*args, **kwargs):
        raise AssertionError("không được gọi AI cho câu thời tiết")

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", no_command)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", no_command)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", boom)
    monkeypatch.setattr(ccs.orchestrator, "chat", boom)

    result = await ccs.handle_channel_text(1, "thời tiết Hà Nội ngày mai", channel="zalo")

    assert "Thời tiết Hà Nội" in result.messages[0]
