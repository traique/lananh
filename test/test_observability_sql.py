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
