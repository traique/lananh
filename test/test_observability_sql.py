"""SQL truy vết lượt / quota provider (migrations/025) trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng
một lần). Dùng chung fixture ``delivery_db`` của file đó.
"""

from datetime import datetime, timedelta, timezone

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db


@pytest.mark.asyncio
async def test_migration_025_chay_lai_duoc(delivery_db):
    sql = (_delivery_sql.MIGRATIONS / "025_turn_trace_quota.sql").read_text("utf-8")
    await delivery_db.execute(sql)
    assert await delivery_db.fetchval("SELECT to_regclass('turn_metrics') IS NOT NULL")


@pytest.mark.asyncio
async def test_record_provider_call_ghi_ma_luot_va_token(delivery_db):
    await db.record_provider_call(
        "groq",
        "gpt-oss",
        latency_ms=1200,
        stage="chat",
        prompt_chars=900,
        turn_id="telegram:1",
        prompt_tokens=300,
        completion_tokens=80,
    )
    await db.record_provider_call("api1", "gemini", outcome="timeout", turn_id="telegram:1")
    await db.record_provider_call("groq", "gpt-oss", stage="utility")
    rows = await db.provider_calls_for_turn("telegram:1")
    assert [(r["provider"], r["outcome"]) for r in rows] == [("groq", "ok"), ("api1", "timeout")]
    assert (rows[0]["prompt_tokens"], rows[0]["completion_tokens"], rows[0]["prompt_chars"]) == (
        300,
        80,
        900,
    )


@pytest.mark.asyncio
async def test_do_tre_theo_provider_bo_turn_timeout_khoi_ty_le_loi(delivery_db):
    await delivery_db.execute(
        """
        INSERT INTO provider_calls (provider, model, outcome, latency_ms, created_at) VALUES
            ('groq', 'm', 'ok', 1000, now()),
            ('groq', 'm', 'ok', 3000, now()),
            ('groq', 'm', 'rate_limited', 50, now()),
            ('groq', 'm', 'turn_timeout', 9000, now()),
            ('groq', 'm', 'ok', 99999, now() - interval '3 days'),
            ('api1', 'g', 'error', 400, now())
        """
    )
    rows = {row["provider"]: row for row in await db.provider_latency_summary(24)}
    assert rows["groq"]["calls"] == 4 and rows["groq"]["failed"] == 1
    assert rows["groq"]["p50_ms"] == 2000
    assert rows["api1"]["failed"] == 1 and rows["api1"]["p50_ms"] is None


@pytest.mark.asyncio
async def test_turn_metrics_ghi_doc_va_tong_hop(delivery_db):
    for total, channel, outcome in ((1000, "telegram", "ok"), (3000, "telegram", "error")):
        await db.record_turn_metrics(
            turn_id=f"{channel}:{total}",
            channel=channel,
            user_kind="owner",
            lock_wait_ms=5,
            slot_wait_ms=0,
            context_ms=200,
            llm_ms=total - 300,
            send_ms=80,
            total_ms=total,
            llm_calls=2,
            final_provider="groq",
            fallback_used=outcome != "ok",
            outcome=outcome,
        )
    await db.record_turn_metrics(turn_id="job:x:2026-10-26", channel="job", total_ms=5, llm_calls=1)
    recent = await db.recent_turn_metrics(24, 10)
    assert [r["turn_id"] for r in recent][0] == "job:x:2026-10-26"
    summary = await db.turn_stage_summary(24)
    assert [s["channel"] for s in summary] == ["telegram"]  # bỏ job nền
    assert summary[0]["turns"] == 2 and summary[0]["failed"] == 1 and summary[0]["fallback"] == 1
    assert summary[0]["total_p50"] == 2000


