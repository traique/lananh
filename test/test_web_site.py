"""Test web công khai (web_site.py): trang HTML, JSON API, health check giữ nguyên."""

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
        health = await c.get("/")
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        assert (await c.head("/")).status_code == 200
        page = await c.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
        assert page.status_code == 200
        assert "Hôm nay" in page.text and 'alt="LCTA"' in page.text


async def test_chua_dang_nhap_van_xem_duoc_header_co_nut_dang_nhap(client):
    r = await client.get("/lich/hoi", params={"q": "rằm tháng 8"})
    assert r.status_code == 200 and "rằm" in r.text
    assert 'href="/admin?next=%2Flich%2Fhoi%3Fq%3D' in r.text and ">Đăng nhập</a>" in r.text
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
        assert (await c.get("/")).json() == {"status": "ok"}
        page = await c.get("/", headers={"Accept": "text/html"})
        assert page.status_code == 200 and "Hôm nay" in page.text
        assert 'href="/admin?next=%2F"' in page.text


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
    # Chưa đăng nhập: bấm AI (ai=1) cũng không gọi AI, chỉ hiện nút đăng nhập.
    html = (await client.post("/tu-vi", data={**_TUVI_FORM, "ai": "1"})).text
    assert calls == [] and "Đăng nhập để dùng AI" in html and 'href="/admin?next=%2Ftu-vi"' in html
    html = (await client.post("/than-so-hoc", data={"ngay": "1990-03-15", "ai": "1"})).text
    assert calls == [] and "Đăng nhập để dùng AI" in html

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
    assert r.status_code == 303 and "tb=dang-nhap" in r.headers["location"] and store == {}

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
    assert "XyzLaHoat" not in html and "ok-box" not in html.split("</style>")[1]  # chỉ mã cố định


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
        assert page.status_code == 200 and "chưa hiểu" in page.text, q
        api = await client.get("/api/v1/calendar/answers", params={"q": q})
        assert api.status_code == 200, q


async def test_thoi_tiet_gioi_han_dia_diem_la(client, monkeypatch):
    calls = []

    async def fake_get_weather(location="", days=1):
        calls.append(location)
        return _FAKE_WEATHER

    monkeypatch.setattr(web_site.weather, "get_weather", fake_get_weather)
    monkeypatch.setattr(web_site, "_new_place_times", web_site.collections.deque())
    monkeypatch.setattr(web_site, "_NEW_PLACE_LIMIT", 3)
    for i in range(3):
        assert "°C" in (await client.get("/thoi-tiet", params={"noi": f"La{i}"})).text
    html = (await client.get("/thoi-tiet", params={"noi": "La9"})).text
    assert "tạm giới hạn" in html and 'role="alert"' in html and len(calls) == 3
    # Địa điểm trong danh sách hay xem và mặc định không bị giới hạn.
    assert "°C" in (await client.get("/thoi-tiet", params={"noi": "đà lạt"})).text
    assert "°C" in (await client.get("/thoi-tiet")).text
    assert len(calls) == 5
    assert web_site._new_place_allowed(now=10_000.0)  # cửa sổ 10 phút đã qua


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
    assert data["challenges"][0]["label"] == "0-35 tuổi" and data["combinedChart"]["5"] == 6


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
