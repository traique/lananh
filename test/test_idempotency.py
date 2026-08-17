import pytest

from core import idempotency


@pytest.mark.asyncio
async def test_telegram_update_claim_is_atomic(monkeypatch):
    results = iter(["INSERT 0 1", "INSERT 0 0"])

    class FakePool:
        async def execute(self, query, *args):
            return next(results)

    async def fake_get_pool():
        return FakePool()

    async def fake_ensure_schema():
        return None

    monkeypatch.setattr(idempotency.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(idempotency, "ensure_schema", fake_ensure_schema)

    assert await idempotency.claim_telegram_update(123) is True
    assert await idempotency.claim_telegram_update(123) is False


@pytest.mark.asyncio
async def test_zoom_event_claim_is_atomic(monkeypatch):
    results = iter(["INSERT 0 1", "INSERT 0 0"])

    class FakePool:
        async def execute(self, query, *args):
            return next(results)

    async def fake_get_pool():
        return FakePool()

    async def fake_ensure_schema():
        return None

    monkeypatch.setattr(idempotency.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(idempotency, "ensure_schema", fake_ensure_schema)

    assert await idempotency.claim_zoom_event("evt-1") is True
    assert await idempotency.claim_zoom_event("evt-1") is False


@pytest.mark.asyncio
async def test_zalo_cached_response_is_replayed(monkeypatch):
    class FakePool:
        async def fetchval(self, query, *args):
            return '{"messages":["cached"],"provider":null}'

    async def fake_get_pool():
        return FakePool()

    async def fake_ensure_schema():
        return None

    monkeypatch.setattr(idempotency.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(idempotency, "ensure_schema", fake_ensure_schema)

    value = await idempotency.get_zalo_response("bot", "message-1", "text")

    assert value == {"messages": ["cached"], "provider": None}
