"""SQL lịch sử lệnh (bảng prompts + results, /history) và bản ghi lượt gọi đi kèm.

prompts chỉ giữ HISTORY_RETENTION_LIMIT dòng gần nhất mỗi người (results xoá theo nhờ
ON DELETE CASCADE, có index từ migrations/033) - chỉ phục vụ /history. Số liệu sử dụng
thật nằm ở user_calls (không bị cắt theo số lượng).
"""

from typing import Any

# Chỉ giữ tối đa N prompt gần nhất / user - tránh bảng phình vô hạn theo thời gian
# (bot 1 người dùng nhưng chạy 24/7).
HISTORY_RETENTION_LIMIT = 20


async def save(
    pool: Any,
    telegram_user_id: int,
    command_type: str,
    prompt: str,
    channel: str = "telegram",
) -> int:
    """Lưu 1 prompt + 1 dòng user_calls trong cùng transaction rồi cắt prompts của người
    này về HISTORY_RETENTION_LIMIT dòng; trả id prompt."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO prompts (telegram_user_id, command_type, prompt, channel) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                telegram_user_id,
                command_type,
                prompt,
                channel,
            )
            # Ghi song song vào user_calls (KHÔNG bị dọn theo số lượng như `prompts`
            # bên dưới) - nguồn số liệu thật cho usage_by_user()/`/thongke`.
            await conn.execute(
                "INSERT INTO user_calls (channel, telegram_user_id, kind) VALUES ($1, $2, $3)",
                channel,
                telegram_user_id,
                command_type,
            )
            # Chạy ngay sau mỗi insert nên bảng không bao giờ phình quá giới hạn. Việc
            # dọn này CHỈ phục vụ /history - KHÔNG dùng để đếm usage (xem user_calls).
            await conn.execute(
                """
                DELETE FROM prompts
                WHERE telegram_user_id = $1
                  AND id NOT IN (
                      SELECT id FROM prompts
                      WHERE telegram_user_id = $1
                      ORDER BY id DESC
                      LIMIT $2
                  )
                """,
                telegram_user_id,
                HISTORY_RETENTION_LIMIT,
            )
    return row["id"]


async def prune_all(pool: Any) -> None:
    """Cắt prompts của MỌI người về HISTORY_RETENTION_LIMIT dòng (init_db, lúc khởi động)."""
    await pool.execute(
        """
        DELETE FROM prompts p
        WHERE p.id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY telegram_user_id ORDER BY id DESC
                ) AS rn
                FROM prompts
            ) ranked
            WHERE ranked.rn <= $1
        )
        """,
        HISTORY_RETENTION_LIMIT,
    )


async def save_result(
    pool: Any,
    prompt_id: int,
    result_type: str,
    content_text: str | None = None,
    file_path: str | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    """``provider``/``model`` (migrations/021): ai trả lời lượt này - đánh giá 👍/👎
    sau đó (services/answer_feedback.py) biết câu trả lời của provider nào."""
    await pool.execute(
        "INSERT INTO results (prompt_id, result_type, content_text, file_path, provider, model) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        prompt_id,
        result_type,
        content_text,
        file_path,
        provider,
        model,
    )


async def history(pool: Any, telegram_user_id: int, limit: int = 10) -> list[tuple]:
    """(command_type, prompt, created_at ISO, các loại kết quả "a,b") mới nhất trước."""
    rows = await pool.fetch(
        """
        SELECT p.command_type, p.prompt, p.created_at,
               STRING_AGG(DISTINCT r.result_type, ',') AS result_types
        FROM prompts p
        LEFT JOIN results r ON r.prompt_id = p.id
        WHERE p.telegram_user_id = $1
        GROUP BY p.id
        ORDER BY p.id DESC
        LIMIT $2
        """,
        telegram_user_id,
        limit,
    )
    return [
        (r["command_type"], r["prompt"], r["created_at"].isoformat(), r["result_types"] or "")
        for r in rows
    ]
