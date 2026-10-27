"""SQL việc nền + tác vụ định kỳ (migrations/027, core/repositories/jobs.py) trên
PostgreSQL thật: trần hàng đợi, nhận việc không trùng giữa 2 instance, huỷ, ghi kết
quả chỉ khi còn running, khởi động lại (chạy lại 1 lần rồi failed), nhận lần chạy
tác vụ không đôi, xoá dữ liệu người dùng Zalo.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

import datetime as dt

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db
OWNER = 4242
VN = dt.timezone(dt.timedelta(hours=7))


def _ts(value) -> dt.datetime:
    """PGlite trả timestamptz trong RETURNING dạng chuỗi ISO; asyncpg trả datetime."""
    if isinstance(value, str):
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


@pytest.mark.asyncio
async def test_tran_hang_doi_va_nhan_viec_khong_trung(delivery_db):
    assert await db.bg_job_insert(OWNER, "telegram", "a", user_cap=2, global_cap=10) == (1, "")
    assert (await db.bg_job_insert(OWNER, "telegram", "b", user_cap=2, global_cap=10))[0] == 2
    assert await db.bg_job_insert(OWNER, "telegram", "c", user_cap=2, global_cap=10) == (
        None,
        "user_cap",
    )
    assert await db.bg_job_insert(-5, "zalo", "z", user_cap=2, global_cap=2) == (None, "global_cap")
    first = await db.bg_job_claim(120)
    second = await db.bg_job_claim(120)
    assert (first["id"], second["id"]) == (1, 2)  # 2 instance nhận 2 việc khác nhau
    assert first["status"] == "running" and first["attempts"] == 1
    assert await db.bg_job_claim(120) is None
    assert await db.bg_job_position(2) == 1
    assert [r["id"] for r in await db.bg_job_active(OWNER)] == [1, 2]


@pytest.mark.asyncio
async def test_huy_va_ghi_ket_qua_chi_khi_con_running(delivery_db):
    await db.bg_job_insert(OWNER, "telegram", "a", user_cap=3, global_cap=10)
    await db.bg_job_claim(120)
    assert await db.bg_job_cancel(-5, 1) is None  # việc của người khác
    assert await db.bg_job_cancel(OWNER, 1) == "running"
    assert await db.bg_job_renew(1, 120) is False  # nhịp gia hạn thấy đã huỷ
    finished = await db.bg_job_finish(
        1, "succeeded", result="x", error=None, no_reply=False, tokens=10, provider="groq"
    )
    assert finished is False
    row = await db.bg_job_get(OWNER, 1)
    assert row["status"] == "cancelled" and row["result"] is None
    assert await db.bg_job_cancel(OWNER, 1) is None

    await db.bg_job_insert(OWNER, "telegram", "b", user_cap=3, global_cap=10)
    await db.bg_job_claim(120)
    assert await db.bg_job_renew(2, 120)
    assert await db.bg_job_finish(
        2, "succeeded", result="kết quả", error=None, no_reply=False, tokens=42, provider="groq"
    )
    row = await db.bg_job_get(OWNER, 2)
    assert (row["status"], row["result"], row["tokens"]) == ("succeeded", "kết quả", 42)
    assert [r["id"] for r in await db.bg_job_list(OWNER)] == [2, 1]
    assert await db.bg_job_get(-5, 2) is None


@pytest.mark.asyncio
async def test_khoi_dong_lai_chay_lai_1_lan_roi_failed(delivery_db):
    await db.bg_job_insert(OWNER, "telegram", "a", user_cap=3, global_cap=10)
    await db.bg_job_claim(120)
    # Process chết: lease hết hạn.
    await delivery_db.execute("UPDATE bg_jobs SET lease_until = now() - interval '1 second'")
    assert await db.bg_job_recover(2) == []  # lần 1: đưa về hàng đợi
    again = await db.bg_job_claim(120)
    assert again["id"] == 1 and again["attempts"] == 2
    await delivery_db.execute("UPDATE bg_jobs SET lease_until = now() - interval '1 second'")
    failed = await db.bg_job_recover(2)
    assert [(r["id"], r["status"], r["error"]) for r in failed] == [(1, "failed", "interrupted")]
    assert await db.bg_job_claim(120) is None
    # Việc còn lease (instance khác đang chạy lúc deploy chồng) không bị đụng.
    await db.bg_job_insert(OWNER, "telegram", "b", user_cap=3, global_cap=10)
    await db.bg_job_claim(120)
    assert await db.bg_job_recover(2) == []
    assert (await db.bg_job_get(OWNER, 2))["status"] == "running"


@pytest.mark.asyncio
async def test_nhan_lan_chay_tac_vu_khong_doi(delivery_db):
    first_run = dt.datetime(2026, 10, 27, 8, 0, tzinfo=VN)
    rid = await db.routine_insert(
        OWNER, "telegram", "tóm tắt tin VNM", {"type": "daily", "at": "08:00"}, first_run
    )
    assert await db.routine_count(OWNER) == 1
    now = first_run + dt.timedelta(minutes=1)
    due = await db.routine_due(now)
    assert [r["id"] for r in due] == [rid] and due[0]["recurrence"]["type"] == "daily"
    nxt = first_run + dt.timedelta(days=1)
    job_id = await db.routine_fire(due[0], due[0]["next_run_at"], nxt, enqueue=True)
    assert job_id is not None
    # Instance thứ 2 cầm bản đọc cũ: không xếp thêm.
    assert await db.routine_fire(due[0], due[0]["next_run_at"], nxt, enqueue=True) is None
    assert await db.routine_due(now) == []
    assert await db.routine_runs_since(first_run - dt.timedelta(hours=8)) == 1
    job = await db.bg_job_claim(120)
    assert (job["kind"], job["routine_id"]) == ("routine", rid)
    assert _ts(job["scheduled_for"]) == first_run
    await db.bg_job_finish(
        job["id"], "succeeded", result=None, error=None, no_reply=True, tokens=None, provider=None
    )
    listed = await db.routine_list(OWNER)
    assert listed[0]["last_status"] == "succeeded" and listed[0]["last_no_reply"] is True
    assert len(await db.routine_jobs_since(OWNER, first_run - dt.timedelta(hours=8))) == 1
    # Chỉ dời lịch (bỏ lần chạy): không có việc mới.
    later = nxt + dt.timedelta(minutes=1)
    due = await db.routine_due(later)
    assert await db.routine_fire(due[0], due[0]["next_run_at"], None, enqueue=False) is None
    assert await db.routine_runs_since(first_run - dt.timedelta(hours=8)) == 1
    assert await db.routine_set_status(OWNER, rid, "paused")
    assert not await db.routine_set_status(-5, rid, "active")
    assert await db.routine_delete(OWNER, rid)
    # Việc cũ giữ lại (routine_id về NULL), không lỗi FK.
    assert (await db.bg_job_get(OWNER, job["id"]))["routine_id"] is None


@pytest.mark.asyncio
async def test_xoa_du_lieu_nguoi_dung(delivery_db):
    from channels import zalo_users

    await db.bg_job_insert(-5, "zalo", "việc zalo", user_cap=3, global_cap=10)
    await db.routine_insert(
        -5, "zalo", "tóm tắt", {"type": "daily", "at": "08:00"}, dt.datetime.now(VN)
    )
    await db.bg_job_insert(OWNER, "telegram", "việc chủ bot", user_cap=3, global_cap=10)
    await delivery_db.execute("UPDATE bg_jobs SET status = 'succeeded' WHERE id = 2")
    await db.bg_job_insert(OWNER, "telegram", "đang chờ", user_cap=3, global_cap=10)
    await zalo_users.delete_own_data(-5)
    assert await db.bg_job_list(-5) == [] and await db.routine_count(-5) == 0
    await db.bg_job_clear_user(OWNER)  # /xoadulieu chủ bot: chỉ xoá việc đã xong
    assert [r["prompt"] for r in await db.bg_job_list(OWNER)] == ["đang chờ"]
    assert len(await db.bg_job_recent_all()) == 1
