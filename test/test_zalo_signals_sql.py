"""Tín hiệu cổ phiếu nhóm Zalo (migration 031) trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import test_delivery_sql as _delivery_sql
from channels import zalo_repository
from channels import zalo_signal_repository as repo

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db

NOW_MS = int(datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc).timestamp() * 1000)


async def _store(message_id, sender_id, text, *, group="g1", role="owner", quote="", minute=0):
    await zalo_repository.save_group_message(
        account_id="acc",
        group_id=group,
        message_id=message_id,
        sender_id=sender_id,
        sender_name=f"Tên {sender_id}",
        text=text,
        sent_at_ms=NOW_MS + minute * 60_000,
        quote_text=quote,
        sender_role=role,
    )


async def _settle(pool):
    await pool.execute("UPDATE zalo_group_messages SET created_at = now() - interval '1 minute'")


@pytest.mark.asyncio
async def test_che_do_quet_tin_moi_va_nguoi_theo_doi(delivery_db):
    pool = delivery_db
    await zalo_repository.add_group("acc", "g1", "room-vip")
    await zalo_repository.add_group("acc", "g2", "gia-dinh")
    assert await repo.list_groups("acc") == [("g2", "gia-dinh", "off"), ("g1", "room-vip", "off")]
    assert await repo.set_mode("acc", "room-vip", "admin") == 1
    with pytest.raises(ValueError):
        await repo.set_mode("acc", None, "lung-tung")

    await _store("m1", "boss", "HPG gom dần", quote="HPG thế nào?")
    await _store("m2", "mem", "ăn trưa chưa", role="member", minute=1)
    await _store("m3", "x", "nhóm tắt", group="g2", minute=2)
    assert await repo.settled_max_id("acc") == 0, "tin chưa đủ 3 giây chưa được quét"
    await _settle(pool)
    upto = await repo.settled_max_id("acc")
    rows = await repo.messages_between("acc", 0, upto, 50)
    assert [(r.message_id, r.sender_role, r.quote_text, r.mode) for r in rows] == [
        ("m1", "owner", "HPG thế nào?", "admin"),
        ("m2", "member", "", "admin"),
    ], "nhóm tắt tín hiệu không được đọc"
    assert len(await repo.messages_between("acc", 0, upto, 1)) == 1

    assert await repo.load_cursor("acc") is None
    await repo.save_cursor("acc", upto)
    assert await repo.load_cursor("acc") == upto

    await _store("m4", "mem", "thêm 1 tin", role="member", minute=3)
    senders = await repo.recent_senders("acc", "g1", days=3650)
    assert [(s[0], s[2], s[3]) for s in senders] == [("mem", "member", 2), ("boss", "owner", 1)]
    top_one = await repo.recent_senders("acc", "g1", days=3650, limit=1)
    assert [s[0] for s in top_one] == ["mem"], "nhắn nhiều trước"
    await repo.add_person("acc", "g1", "mem", "Tên mem")
    await repo.add_person("acc", "g1", "mem", "Tên mới")
    assert await repo.list_people("acc") == {"g1": {"mem": "Tên mới"}}
    assert await repo.remove_person("acc", "g1", "mem")
    assert not await repo.remove_person("acc", "g1", "mem")


@pytest.mark.asyncio
async def test_luu_tin_hieu_chong_trung_nhan_gui_nguyen_tu(delivery_db):
    await zalo_repository.add_group("acc", "g1", "room-vip")
    await zalo_repository.add_group("acc", "g2", "room-2")
    await _store("m1", "boss", "HPG gom dần")
    await _store("m2", "boss2", "HPG múc", group="g2")
    await _settle(delivery_db)
    rows = await repo.messages_between("acc", 0, 10**9, 50)
    assert rows == [], "chưa bật nhóm nào"
    await repo.set_mode("acc", None, "all")
    first, second = await repo.messages_between("acc", 0, 10**9, 50)

    values = dict(
        symbol="HPG",
        action="buy",
        kind="recommend",
        entry="26.5",
        target="",
        stop="",
        quote="HPG gom dần",
        signal_date=date(2026, 11, 25),
    )
    one = await repo.save_signal("acc", first, **values)
    assert one is not None
    assert await repo.save_signal("acc", first, **values) is None, "nhắc lại trong ngày"
    assert await repo.save_signal("acc", first, **{**values, "action": "sell"}) is not None
    assert await repo.save_signal("acc", second, **values) is not None

    crowd = await repo.consensus("acc", date(2026, 11, 25), ["HPG"])
    assert crowd == {("HPG", "buy"): (2, 2), ("HPG", "sell"): (1, 1)}
    top = await repo.top_symbols("acc", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert top[0][:4] == ("HPG", "buy", 2, 2)

    claimed = await repo.claim_pending("acc", "zoom", 6, 2)
    assert [s.id for s in claimed] == sorted(s.id for s in claimed) and len(claimed) == 2
    assert claimed[0].alias == "room-vip" and claimed[0].entry == "26.5"
    rest = await repo.claim_pending("acc", "zoom", 6, 10)
    assert len(rest) == 1, "đã nhận thì không nhận lại"
    assert await repo.claim_pending("acc", "zoom", 6, 10) == []
    assert len(await repo.claim_pending("acc", "zalo", 6, 10)) == 3, "mỗi kênh đánh dấu riêng"
    await repo.release([claimed[0].id], "zoom")
    assert [s.id for s in await repo.claim_pending("acc", "zoom", 6, 10)] == [claimed[0].id]

    found = await repo.signals_since("acc", datetime(2026, 1, 1, tzinfo=timezone.utc), symbol="HPG")
    assert len(found) == 3
    cutoff = datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc)
    before = await repo.signals_since("acc", datetime(2026, 1, 1, tzinfo=timezone.utc), end=cutoff)
    assert before == [], "end loại tín hiệu từ mốc đó trở đi (homqua)"
    assert (
        await repo.signals_since("acc", datetime(2026, 1, 1, tzinfo=timezone.utc), symbol="SSI")
        == []
    )

    # Xoá nhóm: tín hiệu và người theo dõi đi theo (ON DELETE CASCADE).
    await zalo_repository.remove_group("acc", "room-2")
    assert len(await repo.signals_since("acc", datetime(2026, 1, 1, tzinfo=timezone.utc))) == 2


@pytest.mark.asyncio
async def test_don_tin_hieu_cu(delivery_db, monkeypatch):
    await zalo_repository.add_group("acc", "g1", "room-vip")
    await repo.set_mode("acc", "g1", "admin")
    await _store("m1", "boss", "HPG gom dần")
    await _settle(delivery_db)
    (message,) = await repo.messages_between("acc", 0, 10**9, 5)
    old = message.__class__(
        **{**message.__dict__, "sent_at": message.sent_at - timedelta(days=400)}
    )
    await repo.save_signal(
        "acc",
        old,
        symbol="HPG",
        action="buy",
        kind="self",
        entry="",
        target="",
        stop="",
        quote="cũ",
        signal_date=date(2025, 10, 21),
    )
    monkeypatch.setenv("ZALO_SIGNAL_RETENTION_DAYS", "90")
    await repo.cleanup("acc")
    assert await repo.signals_since("acc", datetime(2000, 1, 1, tzinfo=timezone.utc)) == []
