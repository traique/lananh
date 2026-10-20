"""Opt-in integration tests. TEST_DATABASE_URL must point to a disposable PostgreSQL DB.

Each test creates and drops an isolated schema; never point it at the bot's live DB.
"""

import os
import secrets
from pathlib import Path
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

from core import database as db, webhook_inbox, idempotency
from channels import facebook_repository as facebook, zalo_repository
from stock import portfolio

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest_asyncio.fixture
async def sql_pool(monkeypatch):
    if os.getenv("TEST_PGLITE") == "1":
        # "test" trùng tên package chuẩn của Python -> import theo đường dẫn.
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent / "sql_support"))
        from pglite_pool import Pool

        pool = await Pool.create()
        try:
            yield pool
        finally:
            await pool.close()
        return
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to a disposable PostgreSQL database")
    schema = "test_delivery_" + secrets.token_hex(6)
    conn = await asyncpg.connect(url, statement_cache_size=0)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.close()
    pool = await asyncpg.create_pool(
        url, min_size=1, max_size=2, statement_cache_size=0, server_settings={"search_path": schema}
    )
    try:
        yield pool
    finally:
        await pool.close()
        conn = await asyncpg.connect(url, statement_cache_size=0)
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


async def migrate(pool, *, include_recovery=True):
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if not include_recovery and path.name.startswith("008"):
            continue
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(path.read_text())


@pytest_asyncio.fixture
async def delivery_db(sql_pool, monkeypatch):
    await migrate(sql_pool)
    monkeypatch.setattr(db, "get_pool", lambda: _pool(sql_pool))
    monkeypatch.setattr(db, "ensure_migrations", _no_migrations)
    return sql_pool


async def _pool(pool):
    return pool


async def _no_migrations(**kwargs):
    pass


@pytest.mark.asyncio
async def test_upgrade_preserves_dedup_and_quarantines_interrupted_posts(sql_pool):
    await migrate(sql_pool, include_recovery=False)
    await sql_pool.execute("INSERT INTO telegram_processed_updates VALUES (100, now())")
    await sql_pool.execute("INSERT INTO zoom_processed_events VALUES ('old', now())")
    await sql_pool.execute(
        "INSERT INTO zalo_users (external_id, internal_user_id) VALUES ('z-user', -9)"
    )
    await sql_pool.execute(
        "INSERT INTO reminders (telegram_user_id,message,due_at) VALUES (-9,'legacy',now())"
    )
    post = await sql_pool.fetchval(
        "INSERT INTO facebook_post_queue(account_id,group_id,sender_id,sender_name,source_message_ids,original_content,processed_content,status) VALUES ('a','g','s','n',ARRAY['m'],'x','x','POSTING') RETURNING id"
    )
    await sql_pool.execute((MIGRATIONS / "008_delivery_recovery.sql").read_text())
    assert await sql_pool.fetchval("SELECT count(*) FROM webhook_inbox WHERE status='DONE'") == 2
    assert (
        await sql_pool.fetchval("SELECT status FROM facebook_post_targets WHERE post_id=$1", post)
        == "UNKNOWN"
    )
    row = await sql_pool.fetchrow("SELECT channel,recipient_id FROM reminders")
    assert dict(row) == {"channel": "zalo", "recipient_id": "z-user"}


@pytest.mark.asyncio
async def test_inbox_duplicate_claim_and_expired_worker_recovery(delivery_db):
    pool = delivery_db
    assert await webhook_inbox.enqueue("telegram", "1", {"update_id": 1})
    assert not await webhook_inbox.enqueue("telegram", "1", {"update_id": 999})
    old = await webhook_inbox.claim()
    assert old["attempts"] == 1
    assert await webhook_inbox.claim() is None
    await pool.execute("UPDATE webhook_inbox SET lease_until=now()-interval '1 second'")
    recovered = await webhook_inbox.claim()
    assert recovered["lease_token"] != old["lease_token"] and recovered["attempts"] == 2
    await webhook_inbox.finish(old)  # stale owner must not change the new lease
    assert await pool.fetchval("SELECT status FROM webhook_inbox") == "PROCESSING"
    await webhook_inbox.finish(recovered)
    row = await pool.fetchrow("SELECT status,payload FROM webhook_inbox")
    assert row["status"] == "DONE" and row["payload"] is None
    assert await webhook_inbox.claim() is None


