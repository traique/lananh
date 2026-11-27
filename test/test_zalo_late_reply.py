"""Lượt Zalo xong SAU khi gateway đã thôi chờ (240 s): câu trả lời được gửi bù qua
outbox thay vì bỏ mất (channels/router.py _deliver_if_gateway_gave_up)."""

import pytest

import messages as messages_module
from channels import router as zalo_router
from channels.contracts import ZaloMessageRequest, ZaloMessageResponse

PAYLOAD = ZaloMessageRequest(
    account_id="acc", sender_id="z1", conversation_id="z1", message_id="m7", text="phân tích HPG"
)


class _Request:
    def __init__(self, disconnected: bool):
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


@pytest.fixture
def outbox(monkeypatch):
    queued = []

    async def enqueue(account_id, recipient_id, content):
        queued.append((account_id, recipient_id, content))

    monkeypatch.setattr(zalo_router.zalo_repository, "enqueue_outbox", enqueue)
    return queued


@pytest.mark.asyncio
async def test_reply_is_queued_when_gateway_already_gave_up(outbox):
    response = ZaloMessageResponse(messages=["Phần 1", "Phần 2"], provider=None)

    await zalo_router._deliver_if_gateway_gave_up(_Request(True), PAYLOAD, response)

    assert outbox == [
        ("acc", "z1", f"{messages_module.ZALO_LATE_REPLY_PREFIX}\nPhần 1"),
        ("acc", "z1", "Phần 2"),
    ]


@pytest.mark.asyncio
async def test_nothing_is_queued_while_gateway_still_waits(outbox):
    response = ZaloMessageResponse(messages=["Trả lời"], provider=None)

    await zalo_router._deliver_if_gateway_gave_up(_Request(False), PAYLOAD, response)
    await zalo_router._deliver_if_gateway_gave_up(None, PAYLOAD, response)
    await zalo_router._deliver_if_gateway_gave_up(
        _Request(True), PAYLOAD, ZaloMessageResponse(messages=[], provider=None)
    )

    assert outbox == []


@pytest.mark.asyncio
async def test_outbox_error_does_not_break_the_turn(monkeypatch):
    async def broken(*args):
        raise RuntimeError("db down")

    monkeypatch.setattr(zalo_router.zalo_repository, "enqueue_outbox", broken)
    response = ZaloMessageResponse(messages=["Trả lời"], provider=None)

    await zalo_router._deliver_if_gateway_gave_up(_Request(True), PAYLOAD, response)
