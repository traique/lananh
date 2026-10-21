"""Unit test cho services/tools.py (function calling: router JSON + thực thi tool)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import database as db  # noqa: E402
from ai import official_client  # noqa: E402
from services import tools  # noqa: E402


@pytest.mark.asyncio
async def test_save_note_duoc_route_va_thuc_thi(monkeypatch):
    calls = []

    async def fake_add_note(uid, content, **delivery):
        calls.append(("add_note", uid, content))

    async def fake_generate_utility_json(prompt):
        return {"tool": "save_note", "args": {"content": "Tuần sau họp đối tác Nhật"}}

    monkeypatch.setattr(db, "add_note", fake_add_note)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate_utility_json)

    result = await tools.maybe_run_tool(1, "ghi chú giúp anh tuần sau họp đối tác Nhật")

    assert calls == [("add_note", 1, "Tuần sau họp đối tác Nhật")]
    assert result is not None
    assert "Tuần sau họp đối tác Nhật" in result


@pytest.mark.asyncio
async def test_tool_none_khong_chay_handler_nao(monkeypatch):
    async def fake_generate_none(prompt):
        return {"tool": "none", "args": {}}

    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate_none)

    result = await tools.maybe_run_tool(1, "nhớ mua rau về ăn tối nhé em")
    assert result is None


@pytest.mark.asyncio
async def test_loi_router_khong_raise(monkeypatch):
    async def fake_raise(prompt):
        raise RuntimeError("mô phỏng lỗi API")

    monkeypatch.setattr(official_client, "generate_utility_json", fake_raise)

    result = await tools.maybe_run_tool(1, "ghi chú vài chữ bất kỳ")
    assert result is None  # không raise, chat chính vẫn tiếp tục bình thường


@pytest.mark.asyncio
async def test_set_reminder_toi_thieu_1_phut(monkeypatch):
    """minutes_from_now <= 0 (Gemini tính sai hoặc không trả) -> vẫn phải
    đặt được reminder tối thiểu 1 phút, không được lưu due_at ở quá khứ."""
    calls = []

    async def fake_add_reminder(uid, message, due_at, **delivery):
        calls.append((uid, message, due_at))

    async def fake_generate(prompt):
        return {"tool": "set_reminder", "args": {"message": "uống thuốc", "minutes_from_now": -5}}

    monkeypatch.setattr(db, "add_reminder", fake_add_reminder)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate)

    await tools.maybe_run_tool(1, "nhắc anh uống thuốc")

    assert len(calls) == 1
    _, message, due_at = calls[0]
    assert message == "uống thuốc"


@pytest.mark.asyncio
async def test_get_portfolio_loc_dung_fact_lien_quan(monkeypatch):
    async def fake_get_facts(uid):
        return [("ten", "Trai"), ("danh_muc_dau_tu", "FPT, HPG"), ("mau_thich", "xanh")]

    async def fake_generate(prompt):
        return {"tool": "get_portfolio", "args": {}}

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate)

    result = await tools.maybe_run_tool(1, "danh mục của anh gồm gì nhỉ")

    assert result is not None
    assert "FPT, HPG" in result
    assert "mau_thich" not in result  # không liên quan danh mục -> không lẫn vào


@pytest.mark.asyncio
async def test_get_weather_khong_noi_dia_diem_dung_mac_dinh(monkeypatch):
    from services import weather

    calls = []

    async def fake_get_weather(location="", days=1):
        calls.append((location, days))
        return {"location": weather.DEFAULT_LABEL, "source": "Open-Meteo", "now": {}, "daily": []}

    async def fake_generate(prompt):
        return {"tool": "get_weather", "args": {"location": "", "days": 2}}

    monkeypatch.setattr(weather, "get_weather", fake_get_weather)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate)

    result = await tools.maybe_run_tool(1, "mai trời có mưa không em")

    assert calls == [("", 2)]
    assert result is not None and "TP. Hồ Chí Minh" in result


@pytest.mark.asyncio
async def test_get_weather_loi_tra_thong_bao_khong_raise(monkeypatch):
    from services import weather

    async def fake_get_weather(location="", days=1):
        raise weather.WeatherError("mô phỏng lỗi")

    monkeypatch.setattr(weather, "get_weather", fake_get_weather)

    result = await tools._tool_get_weather(1, location="Huế", days=1)

    assert "KHÔNG tự bịa" in result


@pytest.mark.asyncio
async def test_lunar_calendar_duong_sang_am():
    result = await tools._tool_lunar_calendar(1, date="2026-10-21")
    assert "ngày 12 tháng 9 năm Bính Ngọ" in result
    assert "Hàn lộ" in result


@pytest.mark.asyncio
async def test_lunar_calendar_am_sang_duong_co_nam():
    result = await tools._tool_lunar_calendar(1, lunar_day=15, lunar_month=8, lunar_year=2026)
    assert result.startswith("Ngày 15 tháng 8 năm Bính Ngọ âm lịch")
    assert "Thứ Sáu 25/09/2026" in result


@pytest.mark.asyncio
async def test_lunar_calendar_khong_noi_nam_lay_lan_sap_toi():
    import datetime as dt

    from services import lunar

    result = await tools._tool_lunar_calendar(1, lunar_day=1, lunar_month=1)
    today = dt.datetime.now(tools._VN_TZ).date()
    tet = lunar.next_tet(today)
    assert f"{tet:%d/%m/%Y}" in result


@pytest.mark.asyncio
async def test_lunar_calendar_ngay_30_thang_thieu_va_nhuan_khong_ton_tai():
    # Tháng 2 âm năm 2026 chỉ có 29 ngày (mùng 1 tháng 2 là 19/03, mùng 1 tháng 3 là 17/04).
    result = await tools._tool_lunar_calendar(1, lunar_day=30, lunar_month=2, lunar_year=2026)
    assert "chỉ có 29 ngày" in result
    result = await tools._tool_lunar_calendar(
        1, lunar_day=1, lunar_month=3, lunar_year=2025, leap=True
    )
    assert "không có tháng 3 nhuận" in result
    assert "không hợp lệ" in await tools._tool_lunar_calendar(1, date="31/02/2026")