@pytest.mark.asyncio
async def test_inbox_retry_backoff_retains_payload(delivery_db):
    pool = delivery_db
    await webhook_inbox.enqueue("zoom", "z1", {"message": "test"})
    row = await webhook_inbox.claim()
    await webhook_inbox.retry(row, RuntimeError("private failure details"))
    stored = await pool.fetchrow("SELECT * FROM webhook_inbox")
    assert stored["status"] == "PENDING" and stored["payload"] is not None
    assert stored["last_error"] == "RuntimeError" and stored["lease_token"] is None
    assert await webhook_inbox.claim() is None
    await pool.execute("UPDATE webhook_inbox SET available_at=now()-interval '1 second'")
    assert (await webhook_inbox.claim())["attempts"] == 2


@pytest.mark.asyncio
async def test_reminder_event_retry_keeps_original_due_and_gateway_ack_marks_sent(delivery_db):
    pool = delivery_db
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    first = await db.add_reminder(
        -1,
        "test",
        due,
        channel="zalo",
        recipient_id="user",
        account_id="account",
        event_key="zalo:a:m",
    )
    again = await db.add_reminder(
        -1,
        "test",
        due + timedelta(hours=1),
        channel="zalo",
        recipient_id="user",
        account_id="account",
        event_key="zalo:a:m",
    )
    assert first == again
    claimed = await idempotency.claim_due_reminders()
    assert len(claimed) == 1 and claimed[0].channel == "zalo"
    reminder_id = claimed[0].id
    await zalo_repository.enqueue_reminder(reminder_id, "account", "user", "test")
    await zalo_repository.enqueue_reminder(reminder_id, "account", "user", "test")
    outbox = await zalo_repository.get_account_outbox("account")
    assert len(outbox) == 1 and outbox[0]["recipient_id"] == "user"
    assert not await pool.fetchval("SELECT sent FROM reminders")
    await zalo_repository.mark_outbox_sent(outbox[0]["id"])
    assert await pool.fetchval("SELECT sent FROM reminders")
    assert not await zalo_repository.get_account_outbox("account")
    await db.add_note(-1, "note", event_key="zalo:a:m")
    await db.add_note(-1, "note", event_key="zalo:a:m")
    assert await pool.fetchval("SELECT count(*) FROM notes") == 1


async def queue_post(pool):
    await facebook.add_group("a", "g", "group")
    return await facebook.create_post(
        account_id="a",
        group_id="g",
        sender_id="s",
        sender_name="n",
        source_message_ids=["m"],
        content="hello",
        media=[],
    )


@pytest.mark.asyncio
async def test_facebook_expired_claim_does_not_repeat_unknown_creation(delivery_db):
    pool = delivery_db
    post_id = await queue_post(pool)
    await facebook.ensure_targets(post_id, ["default", "2"])
    first = await facebook.claim_post("a", post_id)
    token = first["claim_token"]
    assert await facebook.claim_post("a", post_id) is None
    await facebook.mark_target_creating(post_id, "default", token)
    await pool.execute(
        "UPDATE facebook_post_queue SET lease_until=now()-interval '1 second' WHERE id=$1", post_id
    )
    second = await facebook.claim_post("a", post_id)
    states = {row["page_key"]: row["status"] for row in await facebook.list_targets(post_id)}
    assert states == {"default": "UNKNOWN", "2": "PENDING"}
    with pytest.raises(RuntimeError):
        await facebook.record_target_posted(
            post_id, "default", "old-owner-post", None, claim_token=token
        )
    await facebook.release_post_claim("a", post_id, token, "stale")
    assert (await facebook.get_post("a", post_id))["claim_token"] == second["claim_token"]
    await facebook.record_target_posted(
        post_id, "default", "verified-post", None, claim_token=second["claim_token"]
    )
    await facebook.record_target_error(
        post_id, "default", "later read error", claim_token=second["claim_token"]
    )
    assert (await facebook.list_targets(post_id))[1]["facebook_post_id"] == "verified-post"
    assert (
        await facebook.finalize_post_status("a", post_id, claim_token=second["claim_token"])
        == "ERROR"
    )
    third = await facebook.claim_post("a", post_id)
    await facebook.mark_target_creating(post_id, "2", third["claim_token"])
    await facebook.record_target_posted(
        post_id, "2", "page2-post", None, claim_token=third["claim_token"]
    )
    assert (
        await facebook.finalize_post_status("a", post_id, claim_token=third["claim_token"])
        == "POSTED"
    )


