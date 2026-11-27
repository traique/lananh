"""SQL truy vết lượt trả lời và quota provider (migrations/015, 025).

- provider_calls: độ trễ p50/p95 + tỉ lệ lỗi theo provider (/status, /admin), các
  lượt gọi của 1 lượt trả lời (mã lượt core/trace.py), số lượt/token đã dùng trong
  cửa sổ quota của từng provider (services/quota_burn.py).
- turn_metrics: 1 dòng/lượt (chờ khoá, chờ slot, gom ngữ cảnh, LLM, gửi tin).
- user_calls: chuỗi thời gian lượt gọi AI cho biểu đồ trang admin.

Mọi truy vấn đều có cửa sổ thời gian + LIMIT, đi theo index sẵn có
(idx_provider_calls_provider, idx_provider_calls_turn, idx_turn_metrics_created).
"""

from datetime import datetime
from typing import Any

# Lượt chờ/hết hạn chót của CẢ lượt không phải lỗi của provider - không tính vào
# tỉ lệ lỗi /status.
_NOT_PROVIDER_FAULT = ("turn_timeout",)


async def latency_by_provider(pool: Any, since_hours: int = 24) -> list[dict]:
    """Mỗi provider: số lượt, số lượt lỗi, p50/p95 độ trễ của lượt thành công (/status)."""
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
    """1 dòng turn_metrics khi 1 lượt kết thúc (core/trace.py ghi ở nền)."""
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


async def recent_turns(pool: Any, since_hours: int = 24, limit: int = 50) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT turn_id, channel, user_kind, lock_wait_ms, slot_wait_ms, context_ms, llm_ms,
               send_ms, total_ms, llm_calls, final_provider, fallback_used, outcome, created_at
        FROM turn_metrics
        WHERE created_at >= now() - make_interval(hours => $1)
        ORDER BY created_at DESC, id DESC
        LIMIT $2
        """,
        since_hours,
        limit,
    )
    return [dict(row) for row in rows]


async def turn_stage_summary(pool: Any, since_hours: int = 24) -> list[dict]:
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


async def provider_calls_for_turn(pool: Any, turn_id: str, limit: int = 50) -> list[dict]:
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
    """Lượt/token/credit đã dùng từ ``since`` cho từng provider (services/quota_burn.py). Lượt bị 429 (rate_limited)
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


_TIMELINE_BUCKETS = ("hour", "day")


async def calls_timeline(pool: Any, since_hours: int, bucket: str) -> list[dict]:
    """Số lượt gọi AI (bảng user_calls) trong ``since_hours`` giờ qua, gom theo giờ/ngày
    giờ VN - biểu đồ trang admin (web_admin.py điền 0 cho mốc trống). ``bucket`` được
    chèn thẳng vào SQL nên chỉ nhận "hour" | "day"."""
    if bucket not in _TIMELINE_BUCKETS:
        raise ValueError(f"bucket không hợp lệ: {bucket!r}")
    rows = await pool.fetch(
        f"""
        SELECT date_trunc('{bucket}', created_at AT TIME ZONE 'Asia/Ho_Chi_Minh') AS bucket,
               COUNT(*) AS calls
        FROM user_calls
        WHERE created_at >= now() - ($1 || ' hours')::interval
        GROUP BY 1
        ORDER BY 1
        """,
        str(since_hours),
    )
    return [dict(row) for row in rows]


# ─── provider_calls / user_calls: số liệu cho /admin, /thongke ───────────────


async def insert_provider_call(
    pool: Any,
    provider: str,
    model: str,
    *,
    outcome: str = "ok",
    latency_ms: int | None = None,
    stage: str | None = None,
    prompt_chars: int | None = None,
    turn_id: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    """Ghi 1 lượt gọi provider/model (bảng provider_calls). ``outcome``: ok |
    error | timeout | rate_limited | contaminated. ``turn_id`` (mã lượt
    core/trace.py) và token (provider tự báo) - migrations/025. Lỗi DB ở đây
    KHÔNG được để văng lên caller (chỉ là số liệu thống kê, không phải luồng
    chính) - caller (ai/call_log.py) tự bọc try/except khi gọi hàm này."""
    await pool.execute(
        """INSERT INTO provider_calls (provider, model, outcome, latency_ms, stage, prompt_chars,
                                       turn_id, prompt_tokens, completion_tokens)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
        provider,
        model,
        outcome,
        latency_ms,
        stage,
        prompt_chars,
        turn_id,
        prompt_tokens,
        completion_tokens,
    )


async def provider_outcomes(pool: Any, since_hours: int = 24 * 7) -> list[dict]:
    """Số lượt theo (provider, outcome) + độ trễ trung vị/p95 - trang admin dùng
    để thấy provider nào chậm/lỗi/hết quota thay vì chỉ đếm lượt thành công."""
    rows = await pool.fetch(
        """
        SELECT provider, outcome, COUNT(*) AS calls,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
               MAX(created_at) AS last_at
        FROM provider_calls
        WHERE created_at >= now() - ($1 || ' hours')::interval
        GROUP BY provider, outcome
        ORDER BY provider, calls DESC
        """,
        str(since_hours),
    )
    return [dict(row) for row in rows]


async def usage_by_model(pool: Any, since_hours: int = 24 * 7) -> list[dict]:
    """Số lượt gọi thành công (bảng provider_calls) theo (provider, model),
    dùng cho trang admin - khác usage_by_user() ở chỗ đây là góc nhìn theo
    HẠ TẦNG AI đang trả lời, không phải theo user/kênh nào đang hỏi."""
    rows = await pool.fetch(
        """
        SELECT provider, model, COUNT(*) AS calls, MAX(created_at) AS last_call_at
        FROM provider_calls
        WHERE created_at >= now() - ($1 || ' hours')::interval
          AND outcome = 'ok'
        GROUP BY provider, model
        ORDER BY calls DESC
        """,
        str(since_hours),
    )
    return [dict(row) for row in rows]


async def usage_by_user(pool: Any, since_hours: int = 24 * 7) -> list[dict]:
    """Số lượt gọi AI (bảng user_calls - KHÔNG phải ``prompts``: prompts bị cắt còn 20
    dòng/người, user_calls mới là nguồn số liệu thật) theo (channel, telegram_user_id),
    dùng cho trang admin và /thongke. telegram_user_id âm = tài khoản Zalo đã pair (xem
    channels/zalo_users.py); dương = chủ bot (Telegram/Zoom dùng chung
    config.ALLOWED_USER_ID vì Zoom hiện chỉ có 1 pairing)."""
    rows = await pool.fetch(
        """
        SELECT channel, telegram_user_id, COUNT(*) AS calls, MAX(created_at) AS last_call_at
        FROM user_calls
        WHERE created_at >= now() - ($1 || ' hours')::interval
        GROUP BY channel, telegram_user_id
        ORDER BY calls DESC
        """,
        str(since_hours),
    )
    return [dict(row) for row in rows]
