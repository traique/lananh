"""Ghi tin vào zalo_outbox thì báo gateway gửi ngay (POST /outbox/kick) thay vì chờ
lượt poll 90 s - channels/zalo_repository.kick_gateway."""

import asyncio

import pytest

from channels import zalo_gateway_status, zalo_repository


@pytest.fixture
def kicks(monkeypatch):
    calls = []
    gate = asyncio.Event()
    gate.set()

    async def fake_kick():
        calls.append(1)
        await gate.wait()

    monkeypatch.setattr(zalo_gateway_status, "kick_outbox", fake_kick)
    monkeypatch.setattr(zalo_repository, "_kick_task", None)
    monkeypatch.setattr(zalo_repository, "_kick_again", False)
    return calls, gate


async def _settle():
    task = zalo_repository._kick_task
    if task is not None:
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_kicks_while_one_is_in_flight_coalesce_into_one_more(kicks):
    calls, gate = kicks
    gate.clear()

    zalo_repository.kick_gateway()
    await asyncio.sleep(0)
    for _ in range(5):
        zalo_repository.kick_gateway()
    gate.set()
    await _settle()

    assert len(calls) == 2  # kick đang bay + đúng 1 kick gộp cho các tin ghi sau đó


@pytest.mark.asyncio
async def test_enqueue_outbox_kicks_gateway_after_insert(kicks, monkeypatch):
    calls, _ = kicks
    executed = []

    class _Pool:
        async def execute(self, sql, *args):
            executed.append(args)
            assert calls == []  # kick chỉ chạy SAU khi tin đã ghi

    async def pool():
        return _Pool()

    async def no_migrations():
        return None

    monkeypatch.setattr(zalo_repository.db, "get_pool", pool)
    monkeypatch.setattr(zalo_repository, "ensure_schema", no_migrations)

    await zalo_repository.enqueue_outbox("acc", "u1", "Nhắc việc")
    await _settle()

    assert executed == [("acc", "u1", "Nhắc việc")]
    assert calls == [1]


def test_kick_without_event_loop_is_ignored(monkeypatch):
    monkeypatch.setattr(zalo_repository, "_kick_task", None)
    zalo_repository.kick_gateway()  # script/CLI: không ném, vòng poll vẫn gửi
    assert zalo_repository._kick_task is None
