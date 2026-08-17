import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import zalo_users  # noqa: E402


class FakePool:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._next_uid = -1

    async def fetchrow(self, query, *args):
        query_lower = query.lower()
        if "select external_id, internal_user_id, display_name, role, status from zalo_users where" in query_lower:
            return self.rows.get(args[0])
        if "insert into zalo_users" in query_lower:
            external_id, display_name, role = args
            existing = self.rows.get(external_id)
            if existing and not display_name:
                display_name = existing["display_name"]
            internal_user_id = existing["internal_user_id"] if existing else self._next_uid
            if not existing:
                self._next_uid -= 1
            row = {
                "external_id": external_id,
                "internal_user_id": internal_user_id,
                "display_name": display_name,
                "role": role,
                "status": "active",
            }
            self.rows[external_id] = row
            return row
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, *args):
        query_lower = query.lower()
        if "update zalo_users set role" in query_lower:
            external_id = args[0]
            if external_id not in self.rows:
                return "UPDATE 0"
            self.rows[external_id]["role"] = "user"
            return "UPDATE 1"
        if "update zalo_users set status" in query_lower:
            external_id, status = args
            if external_id not in self.rows:
                return "UPDATE 0"
            self.rows[external_id]["status"] = status
            return "UPDATE 1"
        if "delete from zalo_users" in query_lower:
            external_id = args[0]
            if external_id not in self.rows:
                return "DELETE 0"
            del self.rows[external_id]
            return "DELETE 1"
        raise AssertionError(f"unexpected query: {query}")

    async def fetch(self, query, *args):
        return sorted(
            self.rows.values(), key=lambda r: (r["role"] != "admin", r["display_name"], r["external_id"])
        )

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def patched_db(monkeypatch):
    pool = FakePool()

    async def fake_get_pool():
        return pool

    async def fake_ensure_schema():
        return None

    monkeypatch.setattr(zalo_users.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(zalo_users, "ensure_schema", fake_ensure_schema)
    zalo_users._notified_unpaired.clear()
    zalo_users._alert_callback = None
    return pool


@pytest.mark.asyncio
async def test_pair_new_user_defaults_to_role_user():
    user = await zalo_users.pair("z1", "Anh Tuấn")
    assert user.role == "user"
    assert user.status == "active"
    assert user.display_name == "Anh Tuấn"


@pytest.mark.asyncio
async def test_pair_as_admin_sets_admin_role():
    user = await zalo_users.pair_as_admin("z2", "Chị Lan")
    assert user.role == "admin"
    assert user.is_admin is True


@pytest.mark.asyncio
async def test_pair_does_not_demote_existing_admin():
    await zalo_users.pair_as_admin("z3", "Admin")
    user = await zalo_users.pair("z3", "")
    assert user.role == "admin"  # /zalopair không hạ quyền admin đã có


@pytest.mark.asyncio
async def test_demote_to_user():
    await zalo_users.pair_as_admin("z4", "Admin")
    found = await zalo_users.demote_to_user("z4")
    assert found is True
    user = await zalo_users.resolve("z4")
    assert user.role == "user"


@pytest.mark.asyncio
async def test_demote_unknown_user_returns_false():
    assert await zalo_users.demote_to_user("khong-ton-tai") is False


@pytest.mark.asyncio
async def test_set_status_lock_and_unlock():
    await zalo_users.pair("z5", "")
    assert await zalo_users.set_status("z5", "suspended") is True
    user = await zalo_users.resolve("z5")
    assert user.status == "suspended"
    assert user.is_active is False
    assert await zalo_users.set_status("z5", "active") is True


@pytest.mark.asyncio
async def test_set_status_invalid_raises():
    with pytest.raises(ValueError):
        await zalo_users.set_status("z5", "banned")


@pytest.mark.asyncio
async def test_remove_user():
    await zalo_users.pair("z6", "")
    assert await zalo_users.remove("z6") is True
    assert await zalo_users.resolve("z6") is None
    assert await zalo_users.remove("z6") is False


@pytest.mark.asyncio
async def test_resolve_unknown_returns_none():
    assert await zalo_users.resolve("khong-ton-tai") is None


@pytest.mark.asyncio
async def test_list_users_admin_first():
    await zalo_users.pair("z7", "Bob")
    await zalo_users.pair_as_admin("z8", "Alice")
    users = await zalo_users.list_users()
    assert users[0].external_id == "z8"
    assert users[0].role == "admin"


@pytest.mark.asyncio
async def test_internal_user_id_is_negative_and_unique():
    user_a = await zalo_users.pair("za", "A")
    user_b = await zalo_users.pair("zb", "B")
    assert user_a.internal_user_id < 0
    assert user_b.internal_user_id < 0
    assert user_a.internal_user_id != user_b.internal_user_id


@pytest.mark.asyncio
async def test_internal_user_id_stable_across_role_changes():
    """internal_user_id KHÔNG được đổi khi nâng/hạ quyền hay pair lại - đây là
    khoá cho toàn bộ lịch sử chat/trí nhớ của người đó, đổi là mất ngữ cảnh."""
    first = await zalo_users.pair("zc", "C")
    promoted = await zalo_users.pair_as_admin("zc", "")
    demoted_ok = await zalo_users.demote_to_user("zc")
    after = await zalo_users.resolve("zc")

    assert promoted.internal_user_id == first.internal_user_id
    assert demoted_ok is True
    assert after.internal_user_id == first.internal_user_id


@pytest.mark.asyncio
async def test_notify_unpaired_calls_alert_once():
    import asyncio

    calls = []

    async def fake_alert(text):
        calls.append(text)

    zalo_users.set_alert_callback(fake_alert)
    zalo_users.notify_unpaired("zX", "Người lạ")
    zalo_users.notify_unpaired("zX", "Người lạ")  # gọi lần 2 không gửi thêm
    await asyncio.sleep(0)  # để task nền (create_task) kịp chạy
    assert len(calls) == 1
    assert "zX" in calls[0]
    assert "zX" in zalo_users._notified_unpaired
