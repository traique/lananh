"""Test web công khai (web_site.py): trang HTML, JSON API, health check giữ nguyên."""

import httpx
import pytest
from fastapi import FastAPI

import web_site


async def _logged_in(request):
    return True


async def _logged_out(request):
    return False


@pytest.fixture
async def client(monkeypatch):
    from ai import orchestrator

    async def boom(*args, **kwargs):
        raise AssertionError("web công khai không được gọi AI")

    monkeypatch.setattr(orchestrator, "ask", boom)
    monkeypatch.setattr(orchestrator, "chat", boom)
    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)
    app = FastAPI()
    app.include_router(web_site.static_router)
    app.include_router(web_site.router)
    app.include_router(web_site.api_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


async def test_root_tra_html_cho_trinh_duyet_json_cho_health_check(monkeypatch):
    import web

    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)

    transport = httpx.ASGITransport(app=web.api)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        health = await c.get("/")
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        assert (await c.head("/")).status_code == 200
        page = await c.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
        assert page.status_code == 200
        assert "Hôm nay" in page.text and 'alt="LCTA"' in page.text


async def test_chua_dang_nhap_chuyen_sang_admin_api_401_anh_van_cong_khai(client, monkeypatch):
    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_out)
    r = await client.get("/lich/hoi", params={"q": "rằm tháng 8"})
    assert r.status_code == 303
    assert (
        r.headers["location"]
        == "/admin?next=%2Flich%2Fhoi%3Fq%3Dr%25E1%25BA%25B1m%2Bth%25C3%25A1ng%2B8"
    )
    for path in ("/lich", "/thoi-tiet", "/tu-vi", "/than-so-hoc", "/lich/ngay-le"):
        assert (await client.get(path)).status_code == 303, path
    r = await client.post("/than-so-hoc", data={"ngay": "1990-03-15", "ten": "A"})
    assert r.status_code == 303  # không tính toán, không lộ dữ liệu khi chưa đăng nhập
    r = await client.get("/api/v1/calendar/days/2026-10-22")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert (await client.get("/static/logo-light.webp")).status_code == 200
    assert (await client.get("/favicon.ico")).status_code == 200


async def test_root_chua_dang_nhap_chuyen_sang_admin_health_check_van_json(monkeypatch):
    import web

    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_out)
    transport = httpx.ASGITransport(app=web.api)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        assert (await c.get("/")).json() == {"status": "ok"}
        page = await c.get("/", headers={"Accept": "text/html"})
        assert page.status_code == 303 and page.headers["location"] == "/admin?next=%2F"
        assert (await c.get("/privacy")).status_code == 200  # trang chính sách vẫn công khai


async def test_header_co_link_quan_tri_va_dang_xuat(client):
    html = (await client.get("/lich")).text
    assert 'href="/admin">Quản trị</a>' in html
    assert '<form method="post" action="/admin/logout">' in html


async def test_trang_lich_thang(client):
    r = await client.get("/lich", params={"ngay": "2026-10-24"})
    assert r.status_code == 200
    assert "Tháng 10/2026" in r.text and 'aria-current="date"' in r.text
    assert "ngày 15 tháng 9 năm Bính Ngọ âm lịch" in r.text
    assert 'href="/lich/hoi"' in r.text  # thanh mục con


async def test_trang_lich_tach_rieng(client):
    ask = await client.get("/lich/hoi", params={"q": "trung thu"})
    assert "Tết Trung thu" in ask.text and 'aria-current="page">Hỏi lịch' in ask.text
    conv = await client.get("/lich/doi-ngay", params={"am": "15/8/2027"})
    assert "Thứ Tư 15/09/2027" in conv.text
    bad = await client.get("/lich/doi-ngay", params={"am": "30/2/2026"})
    assert 'role="alert"' in bad.text and "chỉ có 29 ngày" in bad.text
    holidays = await client.get("/lich/ngay-le")
    assert "Tết Nguyên đán" in holidays.text and "Ngày Nhà giáo Việt Nam" in holidays.text
    assert 'role="alert"' in (await client.get("/lich", params={"thang": "abc"})).text


