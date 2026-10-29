"""Test web công khai (web_site.py): trang HTML, JSON API, health check giữ nguyên."""

import datetime as dt
import json

import httpx
import pytest
from fastapi import FastAPI

import web_site


async def _logged_in(request):
    return True


async def _logged_out(request):
    return False


async def _no_setting(key):
    return None


@pytest.fixture
async def client(monkeypatch):
    from ai import orchestrator

    async def boom(*args, **kwargs):
        raise AssertionError("web công khai không được gọi AI")

    monkeypatch.setattr(orchestrator, "ask", boom)
    monkeypatch.setattr(orchestrator, "chat", boom)
    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_out)
    monkeypatch.setattr(web_site.weather_places, "_cache", None)
    monkeypatch.setattr(web_site.weather_places.db, "get_setting", _no_setting)
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
        # Health check Render/UptimeRobot và client xin JSON: vẫn JSON nhẹ.
        for headers in (
            {"Accept": "application/json"},
            {"Accept": "*/*", "User-Agent": "Render/1.0"},
            {"User-Agent": "Mozilla/5.0+(compatible; UptimeRobot/2.0)"},
        ):
            health = await c.get("/", headers=headers)
            assert health.status_code == 200 and health.json() == {"status": "ok"}, headers
        assert (await c.head("/")).status_code == 200
        page = await c.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
        assert page.status_code == 200
        assert "Hôm nay" in page.text and 'alt="LCTA"' in page.text
        # Trình đọc link Facebook/Zalo gửi Accept: */* -> trang HTML có thẻ og:, không
        # phải {"status":"ok"} (link chia sẻ hiện trắng).
        for agent in ("facebookexternalhit/1.1", "Zalo", "python-httpx"):
            crawl = await c.get("/", headers={"Accept": "*/*", "User-Agent": agent})
            assert crawl.headers["content-type"].startswith("text/html"), agent
            assert '<meta property="og:title"' in crawl.text
            assert (
                '<meta property="og:image" content="https://testserver/static/og-image.png?v='
                in (crawl.text)
            )


async def test_trang_cong_khai_khong_co_dang_nhap(client, monkeypatch):
    """Khách xem tự do; chỉ /admin có ô đăng nhập. Trang công khai không có nút hay câu
    nào nhắc đăng nhập/tài khoản/AI cho khách."""

    async def fake_get_weather(location="", days=1):
        return _FAKE_WEATHER

    # /thoi-tiet không được gọi Open-Meteo/wttr.in thật (CI chặn mạng trong test)
    monkeypatch.setattr(web_site.weather, "get_weather", fake_get_weather)
    r = await client.get("/lich/hoi", params={"q": "rằm tháng 8"})
    assert r.status_code == 200 and "rằm" in r.text
    for path in ("/", "/lich/hoi", "/thoi-tiet", "/tu-vi", "/than-so-hoc", "/ky-mon"):
        html = (await client.get(path)).text.lower()
        assert "đăng nhập" not in html and "/admin?next" not in html, path
        assert "tài khoản" not in html and "luận giải bằng ai" not in html, path
    assert ">Quản trị</a>" not in r.text and "/admin/logout" not in r.text
    for path in ("/lich", "/tu-vi", "/than-so-hoc", "/lich/ngay-le", "/lich/doi-ngay"):
        assert (await client.get(path)).status_code == 200, path
    assert (await client.get("/api/v1/calendar/days/2026-10-22")).status_code == 200


async def test_da_dang_nhap_moi_hien_nut_quan_tri(client, monkeypatch):
    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)
    html = (await client.get("/lich")).text
    assert 'href="/admin">Quản trị</a>' in html
    assert '<form method="post" action="/admin/logout">' in html
    assert ">Đăng nhập</a>" not in html


async def test_root_html_cong_khai_health_check_van_json(monkeypatch):
    import web

    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_out)
    transport = httpx.ASGITransport(app=web.api)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        assert (await c.get("/", headers={"Accept": "application/json"})).json() == {"status": "ok"}
        page = await c.get("/", headers={"Accept": "text/html"})
        assert page.status_code == 200 and "Hôm nay" in page.text
        assert "đăng nhập" not in page.text.lower()


_TUVI_FORM = {
    "ngay": "15",
    "thang": "3",
    "nam": "1990",
    "gio": "5",
    "gioi": "male",
    "lich": "duong",
}


async def test_ai_luan_giai_chi_khi_dang_nhap(client, monkeypatch):
    calls = []

    async def fake_ai_text(user_id, kind, channel, explain):
        calls.append((kind, channel))
        return "## Tổng quan\n**Mệnh** vững vàng."

    monkeypatch.setattr(web_site.divination_commands, "ai_text", fake_ai_text)
    # Chưa đăng nhập: bấm AI (ai=1) cũng không gọi AI, và không thấy khối AI.
    html = (await client.post("/tu-vi", data={**_TUVI_FORM, "ai": "1"})).text
    assert calls == [] and "Luận giải bằng AI" not in html
    html = (await client.post("/than-so-hoc", data={"ngay": "1990-03-15", "ai": "1"})).text
    assert calls == [] and "Luận giải bằng AI" not in html

    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)
    html = (await client.post("/tu-vi", data=_TUVI_FORM)).text
    assert calls == [] and "AI luận giải lá số" in html  # chưa bấm thì chưa tốn lượt AI
    assert '<input type="hidden" name="nam" value="1990">' in html
    html = (await client.post("/tu-vi", data={**_TUVI_FORM, "ai": "1"})).text
    assert calls == [("tuvi_chitiet", "web")]
    assert "Tổng quan\nMệnh vững vàng." in html and "**" not in html
    html = (
        await client.post("/than-so-hoc", data={"ngay": "1990-03-15", "ten": "An", "ai": "1"})
    ).text
    assert calls[-1] == ("tsh_chitiet", "web") and "Mệnh vững vàng." in html


async def test_them_xoa_dia_diem_thoi_tiet(client, monkeypatch):
    store = {}

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    async def fake_get_weather(location="", days=1):
        return _FAKE_WEATHER

    monkeypatch.setattr(web_site.weather_places.db, "get_setting", get_setting)
    monkeypatch.setattr(web_site.weather_places.db, "set_setting", set_setting)
    monkeypatch.setattr(web_site.weather, "get_weather", fake_get_weather)

    # Chưa đăng nhập: thấy chip nhưng không có nút thêm/xoá, POST bị từ chối.
    html = (await client.get("/thoi-tiet")).text
    assert ">Nha Trang</a>" in html and 'class="chip-x"' not in html and "dia-diem/them" not in html
    r = await client.post("/thoi-tiet/dia-diem/them", data={"ten": "Vũng Tàu"})
    assert r.status_code == 303 and r.headers["location"] == "/thoi-tiet" and store == {}

    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)
    html = (await client.get("/thoi-tiet", params={"noi": "Vũng Tàu"})).text
    assert 'aria-label="Xoá Huế"' in html and 'value="Vũng Tàu" placeholder' in html
    r = await client.post(
        "/thoi-tiet/dia-diem/them", data={"ten": " Vũng  Tàu ", "noi": "Vũng Tàu"}
    )
    assert r.headers["location"] == "/thoi-tiet?tb=da-them&noi=V%C5%A9ng+T%C3%A0u"
    html = (await client.get(r.headers["location"])).text
    assert ">Vũng Tàu</a>" in html and "Đã thêm địa điểm." in html
    await client.post(
        "/thoi-tiet/dia-diem/them", data={"ten": "vung tau"}
    )  # trùng (bỏ dấu) -> bỏ qua
    r = await client.post("/thoi-tiet/dia-diem/xoa", data={"ten": "Huế"})
    assert "tb=da-xoa" in r.headers["location"]
    assert web_site.weather_places.json.loads(store["web_weather_places"]) == [
        "TP.HCM", "Hà Nội", "Đà Nẵng", "Nha Trang", "Đà Lạt", "Cần Thơ", "Hải Phòng", "Vũng Tàu",
    ]  # fmt: skip
    r = await client.post("/thoi-tiet/dia-diem/them", data={"ten": "<script>"})
    assert "tb=ten" in r.headers["location"]
    html = (await client.get("/thoi-tiet", params={"tb": "XyzLaHoat"})).text
    assert "XyzLaHoat" not in html and "ok-box" not in html.split("</head>")[1]  # chỉ mã cố định


async def test_trang_lich_thang(client):
    r = await client.get("/lich", params={"ngay": "2026-10-24"})
    assert r.status_code == 200
    assert "<h1>Lịch âm tháng 10/2026</h1>" in r.text and 'aria-current="date"' in r.text
    assert "ngày 15 tháng 9 năm Bính Ngọ âm lịch" in r.text
    assert 'href="/lich/hoi"' in r.text  # thanh mục con


async def test_trang_lich_tach_rieng(client):
    ask = await client.get("/lich/hoi", params={"q": "trung thu"})
    assert "Tết Trung thu" in ask.text and 'aria-current="page">Hỏi lịch' in ask.text
    conv = await client.get("/lich/doi-ngay", params={"am": "15/8/2027"})
    assert "Thứ Tư, 15/09/2027" in conv.text
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
    assert "Lập lá số khác" in r.text and 'id="ngay"' not in r.text  # trang kết quả riêng
    assert "(đắc)" in r.text and 'title="miếu"' in r.text  # độ sáng sao
    assert "Hóa Lộc" in r.text and "Đẩu Quân" in r.text
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


