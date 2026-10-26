"""Trí nhớ v2 (migrations/017, 018 + core/repositories/memory.py) trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng
một lần). Dùng chung fixture ``delivery_db`` của file đó.
"""

import datetime as dt

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db

UID = 777


async def _current(uid=UID):
    return {row["key"]: row for row in await db.get_fact_rows(uid)}


@pytest.mark.asyncio
async def test_save_fact_giu_gia_tri_cu_va_bao_ve_fact_nguoi_dung(delivery_db):
    assert await db.save_fact(UID, "cong_viec", "PM ở A", importance=3) == "added"
    assert await db.save_fact(UID, "cong_viec", "PM ở A") == "unchanged"
    assert await db.save_fact(UID, "cong_viec", "PM ở B") == "updated"
    current = await _current()
    assert current["cong_viec"]["value"] == "PM ở B"
    old = await db.get_superseded_facts(UID)
    assert [row["value"] for row in old] == ["PM ở A"]
    # Bản cũ trỏ sang bản mới; chỉ 1 dòng hiện hành / key (unique index từng phần).
    assert old[0]["id"] != current["cong_viec"]["id"]
    superseded_by = await delivery_db.fetchval(
        "SELECT superseded_by FROM user_facts WHERE id = $1", old[0]["id"]
    )
    assert superseded_by == current["cong_viec"]["id"]

    # Fact do người dùng tự nhập (/nho): bộ trích xuất không được ghi đè/vô hiệu hoá.
    assert await db.save_fact(UID, "ten", "Trai", source="user", pinned=True) == "added"
    assert await db.save_fact(UID, "ten", "Tên khác") == "protected"
    ten_id = (await _current())["ten"]["id"]
    assert await db.invalidate_fact(UID, ten_id) is None
    assert (await _current())["ten"]["value"] == "Trai"
    # Người dùng tự sửa thì được.
    assert await db.save_fact(UID, "ten", "Trai Nguyễn", source="user", protect=False) == "updated"
    assert (await _current())["ten"]["pinned"] is True


@pytest.mark.asyncio
async def test_forget_xoa_han_moi_phien_ban_va_invalidate_giu_lich_su(delivery_db):
    await db.save_fact(UID, "que", "Huế")
    await db.save_fact(UID, "que", "Đà Nẵng")
    fact_id = (await _current())["que"]["id"]
    assert await db.invalidate_fact(UID, fact_id) == "que"
    assert "que" not in await _current()
    assert len(await db.get_superseded_facts(UID)) == 2
    await db.delete_fact(UID, "que")
    assert await db.get_superseded_facts(UID) == []


@pytest.mark.asyncio
async def test_trim_giu_fact_ghim_va_quan_trong_thay_vi_moi_nhat(delivery_db):
    await db.save_fact(UID, "ngay_sinh", "15/3/1990", importance=3)
    await db.save_fact(UID, "ghim", "x", pinned=True, importance=1)
    for i in range(5):
        await db.save_fact(UID, f"chuyen_vat_{i}", "y", importance=1)
    await db.trim_facts(UID, 3)
    keys = set(await _current())
    assert {"ngay_sinh", "ghim"} <= keys
    assert len(keys) == 3
    # Giá trị cũ có trần theo key.
    for i in range(6):
        await db.save_fact(UID, "ngay_sinh", f"v{i}", importance=3)
    await db.trim_facts(UID, 3, superseded_per_key=2, superseded_total=10)
    old = [r for r in await db.get_superseded_facts(UID, 50) if r["key"] == "ngay_sinh"]
    assert [r["value"] for r in old] == ["v4", "v3"]


@pytest.mark.asyncio
async def test_lich_su_chat_kind_va_cua_so_trich_nho(delivery_db):
    await db.add_chat_turn(UID, "phân tích FPT", "📊 FPT ...", kind="stock")
    await db.add_chat_turn(UID, "sao em bảo giữ", "Vì hệ thống ...")
    rows = await db.get_chat_rows_after(UID, 0, max_chars=5)
    assert [(r["role"], r["kind"]) for r in rows] == [
        ("user", "stock"),
        ("model", "stock"),
        ("user", "chat"),
        ("model", "chat"),
    ]
    assert rows[1]["content"] == "📊 FPT"
    after = await db.get_chat_rows_after(UID, rows[1]["id"])
    assert [r["content"] for r in after] == ["sao em bảo giữ", "Vì hệ thống ..."]
    last = await db.get_last_model_row(UID)
    assert last["kind"] == "chat"
    recent = await db.get_recent_chat_rows(UID, 2)
    assert [r["role"] for r in recent] == ["model", "user"]