@pytest.mark.asyncio
async def test_full_sale_and_delete_record_closed_position_without_affecting_other_users(
    delivery_db,
):
    await portfolio.set_holding(1, "VCB", 100, 80000)
    await portfolio.set_holding(2, "VCB", 200, 80000)
    await portfolio.sell(1, "VCB", 50)
    assert not await portfolio.was_closed(1, "VCB")
    await portfolio.sell(1, "VCB")
    assert await portfolio.was_closed(1, "VCB")
    assert await portfolio.get_holding(1, "VCB") is None
    assert (await portfolio.get_holding(2, "VCB")).quantity == 200
    assert await portfolio.delete_holding(2, "VCB")
    assert await portfolio.was_closed(2, "VCB")


@pytest.mark.asyncio
async def test_legacy_shopee_cache_requires_confirmation_and_group_post_retries_deduplicate(
    delivery_db,
):
    pool = delivery_db
    source = "https://shopee.vn/product/1/2"
    await pool.execute(
        "INSERT INTO shopee_affiliate_links(account_id,source_url,affiliate_url,canonical_key) VALUES ('a',$1,'https://s.shopee.vn/old','item:1:2')",
        source,
    )
    assert not await facebook.get_affiliate_links("a", [source])
    await facebook.set_affiliate_link("a", source, "https://s.shopee.vn/new")
    assert (await facebook.get_affiliate_links("a", [source]))[source].endswith("new")
    first = await queue_post(pool)
    assert await queue_post(pool) == first
    assert await pool.fetchval("SELECT count(*) FROM facebook_post_queue") == 1


@pytest.mark.asyncio
async def test_migration_drops_stored_shopee_session_and_keeps_other_settings(delivery_db):
    pool = delivery_db
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES ('shopee:affiliate:storage_state:v1','secret'),"
        "('other','kept')"
    )
    await pool.execute((MIGRATIONS / "009_drop_shopee_session.sql").read_text())
    assert await pool.fetchval("SELECT count(*) FROM settings WHERE key LIKE 'shopee:%'") == 0
    assert await pool.fetchval("SELECT value FROM settings WHERE key='other'") == "kept"


async def _queue(text, msg_id, *, media=None, fingerprint=True):
    from services import facebook_dedup

    return await facebook.create_post(
        account_id="a",
        group_id="g",
        sender_id="s",
        sender_name="n",
        source_message_ids=[msg_id],
        content=text,
        media=media or [],
        fingerprint=facebook_dedup.build_fingerprint(text) if fingerprint else None,
    )


@pytest.mark.asyncio
async def test_duplicate_posts_never_enter_queue_and_retries_still_resolve(delivery_db):
    pool = delivery_db
    await facebook.add_group("a", "g", "group")
    first = await _queue("Áo khoác dù chống nắng 159k https://s.shopee.vn/AbC", "m1")
    # Gateway gửi lại đúng sự kiện cũ -> trả bài cũ, không bị coi là trùng.
    assert await _queue("Áo khoác dù chống nắng 159k https://s.shopee.vn/AbC", "m1") == first
    with pytest.raises(facebook.DuplicatePostError) as exc:
        await _queue("Caption khác hẳn nhưng cùng link https://s.shopee.vn/AbC", "m2")
    assert exc.value.match.post_id == first
    assert await pool.fetchval("SELECT count(*) FROM facebook_post_queue") == 1
    assert await pool.fetchval("SELECT count(*) FROM facebook_post_fingerprints") == 1


