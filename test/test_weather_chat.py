"""Test services/weather_chat.py: câu hỏi thời tiết trả lời thẳng, không gọi AI."""

import datetime as dt

import pytest

from services import weather, weather_chat

TODAY = dt.date(2026, 10, 22)  # Thứ Năm


@pytest.fixture(autouse=True)
def _no_hourly_or_air_network(monkeypatch):
    """Mặc định: theo giờ lỗi (quay về dự báo ngày như cũ), AQI tốt (không cảnh
    báo), không ai đặt thành phố riêng - test cũ giữ nguyên hành vi, không mở mạng."""

    async def hourly_down(location=""):
        raise weather.WeatherError("không có mạng trong test")

    async def good_air(location=""):
        return {"location": location or weather.DEFAULT_LABEL, "us_aqi": 40, "pm2_5": 9.0}

    async def no_home(user_id):
        return ""

    monkeypatch.setattr(weather, "get_hourly", hourly_down)
    monkeypatch.setattr(weather, "get_air_quality", good_air)
    monkeypatch.setattr(weather_chat.weather_home, "get", no_home)


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


# ─── Theo giờ, AQI, thành phố riêng ─────────────────────────────────────────


def _hours(start: dt.date, chances: dict[int, int], count: int = 48) -> list:
    """``count`` giờ từ 0h ngày ``start``; ``chances`` map giờ thứ mấy -> % mưa."""
    out = []
    for i in range(count):
        moment = dt.datetime.combine(start, dt.time()) + dt.timedelta(hours=i)
        rain = chances.get(i, 10)
        out.append(weather.HourPoint(moment, rain, 0.5 if rain >= 50 else 0.0, 30.0, 61))
    return out


@pytest.fixture
def hourly(monkeypatch, calls):
    seen = []

    async def fake_hourly(location=""):
        seen.append(location)
        return {
            "location": location or weather.DEFAULT_LABEL,
            "source": "Open-Meteo",
            "hours": _hours(TODAY, {15: 70, 16: 85, 17: 60, 24 + 19: 55}),
        }

    monkeypatch.setattr(weather, "get_hourly", fake_hourly)
    return seen


async def test_chieu_nay_tra_theo_gio(hourly, calls):
    reply = await weather_chat.answer("chiều nay có mưa không", TODAY)
    assert reply.startswith("🌦 Chiều nay (13h-18h) ở TP. Hồ Chí Minh")
    assert "Dễ mưa (từ 50%) khoảng 15h-18h; cao nhất 85% lúc 16h" in reply
    assert "Từng giờ: 13h 10% · 14h 10% · 15h 70% · 16h 85% · 17h 60%" in reply
    assert "lượng mưa dự kiến ~1,5 mm" in reply and "áo mưa" in reply
    assert hourly == [""] and calls == []  # không cần gọi dự báo theo ngày


async def test_theo_gio_goi_dung_cach_xung_ho(hourly, calls):
    from services import reply_audience

    with reply_audience.use(reply_audience.ReplyAudience(address_form="chú")):
        reply = await weather_chat.answer("chiều nay có mưa không", TODAY)
    assert "Nhớ mang áo mưa nha chú ☔" in reply and "nha anh" not in reply


async def test_toi_mai_va_buoi_dang_dien_ra(hourly, calls):
    reply = await weather_chat.answer("tối mai trời có mưa không", TODAY)
    assert "Tối mai (18h-22h)" in reply and "khoảng 19h-20h" in reply
    now = dt.datetime.combine(TODAY, dt.time(16, 20))
    reply = await weather_chat.answer("chiều nay có mưa không", TODAY, now=now)
    assert "Từng giờ: 16h 85% · 17h 60%" in reply  # chỉ phần còn lại của buổi


async def test_may_gio_mua_tinh_tu_bay_gio(hourly, calls):
    now = dt.datetime.combine(TODAY, dt.time(9, 5))
    reply = await weather_chat.answer("hôm nay mấy giờ mưa", TODAY, now=now)
    assert "Hôm nay (theo giờ)" in reply and "khoảng 15h-18h" in reply
    assert "Từng giờ: 9h 10% · 11h 10%" in reply  # từ giờ hiện tại, bước 2 giờ khi dài


async def test_dem_mai_du_ca_dem(monkeypatch, calls):
    async def three_days(location=""):
        hours = _hours(TODAY, {48 + 3: 80}, count=72)  # mưa lúc 3h sáng ngày kia
        return {"location": "Huế", "source": "Open-Meteo", "hours": hours}

    monkeypatch.setattr(weather, "get_hourly", three_days)
    reply = await weather_chat.answer("Huế đêm mai có mưa không", TODAY)
    assert "Đêm mai (22h-6h)" in reply and "Dễ mưa (từ 50%) khoảng 3h-4h" in reply
    assert "chưa có" not in reply


