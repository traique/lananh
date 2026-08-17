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

    async def fake_add_note(uid, content):
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

    result = await tools.maybe_run_tool(1, "hôm nay trời đẹp quá")
    assert result is None


@pytest.mark.asyncio
async def test_loi_router_khong_raise(monkeypatch):
    async def fake_raise(prompt):
        raise RuntimeError("mô phỏng lỗi API")

    monkeypatch.setattr(official_client, "generate_utility_json", fake_raise)

    result = await tools.maybe_run_tool(1, "bất kỳ gì")
    assert result is None  # không raise, chat chính vẫn tiếp tục bình thường


@pytest.mark.asyncio
async def test_set_reminder_toi_thieu_1_phut(monkeypatch):
    """minutes_from_now <= 0 (Gemini tính sai hoặc không trả) -> vẫn phải
    đặt được reminder tối thiểu 1 phút, không được lưu due_at ở quá khứ."""
    calls = []

    async def fake_add_reminder(uid, message, due_at):
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
