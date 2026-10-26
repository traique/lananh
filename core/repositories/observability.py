"""SQL truy vết lượt trả lời và quota provider (migrations/015, 025).

- provider_calls: độ trễ p50/p95 + tỉ lệ lỗi theo provider (/status, /admin), các
  lượt gọi của 1 lượt trả lời (mã lượt core/trace.py), số lượt/token đã dùng trong
  cửa sổ quota của từng provider (services/quota_burn.py).
- turn_metrics: 1 dòng/lượt (chờ khoá, chờ slot, gom ngữ cảnh, LLM, gửi tin).

Mọi truy vấn đều có cửa sổ thời gian + LIMIT, đi theo index sẵn có
(idx_provider_calls_provider, idx_provider_calls_turn, idx_turn_metrics_created).
"""

from datetime import datetime
from typing import Any

# Lượt chờ/hết hạn chót của CẢ lượt không phải lỗi của provider - không tính vào
# tỉ lệ lỗi /status.
_NOT_PROVIDER_FAULT = ("turn_timeout",)


async def latency_by_provider(pool: Any, since_hours: int) -> list[dict]:
    """Mỗi provider: số lượt, số lượt lỗi, p50/p95 độ trễ của lượt thành công."""
    rows = await pool.fetch(
        """
        SELECT provider,
               count(*) AS calls,
               count(*) FILTER (WHERE outcome <> 'ok' AND outcome <> ALL($2::text[])) AS failed,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                   FILTER (WHERE outcome = 'ok') AS p50_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                   FILTER (WHERE outcome = 'ok') AS p95_ms
        FROM provider_calls
        WHERE created_at >= now() - make_interval(hours => $1)
        GROUP BY provider
        ORDER BY calls DESC
        LIMIT 20
        """,
        since_hours,
        list(_NOT_PROVIDER_FAULT),
    )
    return [dict(row) for row in rows]


async def insert_turn_metrics(pool: Any, **row: Any) -> None:
    await pool.execute(
        """
        INSERT INTO turn_metrics (turn_id, channel, user_kind, lock_wait_ms, slot_wait_ms,
                                  context_ms, llm_ms, send_ms, total_ms, llm_calls,
                                  final_provider, fallback_used, outcome)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        """,
        row["turn_id"],
        row.get("channel") or "",
        row.get("user_kind") or "",
        row.get("lock_wait_ms"),
        row.get("slot_wait_ms"),
        row.get("context_ms"),
        row.get("llm_ms"),
        row.get("send_ms"),
        row.get("total_ms"),
        int(row.get("llm_calls") or 0),
        row.get("final_provider"),
        bool(row.get("fallback_used")),
        row.get("outcome") or "ok",
    )


async def recent_turns(pool: Any, since_hours: int, limit: int) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT turn_id, channel, user_kind, lock_wait_ms, slot_wait_ms, context_ms, llm_ms,
               send_ms, total_ms, llm_calls, final_provider, fallback_used, outcome, created_at
        FROM turn_metrics
        WHERE created_at >= now() - make_interval(hours => $1)
        ORDER BY created_at DESC
        LIMIT $2
        """,
        since_hours,
        limit,
    )
    return [dict(row) for row in rows]


async def turn_stage_summary(pool: Any, since_hours: int) -> list[dict]:
    """p50/p95 từng giai đoạn theo kênh (lượt của người dùng, bỏ job nền)."""
    rows = await pool.fetch(
        """
        SELECT channel, count(*) AS turns,
               count(*) FILTER (WHERE outcome <> 'ok') AS failed,
               count(*) FILTER (WHERE fallback_used) AS fallback,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms) AS total_p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) AS total_p95,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY lock_wait_ms) AS lock_p95,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY slot_wait_ms) AS slot_p95,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY context_ms) AS context_p95,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY llm_ms) AS llm_p95,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY send_ms) AS send_p95
        FROM turn_metrics
        WHERE created_at >= now() - make_interval(hours => $1) AND channel <> 'job'
        GROUP BY channel
        ORDER BY turns DESC
        """,
        since_hours,
    )
    return [dict(row) for row in rows]


async def provider_calls_for_turn(pool: Any, turn_id: str, limit: int) -> list[dict]:
    """Các lượt gọi provider của 1 lượt trả lời (thác thời gian trên /admin)."""
    rows = await pool.fetch(
        """
        SELECT provider, model, outcome, latency_ms, stage, prompt_chars, prompt_tokens,
               completion_tokens, created_at
        FROM provider_calls
        WHERE turn_id = $1
        ORDER BY id
        LIMIT $2
        """,
        turn_id,
        limit,
    )
    return [dict(row) for row in rows]


async def burn_since(pool: Any, providers: list[str], since: datetime) -> list[dict]:
    """Lượt gọi + token từ ``since`` cho từng provider. Lượt bị 429 (rate_limited)
    không tính là lượt đã dùng; ``credits`` của Tavily: advanced = 2, basic = 1."""
    rows = await pool.fetch(
        """
        SELECT provider,
               count(*) FILTER (WHERE outcome <> 'rate_limited') AS requests,
               count(*) FILTER (WHERE outcome = 'rate_limited') AS rate_limited,
               COALESCE(sum(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(sum(completion_tokens), 0) AS completion_tokens,
               COALESCE(sum(CASE WHEN outcome = 'rate_limited' THEN 0
                                 WHEN model = 'advanced' THEN 2 ELSE 1 END), 0) AS credits
        FROM provider_calls
        WHERE provider = ANY($1::text[]) AND created_at >= $2
        GROUP BY provider
        """,
        providers,
        since,
    )
    return [dict(row) for row in rows]
