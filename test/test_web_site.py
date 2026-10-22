"""Test web công khai (web_site.py): trang HTML, JSON API, health check giữ nguyên."""

import httpx
import pytest
from fastapi import FastAPI

import web_site


@pytest.fixture
async def client(monkeypatch):
    from ai import orchestrator

    async def boom(*args, **kwargs):
        raise AssertionError("web công khai không được gọi AI")

    monkeypatch.setattr(orchestrator, "ask", boom)
    monkeypatch.setattr(orchestrator, "chat", boom)
    app = FastAPI()
    app.include_router(web_site.router)
    app.include_router(web_site.api_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


async def test_root_tra_html_cho_trinh_duyet_json_cho_health_check():
    import web

    transport = httpx.ASGITransport(app=web.api)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        health = await c.get("/")
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        assert (await c.head("/")).status_code == 200
        page = await c.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
        assert page.status_code == 200
        assert "<h1>Lịch âm, tử vi, thần số học</h1>" in page.text


async def test_trang_lich(client):
    r = await client.get("/lich", params={"ngay": "2026-10-24", "q": "trung thu"})
    assert r.status_code == 200
    assert "Tháng 10/2026" in r.text
    assert 'aria-current="date"' in r.text
    assert "Tết Trung thu" in r.text
    assert "ngày 15 tháng 9 năm Bính Ngọ âm lịch" in r.text


async def test_trang_lich_doi_ngay_am_va_loi_nhap(client):
    r = await client.get("/lich", params={"am": "15/8/2027"})
    assert "Thứ Tư 15/09/2027" in r.text
    bad = await client.get("/lich", params={"thang": "abc"})
    assert bad.status_code == 200 and 'role="alert"' in bad.text


async def test_trang_tuvi_post_va_loi(client):
    r = await client.post(
        "/tu-vi", data={"ngay": "15", "thang": "3", "nam": "1990", "gio": "5", "gioi": "male"}
    )
    assert "Thổ ngũ cục" in r.text and "Phá Quân" in r.text
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
    assert "Con số chủ đạo" in r.text
    assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text


async def test_static_chi_phuc_vu_file_trong_danh_sach(client):
    assert (await client.get("/static/logo.svg")).headers["content-type"] == "image/svg+xml"
    assert (await client.get("/static/site.css")).status_code == 200
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
