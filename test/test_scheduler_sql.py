"""SQL "còn bao lâu tới nhắc claim được" của vòng nhắc (scheduler._seconds_until_next_due)
trên PostgreSQL thật - đúng điều kiện claim của idempotency.claim_due_reminders.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

from datetime import datetime, timedelta, timezone

import pytest

import scheduler
import test_delivery_sql as _delivery_sql
from core import database as db
from core import idempotency

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db


async def _add(minutes: float, uid: int = 42) -> int:
    due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await db.add_reminder(uid, f"nhắc {minutes}", due, channel="telegram", recipient_id=str(uid))
    rows = await (await db.get_pool()).fetch(
        "SELECT id FROM reminders WHERE message = $1", f"nhắc {minutes}"
    )
    return int(rows[0]["id"])


@pytest.mark.asyncio
async def test_khong_con_nhac_cho_thi_none(delivery_db):
    assert await scheduler._seconds_until_next_due() is None


@pytest.mark.asyncio
async def test_nhac_xa_nhat_lay_som_nhat_va_bo_nhac_da_gui(delivery_db):
    pool = delivery_db
    sent_id = await _add(5)
    await _add(60)
    await _add(30)
    await pool.execute("UPDATE reminders SET sent = true WHERE id = $1", sent_id)
    wait = await scheduler._seconds_until_next_due()
    assert wait is not None and 29 * 60 < wait <= 30 * 60


@pytest.mark.asyncio
async def test_nhac_qua_han_co_ngay_va_da_claim_thi_tinh_tu_luc_het_lease(delivery_db):
    await _add(-3)
    wait = await scheduler._seconds_until_next_due()
    assert wait is not None and wait <= 0
    claimed = await idempotency.claim_due_reminders(
        limit=scheduler._CLAIM_LIMIT, lease=scheduler._CLAIM_LEASE
    )
    assert len(claimed) == 1
    # Đã claim (vd Zalo chờ gateway ack): chỉ claim lại được sau lease 5 phút.
    wait = await scheduler._seconds_until_next_due()
    assert wait is not None and 4 * 60 < wait <= 5 * 60
    # Giao lỗi nhả claim: claim lại được ngay.
    await idempotency.release_reminder_claim(claimed[0].id)
    wait = await scheduler._seconds_until_next_due()
    assert wait is not None and wait <= 0
