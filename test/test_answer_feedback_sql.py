"""SQL đánh giá câu trả lời (migrations/021, core/repositories/feedback.py), xoá cặp
lịch sử để làm lại và đọc 1 nhắc việc theo chủ - trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db


async def _chat_turn(uid, prompt, answer, *, provider="groq", model="gpt-oss", channel="telegram"):
    prompt_id = await db.save_prompt(uid, "chat", prompt, channel=channel)
    await db.save_result(prompt_id, "chat", content_text=answer, provider=provider, model=model)
    return prompt_id


@pytest.mark.asyncio
async def test_danh_gia_upsert_snapshot_va_thong_ke(delivery_db):
    pid = await _chat_turn(42, "lạm phát là gì", "là giá tăng")
    snap = await db.answer_feedback_turn(42, pid)
    assert snap["provider"] == "groq" and snap["answer_text"] == "là giá tăng"
    assert await db.answer_feedback_turn(-5, pid) is None  # không đọc lượt của người khác

    async def rate(rating, reason=None, uid=42, prompt_id=pid, provider="groq", model="gpt-oss"):
        return await db.answer_feedback_upsert(
            channel="telegram",
            user_id=uid,
            prompt_id=prompt_id,
            rating=rating,
            reason=reason,
            provider=provider,
            model=model,
            prompt_text="lạm phát là gì",
            answer_text="là giá tăng",
        )

    first = await rate(1)
    assert await rate(-1, "dài quá") == first  # đổi ý: cập nhật cùng dòng
    assert await rate(-1) == first  # không lý do mới: giữ lý do cũ
    other = await _chat_turn(-5, "câu thành viên", "đáp", provider="api1", model="gemini")
    await rate(1, uid=-5, prompt_id=other, provider="api1", model="gemini")
    with pytest.raises(Exception, match="answer_feedback_rating_check"):
        await rate(0)

    stats = {
        (r["provider"], r["model"]): (r["up"], r["down"])
        for r in await db.answer_feedback_stats(30)
    }
    assert stats == {("groq", "gpt-oss"): (0, 1), ("api1", "gemini"): (1, 0)}
    recent = await db.answer_feedback_recent_down(42, 30)
    assert [(r["reason"], r["prompt_text"]) for r in recent] == [("dài quá", "lạm phát là gì")]
    assert await db.answer_feedback_recent_down(-5, 30) == []

    # /sai: lượt chat gần nhất có câu trả lời thật (bỏ lượt lỗi).
    failed = await db.save_prompt(42, "chat", "lượt lỗi")
    await db.save_result(failed, "chat", content_text="error: TimeoutError")
    last = await db.answer_feedback_last_chat(42)
    assert last["prompt_id"] == pid

    await db.answer_feedback_clear(42)
    assert await db.answer_feedback_recent_down(42, 30) == []
    stats = await db.answer_feedback_stats(30)
    assert [(r["provider"], r["up"]) for r in stats] == [("api1", 1)]


@pytest.mark.asyncio
async def test_luu_luot_tra_id_va_xoa_dung_cac_dong_do(delivery_db):
    """🔄/✂️: add_chat_turn trả id 2 dòng vừa lưu; delete_chat_rows xoá đúng các
    dòng đó của chính người dùng (kể cả khi cặp mới đã được lưu sau nó)."""
    first = await db.add_chat_turn(42, "câu 1", "đáp 1")
    second = await db.add_chat_turn(42, "câu 2", "đáp 2")
    other = await db.add_chat_turn(-5, "câu khác", "đáp khác")
    assert len(first) == len(second) == 2 and len(set(first + second + other)) == 6
    assert await db.delete_chat_rows(-5, first) == 0  # không xoá dòng của người khác
    assert await db.delete_chat_rows(42, first) == 2
    assert await db.delete_chat_rows(42, first) == 0  # lặp lại: không xoá thêm
    assert await db.delete_chat_rows(42, []) == 0
    rows = await db.get_recent_chat_rows(42)
    assert [r["content"] for r in rows] == ["đáp 2", "câu 2"]
    assert [r["content"] for r in await db.get_recent_chat_rows(-5)] == ["đáp khác", "câu khác"]


@pytest.mark.asyncio
async def test_doc_nhac_viec_theo_chu(delivery_db):
    import datetime as dt

    due = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    await db.add_reminder(42, "gọi mẹ", due, event_key="k1")
    rid = await delivery_db.fetchval("SELECT id FROM reminders WHERE event_key = 'k1'")
    row = await db.reminder_get(42, rid)
    assert row["message"] == "gọi mẹ" and row["done_at"] is None
    assert await db.reminder_get(-5, rid) is None
