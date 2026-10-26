"""Persistence operations for short-term chat history (bảng chat_messages)."""

from typing import Any

from core.text_normalize import fold

# Bỏ dấu tiếng Việt ngay trong SQL (translate() có sẵn ở mọi Postgres, kể cả
# PGlite của CI - không cần extension unaccent) để tìm lịch sử KHÔNG phân biệt
# dấu, khớp core.text_normalize.fold ở phía Python. Gồm cả chữ hoa: lower() của
# Postgres với locale C không đổi được chữ hoa có dấu.
_VN_LOWER = "àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ"
VN_ACCENTED = _VN_LOWER + _VN_LOWER.upper()
VN_PLAIN = "".join(fold(ch) for ch in VN_ACCENTED)


async def add_turn(
    pool: Any,
    telegram_user_id: int,
    user_text: str,
    model_text: str,
    *,
    retention: int,
    kind: str = "chat",
) -> list[int]:
    """INSERT tin người dùng + câu trả lời của bot trong CÙNG 1 transaction rồi
    cắt bảng về ``retention`` dòng gần nhất của người dùng đó. ``kind`` (cột của
    migrations/017) ghi cho cả 2 dòng. Trả id 2 dòng vừa lưu."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetch(
                "INSERT INTO chat_messages (telegram_user_id, role, content, kind) "
                "VALUES ($1, 'user', $2, $4), ($1, 'model', $3, $4) RETURNING id",
                telegram_user_id,
                user_text,
                model_text,
                kind,
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
    return [int(row["id"]) for row in inserted]


async def delete_rows(pool: Any, telegram_user_id: int, ids: list[int]) -> int:
    """Xoá các dòng ``ids`` CỦA ``telegram_user_id`` (id lạ/của người khác bỏ qua);
    trả số dòng đã xoá."""
    if not ids:
        return 0
    status = await pool.execute(
        "DELETE FROM chat_messages WHERE telegram_user_id = $1 AND id = ANY($2::bigint[])",
        telegram_user_id,
        [int(row_id) for row_id in ids],
    )
    try:
        return int(str(status).rsplit(" ", 1)[-1])  # "DELETE 2"
    except ValueError:
        return 0


async def search(
    pool: Any,
    telegram_user_id: int,
    patterns: list[str],
    *,
    scan_limit: int,
    max_chars: int,
):
    """Dòng chat_messages CỦA ``telegram_user_id`` (mới -> cũ, tối đa
    ``scan_limit``) mà bản bỏ dấu chứa ít nhất 1 mẫu LIKE trong ``patterns``
    (vd "%etf%"); kèm câu cùng lượt (tin người dùng <-> câu trả lời liền kề)."""
    return await pool.fetch(
        """
        SELECT m.id, m.role, left(m.content, $4) AS content, m.kind, m.created_at,
               left(p.content, $4) AS pair_content
        FROM chat_messages m
        LEFT JOIN chat_messages p
               ON p.telegram_user_id = m.telegram_user_id
              AND p.role <> m.role
              AND p.id = CASE WHEN m.role = 'user' THEN m.id + 1 ELSE m.id - 1 END
        WHERE m.telegram_user_id = $1
          AND translate(lower(m.content), $5, $6) LIKE ANY ($2::text[])
        ORDER BY m.id DESC
        LIMIT $3
        """,
        telegram_user_id,
        patterns,
        scan_limit,
        max_chars,
        VN_ACCENTED,
        VN_PLAIN,
    )