@pytest.mark.asyncio
async def test_ghi_chu_phien_upsert_giu_truong_cu_va_cat_tran(delivery_db, monkeypatch):
    start = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    await db.upsert_session_note(UID, 10, title="Bàn FPT", bullets="- a", started_at=start)
    await db.upsert_session_note(UID, 10, summary="Tóm tắt phiên", started_at=start)
    note = await db.get_session_note(UID, 10)
    assert (note["title"], note["bullets"], note["summary"]) == ("Bàn FPT", "- a", "Tóm tắt phiên")
    monkeypatch.setattr(db, "SESSION_NOTES_RETENTION", 2)
    await db.upsert_session_note(UID, 20, title="Phiên 2", started_at=start)
    await db.upsert_session_note(UID, 30, title="Phiên 3", started_at=start)
    notes = await db.get_session_notes(UID, 10)
    assert [n["session_key"] for n in notes] == [30, 20]
    others = await db.get_session_notes(UID, 10, exclude_session_key=30)
    assert [n["session_key"] for n in others] == [20]


@pytest.mark.asyncio
async def test_ghi_chu_tim_va_xoa(delivery_db):
    await db.add_note(UID, "size giày của vợ là 38")
    await db.add_note(UID, "mua sữa")
    notes = await db.get_recent_notes(UID)
    assert [n["content"] for n in notes] == ["mua sữa", "size giày của vợ là 38"]
    assert await db.delete_note(UID, notes[0]["id"]) is True
    assert await db.delete_note(UID + 1, notes[1]["id"]) is False  # không xoá ghi chú người khác
    assert [n["content"] for n in await db.get_recent_notes(UID)] == ["size giày của vợ là 38"]
    assert await db.clear_notes(UID) == 1


@pytest.mark.asyncio
async def test_su_kien_hoi_tham_claim_release_va_het_han(delivery_db):
    today = dt.date(2026, 10, 26)
    assert await db.add_user_event(UID, "phỏng vấn", "mai", today) is True
    assert await db.add_user_event(UID, "phỏng vấn", "mai", today) is False  # trùng
    await db.add_user_event(UID, "đi khám", "thứ 6", today + dt.timedelta(days=4))
    await db.add_user_event(-5, "họp lớp", "hôm nay", today)
    assert set(await db.users_with_due_events(today)) == {UID, -5}

    claimed = await db.claim_due_user_event(UID, today)
    assert claimed["title"] == "phỏng vấn"
    assert await db.claim_due_user_event(UID, today) is None  # sự kiện sau chưa tới ngày
    await db.release_user_event(claimed["id"])
    again = await db.claim_due_user_event(UID, today)
    assert again["id"] == claimed["id"]
    await db.mark_user_event_asked(again["id"])
    await db.release_user_event(again["id"])  # đã gửi -> không trả về pending nữa
    assert await db.claim_due_user_event(UID, today) is None

    assert await db.expire_user_events(today + dt.timedelta(days=1)) == 1  # của -5
    assert await db.finish_user_events(UID) == 1  # "đi khám"
    statuses = {row["title"]: row["status"] for row in await db.list_user_events(UID)}
    assert statuses == {"phỏng vấn": "asked", "đi khám": "done"}


@pytest.mark.asyncio
async def test_ghi_de_ghi_chu_phien_va_xong_su_kien_theo_id(delivery_db):
    """/quen 1 điều: ghi đè (kể cả rỗng) ghi chú phiên không đổi thứ tự, cả 3 trường
    rỗng thì xoá; chỉ sự kiện còn chờ của chính người đó trong danh sách id -> done."""
    start = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    await db.upsert_session_note(
        UID, 10, title="Sức khoẻ", bullets="- tiểu đường\n- bơi", summary="A. B.", started_at=start
    )
    await db.upsert_session_note(UID, 20, title="Phiên 2", started_at=start)
    await db.rewrite_session_note(UID, 10, title="Sức khoẻ", bullets="- bơi", summary="")
    note = await db.get_session_note(UID, 10)
    assert (note["title"], note["bullets"], note["summary"]) == ("Sức khoẻ", "- bơi", "")
    assert [n["session_key"] for n in await db.get_session_notes(UID, 10)] == [20, 10]
    await db.rewrite_session_note(UID, 20, title="", bullets="", summary="")
    assert await db.get_session_note(UID, 20) is None

    today = dt.date(2026, 10, 26)
    await db.add_user_event(UID, "tái khám tiểu đường", "mai", today)
    await db.add_user_event(UID, "phỏng vấn", "mai", today)
    await db.add_user_event(-5, "tái khám tiểu đường", "mai", today)
    events = {e["title"]: e["id"] for e in await db.list_user_events(UID)}
    other = (await db.list_user_events(-5))[0]["id"]
    assert await db.finish_user_event_ids(UID, [events["tái khám tiểu đường"], other]) == 1
    assert await db.finish_user_event_ids(UID, [events["tái khám tiểu đường"]]) == 0
    assert await db.finish_user_event_ids(UID, []) == 0
    statuses = {e["title"]: e["status"] for e in await db.list_user_events(UID)}
    assert statuses == {"tái khám tiểu đường": "done", "phỏng vấn": "pending"}
    assert (await db.list_user_events(-5))[0]["status"] == "pending"