@pytest.mark.asyncio
async def test_pending_cap_deletes_oldest_pending_only(delivery_db):
    pool = delivery_db
    await facebook.add_group("a", "g", "group")
    ids = [
        await _queue(f"Sản phẩm số {i} rất đẹp giá {100 + i}k https://s.shopee.vn/p{i}", f"m{i}")
        for i in range(6)
    ]
    # Bài lỗi (đã có page đăng) không bao giờ bị xoá tự động.
    await pool.execute("UPDATE facebook_post_queue SET status = 'ERROR' WHERE id = $1", ids[0])
    await pool.execute(
        "UPDATE facebook_post_queue SET created_at = now() - make_interval(mins => 10 - id::int)"
    )
    pruned = await facebook.enforce_pending_cap("a", limit=3)
    assert pruned == ids[1:3]
    remaining = [
        r["id"] for r in await pool.fetch("SELECT id FROM facebook_post_queue ORDER BY id")
    ]
    assert remaining == [ids[0], *ids[3:]]
    # Dấu vân tay của bài bị xoá vẫn chặn bài đăng lại.
    with pytest.raises(facebook.DuplicatePostError):
        await _queue("Sản phẩm số 1 rất đẹp giá 101k https://s.shopee.vn/p1", "again")


@pytest.mark.asyncio
async def test_media_is_dropped_after_reject_and_history_is_pruned(delivery_db):
    pool = delivery_db
    await facebook.add_group("a", "g", "group")
    post_id = await _queue(
        "Bài có ảnh để bỏ qua nha mọi người", "m1", media=[("image/jpeg", b"x" * 10)]
    )
    assert await pool.fetchval("SELECT count(*) FROM facebook_post_media") == 1
    assert await facebook.reject_post("a", post_id)
    assert await pool.fetchval("SELECT count(*) FROM facebook_post_media") == 0
    await pool.execute(
        "UPDATE facebook_post_queue SET created_at = now() - interval '40 days' WHERE id = $1",
        post_id,
    )
    await pool.execute(
        "UPDATE facebook_post_fingerprints SET created_at = now() - interval '40 days'"
    )
    result = await facebook.prune_history(force=True)
    assert result["posts"] == 1 and result["fingerprints"] == 1
    assert await pool.fetchval("SELECT count(*) FROM facebook_post_queue") == 0


@pytest.mark.asyncio
async def test_reset_clears_fingerprints_so_ids_can_restart(delivery_db):
    pool = delivery_db
    await facebook.add_group("a", "g", "group")
    await _queue("Một bài để reset nha mọi người ơi", "m1")
    await facebook.reset_posts("a")
    assert await pool.fetchval("SELECT count(*) FROM facebook_post_fingerprints") == 0
    assert await _queue("Một bài để reset nha mọi người ơi", "m2") == 1


@pytest.mark.asyncio
async def test_vacuum_full_reclaims_space_of_deleted_media(delivery_db):
    from services import db_maintenance

    await facebook.add_group("a", "g", "group")
    post_id = await _queue(
        "Bài nhiều ảnh để thử thu hồi dung lượng",
        "m1",
        media=[("image/jpeg", bytes(range(256)) * 4000) for _ in range(5)],
    )
    await facebook.reject_post("a", post_id)  # xoá ảnh
    usage = await db_maintenance.measure()
    assert usage.live_bytes == 0 and usage.posting == 0
    before, after = await db_maintenance.vacuum_full()
    assert after <= before


