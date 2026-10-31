"""SQL thiết bị app (migrations/030, core/repositories/app_devices.py) trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db

OWNER = 111


@pytest.mark.asyncio
async def test_ghep_xac_thuc_thu_hoi(delivery_db):
    device_id = await db.app_device_create_pending(OWNER, "Bếp", "code-a", 600, 5)
    assert device_id
    rows = await db.app_device_list(OWNER)
    assert [r["name"] for r in rows] == ["Bếp"] and rows[0]["paired_at"] is None

    assert await db.app_device_pair("sai", "tok-a") is None
    paired = await db.app_device_pair("code-a", "tok-a")
    assert paired == {"id": device_id, "telegram_user_id": OWNER, "name": "Bếp"}
    assert await db.app_device_pair("code-a", "tok-b") is None  # mã chỉ dùng 1 lần

    assert (await db.app_device_by_token("tok-a"))["id"] == device_id
    assert (await db.app_device_list(OWNER))[0]["paired_at"] is not None

    assert not await db.app_device_revoke(OWNER + 1, device_id)  # máy người khác
    assert await db.app_device_revoke(OWNER, device_id)
    assert await db.app_device_by_token("tok-a") is None
    assert await db.app_device_list(OWNER) == []


@pytest.mark.asyncio
async def test_ma_het_han_va_tran_so_thiet_bi(delivery_db):
    expired = await db.app_device_create_pending(OWNER, "Cũ", "code-old", 0, 2)
    assert expired
    assert await db.app_device_pair("code-old", "tok-old") is None  # hết hạn
    assert await db.app_device_list(OWNER) == []
    # Dòng hết hạn bị dọn khi tạo mã mới -> không tính vào trần.
    assert await db.app_device_create_pending(OWNER, "A", "code-1", 600, 2)
    assert await db.app_device_create_pending(OWNER, "B", "code-2", 600, 2)
    assert await db.app_device_create_pending(OWNER, "C", "code-3", 600, 2) is None
    assert await db.app_device_create_pending(OWNER + 1, "Khác", "code-4", 600, 2)


@pytest.mark.asyncio
async def test_rang_buoc_bang_thiet_bi(delivery_db):
    with pytest.raises(Exception, match="app_devices_name_check"):
        await db.app_device_create_pending(OWNER, "", "code-x", 600, 5)
