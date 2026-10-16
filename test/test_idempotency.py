from datetime import timedelta

import pytest

from core import idempotency


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


@pytest.mark.asyncio
async def test_cleanup_expired_prunes_all_idempotency_tables(monkeypatch):
    queries = []

    class FakePool:
        async def execute(self, query, *args):
            queries.append((query, args))
            return "DELETE 0"

    async def fake_get_pool():
        return FakePool()

    async def fake_ensure_schema():
        return None

    monkeypatch.setattr(idempotency.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(idempotency, "ensure_schema", fake_ensure_schema)

    await idempotency.cleanup_expired()

    sql = "\n".join(query for query, _ in queries)
    # Bảng claim theo update/event đã bỏ (webhook_inbox thay thế) - không còn dọn.
    assert "telegram_processed_updates" not in sql
    assert "zoom_processed_events" not in sql
    assert "zalo_direct_responses" in sql and "webhook_inbox" in sql
    assert "zalo_direct_responses" in sql
    dead = [args for query, args in queries if "status = 'DEAD'" in query]
    assert dead == [(timedelta(days=idempotency.config.WEBHOOK_DEAD_RETENTION_DAYS),)]
    assert all(args == (idempotency.RETENTION,) for query, args in queries if "'DEAD'" not in query)
