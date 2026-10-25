"""Persistence operations for short-term chat history (bảng chat_messages)."""

from typing import Any


async def add_turn(
    pool: Any,
    telegram_user_id: int,
    user_text: str,
    model_text: str,
    *,
    retention: int,
) -> None:
    """INSERT tin người dùng + câu trả lời của bot trong CÙNG 1 transaction rồi
    cắt bảng về ``retention`` dòng gần nhất của người dùng đó."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO chat_messages (telegram_user_id, role, content) "
                "VALUES ($1, 'user', $2), ($1, 'model', $3)",
                telegram_user_id,
                user_text,
                model_text,
            )
            await conn.execute(
                """
                DELETE FROM chat_messages
                WHERE telegram_user_id = $1
                  AND id NOT IN (
                      SELECT id FROM chat_messages
                      WHERE telegram_user_id = $1
                      ORDER BY id DESC
                      LIMIT $2
                  )
                """,
                telegram_user_id,
                retention,
            )
