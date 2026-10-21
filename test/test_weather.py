"""Test services/weather.py với HTTP giả lập (httpx.MockTransport), không gọi mạng."""

import httpx
import pytest

from services import http_client, weather

_FORECAST = {
    "current": {
        "temperature_2m": 31.4,
        "apparent_temperature": 36.2,
        "relative_humidity_2m": 70,
        "weather_code": 2,
        "wind_speed_10m": 11.0,
        "precipitation": 0,
    },
    "daily": {
        "time": ["2026-10-21", "2026-10-22"],
        "weather_code": [80, 61],
        "temperature_2m_min": [25.1, 24.6],
        "temperature_2m_max": [32.8, 31.2],
        "precipitation_probability_max": [75, 60],
        "uv_index_max": [9.1, 8.0],
    },
}


@pytest.fixture
def mock_http(monkeypatch):
    """Trả list request đã gửi; ``routes`` map host -> hàm tạo Response."""
    weather._geo_cache.clear()
    weather._forecast_cache.clear()
    sent: list[httpx.Request] = []
    routes: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return routes[request.url.host](request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_client, "get_client", lambda: client)
    yield sent, routes
    weather._geo_cache.clear()
    weather._forecast_cache.clear()


@pytest.mark.parametrize("location", ["", "Sài Gòn", "TP.HCM", "thành phố Hồ Chí Minh", "hcm"])
async def test_khong_noi_dia_diem_hoac_noi_hcm_dung_toa_do_mac_dinh(mock_http, location):
    sent, routes = mock_http
    routes["api.open-meteo.com"] = lambda r: httpx.Response(200, json=_FORECAST)

    data = await weather.get_weather(location, days=2)

    assert data["location"] == weather.DEFAULT_LABEL
    assert [r.url.host for r in sent] == ["api.open-meteo.com"]  # không geocode
    assert sent[0].url.params["latitude"] == str(weather.DEFAULT_LAT)
    text = weather.describe_weather(data)
    assert "TP. Hồ Chí Minh" in text and "31°C" in text
    assert "Ngày mai (22/10): mưa nhỏ, 25-31°C, khả năng mưa 60%" in text


async def test_dia_diem_cu_the_thi_geocode_va_cache(mock_http):
    sent, routes = mock_http
    routes["geocoding-api.open-meteo.com"] = lambda r: httpx.Response(
        200,
        json={
            "results": [
                {
                    "name": "Hà Nội",
                    "admin1": "Hà Nội",
                    "country": "Việt Nam",
                    "latitude": 21.0245,
                    "longitude": 105.8412,
                }
            ]
        },
    )
    routes["api.open-meteo.com"] = lambda r: httpx.Response(200, json=_FORECAST)

    data = await weather.get_weather("thành phố Hà Nội", days=1)
    again = await weather.get_weather("Hà Nội", days=1)

    assert data["location"] == again["location"] == "Hà Nội, Việt Nam"
    assert sent[0].url.params["name"] == "Hà Nội"  # đã bỏ tiền tố "thành phố"
    assert sent[1].url.params["latitude"] == "21.0245"
    # Lần 2 dùng cache toạ độ + cache dự báo, không gọi mạng thêm.
    assert len(sent) == 2


async def test_khong_tim_thay_dia_diem(mock_http):
    _sent, routes = mock_http
    routes["geocoding-api.open-meteo.com"] = lambda r: httpx.Response(200, json={})

    data = await weather.get_weather("Xyzabc")

    assert data == {"error": "Không tìm thấy địa điểm 'Xyzabc'"}


async def test_open_meteo_loi_thi_dung_wttr(mock_http):
    _sent, routes = mock_http
    routes["api.open-meteo.com"] = lambda r: httpx.Response(503)
    hourly = [{"chanceofrain": "10", "weatherDesc": [{"value": "Sunny"}]}] * 8
    hourly[4] = {"chanceofrain": "80", "lang_vi": [{"value": "Mưa rào"}]}
    routes["wttr.in"] = lambda r: httpx.Response(
        200,
        json={
            "current_condition": [
                {
                    "temp_C": "30",
                    "FeelsLikeC": "34",
                    "humidity": "75",
                    "windspeedKmph": "9",
                    "precipMM": "0.2",
                    "lang_vi": [{"value": "Có mây"}],
                }
            ],
            "weather": [
                {
                    "date": "2026-10-21",
                    "mintempC": "25",
                    "maxtempC": "33",
                    "uvIndex": "8",
                    "hourly": hourly,
                }
            ],
        },
    )

    data = await weather.get_weather("", days=3)

    assert data["source"].startswith("wttr.in")
    assert data["now"]["description"] == "Có mây"
    assert data["daily"][0]["rain_chance_percent"] == 80


async def test_ca_hai_nguon_loi_thi_raise_weather_error(mock_http):
    _sent, routes = mock_http
    routes["api.open-meteo.com"] = lambda r: httpx.Response(500)
    routes["wttr.in"] = lambda r: httpx.Response(500)

    with pytest.raises(weather.WeatherError):
        await weather.get_weather("")


async def test_cache_co_tran(mock_http):
    _sent, routes = mock_http
    routes["geocoding-api.open-meteo.com"] = lambda r: httpx.Response(200, json={})
    for i in range(weather._GEO_CACHE_MAX + 10):
        await weather.get_weather(f"noi-{i}")
    assert len(weather._geo_cache) == weather._GEO_CACHE_MAX
