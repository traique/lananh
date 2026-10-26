import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import zalo_users  # noqa: E402


class FakePool:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._next_uid = -1
        self.data_deletes: list[tuple[str, tuple]] = []
        self.in_transaction = False

    def transaction(self):
        pool = self

        class _Tx:
            async def __aenter__(self):
                pool.in_transaction = True

            async def __aexit__(self, *exc):
                pool.in_transaction = False
                return False

        return _Tx()

    async def fetchval(self, query, *args):
        query_lower = query.lower()
        if "delete from zalo_users" in query_lower and "returning internal_user_id" in query_lower:
            assert self.in_transaction
            row = self.rows.pop(args[0], None)
            return row["internal_user_id"] if row else None
        raise AssertionError(f"unexpected query: {query}")

    async def fetchrow(self, query, *args):
        query_lower = query.lower()
        if (
            "select external_id, internal_user_id, display_name, role, status, address_form "
            "from zalo_users where" in query_lower
        ):
            return self.rows.get(args[0])
        if "insert into zalo_users" in query_lower:
            external_id, display_name, role, address_form = args
            existing = self.rows.get(external_id)
            if existing and not display_name:
                display_name = existing["display_name"]
            if existing and address_form is None:
                address_form = existing["address_form"]
            internal_user_id = existing["internal_user_id"] if existing else self._next_uid
            if not existing:
                self._next_uid -= 1
            row = {
                "external_id": external_id,
                "internal_user_id": internal_user_id,
                "display_name": display_name,
                "role": role,
                "status": "active",
                "address_form": address_form,
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
        if query_lower.startswith("delete from"):
            assert self.in_transaction
            self.data_deletes.append((query_lower, args))
            return "DELETE 1"
        raise AssertionError(f"unexpected query: {query}")

    async def fetch(self, query, *args):
        return sorted(
            self.rows.values(),
            key=lambda r: (r["role"] != "admin", r["display_name"], r["external_id"]),
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
async def test_remove_user(patched_db, monkeypatch):
    forgotten = []

    async def forget_sender(external_id):
        forgotten.append(external_id)
        return True

    monkeypatch.setattr(zalo_users.zalo_gateway_status, "forget_sender", forget_sender)
    user = await zalo_users.pair("z6", "")
    assert await zalo_users.remove("z6") is True
    assert await zalo_users.resolve("z6") is None
    assert await zalo_users.remove("z6") is False
    # Gỡ pairing xong báo gateway quên người này ngay (thôi "đã xem"/typing từ tin kế
    # tiếp); không tìm thấy thì không báo.
    assert forgotten == ["z6"]
    # Xoá pairing kéo theo xoá dữ liệu cá nhân, đúng internal_user_id, cùng transaction.
    tables = {q.split()[2] for q, _ in patched_db.data_deletes}
    assert {
        "zalo_outbox",
        "reminders",
        "chat_messages",
        "user_facts",
        "user_memory_summary",
        "user_memory_highlights",
        "user_session_notes",
        "user_events",
        "notes",
        "prompts",
        "stock_holdings",
        "settings",
    } <= tables
    uid_args = [args for q, args in patched_db.data_deletes if "settings" not in q]
    assert all(args == (user.internal_user_id,) for args in uid_args)
    assert (
        "delete from settings where key = $1",
        (f"memory_enabled_{user.internal_user_id}",),
    ) in [(q, a) for q, a in patched_db.data_deletes]
    # Thành phố thời tiết + bản tin sáng cá nhân cũng là dữ liệu cá nhân.
    uid = user.internal_user_id
    assert (
        "delete from settings where key = any($1::text[])",
        (
            [
                f"weather_home:{uid}",
                f"brief:zalo:{uid}",
                f"brief_sent:zalo:{uid}",
                # Trí nhớ/hỏi thăm (services/personal_settings.memory_user_keys).
                f"memory_wm:{uid}",
                f"memory_forget:{uid}",
                f"memory_consolidated:{uid}",
                f"followup:{uid}",
                f"followup_sent:{uid}",
            ],
        ),
    ) in [(q, a) for q, a in patched_db.data_deletes]


@pytest.mark.asyncio
async def test_delete_user_data_refuses_owner_ids(patched_db):
    with pytest.raises(ValueError):
        await zalo_users._delete_user_data(patched_db, 12345)
    assert not patched_db.data_deletes


def test_notified_unpaired_is_bounded(monkeypatch):
    monkeypatch.setattr(zalo_users, "_NOTIFIED_UNPAIRED_MAX", 3)
    for i in range(10):
        zalo_users.notify_unpaired(f"id{i}")
    assert list(zalo_users._notified_unpaired) == ["id7", "id8", "id9"]


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


@pytest.mark.asyncio
async def test_cach_xung_ho_luu_va_giu_nguyen_khi_pair_lai():
    user = await zalo_users.pair("zm", "Mẹ", address_form="cô")
    assert user.address_form == "cô"
    # Pair lại / nâng quyền không truyền cách xưng hô -> giữ nguyên.
    again = await zalo_users.pair("zm", "")
    promoted = await zalo_users.pair_as_admin("zm", "")
    assert again.address_form == "cô" and promoted.address_form == "cô"
    changed = await zalo_users.pair("zm", "", address_form="Chị")
    assert changed.address_form == "chị"
    assert (await zalo_users.resolve("zm")).address_form == "chị"
    assert (await zalo_users.pair("zn", "")).address_form is None


@pytest.mark.asyncio
async def test_cach_xung_ho_ngoai_danh_sach_bi_tu_choi():
    with pytest.raises(ValueError):
        await zalo_users.pair("zq", "", address_form="sếp")


@pytest.mark.asyncio
async def test_thanh_vien_tu_xoa_du_lieu_giu_pairing(patched_db):
    user = await zalo_users.pair("z8", "")
    await zalo_users.delete_own_data(user.internal_user_id)
    assert await zalo_users.resolve("z8") is not None  # vẫn chat tiếp được
    tables = {q.split()[2] for q, _ in patched_db.data_deletes}
    assert {"user_facts", "notes", "chat_messages", "user_session_notes"} <= tables
    with pytest.raises(ValueError):
        await zalo_users.delete_own_data(42)  # id chủ bot (dương) bị từ chối
