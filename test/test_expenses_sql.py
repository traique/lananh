"""SQL chi tiêu (migrations/020, core/repositories/expenses.py) trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

from datetime import timedelta

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db


async def _add(uid, amount, category, day, key=None):
    return await db.expense_add(
        uid,
        amount,
        category=category,
        note="ghi chú",
        spent_on=day,
        source="chat",
        draft=False,
        event_key=key,
    )


@pytest.mark.asyncio
async def test_tong_theo_danh_muc_bang_sql_va_rieng_tung_nguoi(delivery_db):
    assert await _add(42, 45_000, "an_uong", "2026-10-25", "telegram:1:chi") == (1, True)
    assert await _add(42, 45_000, "an_uong", "2026-10-25", "telegram:1:chi") == (1, False)
    await _add(42, 60_000, "an_uong", "2026-10-26")
    await _add(42, 80_000, "di_lai", "2026-10-26")
    await _add(42, 500_000, "nha_cua", "2026-09-30")  # tháng trước
    await _add(-5, 999_000, "an_uong", "2026-10-26")  # thành viên Zalo khác

    rows = await db.expense_totals(42, "2026-10-01", "2026-11-01")
    assert [(r["category"], int(r["total"]), int(r["items"])) for r in rows] == [
        ("an_uong", 105_000, 2),
        ("di_lai", 80_000, 1),
    ]
    recent = await db.expense_recent(42, 2)
    assert [r["spent_on"] for r in recent] == ["2026-10-26", "2026-10-26"]
    assert await db.expense_count_since(42, timedelta(hours=24)) == 4


@pytest.mark.asyncio
async def test_xoa_chi_cua_chinh_nguoi_do_va_check_so_tien(delivery_db):
    expense_id, _ = await _add(42, 45_000, "an_uong", "2026-10-26")
    assert await db.expense_delete(-5, expense_id) is None
    last = await db.expense_last(42, timedelta(minutes=10))
    assert last["id"] == expense_id and last["spent_on"] == "2026-10-26"
    removed = await db.expense_delete(42, expense_id)
    assert removed["amount_vnd"] == 45_000
    with pytest.raises(Exception, match="expenses_amount_check"):
        await _add(42, 0, "khac", "2026-10-26")


@pytest.mark.asyncio
async def test_zaloxoa_xoa_chi_tieu_thanh_vien(delivery_db):
    from channels import zalo_users

    member = await zalo_users.pair("z-member", "Member")
    await _add(member.internal_user_id, 45_000, "an_uong", "2026-10-26")
    await _add(999, 45_000, "an_uong", "2026-10-26")
    assert await zalo_users.remove("z-member")
    owners = await delivery_db.fetch("SELECT telegram_user_id FROM expenses")
    assert [r["telegram_user_id"] for r in owners] == [999]
