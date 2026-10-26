"""SQL đánh giá câu trả lời (bảng answer_feedback, migrations/021) và ảnh chụp lượt
chat cần đánh giá (prompts + results.provider/model)."""

from typing import Any


def _snapshot_sql(chars_param: str, where: str) -> str:
    """SELECT câu hỏi + kết quả cuối (kèm provider/model) của 1 lượt; ``chars_param``
    là số thứ tự tham số trần độ dài chữ ($2, $3...)."""
    return f"""
        SELECT p.id AS prompt_id, p.channel, left(p.prompt, {chars_param}) AS prompt_text,
               left(r.content_text, {chars_param}) AS answer_text, r.provider, r.model
        FROM prompts p
        JOIN LATERAL (
            SELECT content_text, provider, model FROM results
            WHERE prompt_id = p.id ORDER BY id DESC LIMIT 1
        ) r ON true
        WHERE {where}
    """


async def upsert(
    pool: Any,
    *,
    channel: str,
    user_id: int,
    prompt_id: int,
    rating: int,
    reason: str | None,
    provider: str | None,
    model: str | None,
    prompt_text: str,
    answer_text: str,
) -> int:
    """Ghi/cập nhật đánh giá (bấm lại = đổi ý; lý do cũ giữ nếu lần này không có)."""
    return await pool.fetchval(
        """
        INSERT INTO answer_feedback (channel, telegram_user_id, prompt_id, rating, reason,
                                     provider, model, prompt_text, answer_text)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (telegram_user_id, prompt_id) DO UPDATE SET
            rating = EXCLUDED.rating,
            reason = COALESCE(EXCLUDED.reason, answer_feedback.reason),
            provider = COALESCE(answer_feedback.provider, EXCLUDED.provider),
            model = COALESCE(answer_feedback.model, EXCLUDED.model),
            updated_at = now()
        RETURNING id
        """,
        channel,
        user_id,
        prompt_id,
        rating,
        reason,
        provider,
        model,
        prompt_text,
        answer_text,
    )


async def turn_snapshot(pool: Any, user_id: int, prompt_id: int, max_chars: int) -> dict | None:
    """Câu hỏi + câu trả lời + provider của 1 lượt (chỉ của chính người đó)."""
    row = await pool.fetchrow(
        _snapshot_sql("$3", "p.id = $2 AND p.telegram_user_id = $1"),
        user_id,
        prompt_id,
        max_chars,
    )
    return dict(row) if row else None


async def last_chat_snapshot(pool: Any, user_id: int, max_chars: int) -> dict | None:
    """Lượt chat gần nhất có câu trả lời thật (bỏ lượt lỗi) - cho /sai."""
    row = await pool.fetchrow(
        _snapshot_sql(
            "$2",
            "p.telegram_user_id = $1 AND p.command_type = 'chat' "
            "AND r.content_text NOT LIKE 'error: %' ORDER BY p.id DESC LIMIT 1",
        ),
        user_id,
        max_chars,
    )
    return dict(row) if row else None


async def stats(pool: Any, days: int) -> list[dict]:
    """Số 👍/👎 theo provider/model trong ``days`` ngày (mọi kênh, chỉ đếm)."""
    rows = await pool.fetch(
        """
        SELECT COALESCE(provider, '?') AS provider, COALESCE(model, '') AS model,
               count(*) FILTER (WHERE rating > 0) AS up,
               count(*) FILTER (WHERE rating < 0) AS down
        FROM answer_feedback
        WHERE created_at >= now() - make_interval(days => $1)
        GROUP BY 1, 2
        ORDER BY down DESC, up DESC
        LIMIT 20
        """,
        days,
    )
    return [dict(row) for row in rows]


async def recent_down(pool: Any, user_id: int, days: int, limit: int) -> list[dict]:
    """👎 gần đây của CHÍNH người xem (không lộ câu hỏi của thành viên khác)."""
    rows = await pool.fetch(
        """
        SELECT provider, model, left(prompt_text, 120) AS prompt_text, reason, channel,
               created_at
        FROM answer_feedback
        WHERE telegram_user_id = $1 AND rating < 0
          AND created_at >= now() - make_interval(days => $2)
        ORDER BY created_at DESC
        LIMIT $3
        """,
        user_id,
        days,
        limit,
    )
    return [dict(row) for row in rows]


async def owner_down_for_evals(pool: Any, user_id: int, days: int, limit: int) -> list[dict]:
    """👎 của ĐÚNG 1 người trên kênh Telegram (chủ bot) để làm case đánh giá ứng viên
    (services/eval_export.py) - không bao giờ lấy dòng Zalo/Zoom hay của người khác."""
    rows = await pool.fetch(
        """
        SELECT id, prompt_text, answer_text, reason, provider, model, created_at
        FROM answer_feedback
        WHERE telegram_user_id = $1 AND channel = 'telegram' AND rating < 0
          AND created_at >= now() - make_interval(days => $2)
        ORDER BY created_at DESC
        LIMIT $3
        """,
        user_id,
        days,
        limit,
    )
    return [dict(row) for row in rows]


async def clear(pool: Any, user_id: int) -> None:
    await pool.execute("DELETE FROM answer_feedback WHERE telegram_user_id = $1", user_id)
