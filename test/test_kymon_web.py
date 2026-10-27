"""Test trang /ky-mon và JSON API POST /api/v1/qimen/charts (web_site.py)."""

import datetime as dt
import re

import httpx
import pytest
from fastapi import FastAPI

import web_site
from services import kymon, site_data

_NOW = dt.datetime(2026, 10, 25, 14, 37, tzinfo=kymon.VN_TZ)


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
    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_out)
    monkeypatch.setattr(site_data, "now", lambda: _NOW)
    app = FastAPI()
    app.include_router(web_site.static_router)
    app.include_router(web_site.router)
    app.include_router(web_site.api_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


async def test_trang_ky_mon_mac_dinh_luc_nay(client):
    r = await client.get("/ky-mon")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    html = r.text
    assert "<h1>Kỳ Môn Độn Giáp</h1>" in html
    assert 'value="2026-10-25"' in html and 'value="14:37"' in html
    expected = kymon.build_chart(_NOW)
    assert f"{expected.dun_text} {expected.ju} cục" in html
    # Thanh bên: mục mới nằm cùng nhóm với Tử vi, đang được chọn.
    assert 'href="/ky-mon" aria-current="page"' in html
    assert (
        html.index('href="/tu-vi"')
        < html.index('href="/ky-mon"')
        < html.index('href="/than-so-hoc"')
    )
    # 9 ô theo thứ tự Tốn Ly Khôn / Chấn Trung Đoài / Cấn Khảm Càn, mỗi ô có nhãn.
    labels = re.findall(r'aria-label="Cung (\d)', html)
    assert labels == ["4", "9", "2", "3", "5", "7", "8", "1", "6"]
    assert "Luận giải bằng AI" not in html and "<textarea" not in html  # khách không thấy AI
    assert "Về lúc này" not in html


async def test_trang_ky_mon_chon_ngay_gio(client):
    r = await client.get("/ky-mon", params={"ngay": "2026-01-11", "gio": "08:30"})
    html = r.text
    assert "no-cache" not in r.headers.get("cache-control", "")
    assert "Dương độn 8 cục" in html and "Canh Thìn" in html
    assert "Thiên Anh tới cung 1" in html and "Tam kỳ nhập mộ" in html
    assert html.count("<strong>Môn bách</strong>") == 1  # 4 cung gộp một dòng
    assert "Cung 1: Tử môn Thổ khắc cung Thủy; Cung 3:" in html
    assert "Phản ngâm</strong> (sao)" in html
    assert 'class="qm-cell is-zhifu" aria-label="Cung 1 Khảm, hướng Bắc, Trực phù' in html
    assert "Về lúc này" in html


@pytest.mark.parametrize(
    "params,message",
    [
        ({"ngay": "2026-02-30", "gio": "08:00"}, "Ngày không tồn tại"),
        ({"ngay": "2026-01-11", "gio": "25:00"}, "Giờ có dạng HH:MM"),
        ({"ngay": "1800-01-01"}, "1900-2100"),
    ],
)
async def test_trang_ky_mon_nhap_sai_bao_loi_va_hien_ban_luc_nay(client, params, message):
    r = await client.get("/ky-mon", params=params)
    assert r.status_code == 200
    assert 'role="alert"' in r.text and message in r.text
    assert 'value="2026-10-25"' in r.text  # vẫn có bàn của lúc này


async def test_ai_luan_giai_chi_khi_dang_nhap_va_co_cau_hoi(client, monkeypatch):
    calls = []

    async def fake_ai_text(user_id, kind, channel, explain):
        calls.append((kind, channel))
        return "## Tổng quan\n**Khai môn** ở Chấn."

    monkeypatch.setattr(web_site.divination_commands, "ai_text", fake_ai_text)
    form = {"ngay": "2026-01-11", "gio": "08:30", "ai": "1", "cau_hoi": "ký hợp đồng"}
    html = (await client.post("/ky-mon", data=form)).text
    assert calls == [] and "Luận giải bằng AI" not in html  # chưa đăng nhập: không tốn AI

    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)
    html = (await client.get("/ky-mon", params={"ngay": "2026-01-11", "gio": "08:30"})).text
    assert "<textarea" in html and '<input type="hidden" name="gio" value="08:30">' in html
    html = (await client.post("/ky-mon", data={**form, "cau_hoi": "  "})).text
    assert calls == [] and "Nhập việc cần xem" in html
    html = (await client.post("/ky-mon", data={**form, "cau_hoi": "x" * 600})).text
    assert calls == [("kymon_chitiet", "web")]
    assert "Tổng quan\nKhai môn ở Chấn." in html and "**" not in html
    assert "Việc cần xem: " + "x" * kymon.MAX_QUESTION_LEN + "<" in html  # cắt 400 ký tự


