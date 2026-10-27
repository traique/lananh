"""SQL nhật ký ăn uống (migrations/028, core/repositories/meals.py) trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

from datetime import timedelta

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db


async def _add(uid, day, food, kcal, meal="trua", key=None, portion=1.0):
    return await db.meal_add(
        uid,
        eaten_on=day,
        meal=meal,
        food=food,
        portion=portion,
        kcal=kcal,
        kcal_source="table" if kcal is not None else "none",
        source="command",
        event_key=key,
    )


@pytest.mark.asyncio
async def test_tong_kcal_theo_ngay_bang_sql_va_rieng_tung_nguoi(delivery_db):
    assert await _add(42, "2026-10-26", "Phở bò", 450, "sang", "telegram:1:an:1") == (1, True)
    assert await _add(42, "2026-10-26", "Phở bò", 450, "sang", "telegram:1:an:1") == (1, False)
    await _add(42, "2026-10-26", "Cơm trắng", 400, portion=2)
    await _add(42, "2026-10-26", "Bánh đúc", None)
    await _add(42, "2026-10-25", "Bún chả", 550)
    await _add(-5, "2026-10-26", "Lẩu", 600)  # thành viên Zalo khác

    rows = await db.meal_day(42, "2026-10-26")
    assert [r["food"] for r in rows] == ["Phở bò", "Cơm trắng", "Bánh đúc"]
    assert rows[1]["portion"] == 2.0 and rows[2]["kcal"] is None
    totals = await db.meal_day_totals(42, "2026-10-20", "2026-10-27")
    assert [(r["eaten_on"], int(r["kcal"]), r["items"], r["unknown"]) for r in totals] == [
        ("2026-10-25", 550, 1, 0),
        ("2026-10-26", 850, 3, 1),
    ]
    assert await db.meal_count_since(42, timedelta(hours=24)) == 4
    assert [r["food"] for r in await db.meal_recent(42, 2)] == ["Bánh đúc", "Cơm trắng"]


@pytest.mark.asyncio
async def test_xoa_mon_cua_chinh_nguoi_do_va_check_rang_buoc(delivery_db):
    meal_id, _ = await _add(42, "2026-10-26", "Phở bò", 450)
    assert await db.meal_delete(-5, meal_id) is None
    removed = await db.meal_delete(42, meal_id)
    assert removed["food"] == "Phở bò" and removed["eaten_on"] == "2026-10-26"
    with pytest.raises(Exception, match="meals_meal_check"):
        await _add(42, "2026-10-26", "Phở", 450, meal="dem")
    with pytest.raises(Exception, match="meals_portion_check"):
        await _add(42, "2026-10-26", "Phở", 450, portion=0)
    await _add(42, "2026-10-26", "Phở", 450)
    assert await db.meal_clear(42) == 1


@pytest.mark.asyncio
async def test_zaloxoa_xoa_nhat_ky_an_uong_thanh_vien(delivery_db):
    from channels import zalo_users

    member = await zalo_users.pair("z-member", "Member")
    await _add(member.internal_user_id, "2026-10-26", "Phở bò", 450)
    await _add(999, "2026-10-26", "Phở bò", 450)
    assert await zalo_users.remove("z-member")
    owners = await delivery_db.fetch("SELECT telegram_user_id FROM meals")
    assert [r["telegram_user_id"] for r in owners] == [999]