async def test_tuvi_gio_ty_dau_tren_web_va_api(client):
    html = (await client.get("/tu-vi")).text
    assert 'value="12"' in html and "Tý đầu" in html and "Tháng nhuận: sinh ngày 1-15" in html
    r = await client.post("/tu-vi", data={**_TUVI_FORM, "gio": "12"})
    assert "an theo ngày hôm sau" in r.text and "ngày 20 tháng 2 năm Canh Ngọ" in r.text
    assert "Sinh 15/03/1990 dương lịch" in r.text  # vẫn hiển thị ngày đã nhập
    api = await client.post(
        "/api/v1/tuvi/charts",
        json={"birthDate": "1990-03-15", "hourBranch": 0, "gender": "male", "isEarlyTy": True},
    )
    assert api.json()["isEarlyTy"] is True and api.json()["lunarDate"]["day"] == 20
    bad = await client.post(
        "/api/v1/tuvi/charts",
        json={"birthDate": "1990-03-15", "hourBranch": 5, "gender": "male", "isEarlyTy": True},
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_HOUR"


def test_clean_ai_text_bo_markdown_ca_dong_thut_dau():
    assert web_site._clean_ai_text("  ## Tổng quan\n**Mệnh** tốt") == "Tổng quan\nMệnh tốt"


async def test_hoi_lich_nam_ngoai_khoang_khong_500(client):
    for q in ("25/12/9999", "noel năm 9999", "ngày 25 tháng 12 năm 9999"):
        page = await client.get("/lich/hoi", params={"q": q})
        text = page.text.lower()
        assert page.status_code == 200 and ("chưa hiểu" in text or "1900-2100" in text), q
        api = await client.get("/api/v1/calendar/answers", params={"q": q})
        assert api.status_code == 200, q


async def test_hoi_lich_tren_web_khong_lo_giong_bot(client):
    """Câu không hiểu: gợi ý cách hỏi kiểu web, không có cú pháp /lich hay "Em chưa hiểu"."""
    for q in ("abc", "help", "tuổi ty hợp tuổi nào"):
        html = (await client.get("/lich/hoi", params={"q": q})).text
        answer = html.split('<div class="answer-body">')[1].split("</div>")[0]
        assert "/lich" not in answer and "Em chưa" not in answer, q
    html = (await client.get("/lich/hoi", params={"q": "abc"})).text
    assert "Chưa hiểu câu hỏi này" in html and "xem ngày động thổ" in html
    # Chữ kiểu tin nhắn chat thành chữ của trang: không emoji đầu dòng, "• " là danh sách.
    html = (await client.get("/lich/hoi", params={"q": "các ngày lễ"})).text
    answer = html.split('<div class="answer-body">')[1].split("</div>")[0]
    assert "🎉" not in answer and "•" not in answer and " - " not in answer
    assert answer.startswith("<p>Các ngày lễ sắp tới:</p><ul><li>")
    assert web_site._answer_html("📅 Tháng 10:\n10 T7 | âm 1/9 ⭐ ·hđ\n<b>") == (
        "<p>Tháng 10:<br>10 T7 | âm 1/9 ★ (hđ)<br>&lt;b&gt;</p>"
    )
    api = await client.get("/api/v1/calendar/answers", params={"q": "29/2/2027"})
    assert api.json()["answer"].startswith("Năm 2027 không có ngày 29/2")


async def test_thoi_tiet_gioi_han_dia_diem_la(client, monkeypatch):
    calls = []

    async def fake_get_weather(location="", days=1):
        calls.append(location)
        return _FAKE_WEATHER

    monkeypatch.setattr(web_site.weather, "get_weather", fake_get_weather)
    monkeypatch.setattr(web_site, "_new_place_times", web_site.collections.deque())
    monkeypatch.setattr(web_site, "_new_place_by_ip", web_site.collections.OrderedDict())
    monkeypatch.setattr(web_site, "_NEW_PLACE_PER_IP", 2)
    monkeypatch.setattr(web_site, "_NEW_PLACE_LIMIT", 3)
    ip_a = {"X-Forwarded-For": "1.1.1.1"}
    for i in range(2):
        assert "°C" in (await client.get("/thoi-tiet", params={"noi": f"La{i}"}, headers=ip_a)).text
    html = (await client.get("/thoi-tiet", params={"noi": "La9"}, headers=ip_a)).text
    assert "Bạn vừa tra nhiều địa điểm mới" in html and 'role="alert"' in html
    # Một người gõ liên tục không khoá được người khác.
    ip_b = {"X-Forwarded-For": "2.2.2.2"}
    assert "°C" in (await client.get("/thoi-tiet", params={"noi": "Lb"}, headers=ip_b)).text
    assert len(calls) == 3
    # Trần chung toàn site vẫn còn (3 lượt đã dùng).
    html = (
        await client.get("/thoi-tiet", params={"noi": "Lc"}, headers={"X-Forwarded-For": "3.3.3.3"})
    ).text
    assert "tạm giới hạn" in html and len(calls) == 3
    # Địa điểm trong danh sách hay xem và mặc định không bị giới hạn.
    assert "°C" in (await client.get("/thoi-tiet", params={"noi": "đà lạt"}, headers=ip_a)).text
    assert "°C" in (await client.get("/thoi-tiet", headers=ip_a)).text
    assert len(calls) == 5
    # Địa điểm đã có trong cache toạ độ (kể cả "không tìm thấy") tra lại không trừ lượt.
    monkeypatch.setitem(web_site.weather._geo_cache, "xyzabc", None)
    html = (await client.get("/thoi-tiet", params={"noi": "Xyzabc"}, headers=ip_a)).text
    assert "giới hạn" not in html and calls[-1] == "Xyzabc"
    # Cửa sổ 10 phút đã qua. Tính từ đồng hồ thật: hằng số cố định (10_000) sai
    # khi máy chạy test đã bật quá ~2,6 giờ (time.monotonic() lớn hơn).
    later = web_site.time.monotonic() + web_site._NEW_PLACE_WINDOW_SEC + 1
    assert web_site._new_place_allowed("1.1.1.1", now=later) == ""


def test_bang_ip_tra_dia_diem_co_tran(monkeypatch):
    monkeypatch.setattr(web_site, "_new_place_times", web_site.collections.deque())
    monkeypatch.setattr(web_site, "_new_place_by_ip", web_site.collections.OrderedDict())
    monkeypatch.setattr(web_site, "_NEW_PLACE_LIMIT", 10_000)
    for i in range(web_site._NEW_PLACE_MAX_IPS + 20):
        assert web_site._new_place_allowed(f"10.0.{i // 256}.{i % 256}") == ""
    assert len(web_site._new_place_by_ip) == web_site._NEW_PLACE_MAX_IPS


async def test_trang_than_so_hoc_escape_ho_ten(client):
    r = await client.post("/than-so-hoc", data={"ngay": "1990-03-15", "ten": "<script>x</script>"})
    assert "Con số chủ đạo" in r.text and "Tính cho người khác" in r.text
    assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text
    assert "4 đỉnh cao" in r.text and "4 thử thách" in r.text and "Biểu đồ tổng hợp" in r.text
    no_name = await client.post("/than-so-hoc", data={"ngay": "1990-03-15"})
    assert "Biểu đồ tổng hợp" not in no_name.text and "Nhập họ tên" in no_name.text


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
    assert "<dd>0,3 mm</dd>" in html  # dấu phẩy thập phân kiểu Việt, không "0.3"

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
    css = await client.get("/static/site.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert (await client.get("/static/og-image.png")).headers["content-type"] == "image/png"
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
    assert data["lifePath"]["value"] == 10 and data["soul"]["value"] == 1
    assert data["arrows"][0]["digits"] == "1-5-9"
    r = await client.post(
        "/api/v1/tuvi/charts",
        json={"birthDate": "1990-03-15", "hourBranch": 5, "gender": "male"},
    )
    chart = r.json()
    assert chart["cuc"] == "Thổ ngũ cục"
    assert chart["palaces"][0]["name"] == "Mệnh" and chart["palaces"][0]["branch"] == "Tuất"
    assert chart["palaces"][0]["mainStars"] == ["Phá Quân"]  # trường cũ giữ nguyên
    # Trường mới (thêm, không đổi trường cũ): từng sao kèm độ sáng và Tứ hóa.
    menh = chart["palaces"][0]["stars"][0]
    assert menh == {
        "name": "Phá Quân", "isMain": True, "hoa": None, "brightness": "Đ", "brightnessName": "đắc",
    }  # fmt: skip
    dien = next(p for p in chart["palaces"] if p["name"] == "Điền Trạch")
    assert dien["mainStars"] == ["Thái Dương (Hóa Lộc)", "Thái Âm (Hóa Khoa)"]
    assert dien["stars"][0]["hoa"] == "Hóa Lộc" and dien["stars"][0]["brightness"] == "Đ"
    assert chart["cucRelation"] and chart["isThuan"] is True and chart["brightnessLegend"]["M"]
    assert data["maturity"]["value"] == 4 and data["lifePath"]["single"] == 1
    assert [p["value"] for p in data["pinnacles"]] == [9, 7, 7, 4]
    assert data["challenges"][0]["label"] == "0-26 tuổi, đỉnh ở tuổi 26"
    assert data["pinnacles"][0]["peakAge"] == 26 and data["combinedChart"]["5"] == 6


async def test_api_thang_nhuan_va_dinh_dang_ngay(client):
    # GET conversions: isLeapMonth nhận true/false/1/0, giá trị khác -> 422.
    base = {"lunarDay": 15, "lunarMonth": 6, "lunarYear": 2025}
    normal = await client.get("/api/v1/calendar/conversions", params=base)
    leap = await client.get("/api/v1/calendar/conversions", params={**base, "isLeapMonth": "true"})
    assert normal.json()["date"] == "2025-07-09" and leap.json()["date"] == "2025-08-08"
    assert (
        await client.get("/api/v1/calendar/conversions", params={**base, "isLeapMonth": "1"})
    ).json()["date"] == "2025-08-08"
    bad = await client.get("/api/v1/calendar/conversions", params={**base, "isLeapMonth": "yes"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_LEAP_FLAG"
    # POST tuvi: lịch âm + tháng nhuận; cờ phải là bool; nhuận chỉ đi với lunar.
    body = {"calendar": "lunar", "birthDate": "2025-06-15", "hourBranch": 0, "gender": "male"}
    r = await client.post("/api/v1/tuvi/charts", json={**body, "isLeapMonth": True})
    assert r.json()["lunarDate"]["isLeapMonth"] is True and r.json()["solarDate"] == "2025-08-08"
    r = await client.post("/api/v1/tuvi/charts", json={**body, "isLeapMonth": "true"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_FLAG"
    r = await client.post(
        "/api/v1/tuvi/charts", json={**body, "calendar": "solar", "isLeapMonth": True}
    )
    assert r.json()["error"]["code"] == "INVALID_LEAP_FLAG"
    # birthDate: cả hai POST nhận YYYY-MM-DD lẫn DD/MM/YYYY; GET days không cần đệm 0.
    r = await client.post(
        "/api/v1/tuvi/charts", json={"birthDate": "15/03/1990", "hourBranch": 5, "gender": "male"}
    )
    assert r.status_code == 200 and r.json()["palaces"][0]["mainStars"] == ["Phá Quân"]
    assert (await client.get("/api/v1/calendar/days/1990-3-5")).status_code == 200
    r = await client.post("/api/v1/numerology/readings", json={})
    assert r.status_code == 422 and "Thiếu ngày" in r.json()["error"]["message"]
    r = await client.post(
        "/api/v1/numerology/readings", json={"birthDate": "1990-03-15", "name": "123 !!!"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_NAME"


async def test_api_404_405_cung_dang_loi_va_khong_mo_docs(monkeypatch):
    import web

    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_out)
    transport = httpx.ASGITransport(app=web.api)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        r = await c.get("/api/v1/nope")
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
        r = await c.get("/api/v1/tuvi/charts")
        assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
        for path in ("/docs", "/openapi.json", "/redoc"):
            assert (await c.get(path)).status_code == 404, path
        assert (await c.get("/khong-co")).status_code == 404  # ngoài API: dạng mặc định


async def test_api_giu_truong_cu_va_truong_moi_song_song(client):
    day = (await client.get("/api/v1/calendar/days/2026-10-21")).json()
    assert day["weekday"] == "Thứ Tư" and day["weekdayIndex"] == 2
    month = (await client.get("/api/v1/calendar/months/2026-10")).json()
    assert month["days"][20]["weekday"] == 2 and month["days"][20]["weekdayName"] == "Thứ Tư"
    assert set(day) >= {"date", "weekday", "lunar", "solarTerm", "luckyHours", "dayGod", "isHoangDao",
                        "isTamNuong", "isNguyetKy", "holidays", "nextTet", "daysToTet"}  # fmt: skip
    chart = (
        await client.post(
            "/api/v1/tuvi/charts",
            json={"birthDate": "1990-03-15", "hourBranch": 5, "gender": "male"},
        )
    ).json()
    assert set(chart) >= {"solarDate", "lunarDate", "hourBranch", "gender", "yinYang", "napAm", "element", "cuc",
                          "menhChu", "thanChu", "thanPalace", "tuHoa", "palaces", "menhNotes", "isMenhEmpty",
                          "menhEmptyNote", "disclaimer"}  # fmt: skip
    palace = chart["palaces"][0]
    assert set(palace) >= {"name", "branch", "branchIndex", "stem", "mainStars", "minorStars", "isThan",
                           "hasTuan", "hasTriet", "daiHan", "trangSinh"}  # fmt: skip
    assert palace["daiHan"] == {"from": 5, "to": 14, "fromAge": 5, "toAge": 14}
    assert chart["menhNotes"][0]["star"] == chart["menhNotes"][0]["name"] == "Phá Quân"
    reading = (
        await client.post(
            "/api/v1/numerology/readings", json={"birthDate": "1990-03-15", "name": "An"}
        )
    ).json()
    assert set(reading) >= {"birthDate", "name", "lifePath", "dayNumber", "attitude", "personalYear",
                            "personalMonth", "expression", "soul", "personality", "chart", "missingDigits",
                            "arrows", "emptyArrows", "disclaimer"}  # fmt: skip
    assert reading["asOf"] and reading["personalMonthDetail"]["value"] == reading["personalMonth"]
    assert 1 <= reading["personalMonthDetail"]["month"] <= 12


@pytest.mark.parametrize(
    "kw,code",
    [
        ({"json": {"birthDate": "1990-03-15", "hourBranch": True, "gender": "male"}}, "INVALID_HOUR"),
        ({"json": {"birthDate": "1990-03-15", "hourBranch": 5.0, "gender": "male"}}, "INVALID_HOUR"),
        ({"json": {"birthDate": "1990-03-15", "hourBranch": 12, "gender": "male"}}, "INVALID_HOUR"),
        ({"content": b""}, "INVALID_HOUR"),
        ({"content": b"null", "headers": {"content-type": "application/json"}}, "INVALID_JSON"),
    ],
)  # fmt: skip
async def test_api_tuvi_tu_choi_hour_branch_sai_kieu_va_body_rong(client, kw, code):
    r = await client.post("/api/v1/tuvi/charts", **kw)
    assert r.status_code == 422 and r.json()["error"]["code"] == code


async def test_api_body_qua_lon(client):
    r = await client.post("/api/v1/numerology/readings", content=b"{" + b" " * 5000 + b"}")
    assert r.json()["error"]["code"] == "BODY_TOO_LARGE"


async def test_css_rieng_cache_dai_trang_nhe(client):
    """CSS là 1 file riêng có ?v=<hash nội dung>, cache 1 năm (immutable): chỉ lượt xem
    đầu tải thêm 1 request, các trang sau dùng bản đã lưu, không hỏi lại server (giữ
    --limit-concurrency của uvicorn cho webhook Telegram/Zalo). HTML không còn mang
    ~50 KB CSS mỗi trang. Phông chữ tải từ Google Fonts (máy chủ khác)."""
    import re

    for path in ("/lich", "/lich/hoi", "/tu-vi", "/than-so-hoc"):
        html = (await client.get(path)).text
        assert "<style>" not in html and len(html) < 30_000, path
        sheets = re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)
        assert [h for h in sheets if h.startswith("/static/")] == [
            f"/static/site.css?v={web_site._assets()[0]}"
        ], path
        refs = re.findall(r'(?:src|href|srcset)="(/static/[^"]+)"', html)
        assert refs and all(re.search(r"\?v=[0-9a-f]{10}$", r) for r in refs), path
    for name in ("site.css", "logo-dark.webp"):
        r = await client.get(f"/static/{name}")
        assert "immutable" in r.headers["cache-control"], name


def test_uvicorn_limit_concurrency_du_cho_web_va_webhook():
    import re
    from pathlib import Path

    conf = (Path(__file__).resolve().parent.parent / "supervisord.conf").read_text()
    limit = int(re.search(r"--limit-concurrency (\d+)", conf).group(1))
    assert limit >= 32


async def test_nut_doi_sang_toi_va_logo_theo_giao_dien(client):
    html = (await client.get("/lich")).text
    # Nút ẩn khi không có JS (theo hệ máy); script đọc lựa chọn TRƯỚC khi nạp CSS.
    assert 'id="theme-toggle"' in html and "hidden>" in html
    assert html.index("lcta.theme") < html.index('href="/static/site.css')
    assert 'class="logo-light"' in html and 'class="logo-dark"' in html
    css = (await client.get("/static/site.css")).text
    assert ':root[data-theme="dark"]' in css  # CSS ghi đè theo lựa chọn


def test_dai_gio_hoang_dao_trang_chu():
    lucky = [
        "Dần (3h-5h)",
        "Mão (5h-7h)",
        "Tỵ (9h-11h)",
        "Thân (15h-17h)",
        "Tuất (19h-21h)",
        "Hợi (21h-23h)",
    ]
    hours = web_site._hour_strip(lucky, 9)
    assert [h["name"] for h in hours if h["isGood"]] == ["Dần", "Mão", "Tỵ", "Thân", "Tuất", "Hợi"]
    assert [h["name"] for h in hours if h["isNow"]] == ["Tỵ"]
    # 23h và 0h đều là giờ Tý (vắt qua nửa đêm).
    assert (
        web_site._hour_strip(lucky, 23)[0]["isNow"] and web_site._hour_strip(lucky, 0)[0]["isNow"]
    )
    assert web_site._hour_strip(lucky, 22)[11]["isNow"]


def test_dai_gio_chi_to_dam_mot_gio_tot_gan_nhat():
    lucky = ["Dần (3h-5h)", "Tỵ (9h-11h)", "Thân (15h-17h)"]
    at_ngo = web_site._hour_strip(lucky, 12)  # giờ Ngọ: không tốt
    assert [h["name"] for h in at_ngo if h["isFocus"]] == ["Thân"]
    assert [h["name"] for h in at_ngo if h["isPast"]] == ["Tý", "Sửu", "Dần", "Mão", "Thìn", "Tỵ"]
    at_ty = web_site._hour_strip(lucky, 10)  # đang trong giờ tốt Tỵ
    assert [h["name"] for h in at_ty if h["isFocus"]] == ["Tỵ"]
    # Sau giờ tốt cuối cùng của ngày thì không còn ô nào được tô đậm.
    assert not any(h["isFocus"] for h in web_site._hour_strip(lucky, 18))
    # 23h là giờ Tý mở đầu ngày mới: chưa giờ nào "đã qua".
    assert not any(h["isPast"] for h in web_site._hour_strip(lucky, 23))


async def test_sua_thong_tin_la_so_ve_form_dien_san(client):
    r = await client.post("/tu-vi", data={**_TUVI_FORM, "sua": "1"})
    assert 'id="ngay"' in r.text and 'value="1990"' in r.text and "Thổ ngũ cục" not in r.text
    assert '<option value="5" selected>' in r.text


async def test_form_tuvi_chua_chon_gio_va_ky_khong_mau_tot(client):
    html = (await client.get("/tu-vi")).text
    assert '<option value="" selected disabled>Chọn giờ sinh</option>' in html
    # Tý đầu (23h) đứng trước Tý (0h): xếp theo giờ đồng hồ.
    assert html.index('value="12"') < html.index('<option value="0"')
    no_hour = await client.post("/tu-vi", data={**_TUVI_FORM, "gio": ""})
    assert 'role="alert"' in no_hour.text and "giờ sinh" in no_hour.text
    r = await client.post("/tu-vi", data=_TUVI_FORM)
    assert 'pill sm pos">Hóa Kỵ' not in r.text and 'pill sm warn">Hóa Kỵ' in r.text


def test_trang_chu_co_dai_12_gio():
    import re

    html = bytes(web_site.home_page().body).decode()
    assert (
        len(re.findall(r'class="hour[ "]', html)) == 12 and html.count('aria-current="time"') == 1
    )
    assert 'href="/" aria-current="page"' in html  # mục Hôm nay trên thanh bên


def test_trang_chu_va_admin_co_link_ky_mon():
    from pathlib import Path

    html = bytes(web_site.home_page().body).decode()
    assert 'href="/ky-mon"' in html and 'id="kymon-title"' in html
    assert html.index('id="tuvi-title"') < html.index('id="kymon-title"')  # cạnh Tử vi
    admin = (Path(__file__).resolve().parent.parent / "templates" / "admin.html").read_text("utf-8")
    nav = admin[admin.index("Công cụ web") :]
    assert (
        nav.index('href="/tu-vi"') < nav.index('href="/ky-mon"') < nav.index('href="/than-so-hoc"')
    )


# ─── Xổ số /xo-so + /api/v1/lottery ─────────────────────────────────────────


@pytest.fixture
def lottery_sources(monkeypatch):
    import datetime as dt

    from _vn_data_samples import XSMB_RSS, XSMN_RSS, XSTN_RSS
    from services import site_data, vn_daily_chat
    from services import vn_market_data as market

    today = dt.date(2026, 10, 26)  # thứ Hai
    state = {"down": False}
    region = {
        "mn": market.parse_lottery_rss(XSMN_RSS, "mn", today),
        "mb": market.parse_lottery_rss(XSMB_RSS, "mb", today),
    }
    tay_ninh = market.parse_lottery_rss(XSTN_RSS, "mn", today, name="Tây Ninh")

    async def get_lottery(code):
        if state["down"]:
            raise market.MarketDataError("down")
        return region[code]

    async def get_province(province):
        if state["down"] or province.key != "tay-ninh":
            raise market.MarketDataError("down")
        return tay_ninh

    monkeypatch.setattr(market, "get_lottery", get_lottery)
    monkeypatch.setattr(market, "get_province_lottery", get_province)
    monkeypatch.setattr(vn_daily_chat, "_today", lambda: today)
    monkeypatch.setattr(site_data, "today", lambda: today)
    return state


async def test_trang_xo_so_mac_dinh_va_theo_tinh(client, lottery_sources):
    html = (await client.get("/xo-so")).text
    assert 'href="/xo-so" aria-current="page"' in html  # mục Xổ số trên thanh bên
    assert "Kiên Giang" in html and "729510" in html and "Chủ Nhật 25/10/2026" in html
    assert '<option value="ho-chi-minh">★ TP.HCM (T2/T7)</option>' in html
    html = (await client.get("/xo-so", params={"dai": "tay-ninh"})).text
    assert (
        "<h1>Kết quả xổ số Tây Ninh</h1>" in html
        and "058734" in html
        and "Thứ Năm 22/10/2026" in html
    )
    assert '<option value="tay-ninh" selected>Tây Ninh (T5)</option>' in html
    assert "Đài Tây Ninh quay Thứ Năm lúc 16h15." in html
    html = (await client.get("/xo-so", params={"dai": "tay-ninh", "ngay": "2026-10-26"})).text
    assert "Hôm nay đài Tây Ninh không quay" in html and "22/10/2026" in html


async def test_trang_xo_so_do_ve_va_escape(client, lottery_sources):
    html = (await client.get("/xo-so", params={"dai": "tay-ninh", "ve": "058734"})).text
    assert "Dò vé 058734" in html and "Trúng Giải Đặc biệt" in html
    html = (await client.get("/xo-so", params={"dai": "mn", "ve": "000000"})).text
    assert "Không trúng" in html
    html = (await client.get("/xo-so", params={"dai": "<script>alert(1)</script>"})).text
    assert "<script>alert(1)" not in html and "Không có đài xổ số này" in html
    html = (await client.get("/xo-so", params={"dai": "mn", "ve": "12ab"})).text
    assert "có 6 chữ số" in html and "729510" in html  # vé sai: vẫn hiện kỳ mới nhất
    lottery_sources["down"] = True
    r = await client.get("/xo-so", params={"dai": "tay-ninh"})
    # Nguồn lỗi: 503 + Retry-After cho máy tìm kiếm, người xem vẫn thấy trang báo lỗi.
    assert r.status_code == 503 and r.headers["retry-after"] == "300"
    assert "không tự đoán số" in r.text


async def test_api_lottery(client, lottery_sources):
    r = await client.get("/api/v1/lottery", params={"province": "tay-ninh", "ticket": "058734"})
    data = r.json()
    assert r.status_code == 200 and data["status"] == "latest"
    assert data["target"]["drawDays"] == ["Thứ Năm"] and data["target"]["drawsToday"] is False
    assert data["draw"]["date"] == "2026-10-22"
    assert data["draw"]["provinces"][0]["prizes"]["ĐB"] == ["058734"]
    assert data["ticket"]["results"][0]["wins"][0]["code"] == "ĐB"
    assert data["availableDates"] == ["2026-10-22", "2026-10-15"]
    r = await client.get("/api/v1/lottery", params={"province": "Tây Ninh", "date": "2026-10-15"})
    assert r.json()["status"] == "exact" and r.json()["draw"]["date"] == "2026-10-15"
    r = await client.get("/api/v1/lottery", params={"province": "mb"})
    assert r.json()["draw"]["provinces"][0]["prizes"]["ĐB"] == ["30328"]
    mn = (await client.get("/api/v1/lottery")).json()
    assert mn["target"]["key"] == "mn" and len(mn["draw"]["provinces"]) == 2
    # Feed riêng lỗi -> lọc từ feed miền.
    kg = (await client.get("/api/v1/lottery", params={"province": "kien-giang"})).json()
    assert [p["name"] for p in kg["draw"]["provinces"]] == ["Kiên Giang"]


@pytest.mark.parametrize(
    "params,code",
    [
        ({"province": "xyz"}, "INVALID_PROVINCE"),
        ({"date": "26/13/2026"}, "INVALID_DATE"),
        ({"date": "2030-01-01"}, "INVALID_DATE"),
        ({"province": "mb", "ticket": "123456"}, "INVALID_TICKET"),
    ],
)
async def test_api_lottery_loi_nhap(client, lottery_sources, params, code):
    r = await client.get("/api/v1/lottery", params=params)
    assert r.status_code == 422 and r.json()["error"]["code"] == code


async def test_api_lottery_nguon_loi(client, lottery_sources):
    lottery_sources["down"] = True
    r = await client.get("/api/v1/lottery", params={"province": "mn"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "SOURCE_UNAVAILABLE"


# ─── Giá vàng /gia-vang, giá xăng /gia-xang + /api/v1/gold, /fuel, /fx ───────


@pytest.fixture
def price_sources(monkeypatch):
    """Nguồn giá dựng từ mẫu thật (không mạng); state[<nguồn>] = True để làm nguồn lỗi.
    Ghi lại lượt gọi để kiểm tra trang đi đúng hàm có cache sẵn, không gọi thừa."""
    from _vn_data_samples import FUEL_HTML, FX_XML, SJC_JSON, VANG_TODAY_JSON

    from services import fuel_prices
    from services import vn_market_data as market

    state = {"live": False, "sjc": False, "fx": False, "fuel": False, "calls": []}
    tables = {
        "live": market.parse_vang_today_json(VANG_TODAY_JSON),
        "sjc": market.parse_sjc_json(SJC_JSON),
        "fx": market.parse_fx_xml(FX_XML),
        "fuel": fuel_prices.parse_fuel_html(FUEL_HTML),
    }

    def source(name):
        async def get():
            state["calls"].append(name)
            if state[name]:
                raise market.MarketDataError("down")
            return tables[name]

        return get

    monkeypatch.setattr(market, "get_gold_live", source("live"))
    monkeypatch.setattr(market, "get_gold", source("sjc"))
    monkeypatch.setattr(market, "get_fx", source("fx"))
    monkeypatch.setattr(fuel_prices, "get_fuel", source("fuel"))
    return state


async def test_trang_gia_vang(client, price_sources):
    r = await client.get("/gia-vang")
    html = r.text
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert 'href="/gia-vang" aria-current="page"' in html  # mục Giá vàng trên thanh bên
    for name in ("SJC miếng 9999", "Nhẫn SJC 9999", "DOJI Hà Nội", "DOJI TP.HCM", "PNJ 24K"):
        assert name in html
    assert "Bảo Tín 9999" in html and "Viettin SJC" in html and "VN Gold SJC" in html
    # Bảng giá: mọi ô 2 số lẻ (135,00 thẳng cột với 138,70), đơn vị ghi ngay trên bảng.
    assert (
        ">141,70</span>" in html
        and ">135,00</span>" in html
        and "Cập nhật 07:30 27/10/2026" in html
    )
    assert "Triệu đồng/lượng" in html and "1 lượng = 10 chỉ = 37,5 g" in html
    # ▲▼ kèm chữ cho trình đọc màn hình, màu theo lớp up/down; 0 không ghi "đứng giá".
    up = (
        '<span class="delta up"><span aria-hidden="true">▲</span><span class="sr-only">tăng </span>'
    )
    down = '<span class="delta down"><span aria-hidden="true">▼</span><span class="sr-only">giảm '
    assert up + "0,50</span>" in html and down + "</span>0,30</span>" in html
    assert "đứng giá" not in html
    # Vàng thế giới + quy đổi theo giá bán USD Vietcombank, chênh với SJC miếng.
    assert "4.015,1" in html and "USD/ounce" in html and "26.030" in html
    assert "≈ 126,01 triệu đồng/lượng" in html and "Cao hơn ≈ 15,69 triệu đồng/lượng" in html
    assert "chưa gồm thuế" in html
    # Mục tỷ giá các ngoại tệ chính, số kiểu Việt Nam.
    assert 'id="fx-title"' in html and "25.650" in html and "173,65" in html
    assert "Đô la Mỹ" in html and "Yên Nhật" in html
    assert "vang.today, SJC dự phòng" in html
    assert sorted(price_sources["calls"]) == ["fx", "live"]  # vang.today chạy: không gọi SJC


async def test_trang_gia_vang_du_phong_sjc_va_nguon_loi(client, price_sources):
    price_sources["live"] = True
    html = (await client.get("/gia-vang")).text
    assert "tạm hiện bảng giá SJC" in html and "Vàng SJC 1L, 10L, 1KG" in html
    assert "Hồ Chí Minh" in html and "Hà Nội" not in html  # giữ chi nhánh đầu
    assert ">150,00</span>" in html and 'id="world-title"' not in html and "sjc.com.vn" in html
    price_sources["sjc"] = True
    r = await client.get("/gia-vang")
    assert r.status_code == 503 and 'role="alert"' in r.text and "không tự đoán giá" in r.text
    assert 'id="gold-title"' not in r.text


async def test_trang_gia_vang_thieu_ty_gia_van_hien_bang(client, price_sources):
    price_sources["fx"] = True
    html = (await client.get("/gia-vang")).text
    assert "141,70" in html and "4.015,1" in html and "tạm chưa quy đổi" in html
    assert 'id="fx-title"' not in html and 'role="alert"' not in html


async def test_trang_gia_vang_escape_chu_tu_nguon(client, price_sources, monkeypatch):
    from services import vn_market_data as market

    evil = market.GoldTable(
        updated="<img src=x onerror=alert(1)>",
        rows=(market.GoldRow("<script>alert(1)</script>", "<b>HCM</b>", 1.5e8, 1.52e8),),
    )
    price_sources["live"] = True

    async def get_gold():
        return evil

    monkeypatch.setattr(market, "get_gold", get_gold)
    html = (await client.get("/gia-vang")).text
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html and "<b>HCM</b>" not in html


async def test_trang_gia_xang(client, price_sources):
    r = await client.get("/gia-xang")
    html = r.text
    assert r.status_code == 200 and 'href="/gia-xang" aria-current="page"' in html
    assert "Xăng E5 RON 92" in html and "25.060" in html and "25.560" in html
    assert "DO 0,05S Mức 2" in html and 'id="pvoil-title"' in html
    assert "Dầu DO 0,001S Mức 5" in html
    assert "Cập nhật 00:00 27/10/2026" in html and "giaxanghomnay.com" in html
    assert '<span class="sr-only">giảm </span>1.610' in html
    assert '<span class="sr-only">tăng </span>450' in html
    assert 'data-label="Vùng 2"' in html  # nhãn cột cho bảng gập trên điện thoại
    price_sources["fuel"] = True
    r = await client.get("/gia-xang")
    assert r.status_code == 503 and 'role="alert"' in r.text and "giá xăng dầu đang lỗi" in r.text


async def test_api_gold_fuel_fx(client, price_sources):
    gold = (await client.get("/api/v1/gold")).json()
    assert gold["source"] == "vang.today" and gold["fallback"] is False
    assert gold["updated"] == "07:30 27/10/2026" and gold["unit"] == "VND/lượng"
    assert gold["domestic"][0] == {
        "code": "SJL1L10",
        "name": "SJC miếng 9999",
        "brand": "sjc",
        "branch": "",
        "buy": 138700000,
        "sell": 141700000,
        "changeBuy": 500000,
        "changeSell": 500000,
    }
    assert all(r["code"] != "XAUUSD" for r in gold["domestic"])
    world = gold["world"]
    assert world["priceUsdPerOunce"] == 4015.1 and world["change"] == 9
    conv = world["conversion"]
    assert conv["usdVnd"] == 26030 and conv["vndPerTael"] == 126006476
    assert conv["sjcSellGap"] == 141700000 - 126006476 and "chưa gồm thuế" in conv["note"]
    assert [r["code"] for r in gold["fx"]["rates"]] == ["USD", "EUR", "JPY", "AUD"]

    fuel = (await client.get("/api/v1/fuel")).json()
    assert fuel["source"] == "giaxanghomnay.com" and fuel["unit"] == "VND/lít"
    assert fuel["petrolimex"][0] == {
        "name": "Xăng E5 RON 92",
        "kind": "xang",
        "change": 0,
        "prevChange": -1610,
        "zone1": 25060,
        "zone2": 25560,
    }
    assert fuel["pvoil"][1]["price"] == 29250 and fuel["pvoil"][1]["kind"] == "diesel"

    fx = (await client.get("/api/v1/fx")).json()
    assert fx["source"] == "Vietcombank" and fx["updated"] == "08:03 26/10/2026"
    assert fx["rates"][0] == {
        "code": "USD",
        "name": "US DOLLAR",
        "nameVi": "Đô la Mỹ",
        "cashBuy": 25620.0,
        "transferBuy": 25650.0,
        "sell": 26030.0,
    }


async def test_api_gold_du_phong_va_nguon_loi_503(client, price_sources):
    price_sources["live"] = True
    gold = (await client.get("/api/v1/gold")).json()
    assert gold["source"] == "sjc.com.vn" and gold["fallback"] is True and gold["world"] is None
    assert gold["domestic"][0]["changeSell"] is None and gold["domestic"][0]["sell"] == 150000000
    for key, url in (("sjc", "/api/v1/gold"), ("fuel", "/api/v1/fuel"), ("fx", "/api/v1/fx")):
        price_sources[key] = True
        r = await client.get(url)
        assert r.status_code == 503 and r.json()["error"]["code"] == "SOURCE_UNAVAILABLE"


async def test_gia_vang_het_han_chot_bao_loi_lich_su(client, price_sources, monkeypatch):
    import asyncio

    from services import vn_market_data as market

    async def slow():
        await asyncio.sleep(5)

    monkeypatch.setattr(market, "get_gold_live", slow)
    monkeypatch.setattr(web_site, "_PRICE_TIMEOUT_SEC", 0.05)
    r = await client.get("/gia-vang")
    assert r.status_code == 503 and "đang lỗi hoặc chậm" in r.text
    r = await client.get("/api/v1/gold")
    assert r.status_code == 503 and r.json()["error"]["code"] == "SOURCE_UNAVAILABLE"


def test_thanh_ben_va_trang_chu_co_gia_vang_gia_xang():
    html = bytes(web_site.home_page().body).decode()
    nav = html[html.index('aria-label="Điều hướng chính"') :]
    assert (
        nav.index('href="/xo-so"') < nav.index('href="/gia-vang"') < nav.index('href="/gia-xang"')
    )
    assert 'class="nav-item i-gold"' in nav and 'class="nav-item i-fuel"' in nav
    # Thẻ trên trang chủ chỉ là link: trang chủ không gọi nguồn giá, không phụ thuộc mạng.
    assert 'id="gold-title"' in html and 'id="fuel-title"' in html
    assert 'class="btn" href="/gia-vang"' in html and 'class="btn" href="/gia-xang"' in html
    css = (web_site._STATIC / "site.css").read_text("utf-8")
    assert ".i-gold" in css and ".i-fuel" in css and ".delta.up" in css


# ─── Sửa sau nhận xét dùng thử 27/10 ─────────────────────────────────────────


async def test_404_html_cho_trinh_duyet_json_cho_api(monkeypatch):
    import web

    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_out)
    transport = httpx.ASGITransport(app=web.api)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        page = await c.get("/khong-co-trang-nay", headers={"Accept": "text/html"})
        assert page.status_code == 404 and page.headers["content-type"].startswith("text/html")
        assert (
            "Không tìm thấy trang" in page.text
            and 'name="robots" content="noindex, follow"' in page.text
        )
        assert 'href="/lich"' in page.text  # có đường đi tiếp
        assert (await c.get("/khong-co-trang-nay")).json() == {"detail": "Not Found"}
        api = await c.get("/api/v1/khong-co", headers={"Accept": "text/html"})
        assert api.json()["error"]["code"] == "NOT_FOUND"


async def test_robots_sitemap_va_the_seo(client, monkeypatch):
    robots = (await client.get("/robots.txt")).text
    assert "Disallow: /admin" in robots and "Disallow: /api/" in robots
    assert "Sitemap: https://testserver/sitemap.xml" in robots
    sitemap = await client.get("/sitemap.xml")
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert "<loc>https://testserver/gia-vang</loc>" in sitemap.text and "/admin" not in sitemap.text
    # Có SITE_BASE_URL (tên miền riêng) thì link tuyệt đối theo đó.
    monkeypatch.setattr(web_site.config, "SITE_BASE_URL", "https://lcta.vn")
    html = (
        await client.get("/lich", params={"thang": "2026-10", "fbclid": "x", "utm_source": "zalo"})
    ).text
    assert '<link rel="canonical" href="https://lcta.vn/lich?thang=2026-10">' in html
    assert '<meta property="og:url" content="https://lcta.vn/lich?thang=2026-10">' in html
    assert (
        '<meta property="og:title" content="Lịch âm tháng 10/2026: ngày hoàng đạo, ngày lễ">'
        in html
    )
    assert "<title>Lịch âm tháng 10/2026: ngày hoàng đạo, ngày lễ | LCTA</title>" in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html


def _json_ld(html: str) -> list[dict]:
    raw = html.split('<script type="application/ld+json">', 1)[1].split("</script>", 1)[0]
    return json.loads(raw)["@graph"]


async def test_seo_tieu_de_co_ngay_json_ld_va_noindex(client, price_sources, monkeypatch):
    monkeypatch.setattr(web_site.config, "SITE_BASE_URL", "https://lcta.vn")
    monkeypatch.setattr(web_site.site_data, "today", lambda: dt.date(2026, 10, 27))
    html = (await client.get("/gia-vang")).text
    # Trang giá: tiêu đề có ngày, mô tả có giá dòng đầu, JSON-LD có giờ cập nhật số liệu.
    assert "<title>Giá vàng hôm nay 27/10/2026: SJC, nhẫn 9999, DOJI, PNJ | LCTA</title>" in html
    assert "hôm nay 27/10/2026: mua 138,70, bán 141,70 triệu đồng/lượng" in html
    page, crumbs = _json_ld(html)
    assert page["@type"] == "WebPage" and page["url"] == "https://lcta.vn/gia-vang"
    assert page["dateModified"] == "2026-10-27T07:30:00+07:00"
    assert [c["name"] for c in crumbs["itemListElement"]] == ["Hôm nay", "Giá vàng"]
    assert '<link rel="apple-touch-icon" href="/static/icon.png?v=' in html
    assert '<meta name="theme-color" content="#ffffff"' in html
    # Trang chủ: WebSite + tổ chức có logo vuông (ảnh tĩnh phục vụ được).
    token = web_site._viewer.set((False, "/", "https://lcta.vn"))
    try:
        home = bytes(web_site.home_page().body).decode()
    finally:
        web_site._viewer.reset(token)
    site, org = _json_ld(home)
    assert site["@type"] == "WebSite" and org["logo"] == "https://lcta.vn/static/icon.png"
    day = web_site.site_data.now().date()
    assert f"<title>Lịch âm hôm nay {day:%d/%m/%Y}: giờ hoàng đạo, ngày tốt xấu | LCTA" in home
    assert (await client.get("/static/icon.png")).headers["content-type"] == "image/png"
    # Đổi ngày có kết quả: tiêu đề trả lời luôn câu hỏi.
    html = (await client.get("/lich/doi-ngay", params={"am": "15/8/2027"})).text
    assert "<title>15/8/2027 âm lịch là ngày 15/09/2027 dương lịch | LCTA</title>" in html
    # Trang kết quả riêng: không vào chỉ mục, không canonical/JSON-LD; trang gốc thì có.
    noindex = '<meta name="robots" content="noindex, follow">'
    for path, params in (("/lich/hoi", {"q": "rằm tháng 8"}), ("/ky-mon", {"gio": "08:30"})):
        html = (await client.get(path, params=params)).text
        assert noindex in html and 'rel="canonical"' not in html and "ld+json" not in html
    for path in ("/lich/hoi", "/ky-mon", "/tu-vi"):
        html = (await client.get(path)).text
        assert noindex not in html and 'rel="canonical"' in html, path
    html = (await client.post("/than-so-hoc", data={"ngay": "1990-03-15"})).text
    assert noindex in html


def test_dia_chi_site_thieu_giao_thuc_tu_them_https():
    site_url = web_site.config._site_url
    assert site_url("lananh.onrender.com") == "https://lananh.onrender.com"
    assert site_url(" https://lcta.vn/ ") == "https://lcta.vn"
    assert site_url("http://localhost:8000") == "http://localhost:8000"
    assert site_url("") == ""


async def test_ma_xac_minh_google_search_console(client, monkeypatch):
    tag = '<meta name="google-site-verification" content="AbC-123_xyzToken987">'
    # Dán nguyên thẻ từ Search Console hay chỉ phần content đều được; mã lạ thì bỏ qua.
    assert web_site.config._site_verification(tag) == "AbC-123_xyzToken987"
    assert web_site.config._site_verification(" AbC-123_xyzToken987 ") == "AbC-123_xyzToken987"
    assert web_site.config._site_verification('x" onload="alert(1)') == ""
    assert web_site.config._site_verification("") == ""
    assert "google-site-verification" not in (await client.get("/lich")).text
    monkeypatch.setattr(web_site.config, "GOOGLE_SITE_VERIFICATION", "AbC-123_xyzToken987")
    assert tag in (await client.get("/lich")).text


async def test_sitemap_co_lastmod_va_trang_tra_cuu(client, monkeypatch):
    monkeypatch.setattr(web_site.site_data, "today", lambda: dt.date(2026, 10, 27))  # thứ Ba
    text = (await client.get("/sitemap.xml")).text
    assert "<loc>https://testserver/gia-vang</loc><lastmod>2026-10-27</lastmod>" in text
    assert "<loc>https://testserver/tu-vi</loc></url>" in text  # trang tĩnh: không đoán lastmod
    # Đài TP.HCM quay thứ Hai, thứ Bảy: lastmod là ngày quay gần nhất.
    assert (
        "<loc>https://testserver/xo-so?dai=ho-chi-minh</loc><lastmod>2026-10-26</lastmod>" in text
    )
    assert "/thoi-tiet?noi=H%C3%A0%20N%E1%BB%99i" in text and "noi=TP.HCM" not in text
    assert "/lich?thang=2026-11<" in text and "/lich?thang=2027-09<" in text
    assert "/lich?thang=2027-10<" not in text


async def test_moi_ngay_mot_trang_rieng_co_tieu_de_va_canonical(client):
    # Ô ngày trong lưới tháng mở /lich?ngay=: tiêu đề, mô tả theo ngày (không trùng tiêu
    # đề trang tháng); link cũ kèm ?thang= trỏ canonical về cùng một URL.
    html = (await client.get("/lich", params={"thang": "2026-10", "ngay": "2026-10-05"})).text
    assert "<title>Lịch âm ngày 05/10/2026: 25/8 âm lịch, ngày hoàng đạo | LCTA</title>" in html
    assert '<link rel="canonical" href="https://testserver/lich?ngay=2026-10-05">' in html
    assert (
        "Thứ Hai, 05/10/2026 dương lịch là ngày 25 tháng 8 năm Bính Ngọ âm lịch, ngày Nhâm "
        "Tý, hoàng đạo (Tư Mệnh). Giờ tốt: Tý, Sửu, Mão, Ngọ, Thân, Dậu." in html
    )
    assert 'href="/lich?ngay=2026-10-01"' in html and "&amp;ngay=" not in html
    page, _ = _json_ld(html)
    assert page["url"] == "https://testserver/lich?ngay=2026-10-05"
    html = (await client.get("/lich", params={"ngay": "2026-09-25"})).text
    assert 'Tết Trung thu.">' in html  # ngày lễ ghi thêm vào mô tả
    html = (await client.get("/lich", params={"thang": "2026-10"})).text
    assert "<title>Lịch âm tháng 10/2026: ngày hoàng đạo, ngày lễ | LCTA</title>" in html


async def test_url_nhap_sai_khong_vao_chi_muc_nguon_loi_tra_503(
    client, lottery_sources, monkeypatch
):
    async def fake_get_weather(location="", days=1):
        if location == "Xyzabc":
            return {"error": "Không tìm thấy"}
        if location == "loi":
            raise web_site.weather.WeatherError("down")
        return _FAKE_WEATHER

    monkeypatch.setattr(web_site.weather, "get_weather", fake_get_weather)
    noindex = '<meta name="robots" content="noindex, follow">'
    for path, params in (
        ("/lich", {"thang": "abc"}),
        ("/lich", {"ngay": "2026-02-30"}),
        ("/lich/doi-ngay", {"am": "abc"}),
        ("/xo-so", {"dai": "khong-co"}),
        ("/xo-so", {"dai": "mn", "ve": "12ab"}),
        ("/thoi-tiet", {"noi": "Xyzabc"}),
    ):
        r = await client.get(path, params=params)
        assert r.status_code == 200 and noindex in r.text, params
        assert 'rel="canonical"' not in r.text
    for path, params in (("/lich", {"ngay": "2026-10-05"}), ("/xo-so", {"dai": "mn"})):
        assert noindex not in (await client.get(path, params=params)).text
    r = await client.get("/thoi-tiet", params={"noi": "loi"})
    assert r.status_code == 503 and r.headers["retry-after"] == "300"
    assert "thử lại sau" in r.text


async def test_tieu_de_trang_chu_co_tu_khoa_va_llms_txt(client):
    token = web_site._viewer.set((False, "/", "https://lcta.vn"))
    try:
        home = bytes(web_site.home_page().body).decode()
    finally:
        web_site._viewer.reset(token)
    assert '<h1 class="date"><span class="eyebrow">Lịch âm hôm nay</span> ' in home
    site, org = _json_ld(home)
    assert site["publisher"] == {"@id": org["@id"]} == {"@id": "https://lcta.vn/#organization"}

    r = await client.get("/llms.txt")
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert text.startswith("# LCTA\n\n> Lịch âm dương Việt Nam")
    for path in web_site._SITEMAP_PATHS:  # mọi trang công khai đều có link
        assert f"](https://testserver{path}):" in text, path
    assert "[Giá vàng](https://testserver/api/v1/gold)" in text
    assert "api/v1/calendar/answers?q=r%E1%BA%B1m%20th%C3%A1ng%208" in text


async def test_hoi_dap_chi_o_trang_goc(client, lottery_sources):
    faq = web_site._faq()
    assert set(faq) == set(web_site._SITEMAP_PATHS)  # trang công khai nào cũng có hỏi đáp
    html = (await client.get("/lich")).text
    assert '<h2 id="faq-title">Hỏi đáp</h2>' in html
    assert "<h3>Tam nương, Nguyệt kỵ là những ngày nào?</h3>" in html
    html = (await client.get("/lich/ngay-le")).text
    assert '<a href="/lich/doi-ngay">đổi ngày âm</a>' in html  # [chữ](/đường-dẫn) -> link
    # Trang theo tham số (ngày, tháng, đài) và trang noindex: không lặp khối hỏi đáp.
    for path, params in (
        ("/lich", {"ngay": "2026-10-05"}),
        ("/lich", {"thang": "2026-11"}),
        ("/xo-so", {"dai": "tay-ninh"}),
        ("/lich/hoi", {"q": "rằm tháng 8"}),
    ):
        assert 'id="faq-title"' not in (await client.get(path, params=params)).text, params
    assert 'id="faq-title"' in (await client.get("/xo-so", params={"utm_source": "zalo"})).text


def test_hoi_dap_escape_chi_doi_link_noi_bo():
    html = web_site._faq_html('<b>x</b> [ngoài](https://evil.example) [lịch](/lich?ngay=1&x="y")')
    assert "<b>" not in html and "&lt;b&gt;" in html
    assert "[ngoài](https://evil.example)" in html  # chỉ link nội bộ bắt đầu bằng /
    assert '<a href="/lich?ngay=1&amp;x=&#34;y&#34;">lịch</a>' in html


async def test_web_khong_nhac_trung_quoc(client, price_sources, lottery_sources, monkeypatch):
    """Chủ trang yêu cầu: chữ hiện trên web (trang, kết quả, hỏi đáp, llms.txt, API) không
    có từ liên quan tới Trung Quốc. Riêng bảng tỷ giá vẫn giữ CNY (chủ trang đồng ý)."""
    import re

    async def fake_get_weather(location="", days=1):
        return _FAKE_WEATHER

    monkeypatch.setattr(web_site.weather, "get_weather", fake_get_weather)
    pages = [bytes(web_site.home_page().body).decode()]
    for path in (*web_site._SITEMAP_PATHS[1:], "/lich?ngay=2026-10-05", "/llms.txt"):
        pages.append((await client.get(path)).text)
    for path in ("/api/v1/fx", "/api/v1/gold"):
        pages.append(json.dumps((await client.get(path)).json(), ensure_ascii=False))
    tuvi = {"ngay": "15", "thang": "3", "nam": "1990", "gio": "4", "gioi": "male", "lich": "duong"}
    pages.append((await client.post("/tu-vi", data=tuvi)).text)
    pages.append((await client.post("/than-so-hoc", data={"ngay": "1990-03-15", "ten": "An"})).text)
    text = "\n".join(pages)
    assert "Đô la Mỹ" in text and "Lá số" in text  # đúng là đã render nội dung
    found = re.findall(
        r"trung\s+qu[ốo]c|trung\s+hoa|china|chinese|bắc kinh|[\u4e00-\u9fff]",
        text,
        re.I,
    )
    assert found == []


async def test_trang_dang_nhap_noi_ro_chi_cho_chu_trang_va_co_duong_ve(monkeypatch):
    import web

    transport = httpx.ASGITransport(app=web.api)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        html = (await c.get("/admin", params={"next": "/lich?thang=2026-10"})).text
        assert "chỉ dành cho chủ trang" in html
        assert '<a href="/lich?thang=2026-10">← Quay lại trang đang xem</a>' in html
        assert '<a href="/">← Quay lại' in (await c.get("/admin")).text


async def test_nhap_sai_bao_loi_ngay_duoi_o(client, lottery_sources):
    html = (await client.get("/lich/doi-ngay", params={"am": "abc"})).text
    assert 'aria-invalid="true"' in html and "“abc” không phải ngày âm lịch" in html
    assert html.count('role="alert"') == 1
    html = (await client.get("/xo-so", params={"dai": "mn", "ve": "123"})).text
    assert 'id="ve"' in html and 'aria-invalid="true"' in html
    assert '<span class="field-error" id="form-error" role="alert">Vé xổ số' in html
    html = (await client.get("/lich", params={"thang": "abc"})).text
    assert "Không đọc được tháng “abc”" in html and "YYYY-MM" not in html
    html = (await client.get("/lich", params={"thang": "11/2026"})).text
    assert "<h1>Lịch âm tháng 11/2026</h1>" in html


async def test_bang_cuon_ngang_nhan_focus_va_tsh_khong_lap_tieu_de(client, monkeypatch):
    async def fake_get_weather(location="", days=1):
        return _FAKE_WEATHER

    monkeypatch.setattr(web_site.weather, "get_weather", fake_get_weather)
    html = (await client.get("/thoi-tiet")).text
    assert '<div class="table-wrap" tabindex="0" role="region"' in html
    html = (await client.post("/than-so-hoc", data={"ngay": "1990-03-15", "ten": "An"})).text
    assert html.count("Các con số khác") == 1


async def test_thoi_tiet_nguon_du_phong_ghi_dung_so_ngay(client, monkeypatch):
    async def fallback(location="", days=1):
        return {
            **_FAKE_WEATHER,
            "source": "wttr.in (dự phòng, tối đa 3 ngày)",
            "fallback": True,
            "daily": _FAKE_WEATHER["daily"][:3],
        }

    monkeypatch.setattr(web_site.weather, "get_weather", fallback)
    html = (await client.get("/thoi-tiet")).text
    assert "Hiện tại và dự báo 3 ngày" in html and "dự báo 7 ngày" not in html
    # dòng nguồn của bảng dự báo đã ghi là dự phòng; không thêm băng báo riêng
    assert "Nguồn: wttr.in (dự phòng, tối đa 3 ngày)" in html and "Open-Meteo đang lỗi" not in html


async def test_tu_vi_va_ky_mon_co_dien_giai_cho_khach(client):
    html = (await client.post("/tu-vi", data=_TUVI_FORM)).text
    assert 'id="read-title">Diễn giải lá số</h2>' in html and 'href="#read-title"' in html
    assert html.count('<p class="rl-head">') == 12 and "Bản mệnh" in html
    html = (await client.get("/ky-mon", params={"ngay": "2026-10-27", "gio": "09:30"})).text
    assert 'id="qm-read">Diễn giải bàn</h2>' in html and "Việc gì xem hướng nào" in html
    assert "Hướng nên dùng" in html and "Cách đọc bàn" in html


async def test_luoi_tu_vi_dien_thoai_co_o_giua(client):
    html = (await client.post("/tu-vi", data=_TUVI_FORM)).text
    center = html.split('<nav class="chart-map"')[1].split("</nav>")[0]
    assert '<div class="c"><b>Dương Nam</b>' in center and "Bấm một cung để xem" in center


async def test_luu_vi_tri_hien_tai_trong_cookie_trinh_duyet(client, monkeypatch):
    """Nút "Lấy vị trí hiện tại": toạ độ (làm tròn ~1 km) lưu trong cookie của trình duyệt,
    trang /thoi-tiet không nhập địa điểm thì xem ở đó; không DB, không cần đăng nhập."""
    calls = []

    async def fake_at(lat, lon, label, days=1):
        calls.append((lat, lon, label, days))
        return {**_FAKE_WEATHER, "location": label}

    async def fake_get_weather(location="", days=1):
        calls.append(location)
        return _FAKE_WEATHER

    async def no_name(lat, lon):
        return None  # Nominatim lỗi/tắt: vẫn lưu toạ độ, trang ghi toạ độ thay tên

    monkeypatch.setattr(web_site.weather, "get_weather_at", fake_at)
    monkeypatch.setattr(web_site.weather, "get_weather", fake_get_weather)
    monkeypatch.setattr(web_site.weather, "reverse_geocode", no_name)
    html = (await client.get("/thoi-tiet")).text
    assert 'id="geo-btn" hidden' in html and "Lấy vị trí hiện tại" in html
    assert "Vị trí của tôi</a>" not in html and "Xoá vị trí đã lưu" not in html

    r = await client.post("/thoi-tiet/vi-tri", data={"lat": "11.940419", "lon": "108.458313"})
    assert r.status_code == 303 and r.headers["location"] == "/thoi-tiet?tb=da-luu-vi-tri"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("lcta_vitri=11.94_108.46;") and "Path=/thoi-tiet" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie

    r = await client.get(r.headers["location"])
    html = r.text
    assert calls[-1] == (11.94, 108.46, "Vị trí của tôi (11,94; 108,46)", 7)
    assert "<h1>Thời tiết ở vị trí của bạn</h1>" in html and "Đã lưu vị trí hiện tại" in html
    assert 'href="/thoi-tiet" aria-current="page">' in html and "Xoá vị trí đã lưu" in html
    assert 'href="/thoi-tiet?noi=TP.HCM" aria-current' not in html
    assert r.headers["cache-control"] == "private, no-cache"
    # Gõ địa điểm thì vẫn xem địa điểm đó.
    await client.get("/thoi-tiet", params={"noi": "Huế"})
    assert calls[-1] == "Huế"

    r = await client.post("/thoi-tiet/vi-tri/xoa")
    assert r.status_code == 303 and 'lcta_vitri=""' in r.headers["set-cookie"]
    html = (await client.get(r.headers["location"])).text
    assert "Đã xoá vị trí đã lưu." in html and calls[-1] == ""  # về mặc định TP.HCM

    r = await client.post("/thoi-tiet/vi-tri", data={"lat": "abc", "lon": "200"})
    assert r.headers["location"] == "/thoi-tiet?tb=vi-tri-loi" and "set-cookie" not in r.headers


async def test_vi_tri_trong_cookie_sai_bo_qua_va_tinh_vao_han_muc(client, monkeypatch):
    calls = []

    async def fake_at(lat, lon, label, days=1):
        calls.append((lat, lon))
        return {**_FAKE_WEATHER, "location": label}

    async def fake_get_weather(location="", days=1):
        calls.append(location)
        return _FAKE_WEATHER

    monkeypatch.setattr(web_site.weather, "get_weather_at", fake_at)
    monkeypatch.setattr(web_site.weather, "get_weather", fake_get_weather)
    monkeypatch.setattr(web_site, "_new_place_times", web_site.collections.deque())
    monkeypatch.setattr(web_site, "_new_place_by_ip", web_site.collections.OrderedDict())
    monkeypatch.setattr(web_site, "_NEW_PLACE_PER_IP", 1)
    await client.get("/thoi-tiet", headers={"Cookie": "lcta_vitri=abc"})
    assert calls == [""]  # cookie hỏng: như chưa lưu
    # Toạ độ sửa tay trong cookie, chưa có trong cache: tính vào hạn mức tra địa điểm mới.
    ip = {"X-Forwarded-For": "4.4.4.4"}
    await client.get("/thoi-tiet", headers={**ip, "Cookie": "lcta_vitri=10.00_106.00"})
    html = (
        await client.get("/thoi-tiet", headers={**ip, "Cookie": "lcta_vitri=11.00_107.00"})
    ).text
    assert calls == ["", (10.0, 106.0)] and "Bạn vừa tra nhiều địa điểm mới" in html


async def test_luu_vi_tri_kem_ten_noi_tu_openstreetmap(client, monkeypatch):
    """Tên nơi tra 1 lần lúc lưu rồi cất trong cookie: xem lại không gọi Nominatim."""
    labels, reverse_calls = [], []

    async def fake_at(lat, lon, label, days=1):
        labels.append(label)
        return {**_FAKE_WEATHER, "location": label}

    async def fake_reverse(lat, lon):
        reverse_calls.append((lat, lon))
        return "Phường 1, Đà Lạt"

    monkeypatch.setattr(web_site.weather, "get_weather_at", fake_at)
    monkeypatch.setattr(web_site.weather, "reverse_geocode", fake_reverse)
    monkeypatch.setattr(web_site.weather, "reverse_cached", lambda lat, lon: False)
    monkeypatch.setattr(web_site, "_new_place_times", web_site.collections.deque())
    monkeypatch.setattr(web_site, "_new_place_by_ip", web_site.collections.OrderedDict())
    r = await client.post("/thoi-tiet/vi-tri", data={"lat": "11.94", "lon": "108.46"})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(
        "lcta_vitri=11.94_108.46_Ph%C6%B0%E1%BB%9Dng%201%2C%20%C4%90%C3%A0%20L%E1%BA%A1t;"
    )
    html = (await client.get("/thoi-tiet")).text
    html = (await client.get("/thoi-tiet")).text
    assert reverse_calls == [(11.94, 108.46)]  # chỉ lúc lưu
    assert labels[-1] == "Phường 1, Đà Lạt" and "<h1>Thời tiết Phường 1, Đà Lạt</h1>" in html
    assert 'href="https://www.openstreetmap.org/copyright"' in html  # ghi công ODbL

    # Hết lượt tra địa điểm mới của IP: vẫn lưu toạ độ, không gọi Nominatim.
    monkeypatch.setattr(web_site, "_NEW_PLACE_PER_IP", 0)
    r = await client.post("/thoi-tiet/vi-tri", data={"lat": "21.03", "lon": "105.85"})
    assert r.headers["set-cookie"].startswith("lcta_vitri=21.03_105.85;")
    assert len(reverse_calls) == 1


async def test_ten_noi_trong_cookie_sua_tay_van_an_toan(client, monkeypatch):
    async def fake_at(lat, lon, label, days=1):
        return {**_FAKE_WEATHER, "location": label}

    monkeypatch.setattr(web_site.weather, "get_weather_at", fake_at)
    monkeypatch.setattr(web_site.weather, "forecast_cached", lambda *a: True)
    raw = "10.78_106.70_%3Cscript%3Ex%3C%2Fscript%3E%0A" + "a" * 200
    html = (await client.get("/thoi-tiet", headers={"Cookie": f"lcta_vitri={raw}"})).text
    assert "<script>x" not in html and "&lt;script&gt;x&lt;/script&gt;" in html
    assert web_site._parse_my_place(raw)[2] == ("<script>x</script>" + "a" * 200)[:80]
    # Tên dài nhất (80 ký tự tiếng Việt, percent-encode ~720 ký tự) vẫn đọc lại được.
    longest = "Đường Ngọc Hồi Phường Hoàng Liệt Quận Hoàng Mai Thành phố Hà Nội Việt Nam ỡỡỡỡỡỡỡ"[
        :80
    ]
    value = "21.03_105.85_" + web_site.quote(longest, safe="")
    assert web_site._parse_my_place(value) == (21.03, 105.85, longest)