async def test_link_cu_chuyen_sang_trang_moi(client):
    r = await client.get("/lich", params={"q": "trung thu"})
    assert r.status_code == 307 and r.headers["location"] == "/lich/hoi?q=trung+thu"
    r = await client.get("/lich", params={"am": "15/6/2025", "nhuan": "1"})
    assert r.headers["location"] == "/lich/doi-ngay?am=15%2F6%2F2025&nhuan=1"


async def test_trang_tuvi_post_va_loi(client):
    r = await client.post(
        "/tu-vi", data={"ngay": "15", "thang": "3", "nam": "1990", "gio": "5", "gioi": "male"}
    )
    assert "Thổ ngũ cục" in r.text and "Phá Quân" in r.text
    assert "Lập lá số khác" in r.text and 'name="ngay"' not in r.text  # trang kết quả riêng
    lunar = await client.post(
        "/tu-vi",
        data={
            "ngay": "30",
            "thang": "1",
            "nam": "2026",
            "gio": "0",
            "gioi": "female",
            "lich": "am",
        },
    )
    assert "ngày 30 tháng 1 năm Bính Ngọ" in lunar.text  # ngày 30 âm: ô date không nhập được
    bad = await client.post("/tu-vi", data={"ngay": "31", "thang": "2", "nam": "1990", "gio": "1"})
    assert 'role="alert"' in bad.text


async def test_trang_than_so_hoc_escape_ho_ten(client):
    r = await client.post("/than-so-hoc", data={"ngay": "1990-03-15", "ten": "<script>x</script>"})
    assert "Con số chủ đạo" in r.text and "Tính cho người khác" in r.text
    assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text


_FAKE_WEATHER = {
    "location": "Đà Lạt",
    "source": "Open-Meteo",
    "now": {
        "description": "mưa nhẹ",
        "temperature_c": 18.4,
        "feels_like_c": 17.6,
        "humidity_percent": 91,
        "wind_kmh": 7.2,
        "precipitation_mm": 0.3,
    },
    "daily": [
        {
            "date": f"2026-10-{22 + i}",
            "weekday": "Thứ Năm",
            "description": "có mưa rào",
            "min_c": 15.2,
            "max_c": 22.8,
            "rain_chance_percent": 80,
            "uv_max": 6.1,
        }
        for i in range(7)
    ],
}


async def test_trang_thoi_tiet(client, monkeypatch):
    calls = []

    async def fake_get_weather(location="", days=1):
        calls.append((location, days))
        if location == "Xyzabc":
            return {"error": "Không tìm thấy địa điểm 'Xyzabc'"}
        if location == "loi":
            raise web_site.weather.WeatherError("down")
        return _FAKE_WEATHER

    monkeypatch.setattr(web_site.weather, "get_weather", fake_get_weather)
    html = (await client.get("/thoi-tiet")).text
    assert calls[-1] == ("", 7)  # không nhập -> mặc định TP.HCM ở services/weather
    assert 'href="/thoi-tiet" aria-current="page"' in html  # tab Thời tiết trên menu
    assert "18°C" in html and "Mưa nhẹ" in html and "Hôm nay" in html and "Ngày mai" in html
    assert html.count('class="when"') == 7

    html = (await client.get("/thoi-tiet", params={"noi": "Xyzabc"})).text
    assert 'role="alert"' in html and "Không tìm thấy địa điểm" in html and "°C" not in html
    html = (await client.get("/thoi-tiet", params={"noi": "loi"})).text
    assert 'role="alert"' in html and "thử lại sau" in html
    await client.get("/thoi-tiet", params={"noi": "x" * 500})
    assert len(calls[-1][0]) == 60  # cắt độ dài trước khi tra geocoding


async def test_static_chi_phuc_vu_file_trong_danh_sach(client):
    assert (await client.get("/static/logo-light.webp")).headers["content-type"] == "image/webp"
    assert (await client.get("/static/favicon.png")).status_code == 200
    assert (await client.get("/favicon.ico")).headers["content-type"] == "image/png"
    assert (await client.get("/static/site.css")).status_code == 404  # CSS nhúng trong HTML
    assert (await client.get("/static/..%2Fweb.py")).status_code == 404
    assert (await client.get("/static/web.py")).status_code == 404