async def test_ai_web_chay_duoi_khoa_luot_chu_bot(client, monkeypatch):
    seen = {}

    async def fake_explain(chart, question):
        seen["question"] = question
        seen["ju"] = chart.ju
        return "Luận giải"

    async def fake_ai_text(user_id, kind, channel, explain):
        return await explain()

    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_in)
    monkeypatch.setattr(web_site.kymon, "explain_with_ai", fake_explain)
    monkeypatch.setattr(web_site.divination_commands, "ai_text", fake_ai_text)
    form = {"ngay": "2026-01-11", "gio": "08:30", "ai": "1", "cau_hoi": "đi xa"}
    html = (await client.post("/ky-mon", data=form)).text
    assert seen == {"question": "đi xa", "ju": 8} and "Luận giải" in html


async def test_api_qimen_charts(client):
    r = await client.post("/api/v1/qimen/charts", json={"dateTime": "2026-01-11T08:30"})
    assert r.status_code == 200
    data = r.json()
    assert data["dateTime"] == "2026-01-11T08:30+07:00" and data["timezone"] == "Asia/Ho_Chi_Minh"
    assert data["dun"] == "yang" and data["ju"] == 8 and data["dunText"] == "Dương độn"
    assert data["zhiFu"] == {"star": "Thiên Anh", "palace": 1}
    assert data["pillars"]["hour"]["text"] == "Canh Thìn"
    assert data["solarTerm"]["name"] == "Tiểu hàn" and data["solarTerm"]["startsAt"]
    assert set(data) >= {"pillars", "solarTerm", "dun", "ju", "yuan", "xunShou", "zhiFu", "zhiShi",
                         "void", "horse", "palaces", "patterns", "grid", "disclaimer"}  # fmt: skip
    assert set(data["palaces"][0]) >= {"number", "name", "direction", "earthStems", "heavenStems",
                                       "star", "door", "god", "isVoid", "isHorse"}  # fmt: skip


async def test_api_qimen_mac_dinh_luc_nay_va_mui_gio(client):
    r = await client.post("/api/v1/qimen/charts", json={})
    assert r.json()["dateTime"] == "2026-10-25T14:37+07:00"
    r = await client.post("/api/v1/qimen/charts", content=b"")
    assert r.status_code == 200
    # Có múi giờ: lập bàn theo giờ đồng hồ của múi đó (thực lệ sách giờ Bắc Kinh).
    r = await client.post("/api/v1/qimen/charts", json={"dateTime": "1996-02-09T18:00+08:00"})
    data = r.json()
    assert data["dateTime"] == "1996-02-09T18:00+08:00" and data["timezone"] == "UTC+08:00"
    assert (data["ju"], data["zhiFu"]["star"], data["zhiShi"]["palace"]) == (2, "Thiên Cầm", 8)
    r = await client.post("/api/v1/qimen/charts", json={"dateTime": "2026-01-11T01:30:45Z"})
    assert r.json()["dateTime"] == "2026-01-11T01:30+00:00"


@pytest.mark.parametrize(
    "kw,code",
    [
        ({"json": {"dateTime": "11/01/2026 08:30"}}, "INVALID_DATETIME"),
        ({"json": {"dateTime": "2026-01-11"}}, "INVALID_DATETIME"),
        ({"json": {"dateTime": "2026-02-30T08:00"}}, "INVALID_DATETIME"),
        ({"json": {"dateTime": 20260111}}, "INVALID_DATETIME"),
        ({"json": {"dateTime": "1800-01-01T08:00"}}, "YEAR_OUT_OF_RANGE"),
        ({"content": b"null", "headers": {"content-type": "application/json"}}, "INVALID_JSON"),
        ({"content": b"{" + b" " * 5000 + b"}"}, "BODY_TOO_LARGE"),
    ],
)  # fmt: skip
async def test_api_qimen_loi_cung_mot_dang(client, kw, code):
    r = await client.post("/api/v1/qimen/charts", **kw)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code and r.json()["error"]["message"]


async def test_api_qimen_get_405_cung_dang_loi(monkeypatch):
    import web

    monkeypatch.setattr(web_site.web_admin, "is_logged_in", _logged_out)
    transport = httpx.ASGITransport(app=web.api)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        r = await c.get("/api/v1/qimen/charts")
        assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
        assert (await c.get("/ky-mon")).status_code == 200
