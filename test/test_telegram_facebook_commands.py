import pytest

from handlers import zalo_bridge_commands as bridge


class FakeMessage:
    pass


class FakeUpdate:
    message = FakeMessage()


class FakeContext:
    def __init__(self, args):
        self.args = args


@pytest.mark.asyncio
async def test_telegram_facebook_command_uses_zalo_session_account(monkeypatch):
    seen = {}
    replies = []

    async def fake_account_id():
        return "84901234567"

    async def fake_command(account_id, text):
        seen["account_id"] = account_id
        seen["text"] = text
        return bridge.facebook_commands.ChannelResult(["ok"])

    async def fake_reply(message, text):
        replies.append(text)

    monkeypatch.setattr(bridge.zalo_session, "load_account_id", fake_account_id)
    monkeypatch.setattr(bridge.facebook_commands, "maybe_handle_facebook_command", fake_command)
    monkeypatch.setattr(bridge.common, "reply_long_text", fake_reply)

    await bridge._facebook_command(
        FakeUpdate(), FakeContext(["25", "https://s.shopee.vn/new"]), "/fb_link"
    )

    assert seen == {
        "account_id": "84901234567",
        "text": "/fb_link 25 https://s.shopee.vn/new",
    }
    assert replies == ["ok"]