async def test_api_calendar(client):
    day = (await client.get("/api/v1/calendar/days/2026-10-21")).json()
    assert day["lunar"]["text"] == "ngày 12 tháng 9 năm Bính Ngọ"
    assert day["isHoangDao"] is True and day["dayGod"] == "Thanh Long"
    month = (await client.get("/api/v1/calendar/months/2026-12")).json()
    assert (
        len(month["days"]) == 31 and month["previous"] == "2026-11" and month["next"] == "2027-01"
    )
    conv = await client.get(
        "/api/v1/calendar/conversions",
        params={"lunarDay": 15, "lunarMonth": 8, "lunarYear": 2027},
    )
    assert conv.json()["date"] == "2027-09-15"
    ans = (await client.get("/api/v1/calendar/answers", params={"q": "tết"})).json()
    assert "Tết Nguyên đán" in ans["answer"]


@pytest.mark.parametrize(
    "method,url,body,code",
    [
        ("get", "/api/v1/calendar/days/2026-13-40", None, "INVALID_DATE"),
        ("get", "/api/v1/calendar/days/1800-01-01", None, "YEAR_OUT_OF_RANGE"),
        ("get", "/api/v1/calendar/months/2026-1", None, "INVALID_MONTH"),
        ("get", "/api/v1/calendar/conversions?lunarDay=30&lunarMonth=2&lunarYear=2026", None,
         "LUNAR_DATE_NOT_FOUND"),
        ("post", "/api/v1/numerology/readings", {"birthDate": "15/03/1990", "name": 5}, "INVALID_NAME"),
        ("post", "/api/v1/tuvi/charts", {"birthDate": "1990-03-15", "gender": "male"}, "INVALID_HOUR"),
        ("post", "/api/v1/tuvi/charts", {"birthDate": "1990-03-15", "hourBranch": 5, "gender": "x"},
         "INVALID_GENDER"),
    ],
)  # fmt: skip
async def test_api_loi_cung_mot_dang(client, method, url, body, code):
    r = await (client.get(url) if method == "get" else client.post(url, json=body))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code and r.json()["error"]["message"]


async def test_api_numerology_va_tuvi(client):
    r = await client.post(
        "/api/v1/numerology/readings", json={"birthDate": "1990-03-15", "name": "Nguyễn Văn An"}
    )
    data = r.json()
    assert data["lifePath"]["value"] == 10 and data["soul"]["value"] == 8
    assert data["arrows"][0]["digits"] == "1-5-9"
    r = await client.post(
        "/api/v1/tuvi/charts",
        json={"birthDate": "1990-03-15", "hourBranch": 5, "gender": "male"},
    )
    chart = r.json()
    assert chart["cuc"] == "Thổ ngũ cục"
    assert chart["palaces"][0]["name"] == "Mệnh" and chart["palaces"][0]["branch"] == "Tuất"
    assert chart["palaces"][0]["mainStars"] == ["Phá Quân"]


async def test_api_body_qua_lon(client):
    r = await client.post("/api/v1/numerology/readings", content=b"{" + b" " * 5000 + b"}")
    assert r.json()["error"]["code"] == "BODY_TOO_LARGE"


async def test_trang_khong_keo_css_rieng_anh_cache_dai(client):
    """CSS nhúng sẵn trong HTML; chỉ logo/favicon là file riêng, có ?v=<hash> và
    cache 1 năm. Trước đây mỗi lượt xem tải 3 file song song, vài người xem
    cùng lúc vượt --limit-concurrency của uvicorn -> 503 cả webhook Telegram/Zalo."""
    import re

    for path in ("/lich", "/lich/hoi", "/tu-vi", "/than-so-hoc"):
        html = (await client.get(path)).text
        assert "<style>" in html and 'rel="stylesheet"' not in html
        refs = re.findall(r'(?:src|href|srcset)="(/static/[^"]+)"', html)
        assert refs and all(re.search(r"\?v=[0-9a-f]{10}$", r) for r in refs), path
    r = await client.get("/static/logo-dark.webp")
    assert "immutable" in r.headers["cache-control"]


def test_uvicorn_limit_concurrency_du_cho_web_va_webhook():
    import re
    from pathlib import Path

    conf = (Path(__file__).resolve().parent.parent / "supervisord.conf").read_text()
    limit = int(re.search(r"--limit-concurrency (\d+)", conf).group(1))
    assert limit >= 32
