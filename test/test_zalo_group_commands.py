import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import group_commands


@pytest.mark.asyncio
async def test_add_group_command(monkeypatch):
    saved = {}

    async def fake_add(account_id, group_id, alias):
        saved.update(account_id=account_id, group_id=group_id, alias=alias)

    monkeypatch.setattr(group_commands.zalo_repository, "add_group", fake_add)
    result = await group_commands.maybe_handle_group_command("B", "/themnhom 123 Sale Team")
    assert saved == {"account_id": "B", "group_id": "123", "alias": "sale team"}
    assert "Đã thêm" in result.messages[0]


@pytest.mark.asyncio
async def test_remove_group_command(monkeypatch):
    async def fake_remove(account_id, target):
        return account_id == "B" and target == "sale"

    monkeypatch.setattr(group_commands.zalo_repository, "remove_group", fake_remove)
    result = await group_commands.maybe_handle_group_command("B", "/xoanhom sale")
    assert "Đã ngừng theo dõi" in result.messages[0]


@pytest.mark.asyncio
async def test_unknown_group_command_returns_none():
    assert await group_commands.maybe_handle_group_command("B", "xin chào") is None


@pytest.mark.asyncio
async def test_dangnoi_no_argument_returns_usage():
    result = await group_commands.maybe_handle_group_command("B", "/dangnoi")
    assert "Cú pháp" in result.messages[0]


@pytest.mark.asyncio
async def test_dangnoi_unknown_group_returns_error(monkeypatch):
    async def fake_today_discussion(account_id, target):
        raise ValueError(f"Không tìm thấy nhóm “{target}”.")

    monkeypatch.setattr(group_commands, "today_discussion", fake_today_discussion)
    result = await group_commands.maybe_handle_group_command("B", "/dangnoi sale")
    assert "Không tìm thấy nhóm" in result.messages[0]


@pytest.mark.asyncio
async def test_dangnoi_returns_transcript_chunks(monkeypatch):
    async def fake_today_discussion(account_id, target):
        assert account_id == "B" and target == "sale"
        return "123", "sale", ["phần 1", "phần 2"]

    monkeypatch.setattr(group_commands, "today_discussion", fake_today_discussion)
    result = await group_commands.maybe_handle_group_command("B", "/dangnoi sale")
    assert result.messages == ["phần 1", "phần 2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "target", "enabled"),
    [
        ("/tongket off sale", "sale", False),
        ("/tongket ON sale", "sale", True),
        ("/tongket off", None, False),
        ("/tongket on all", None, True),
    ],
)
async def test_tongket_toggle_daily(monkeypatch, text, target, enabled):
    calls = []

    async def fake_set(account_id, group, flag):
        calls.append((account_id, group, flag))
        return 1

    async def should_not_summarize(*args):
        raise AssertionError("/tongket on|off không được gọi AI tổng kết")

    monkeypatch.setattr(group_commands.zalo_repository, "set_daily_summary", fake_set)
    monkeypatch.setattr(group_commands, "summarize_group", should_not_summarize)
    result = await group_commands.maybe_handle_group_command("B", text)
    assert calls == [("B", target, enabled)]
    assert ("BẬT" if enabled else "TẮT") in result.messages[0]


@pytest.mark.asyncio
async def test_tongket_toggle_unknown_group(monkeypatch):
    async def fake_set(account_id, group, flag):
        return 0

    monkeypatch.setattr(group_commands.zalo_repository, "set_daily_summary", fake_set)
    result = await group_commands.maybe_handle_group_command("B", "/tongket off khong-co")
    assert "Không tìm thấy" in result.messages[0]


@pytest.mark.asyncio
async def test_nhom_shows_daily_status(monkeypatch):
    async def fake_list(account_id):
        return [("1", "sale", True), ("2", "vip", False)]

    monkeypatch.setattr(group_commands.zalo_repository, "list_groups_with_daily", fake_list)
    result = await group_commands.maybe_handle_group_command("B", "/nhom")
    assert "🔔 sale" in result.messages[0]
    assert "🔕 vip" in result.messages[0]


@pytest.mark.asyncio
async def test_themnhom_rejects_toggle_alias():
    result = await group_commands.maybe_handle_group_command("B", "/themnhom 123 off")
    assert "không hợp lệ" in result.messages[0]
