"""SQL hàng đợi thông báo gom (migrations/026, core/repositories/notify.py) và
services/notify_hub.py trên PostgreSQL thật: dedup, trần mỗi người nhận, lease,
xả Zalo trong 1 transaction với zalo_outbox, khởi động lại không mất tin.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

import datetime as dt
from types import SimpleNamespace

import pytest

import test_delivery_sql as _delivery_sql
from core import config
from core import database as db
from services import notify_hub as hub

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db
OWNER = 4242
VN = hub.VN_TZ
NOON = dt.datetime(2026, 10, 26, 12, 0, tzinfo=VN)


@pytest.fixture
def hub_on(monkeypatch, delivery_db):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", OWNER)
    monkeypatch.setattr(hub, "_started", True)
    monkeypatch.setattr(hub, "_pending_hint", set())
    sent: list[tuple[int, str]] = []

    async def telegram(uid, text):
        sent.append((uid, text))

    monkeypatch.setattr(hub, "_telegram_sender", telegram)
    return SimpleNamespace(pool=delivery_db, sent=sent)


@pytest.mark.asyncio
async def test_dedup_va_tran_moi_nguoi_nhan(hub_on):
    first = await db.notify_enqueue("telegram", OWNER, "normal", "system", "a", "k1", 3)
    assert first[0] is not None and first[1] == 0
    assert await db.notify_enqueue("telegram", OWNER, "normal", "system", "a2", "k1", 3) == (
        None,
        0,
    )
    await db.notify_enqueue("telegram", OWNER, "urgent", "facebook_review", "fb", None, 3)
    await db.notify_enqueue("telegram", OWNER, "normal", "system", "b", None, 3)
    # Tin thứ 4 vượt trần 3: bỏ tin NORMAL cũ nhất ("a"), giữ tin URGENT.
    _, dropped = await db.notify_enqueue("telegram", OWNER, "normal", "system", "c", None, 3)
    assert dropped == 1
    rows = await hub_on.pool.fetch(
        "SELECT content, status FROM notify_queue WHERE user_id = $1 ORDER BY id", OWNER
    )
    assert [(r["content"], r["status"]) for r in rows] == [
        ("a", "dropped"),
        ("fb", "pending"),
        ("b", "pending"),
        ("c", "pending"),
    ]
    # Người khác không bị ảnh hưởng bởi trần của chủ bot.
    assert (await db.notify_enqueue("zalo", -5, "normal", "followup", "x", None, 3))[0]
    recipients = {
        (r["channel"], r["user_id"]): r for r in await db.notify_pending_recipients(["x"])
    }
    assert recipients[("telegram", OWNER)]["has_priority"] is True  # có URGENT bị giữ
    assert recipients[("zalo", -5)]["has_priority"] is False


@pytest.mark.asyncio
async def test_lease_khong_cho_2_instance_gui_doi_va_tin_urgent_dung_truoc(hub_on):
    await db.notify_enqueue("telegram", OWNER, "normal", "system", "n1", None, 30)
    await db.notify_enqueue("telegram", OWNER, "urgent", "facebook_review", "u1", None, 30)
    claimed = await db.notify_claim("telegram", OWNER, 10, 300)
    assert [r["content"] for r in claimed] == ["u1", "n1"]
    assert await db.notify_claim("telegram", OWNER, 10, 300) == []  # đang có lease
    await db.notify_release([claimed[0]["id"]])
    again = await db.notify_claim("telegram", OWNER, 10, 300)
    assert [r["content"] for r in again] == ["u1"]
    await db.notify_mark_sent([r["id"] for r in claimed])
    assert await db.notify_count_pending(OWNER) == 0


@pytest.mark.asyncio
async def test_khoi_dong_lai_khong_mat_tin_va_gom_thanh_1_tin(hub_on, monkeypatch):
    monkeypatch.setenv("NOTIFY_QUIET_HOURS", "22:30-06:30")
    night = dt.datetime(2026, 10, 26, 23, 10, tzinfo=VN)
    assert await hub.offer("telegram", OWNER, "⚠️ RAM cao", source="system", now=night)
    assert await hub.offer(
        "telegram", OWNER, "📅 Lịch quyền FPT", source="portfolio_event", now=night
    )
    # Trong giờ yên lặng: chưa gửi gì.
    assert await hub.run_tick(night + dt.timedelta(hours=2)) == 0
    assert hub_on.sent == []
    # "Khởi động lại": RAM của hub mất sạch, chỉ còn DB.
    monkeypatch.setattr(hub, "_pending_hint", set())
    morning = dt.datetime(2026, 10, 27, 6, 35, tzinfo=VN)
    assert await hub.run_tick(morning) == 2
    assert len(hub_on.sent) == 1
    uid, text = hub_on.sent[0]
    assert uid == OWNER and "RAM cao" in text and "Lịch quyền FPT" in text
    assert await hub.run_tick(morning + dt.timedelta(minutes=5)) == 0  # không gửi lại


@pytest.mark.asyncio
async def test_xa_zalo_cung_transaction_voi_outbox(hub_on, monkeypatch):
    from channels import zalo_users

    monkeypatch.setenv("ZALO_ENABLED", "true")
    monkeypatch.setenv("ZALO_BRIDGE_SECRET", "s")
    member = await zalo_users.pair("z-chi", "Chị Lan", address_form="chị")

    async def account():
        return "acc"

    monkeypatch.setattr("channels.zalo_repository.resolve_active_account_id", account)
    uid = member.internal_user_id
    assert await hub.offer("zalo", uid, "Phỏng vấn sao rồi?", source="followup", now=NOON)
    assert await hub.offer("zalo", uid, "📅 Sự kiện quyền VCB", source="portfolio_event", now=NOON)
    assert await hub.flush_recipient("zalo", uid) == 2
    rows = await hub_on.pool.fetch("SELECT recipient_id, content FROM zalo_outbox")
    assert len(rows) == 1 and rows[0]["recipient_id"] == "z-chi"
    assert "cho chị" in rows[0]["content"] and "VCB" in rows[0]["content"]
    assert await hub.flush_recipient("zalo", uid) == 0  # không đưa vào outbox lần 2
    statuses = await hub_on.pool.fetch("SELECT DISTINCT status FROM notify_queue")
    assert [r["status"] for r in statuses] == ["sent"]
    # /zaloxoa xoá luôn hàng đợi của người đó.
    assert await zalo_users.remove("z-chi")
    assert await hub_on.pool.fetchval("SELECT count(*) FROM notify_queue") == 0
