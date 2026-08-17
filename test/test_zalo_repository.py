import pytest

from channels import zalo_repository


@pytest.mark.asyncio
async def test_cleanup_old_messages_binds_retention_as_integer(monkeypatch):
    calls = []

    class FakePool:
        async def execute(self, query, *args):
            calls.append((query, args))

    async def fake_get_pool():
        return FakePool()

    async def fake_ensure_schema():
        return None

    monkeypatch.setenv("ZALO_GROUP_RETENTION_DAYS", "30")
    monkeypatch.setattr(zalo_repository.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(zalo_repository, "ensure_schema", fake_ensure_schema)

    await zalo_repository.cleanup_old_messages("zalo-bot")

    query, args = calls[0]
    assert "$2::integer" in query
    assert args == ("zalo-bot", 30)


@pytest.mark.asyncio
async def test_resolve_default_account_id_returns_row_value(monkeypatch):
    class FakePool:
        async def fetchrow(self, query, *args):
            return {"account_id": "84901234567"}

    async def fake_get_pool():
        return FakePool()

    async def fake_ensure_schema():
        return None

    monkeypatch.setattr(zalo_repository.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(zalo_repository, "ensure_schema", fake_ensure_schema)

    result = await zalo_repository.resolve_default_account_id()
    assert result == "84901234567"


@pytest.mark.asyncio
async def test_resolve_default_account_id_returns_none_when_no_groups(monkeypatch):
    class FakePool:
        async def fetchrow(self, query, *args):
            return None

    async def fake_get_pool():
        return FakePool()

    async def fake_ensure_schema():
        return None

    monkeypatch.setattr(zalo_repository.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(zalo_repository, "ensure_schema", fake_ensure_schema)

    assert await zalo_repository.resolve_default_account_id() is None
