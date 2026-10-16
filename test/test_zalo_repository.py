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


class _RecordingPool:
    def __init__(self, row=None):
        self.calls = []
        self.row = row

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row


@pytest.fixture
def recording_pool(monkeypatch):
    pool = _RecordingPool()

    async def fake_get_pool():
        return pool

    async def fake_ensure_schema():
        return None

    monkeypatch.setattr(zalo_repository.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(zalo_repository, "ensure_schema", fake_ensure_schema)
    return pool


@pytest.mark.asyncio
async def test_account_outbox_skips_failed_and_backoff_rows(recording_pool):
    await zalo_repository.get_account_outbox("acc")
    query, args = recording_pool.calls[0]
    assert "failed_at IS NULL" in query and "next_attempt_at" in query
    assert "chunks_sent" in query
    assert args == ("acc", 20)


@pytest.mark.asyncio
async def test_mark_outbox_failed_binds_limits(recording_pool, monkeypatch):
    monkeypatch.setenv("ZALO_OUTBOX_MAX_ATTEMPTS", "3")
    recording_pool.row = {"id": 1, "recipient_id": "u", "attempts": 1, "failed": False}
    row = await zalo_repository.mark_outbox_failed(1, "e" * 2000, -4)
    assert row["attempts"] == 1
    query, args = recording_pool.calls[0]
    assert "UPDATE reminders" in query
    item_id, error, chunks, max_attempts, base, cap = args
    assert (item_id, chunks, max_attempts) == (1, 0, 3)
    assert len(error) == zalo_repository.OUTBOX_ERROR_MAX_CHARS
    assert (base, cap) == (
        zalo_repository.OUTBOX_RETRY_BASE_SEC,
        zalo_repository.OUTBOX_RETRY_MAX_SEC,
    )


@pytest.mark.parametrize("raw, expected", [("", 5), ("x", 5), ("0", 1), ("99", 20), ("7", 7)])
def test_outbox_max_attempts_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("ZALO_OUTBOX_MAX_ATTEMPTS", raw)
    assert zalo_repository.outbox_max_attempts() == expected


@pytest.mark.asyncio
async def test_resolve_active_account_id_uu_tien_session(monkeypatch):
    # Gateway lưu accountId numeric sau khi đăng nhập - job nền phải dùng đúng id
    # này (trước đây đọc env mặc định "zalo-bot" nên không thấy nhóm/outbox nào).
    from channels import zalo_session

    async def fake_load_account_id():
        return "123456789"

    monkeypatch.setattr(zalo_session, "load_account_id", fake_load_account_id)
    monkeypatch.setenv("ZALO_BOT_ACCOUNT_ID", "zalo-bot")

    assert await zalo_repository.resolve_active_account_id() == "123456789"


@pytest.mark.asyncio
async def test_resolve_active_account_id_fallback_env_roi_groups(monkeypatch):
    from channels import zalo_session

    async def fake_load_account_id():
        return ""

    async def fake_default():
        return "from-groups"

    monkeypatch.setattr(zalo_session, "load_account_id", fake_load_account_id)
    monkeypatch.setattr(zalo_repository, "resolve_default_account_id", fake_default)

    monkeypatch.setenv("ZALO_BOT_ACCOUNT_ID", "custom-id")
    assert await zalo_repository.resolve_active_account_id() == "custom-id"

    monkeypatch.setenv("ZALO_BOT_ACCOUNT_ID", "zalo-bot")
    assert await zalo_repository.resolve_active_account_id() == "from-groups"

    async def fake_default_none():
        return None

    monkeypatch.setattr(zalo_repository, "resolve_default_account_id", fake_default_none)
    monkeypatch.delenv("ZALO_BOT_ACCOUNT_ID", raising=False)
    assert await zalo_repository.resolve_active_account_id() == "zalo-bot"