@pytest.mark.asyncio
async def test_burn_tu_moc_quota_bo_luot_429_va_tinh_credit_tavily(delivery_db):
    since = datetime.now(timezone.utc) - timedelta(hours=2)
    await delivery_db.execute(
        """
        INSERT INTO provider_calls (provider, model, outcome, prompt_tokens, completion_tokens,
                                    created_at) VALUES
            ('api1', 'g', 'ok', 100, 20, now()),
            ('api1', 'g', 'rate_limited', NULL, NULL, now()),
            ('api1', 'g', 'ok', 1, 1, now() - interval '5 hours'),
            ('tavily', 'advanced', 'ok', NULL, NULL, now()),
            ('tavily', 'basic', 'ok', NULL, NULL, now()),
            ('groq', 'm', 'ok', 5, 5, now())
        """
    )
    rows = {r["provider"]: r for r in await db.provider_burn_since(["api1", "tavily"], since)}
    assert set(rows) == {"api1", "tavily"}
    assert rows["api1"]["requests"] == 1 and rows["api1"]["rate_limited"] == 1
    assert rows["api1"]["prompt_tokens"] == 100 and rows["api1"]["completion_tokens"] == 20
    assert rows["tavily"]["credits"] == 3


# ─── Quota ngày thành viên Zalo (services/usage_quota.py) ───────────────────


@pytest.mark.asyncio
async def test_quota_dem_luot_va_luot_nang_tu_dau_ngay(delivery_db):
    start = datetime.now(timezone.utc) - timedelta(hours=3)
    await db.save_prompt(-7, "chat", "chào em", channel="zalo")
    await db.save_prompt(-7, "tuvi_chitiet", "/tuvi_chitiet", channel="zalo")
    await db.record_user_call("zalo", -7, "image")
    await db.record_user_call("zalo", -8, "chat")
    await delivery_db.execute(
        "INSERT INTO user_calls (channel, telegram_user_id, kind, created_at) "
        "VALUES ('zalo', -7, 'chat', now() - interval '1 day'), ('zalo', -7, NULL, now())"
    )
    heavy = ["image", "tuvi_chitiet", "kymon_chitiet", "tsh_chitiet", "chat_search"]
    assert await db.user_calls_since("zalo", -7, start, heavy) == (4, 2)
    # Lượt chat có tra web: đổi lượt chat MỚI NHẤT hôm nay sang chat_search.
    assert await db.relabel_last_user_call("zalo", -7, "chat", "chat_search", start)
    assert await db.user_calls_since("zalo", -7, start, heavy) == (4, 3)
    assert not await db.relabel_last_user_call("zalo", -7, "chat", "chat_search", start)
    rows = await db.user_calls_by_user_since("zalo", start, heavy)
    assert [(r["telegram_user_id"], r["turns"], r["heavy"]) for r in rows] == [
        (-7, 4, 3),
        (-8, 1, 0),
    ]
    # Lượt chat ngày hôm trước không bị đổi loại.
    assert (
        await delivery_db.fetchval(
            "SELECT kind FROM user_calls WHERE created_at < now() - interval '12 hours'"
        )
        == "chat"
    )


@pytest.mark.asyncio
async def test_xuat_eval_chi_lay_thumbs_down_telegram_cua_dung_nguoi(delivery_db):
    await delivery_db.execute(
        """
        INSERT INTO answer_feedback (channel, telegram_user_id, prompt_id, rating, reason,
                                     prompt_text, answer_text) VALUES
            ('telegram', 777, 1, -1, 'bịa số', 'giá vàng?', 'khoảng 149 triệu'),
            ('telegram', 777, 2, 1, NULL, 'chào', 'dạ'),
            ('zoom', 777, 3, -1, NULL, 'zoom hỏi', 'x'),
            ('zalo', -5, 4, -1, NULL, 'thành viên hỏi', 'y'),
            ('telegram', 778, 5, -1, NULL, 'người khác', 'z')
        """
    )
    rows = await db.answer_feedback_owner_down(777, 30)
    assert [r["prompt_text"] for r in rows] == ["giá vàng?"]
    assert rows[0]["reason"] == "bịa số"