async def test_dem_mai_thieu_gio_khong_noi_ca_dem(monkeypatch, calls):
    async def two_days(location=""):
        return {"location": "Huế", "source": "Open-Meteo", "hours": _hours(TODAY, {})}

    monkeypatch.setattr(weather, "get_hourly", two_days)
    reply = await weather_chat.answer("Huế đêm mai có mưa không", TODAY)
    # Nguồn chỉ có tới 24h: không được tóm tắt như thể đã xem cả đêm 22h-6h.
    assert "Đêm mai (22h-24h)" in reply and "22h-6h" not in reply
    assert "sau 24h em chưa có dự báo theo giờ" in reply


async def test_theo_gio_khong_mua(monkeypatch, calls):
    async def dry(location=""):
        return {"location": "Huế", "source": "Open-Meteo", "hours": _hours(TODAY, {14: 30})}

    monkeypatch.setattr(weather, "get_hourly", dry)
    reply = await weather_chat.answer("Huế chiều nay có mưa không", TODAY)
    assert "Ít khả năng mưa (cao nhất 30% lúc 14h)" in reply
    assert "áo mưa" not in reply


async def test_aqi_tren_100_moi_canh_bao(monkeypatch, calls):
    reply = await weather_chat.answer("thời tiết hôm nay thế nào", TODAY)
    assert "Không khí" not in reply  # AQI 40: không nhắc

    async def bad_air(location=""):
        return {"location": "TP. Hồ Chí Minh", "us_aqi": 135, "pm2_5": 52.4}

    monkeypatch.setattr(weather, "get_air_quality", bad_air)
    reply = await weather_chat.answer("thời tiết hôm nay thế nào", TODAY)
    assert "😷 Không khí US AQI 135 (kém - không tốt cho nhóm nhạy cảm), PM2.5 52 µg/m³" in reply
    reply = await weather_chat.answer("mai có mưa không", TODAY)
    assert "AQI" not in reply  # cảnh báo chỉ kèm câu có hôm nay


async def test_hoi_thang_chat_luong_khong_khi(monkeypatch, calls):
    reply = await weather_chat.answer("chất lượng không khí Hà Nội hôm nay", TODAY)
    assert reply.startswith("🌫 Chất lượng không khí Hà Nội: US AQI 40 (tốt)")
    assert "CAMS" in reply and calls == []

    async def down(location=""):
        raise weather.WeatherError("down")

    monkeypatch.setattr(weather, "get_air_quality", down)
    assert (await weather_chat.answer("bụi mịn hôm nay sao rồi", TODAY)).startswith("❌")


async def test_thanh_pho_rieng_khi_khong_noi_dia_diem(monkeypatch, calls):
    async def home(user_id):
        return "Hà Nội" if user_id == -7 else ""

    monkeypatch.setattr(weather_chat.weather_home, "get", home)
    reply = await weather_chat.answer("mai có mưa không", TODAY, user_id=-7)
    assert "Thời tiết Hà Nội" in reply
    await weather_chat.answer("mai có mưa không", TODAY, user_id=42)
    # Nói rõ TP.HCM/Đà Lạt thì không lấy thành phố riêng.
    await weather_chat.answer("TP.HCM mai có mưa không", TODAY, user_id=-7)
    await weather_chat.answer("Đà Lạt mai có mưa không", TODAY, user_id=-7)
    assert calls == [("Hà Nội", 2), ("", 2), ("", 2), ("Đà Lạt", 2)]


async def test_cau_noi_tiep_dung_lai_dia_diem_vua_tra(monkeypatch, calls):
    """ "thế còn ngày mai?" ngay sau câu trả lời thời tiết: tra dự báo thật cho
    đúng địa điểm vừa hỏi, không để AI tự bịa."""
    weather_chat._recent.clear()
    assert await weather_chat.answer("thế còn ngày mai?", TODAY, user_id=5) is None  # chưa hỏi gì
    await weather_chat.answer("Đà Lạt hôm nay có mưa không", TODAY, user_id=5)
    reply = await weather_chat.answer("thế còn ngày mai?", TODAY, user_id=5)
    assert reply is not None and "Thời tiết Đà Lạt" in reply and "Ngày mai" in reply
    reply = await weather_chat.answer("còn Nha Trang thì sao", TODAY, user_id=5)
    assert reply is not None and "Nha Trang" in reply
    # Người khác, câu dài/không có mốc thời gian hay địa danh: không đoán.
    assert await weather_chat.answer("thế còn ngày mai?", TODAY, user_id=6) is None
    assert await weather_chat.answer("vậy còn chuyện công ty thì sao", TODAY, user_id=5) is None
    assert calls == [("Đà Lạt", 1), ("Đà Lạt", 2), ("Nha Trang", 1)]


def test_dia_diem_vua_tra_co_tran_va_het_han():
    weather_chat._recent.clear()
    for uid in range(weather_chat._RECENT_MAX + 5):
        weather_chat.remember_place(uid, "Huế", now=0.0)
    assert len(weather_chat._recent) == weather_chat._RECENT_MAX
    assert weather_chat.recent_place(0, now=1.0) is None  # bị đẩy ra
    assert weather_chat.recent_place(200, now=1.0) == "Huế"
    assert weather_chat.recent_place(200, now=weather_chat._RECENT_TTL_SEC + 10) is None
    weather_chat._recent.clear()
