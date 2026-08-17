import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import router as zalo_router  # noqa: E402
from channels.contracts import ZaloMessageRequest  # noqa: E402
from services.channel_chat_service import ChannelResult  # noqa: E402


class _FakeContextManager:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _payload(sender_id="z1", sender_name="Người dùng", text="xin chào", message_id="m1"):
    return ZaloMessageRequest(
        account_id="acc",
        sender_id=sender_id,
        sender_name=sender_name,
        conversation_id="conv",
        message_id=message_id,
        text=text,
    )


@pytest.fixture(autouse=True)
def patch_common(monkeypatch):
    monkeypatch.setattr(zalo_router, "_secret", lambda: "s3cr3t")
    monkeypatch.setattr(zalo_router, "assistant_turn", lambda: _FakeContextManager())

    async def fake_get_cached(*args, **kwargs):
        return None

    async def fake_save(*args, **kwargs):
        return None

    monkeypatch.setattr(zalo_router.idempotency, "get_zalo_response", fake_get_cached)
    monkeypatch.setattr(zalo_router.idempotency, "save_zalo_response", fake_save)


@pytest.mark.asyncio
async def test_unpaired_sender_gets_silent_reply_and_owner_notified(monkeypatch):
    notified = []

    async def fake_resolve(external_id):
        return None

    monkeypatch.setattr(zalo_router.zalo_users, "resolve", fake_resolve)
    monkeypatch.setattr(
        zalo_router.zalo_users, "notify_unpaired", lambda eid, name="": notified.append((eid, name))
    )

    response = await zalo_router.receive(_payload(sender_id="stranger"), "s3cr3t")
    assert response.messages == []
    assert notified == [("stranger", "Người dùng")]


@pytest.mark.asyncio
async def test_suspended_sender_gets_locked_reply(monkeypatch):
    class FakeUser:
        is_active = False
        is_admin = False
        internal_user_id = -1

    async def fake_resolve(external_id):
        return FakeUser()

    monkeypatch.setattr(zalo_router.zalo_users, "resolve", fake_resolve)

    response = await zalo_router.receive(_payload(), "s3cr3t")
    assert response.messages == [zalo_router.messages_module.ZALO_LOCKED_REPLY]


@pytest.mark.asyncio
async def test_regular_user_never_gets_group_command_checked(monkeypatch):
    class FakeUser:
        is_active = True
        is_admin = False
        internal_user_id = -2

    called = {"group": False}

    async def fake_resolve(external_id):
        return FakeUser()

    async def fake_group_command(account_id, text):
        called["group"] = True
        return ChannelResult(["không nên chạy tới đây"])

    async def fake_handle_channel_text(user_id, text, is_admin=True):
        return ChannelResult(["trả lời bình thường"])

    monkeypatch.setattr(zalo_router.zalo_users, "resolve", fake_resolve)
    monkeypatch.setattr(zalo_router, "maybe_handle_group_command", fake_group_command)
    monkeypatch.setattr(zalo_router, "handle_channel_text", fake_handle_channel_text)

    response = await zalo_router.receive(_payload(text="/tongket sale"), "s3cr3t")
    assert called["group"] is False
    assert response.messages == ["trả lời bình thường"]


@pytest.mark.asyncio
async def test_admin_gets_group_command_checked_first(monkeypatch):
    class FakeUser:
        is_active = True
        is_admin = True
        internal_user_id = -3

    async def fake_resolve(external_id):
        return FakeUser()

    async def fake_group_command(account_id, text):
        return ChannelResult(["tổng kết nhóm"])

    async def fake_handle_channel_text(user_id, text, is_admin=True):
        raise AssertionError("Admin gõ lệnh nhóm không nên rơi xuống handle_channel_text")

    monkeypatch.setattr(zalo_router.zalo_users, "resolve", fake_resolve)
    monkeypatch.setattr(zalo_router, "maybe_handle_group_command", fake_group_command)
    monkeypatch.setattr(zalo_router, "handle_channel_text", fake_handle_channel_text)

    response = await zalo_router.receive(_payload(text="/tongket sale"), "s3cr3t")
    assert response.messages == ["tổng kết nhóm"]


@pytest.mark.asyncio
async def test_each_zalo_user_gets_own_internal_user_id(monkeypatch):
    """Xác nhận user_id truyền vào handle_channel_text là internal_user_id
    RIÊNG của từng zalo_user, KHÔNG phải config.ALLOWED_USER_ID dùng chung -
    đây chính là cơ chế cách ly bộ nhớ/ngữ cảnh giữa các người dùng Zalo."""

    class FakeUserA:
        is_active = True
        is_admin = False
        internal_user_id = -101

    class FakeUserB:
        is_active = True
        is_admin = False
        internal_user_id = -202

    resolved = {"a": FakeUserA(), "b": FakeUserB()}
    captured_user_ids = []

    async def fake_resolve(external_id):
        return resolved[external_id]

    async def fake_handle_channel_text(user_id, text, is_admin=True):
        captured_user_ids.append(user_id)
        return ChannelResult([f"reply-for-{user_id}"])

    monkeypatch.setattr(zalo_router.zalo_users, "resolve", fake_resolve)
    monkeypatch.setattr(zalo_router, "handle_channel_text", fake_handle_channel_text)

    resp_a = await zalo_router.receive(_payload(sender_id="a", text="hi", message_id="ma"), "s3cr3t")
    resp_b = await zalo_router.receive(_payload(sender_id="b", text="hi", message_id="mb"), "s3cr3t")

    assert captured_user_ids == [-101, -202]
    assert captured_user_ids[0] != captured_user_ids[1]
    assert resp_a.messages == ["reply-for--101"]
    assert resp_b.messages == ["reply-for--202"]


@pytest.mark.asyncio
async def test_admin_normal_chat_falls_through_to_handle_channel_text(monkeypatch):
    class FakeUser:
        is_active = True
        is_admin = True
        internal_user_id = -4

    async def fake_resolve(external_id):
        return FakeUser()

    async def fake_group_command(account_id, text):
        return None  # không phải lệnh nhóm

    async def fake_handle_channel_text(user_id, text, is_admin=True):
        return ChannelResult(["chat bình thường"])

    monkeypatch.setattr(zalo_router.zalo_users, "resolve", fake_resolve)
    monkeypatch.setattr(zalo_router, "maybe_handle_group_command", fake_group_command)
    monkeypatch.setattr(zalo_router, "handle_channel_text", fake_handle_channel_text)

    response = await zalo_router.receive(_payload(text="xin chào"), "s3cr3t")
    assert response.messages == ["chat bình thường"]
