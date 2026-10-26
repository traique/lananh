"""SQL nhắc việc v2 (migrations/019, core/repositories/reminders.py) trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

from datetime import datetime, timedelta, timezone

import pytest

import test_delivery_sql as _delivery_sql
from channels import zalo_repository
from core import database as db
from core import idempotency
from services import reminder_service

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db

RULE = {"type": "daily", "at": "07:00"}


async def _series(uid=42, *, channel="telegram", first=None, kind="reminder", event_key=None):
    now = datetime.now(timezone.utc)
    return await db.reminder_create_series(
        uid,
        "uống thuốc",
        kind=kind,
        category="" if kind == "reminder" else "gio",
        recurrence=RULE,
        lead_days=0,
        channel=channel,
        recipient_id="z-user" if channel == "zalo" else str(uid),
        account_id="acc" if channel == "zalo" else "",
        user_jid="",
        event_key=event_key,
        first=first or [(now - timedelta(minutes=1), "uống thuốc")],
    )


@pytest.mark.asyncio
async def test_chuoi_lap_claim_tra_series_va_lan_ke_idempotent(delivery_db):
    pool = delivery_db
    sid, created = await _series(event_key="telegram:1")
    assert created
    assert await _series(event_key="telegram:1") == (sid, False)  # webhook gửi lại
    assert await db.reminder_count_series(42) == 1

    claimed = await idempotency.claim_due_reminders()
    assert len(claimed) == 1
    item = claimed[0]
    assert item.series_id == sid and item.kind == "reminder" and item.due_at is not None

    now = datetime.now(timezone.utc)
    assert await reminder_service.schedule_next(sid, item.due_at, now)
    assert not await reminder_service.schedule_next(sid, item.due_at, now)  # claim lại
    assert await pool.fetchval("SELECT count(*) FROM reminders WHERE series_id = $1", sid) == 2
    series = await db.reminder_list_series(42)
    assert series[0]["recurrence"] == RULE
    assert series[0]["next_due_at"] is not None


@pytest.mark.asyncio
async def test_nhac_lap_zalo_vao_outbox_moi_lan_mot_dong(delivery_db):
    """Mỗi lần là 1 dòng reminders mới -> outbox UNIQUE(reminder_id) không chặn lần 2."""
    pool = delivery_db
    sid, _ = await _series(-7, channel="zalo")
    first = (await idempotency.claim_due_reminders())[0]
    await zalo_repository.enqueue_reminder(first.id, "acc", "z-user", "lần 1")
    await db.reminder_mark_fired(first.id)
    outbox_id = await pool.fetchval("SELECT id FROM zalo_outbox WHERE reminder_id = $1", first.id)
    await zalo_repository.mark_outbox_sent(outbox_id)

    await reminder_service.schedule_next(sid, first.due_at, datetime.now(timezone.utc))
    await pool.execute(
        "UPDATE reminders SET due_at = now() - interval '1 minute' WHERE series_id = $1 AND sent = false",
        sid,
    )
    second = (await idempotency.claim_due_reminders())[0]
    assert second.id != first.id
    await zalo_repository.enqueue_reminder(second.id, "acc", "z-user", "lần 2")
    assert await pool.fetchval("SELECT count(*) FROM zalo_outbox") == 2
    fired = await db.reminder_last_fired(-7, timedelta(minutes=30))
    assert fired is not None and fired["id"] == first.id


@pytest.mark.asyncio
async def test_huy_va_hoan_xoa_tin_outbox_chua_gui(delivery_db):
    pool = delivery_db
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.add_reminder(-7, "gọi mẹ", due, channel="zalo", recipient_id="z", account_id="acc")
    rid = await pool.fetchval("SELECT id FROM reminders")
    await zalo_repository.enqueue_reminder(rid, "acc", "z", "nhắc")

    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert not await db.reminder_reschedule(42, rid, later)  # người khác không dời được
    assert await db.reminder_reschedule(-7, rid, later)
    assert await pool.fetchval("SELECT count(*) FROM zalo_outbox") == 0
    assert await pool.fetchval("SELECT claimed_at FROM reminders") is None

    await zalo_repository.enqueue_reminder(rid, "acc", "z", "nhắc")
    assert await db.reminder_delete(-7, rid)
    assert await pool.fetchval("SELECT count(*) FROM zalo_outbox") == 0
    assert await pool.fetchval("SELECT count(*) FROM reminders") == 0

    sid, _ = await _series(-7, channel="zalo")
    claimed = (await idempotency.claim_due_reminders())[0]
    await zalo_repository.enqueue_reminder(claimed.id, "acc", "z-user", "nhắc")
    assert await db.reminder_delete_series(-7, sid)
    assert await pool.fetchval("SELECT count(*) FROM zalo_outbox") == 0
    assert await pool.fetchval("SELECT count(*) FROM reminder_series") == 0


@pytest.mark.asyncio
async def test_xong_va_ban_tin_bo_lan_nhac_ngay_nho(delivery_db):
    now = datetime.now(timezone.utc)
    await db.add_reminder(42, "họp", now + timedelta(minutes=30))
    await _series(42, kind="ngay_nho", first=[(now + timedelta(minutes=40), "🗓 Hôm nay là giỗ")])
    rows = await db.list_reminders_between(42, now, now + timedelta(days=1))
    assert [message for _, message in rows] == ["họp"]

    rid = await delivery_db.fetchval("SELECT id FROM reminders WHERE message = 'họp'")
    await db.mark_reminder_sent(rid)
    fired = await db.reminder_last_fired(42, timedelta(minutes=30))
    assert fired["id"] == rid
    await db.reminder_mark_done(rid)
    assert await db.reminder_last_fired(42, timedelta(minutes=30)) is None


@pytest.mark.asyncio
async def test_zaloxoa_xoa_ca_chuoi_nhac(delivery_db):
    from channels import zalo_users

    member = await zalo_users.pair("z-member", "Member")
    await _series(member.internal_user_id, channel="zalo")
    await _series(999)
    assert await zalo_users.remove("z-member")
    owners = await delivery_db.fetch("SELECT telegram_user_id FROM reminder_series")
    assert [r["telegram_user_id"] for r in owners] == [999]