@pytest.mark.asyncio
async def test_list_pending_for_refilter_returns_media_count(delivery_db):
    await facebook.add_group("a", "g", "group")
    with_photo = await _queue("Bài có ảnh nha mọi người ơi", "m1", media=[("image/jpeg", b"x")])
    without = await _queue("Bài không có ảnh nha mọi người", "m2")
    rows = await facebook.list_pending_for_refilter("a")
    assert {(r["id"], int(r["media_count"])) for r in rows} == {(with_photo, 1), (without, 0)}


# ─── Runtime reliability (migration 013, DEAD inbox, /zaloxoa, retention) ───


@pytest.mark.asyncio
async def test_inbox_exhausted_event_goes_dead_once_and_is_never_claimed(delivery_db, monkeypatch):
    pool = delivery_db
    sent = []
    monkeypatch.setattr(webhook_inbox.alerts, "send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(webhook_inbox.config, "WEBHOOK_MAX_ATTEMPTS", 2)
    await webhook_inbox.enqueue("telegram", "poison", {"update_id": 7})
    first = await webhook_inbox.claim()
    await webhook_inbox.retry(first, RuntimeError("bug"))
    await pool.execute("UPDATE webhook_inbox SET available_at=now()-interval '1 second'")
    second = await webhook_inbox.claim()
    assert second["attempts"] == 2
    await webhook_inbox.retry(second, RuntimeError("bug"))
    row = await pool.fetchrow("SELECT status, payload, completed_at FROM webhook_inbox")
    assert row["status"] == "DEAD" and row["payload"] is not None and row["completed_at"]
    assert len(sent) == 1
    await pool.execute("UPDATE webhook_inbox SET available_at=now()-interval '1 second'")
    assert await webhook_inbox.claim() is None
    assert await webhook_inbox.reap_exhausted() == 0 and len(sent) == 1


@pytest.mark.asyncio
async def test_inbox_reaps_event_whose_last_attempt_crashed(delivery_db, monkeypatch):
    pool = delivery_db
    sent = []
    monkeypatch.setattr(webhook_inbox.alerts, "send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(webhook_inbox.config, "WEBHOOK_MAX_ATTEMPTS", 1)
    await webhook_inbox.enqueue("zoom", "crash", {"x": 1})
    assert (await webhook_inbox.claim())["attempts"] == 1
    # Process chết giữa chừng: không ai gọi retry(); lease hết hạn.
    await pool.execute("UPDATE webhook_inbox SET lease_until=now()-interval '1 second'")
    assert await webhook_inbox.claim() is None
    assert await webhook_inbox.reap_exhausted() == 1
    assert await pool.fetchval("SELECT status FROM webhook_inbox") == "DEAD" and len(sent) == 1


@pytest.mark.asyncio
async def test_dead_rows_are_kept_then_pruned(delivery_db, monkeypatch):
    pool = delivery_db
    monkeypatch.setattr(idempotency, "ensure_schema", _no_migrations)
    await pool.execute(
        "INSERT INTO webhook_inbox (channel,event_id,status,completed_at) VALUES "
        "('telegram','old','DEAD',now()-interval '30 days'),"
        "('telegram','new','DEAD',now()-interval '3 days')"
    )
    await idempotency.cleanup_expired()
    rows = await pool.fetch("SELECT event_id FROM webhook_inbox ORDER BY event_id")
    assert [r["event_id"] for r in rows] == ["new"]


@pytest.mark.asyncio
async def test_zaloxoa_removes_member_data_but_not_owner_data(delivery_db):
    from channels import zalo_users

    pool = delivery_db
    member = await zalo_users.pair("z-member", "Member")
    uid = member.internal_user_id
    for owner_or_member in (uid, 999):
        await db.add_chat_message(owner_or_member, "user", "xin chào")
        await db.upsert_fact(owner_or_member, "ten", "A")
        await db.set_summary(owner_or_member, "tóm tắt")
        await db.add_note(owner_or_member, "ghi chú")
        await portfolio.set_holding(owner_or_member, "VCB", 10, 80000)
    reminder = await db.add_reminder(
        uid,
        "nhắc",
        datetime.now(timezone.utc),
        channel="zalo",
        recipient_id="z-member",
        account_id="acc",
        event_key="zalo:acc:r1",
    )
    reminder_id = await pool.fetchval("SELECT id FROM reminders WHERE telegram_user_id=$1", uid)
    assert reminder is not None
    await zalo_repository.enqueue_reminder(reminder_id, "acc", "z-member", "nhắc")
    await db.set_setting(f"memory_enabled_{uid}", "0")

    assert await zalo_users.remove("z-member") is True
    for table in (
        "chat_messages",
        "user_facts",
        "user_memory_summary",
        "notes",
        "stock_holdings",
        "reminders",
    ):
        assert (
            await pool.fetchval(f"SELECT count(*) FROM {table} WHERE telegram_user_id=$1", uid) == 0
        )
        assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE telegram_user_id=999") == (
            0 if table == "reminders" else 1
        )
    assert await pool.fetchval("SELECT count(*) FROM zalo_outbox WHERE sent_at IS NULL") == 0
    assert await db.get_setting(f"memory_enabled_{uid}") is None
    assert await zalo_users.resolve("z-member") is None


@pytest.mark.asyncio
async def test_retention_sql_deletes_only_old_sent_rows(delivery_db, monkeypatch):
    from services import db_maintenance

    pool = delivery_db
    await pool.execute(
        "INSERT INTO user_calls (channel,telegram_user_id,created_at) VALUES "
        "('telegram',1,now()-interval '400 days'),('telegram',1,now())"
    )
    await pool.execute(
        "INSERT INTO provider_calls (provider,model,created_at) VALUES "
        "('groq','m',now()-interval '400 days'),('groq','m',now())"
    )
    await pool.execute(
        "INSERT INTO zalo_outbox (account_id,recipient_id,content,created_at,sent_at) VALUES "
        "('a','r','old sent',now()-interval '90 days',now()-interval '90 days'),"
        "('a','r','old unsent',now()-interval '90 days',NULL),"
        "('a','r','new sent',now(),now())"
    )
    await pool.execute(
        "INSERT INTO reminders (telegram_user_id,message,due_at,sent,recipient_id) VALUES "
        "(1,'old sent',now()-interval '200 days',true,'1'),"
        "(1,'old unsent',now()-interval '200 days',false,'1')"
    )
    result = await db_maintenance.prune_retention()
    assert result == {
        "user_calls": 1,
        "provider_calls": 1,
        "zalo_outbox": 1,
        "zalo_outbox_failed": 0,
        "reminders": 1,
    }
    assert [
        r["content"] for r in await pool.fetch("SELECT content FROM zalo_outbox ORDER BY id")
    ] == [
        "old unsent",
        "new sent",
    ]
    assert await pool.fetchval("SELECT message FROM reminders") == "old unsent"


@pytest.mark.asyncio
async def test_daily_summary_toggle_per_group_and_all(delivery_db):
    await zalo_repository.add_group("a", "g1", "sale")
    await zalo_repository.add_group("a", "g2", "vip")
    await zalo_repository.add_group("b", "g9", "khac")
    assert await zalo_repository.list_groups_with_daily("a") == [
        ("g1", "sale", True),
        ("g2", "vip", True),
    ]

    assert await zalo_repository.set_daily_summary("a", "SALE", False) == 1
    assert await zalo_repository.set_daily_summary("a", "khong-co", False) == 0
    assert await zalo_repository.list_groups_with_daily("a") == [
        ("g1", "sale", False),
        ("g2", "vip", True),
    ]

    assert await zalo_repository.set_daily_summary("a", None, False) == 2
    assert [daily for *_, daily in await zalo_repository.list_groups_with_daily("a")] == [
        False,
        False,
    ]
    assert await zalo_repository.list_groups_with_daily("b") == [("g9", "khac", True)]
