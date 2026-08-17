import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import channel_command_service as service


@pytest.mark.asyncio
async def test_help_command():
    messages, provider = await service.maybe_handle_command(1, "/help")
    assert "/gia" in messages[0]
    assert "/tongket" in messages[0]
    assert provider is None


@pytest.mark.asyncio
async def test_non_command_is_not_handled():
    assert await service.maybe_handle_command(1, "xin chào") is None


@pytest.mark.asyncio
async def test_status_denied_for_non_admin():
    result, provider = await service.maybe_handle_command(1, "/status", is_admin=False)
    assert result == ["Lệnh này chỉ dành cho admin."]


@pytest.mark.asyncio
async def test_userouter9_denied_for_non_admin():
    result, provider = await service.maybe_handle_command(1, "/userouter9", is_admin=False)
    assert result == ["Lệnh này chỉ dành cho admin."]


@pytest.mark.asyncio
async def test_model_denied_for_non_admin():
    result, provider = await service.maybe_handle_command(1, "/model auto", is_admin=False)
    assert result == ["Lệnh này chỉ dành cho admin."]


@pytest.mark.asyncio
async def test_status_allowed_for_admin_by_default():
    result, provider = await service.maybe_handle_command(1, "/status")
    assert result != ["Lệnh này chỉ dành cho admin."]
    assert "Provider" in result[0]


@pytest.mark.asyncio
async def test_normal_commands_still_allowed_for_non_admin(monkeypatch):
    """Thành viên thường (is_admin=False) vẫn dùng được tính năng bình thường -
    chỉ 3 lệnh cấu hình toàn cục (/status, /userouter9, /model) mới bị chặn."""
    async def fake_clear(user_id):
        return None
    monkeypatch.setattr(service.orchestrator, "reset_chat", lambda: _noop())
    monkeypatch.setattr(service.db, "clear_chat", fake_clear)
    result, _ = await service.maybe_handle_command(1, "/reset", is_admin=False)
    assert "Đã xoá" in result[0]


async def _noop():
    return None


@pytest.mark.asyncio
async def test_reset_clears_both_sessions(monkeypatch):
    called = []
    async def reset(): called.append("reset_chat")
    async def clear(user_id): called.append(("db", user_id))
    monkeypatch.setattr(service.orchestrator, "reset_chat", reset)
    monkeypatch.setattr(service.db, "clear_chat", clear)
    result, _ = await service.maybe_handle_command(7, "/reset")
    assert called == ["reset_chat", ("db", 7)]
    assert "Đã xoá" in result[0]


@pytest.mark.asyncio
async def test_dich_command_no_argument_returns_usage():
    result, provider = await service.maybe_handle_command(1, "/dich")
    assert "Cú pháp" in result[0]
    assert provider is None


@pytest.mark.asyncio
async def test_dich_command_translates_via_translate_service(monkeypatch):
    async def fake_translate(text, direction=None):
        class FakeResponse:
            used_fallback = False
        return "hello", "ja_vi", FakeResponse()

    monkeypatch.setattr(service.translate_service, "translate", fake_translate)
    result, provider = await service.maybe_handle_command(1, "/dich こんにちは")
    assert "hello" in result[0]
    assert "Tiếng Nhật" in result[0]
    assert provider is None
