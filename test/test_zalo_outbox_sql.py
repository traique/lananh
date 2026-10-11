"""Outbox Zalo thử lại có giới hạn (migration 012) trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng
một lần). Dùng chung fixture ``delivery_db`` của file đó.
"""

from datetime import datetime, timedelta, timezone

import pytest

import test_delivery_sql as _delivery_sql
from channels import zalo_repository
from core import database as db, idempotency

# Dùng lại fixture DB của test_delivery_sql.py (pytest tìm fixture theo tên trong module).
sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db


async def _ids(account="acc"):
    return [row["id"] for row in await zalo_repository.get_account_outbox(account)]


async def _expire_backoff(pool):
    await pool.execute(
        "UPDATE zalo_outbox SET next_attempt_at = now() - interval '1 second' "
        "WHERE next_attempt_at IS NOT NULL"
    )


async def _enqueue(pool, recipient, content):
    return await pool.fetchval(
        "INSERT INTO zalo_outbox (account_id, recipient_id, content) "
        "VALUES ('acc', $1, $2) RETURNING id",
        recipient,
        content,
    )


@pytest.mark.asyncio
async def test_failed_item_backs_off_then_gives_up_without_blocking_others(
    delivery_db, monkeypatch
):
    pool = delivery_db
    monkeypatch.setenv("ZALO_OUTBOX_MAX_ATTEMPTS", "3")
    poison = await _enqueue(pool, "blocked", "tin 1")
    other = await _enqueue(pool, "ok", "tin 2")
    assert await _ids() == [poison, other]

    first = await zalo_repository.mark_outbox_failed(poison, "user blocked bot", 0)
    assert first["attempts"] == 1 and first["failed"] is False
    # Đang backoff: tin lỗi bị bỏ qua, tin của người khác vẫn được lấy.
    assert await _ids() == [other]
    row = await pool.fetchrow(
        "SELECT last_error, EXTRACT(EPOCH FROM next_attempt_at - now())::float8 AS wait "
        "FROM zalo_outbox WHERE id = $1",
        poison,
    )
    assert row["last_error"] == "user blocked bot"
    assert 100 < row["wait"] <= 120

    await _expire_backoff(pool)
    second = await zalo_repository.mark_outbox_failed(poison, "user blocked bot", 0)
    assert second["attempts"] == 2
    wait = await pool.fetchval(
        "SELECT EXTRACT(EPOCH FROM next_attempt_at - now())::float8 FROM zalo_outbox WHERE id = $1",
        poison,
    )
    assert 200 < wait <= 240  # nhân đôi: 240s

    await _expire_backoff(pool)
    third = await zalo_repository.mark_outbox_failed(poison, "user blocked bot", 0)
    assert third["attempts"] == 3 and third["failed"] is True
    await _expire_backoff(pool)
    assert await _ids() == [other]
    # Đã bỏ cuộc: báo lỗi thêm không đổi gì.
    assert await zalo_repository.mark_outbox_failed(poison, "late", 0) is None
    assert await pool.fetchval("SELECT attempts FROM zalo_outbox WHERE id = $1", poison) == 3


@pytest.mark.asyncio
async def test_backoff_keeps_order_per_recipient_only(delivery_db):
    pool = delivery_db
    a1 = await _enqueue(pool, "a", "a1")
    b1 = await _enqueue(pool, "b", "b1")
    a2 = await _enqueue(pool, "a", "a2")
    await zalo_repository.mark_outbox_failed(a1, "rate limited", 1)
    # a2 không được chen lên trước phần còn lại của a1; b1 vẫn đi.
    assert await _ids() == [b1]
    await _expire_backoff(pool)
    rows = await zalo_repository.get_account_outbox("acc")
    assert [(r["id"], r["chunks_sent"]) for r in rows] == [(a1, 1), (b1, 0), (a2, 0)]


@pytest.mark.asyncio
async def test_chunks_sent_never_goes_backwards_and_ack_after_retry(delivery_db):
    pool = delivery_db
    item = await _enqueue(pool, "u", "tin dài")
    await zalo_repository.mark_outbox_failed(item, "e1", 3)
    await _expire_backoff(pool)
    await zalo_repository.mark_outbox_failed(item, "e2", 1)
    assert await pool.fetchval("SELECT chunks_sent FROM zalo_outbox WHERE id = $1", item) == 3
    await _expire_backoff(pool)
    await zalo_repository.mark_outbox_sent(item)
    assert await _ids() == []
    assert await zalo_repository.mark_outbox_failed(item, "after ack", 0) is None


@pytest.mark.asyncio
async def test_abandoned_reminder_is_closed_so_scheduler_stops_reclaiming(delivery_db, monkeypatch):
    pool = delivery_db
    monkeypatch.setenv("ZALO_OUTBOX_MAX_ATTEMPTS", "1")
    await db.add_reminder(
        -1,
        "nhắc",
        datetime.now(timezone.utc) - timedelta(minutes=1),
        channel="zalo",
        recipient_id="user",
        account_id="acc",
        event_key="zalo:acc:r",
    )
    claimed = await idempotency.claim_due_reminders()
    await zalo_repository.enqueue_reminder(claimed[0].id, "acc", "user", "⏰ nhắc")
    (item,) = await _ids()
    result = await zalo_repository.mark_outbox_failed(item, "blocked", 0)
    assert result["failed"] is True and result["reminder_id"] == claimed[0].id
    reminder = await pool.fetchrow("SELECT sent, claimed_at FROM reminders")
    assert reminder["sent"] is True and reminder["claimed_at"] is None
    assert await idempotency.claim_due_reminders(lease=timedelta(0)) == []


@pytest.mark.asyncio
async def test_retry_success_still_marks_reminder_sent(delivery_db):
    pool = delivery_db
    await db.add_reminder(
        -1,
        "nhắc",
        datetime.now(timezone.utc) - timedelta(minutes=1),
        channel="zalo",
        recipient_id="user",
        account_id="acc",
        event_key="zalo:acc:r2",
    )
    claimed = await idempotency.claim_due_reminders()
    await zalo_repository.enqueue_reminder(claimed[0].id, "acc", "user", "⏰ nhắc")
    (item,) = await _ids()
    await zalo_repository.mark_outbox_failed(item, "tạm lỗi", 0)
    assert await pool.fetchval("SELECT sent FROM reminders") is False
    await _expire_backoff(pool)
    assert await _ids() == [item]
    await zalo_repository.mark_outbox_sent(item)
    assert await pool.fetchval("SELECT sent FROM reminders") is True
