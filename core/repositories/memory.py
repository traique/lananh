"""Persistence cho trí nhớ hội thoại v2 (migrations/017, 018).

- user_facts: fact hiện hành (invalid_at IS NULL) + vài giá trị cũ đã bị thay
  (invalid_at, superseded_by) - "vô hiệu hoá thay vì xoá".
- user_memory_highlights: thêm đọc theo id để /quen xoá đúng dòng.
- user_session_notes: ghi chú + tóm tắt theo phiên.
- chat_messages: đọc cửa sổ chưa trích nhớ (watermark), dòng model gần nhất.
- notes: đọc gần đây có trần để tìm kiếm, xoá 1 ghi chú.
- user_events: sự kiện cho hỏi thăm chủ động (services/proactive_service.py).

Mọi hàm nhận ``pool`` (asyncpg pool hoặc pool PGlite của test) - core/database.py
bọc @_with_reconnect.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

# Thứ tự ưu tiên giữ/đưa vào prompt: ghim > quan trọng > mới cập nhật.
_RANK_ORDER = "pinned DESC, importance DESC, updated_at DESC, id DESC"
_FACT_COLUMNS = (
    "id, key, value, pinned, importance, category, source, created_at, updated_at, valid_from"
)

# Kết quả save_fact.
ADDED, UPDATED, UNCHANGED, PROTECTED = "added", "updated", "unchanged", "protected"


# ─── user_facts ──────────────────────────────────────────────────────────────


async def current_facts(pool: Any, telegram_user_id: int) -> list[Any]:
    return await pool.fetch(
        f"SELECT {_FACT_COLUMNS} FROM user_facts "
        f"WHERE telegram_user_id = $1 AND invalid_at IS NULL ORDER BY {_RANK_ORDER}",
        telegram_user_id,
    )


async def superseded_facts(pool: Any, telegram_user_id: int, limit: int) -> list[Any]:
    """Giá trị cũ đã bị thay (mới bị thay trước) - để trả lời "trước đây..."."""
    return await pool.fetch(
        f"SELECT {_FACT_COLUMNS}, invalid_at FROM user_facts "
        "WHERE telegram_user_id = $1 AND invalid_at IS NOT NULL "
        "ORDER BY invalid_at DESC, id DESC LIMIT $2",
        telegram_user_id,
        limit,
    )


async def save_fact(
    pool: Any,
    telegram_user_id: int,
    key: str,
    value: str,
    *,
    importance: int,
    category: str,
    source: str,
    pinned: bool,
    protect: bool,
) -> str:
    """Thêm/cập nhật fact hiện hành của ``key`` trong 1 transaction.

    Giá trị đổi -> dòng cũ được vô hiệu hoá (giữ lại làm lịch sử, superseded_by
    trỏ dòng mới) rồi chèn dòng mới. ``protect`` (bộ trích xuất tự động): không
    đụng dòng đang ghim hoặc do người dùng tự nhập (source='user')."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT id, value, pinned, source, importance FROM user_facts "
                "WHERE telegram_user_id = $1 AND key = $2 AND invalid_at IS NULL FOR UPDATE",
                telegram_user_id,
                key,
            )
            if current is not None:
                if protect and (current["pinned"] or current["source"] == "user"):
                    return PROTECTED
                keep_pinned = bool(pinned or current["pinned"])
                keep_source = "user" if "user" in (source, current["source"]) else "auto"
                if current["value"] == value:
                    await conn.execute(
                        "UPDATE user_facts SET importance = GREATEST(importance, $2), "
                        "category = CASE WHEN $3 = '' THEN category ELSE $3 END, "
                        "pinned = $4, source = $5, updated_at = now() WHERE id = $1",
                        current["id"],
                        importance,
                        category,
                        keep_pinned,
                        keep_source,
                    )
                    return UNCHANGED
                await conn.execute(
                    "UPDATE user_facts SET invalid_at = now() WHERE id = $1", current["id"]
                )
                new_id = await conn.fetchval(
                    "INSERT INTO user_facts (telegram_user_id, key, value, updated_at, pinned, "
                    "importance, category, source, created_at, valid_from) "
                    "VALUES ($1, $2, $3, now(), $4, $5, $6, $7, now(), now()) RETURNING id",
                    telegram_user_id,
                    key,
                    value,
                    keep_pinned,
                    importance,
                    category,
                    keep_source,
                )
                await conn.execute(
                    "UPDATE user_facts SET superseded_by = $2 WHERE id = $1", current["id"], new_id
                )
                return UPDATED
            await conn.execute(
                "INSERT INTO user_facts (telegram_user_id, key, value, updated_at, pinned, "
                "importance, category, source, created_at, valid_from) "
                "VALUES ($1, $2, $3, now(), $4, $5, $6, $7, now(), now())",
                telegram_user_id,
                key,
                value,
                pinned,
                importance,
                category,
                source,
            )
            return ADDED


async def invalidate_fact(
    pool: Any, telegram_user_id: int, fact_id: int, *, protect: bool
) -> str | None:
    """Vô hiệu hoá 1 fact hiện hành (giữ làm lịch sử). Trả key, hoặc None nếu
    không có/được bảo vệ."""
    return await pool.fetchval(
        "UPDATE user_facts SET invalid_at = now() "
        "WHERE id = $2 AND telegram_user_id = $1 AND invalid_at IS NULL "
        "AND (NOT $3 OR (NOT pinned AND source <> 'user')) RETURNING key",
        telegram_user_id,
        fact_id,
        protect,
    )


async def forget_key(pool: Any, telegram_user_id: int, key: str) -> int:
    """XOÁ HẲN mọi phiên bản (hiện hành + cũ) của 1 key - người dùng chủ động
    bảo quên thì không được giữ lịch sử."""
    result = await pool.execute(
        "DELETE FROM user_facts WHERE telegram_user_id = $1 AND key = $2",
        telegram_user_id,
        key,
    )
    return _count(result)


async def set_pinned(pool: Any, telegram_user_id: int, fact_id: int, pinned: bool) -> bool:
    result = await pool.execute(
        "UPDATE user_facts SET pinned = $3 "
        "WHERE id = $2 AND telegram_user_id = $1 AND invalid_at IS NULL",
        telegram_user_id,
        fact_id,
        pinned,
    )
    return _count(result) > 0


async def trim_facts(
    pool: Any,
    telegram_user_id: int,
    *,
    keep_current: int,
    keep_superseded_per_key: int,
    keep_superseded_total: int,
) -> None:
    """Giữ ``keep_current`` fact hiện hành theo thứ tự ưu tiên (ghim không bao giờ
    bị cắt ở đây) và có trần riêng cho giá trị cũ."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"""
                DELETE FROM user_facts
                WHERE telegram_user_id = $1 AND invalid_at IS NULL AND NOT pinned
                  AND id NOT IN (
                      SELECT id FROM user_facts
                      WHERE telegram_user_id = $1 AND invalid_at IS NULL
                      ORDER BY {_RANK_ORDER}
                      LIMIT $2
                  )
                """,
                telegram_user_id,
                keep_current,
            )
            await conn.execute(
                """
                DELETE FROM user_facts WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY key ORDER BY invalid_at DESC, id DESC
                        ) AS rn
                        FROM user_facts
                        WHERE telegram_user_id = $1 AND invalid_at IS NOT NULL
                    ) ranked
                    WHERE ranked.rn > $2
                )
                """,
                telegram_user_id,
                keep_superseded_per_key,
            )
            await conn.execute(
                """
                DELETE FROM user_facts
                WHERE telegram_user_id = $1 AND invalid_at IS NOT NULL
                  AND id NOT IN (
                      SELECT id FROM user_facts
                      WHERE telegram_user_id = $1 AND invalid_at IS NOT NULL
                      ORDER BY invalid_at DESC, id DESC
                      LIMIT $2
                  )
                """,
                telegram_user_id,
                keep_superseded_total,
            )


# ─── user_memory_highlights ──────────────────────────────────────────────────


async def highlight_rows(pool: Any, telegram_user_id: int, limit: int) -> list[Any]:
    """(id, content, created_at) mới nhất trước."""
    return await pool.fetch(
        "SELECT id, content, created_at FROM user_memory_highlights "
        "WHERE telegram_user_id = $1 ORDER BY created_at DESC, id DESC LIMIT $2",
        telegram_user_id,
        limit,
    )


async def delete_highlights(pool: Any, telegram_user_id: int, ids: list[int]) -> int:
    if not ids:
        return 0
    result = await pool.execute(
        "DELETE FROM user_memory_highlights WHERE telegram_user_id = $1 AND id = ANY($2::bigint[])",
        telegram_user_id,
        ids,
    )
    return _count(result)


# ─── user_session_notes ──────────────────────────────────────────────────────


async def upsert_session_note(
    pool: Any,
    telegram_user_id: int,
    session_key: int,
    *,
    title: str,
    bullets: str,
    summary: str,
    started_at: dt.datetime,
    keep_n: int,
) -> None:
    """Ghi (hoặc cập nhật) ghi chú của 1 phiên rồi cắt về ``keep_n`` phiên gần nhất.
    Trường rỗng giữ nguyên giá trị đã có."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO user_session_notes
                    (telegram_user_id, session_key, title, bullets, summary, started_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, now())
                ON CONFLICT (telegram_user_id, session_key) DO UPDATE SET
                    title = CASE WHEN EXCLUDED.title = '' THEN user_session_notes.title
                                 ELSE EXCLUDED.title END,
                    bullets = CASE WHEN EXCLUDED.bullets = '' THEN user_session_notes.bullets
                                   ELSE EXCLUDED.bullets END,
                    summary = CASE WHEN EXCLUDED.summary = '' THEN user_session_notes.summary
                                   ELSE EXCLUDED.summary END,
                    updated_at = now()
                """,
                telegram_user_id,
                session_key,
                title,
                bullets,
                summary,
                started_at,
            )
            await conn.execute(
                """
                DELETE FROM user_session_notes
                WHERE telegram_user_id = $1
                  AND id NOT IN (
                      SELECT id FROM user_session_notes
                      WHERE telegram_user_id = $1
                      ORDER BY updated_at DESC, id DESC
                      LIMIT $2
                  )
                """,
                telegram_user_id,
                keep_n,
            )


async def session_notes(
    pool: Any, telegram_user_id: int, limit: int, *, exclude_session_key: int | None = None
) -> list[Any]:
    """Ghi chú phiên mới nhất trước (bỏ phiên ``exclude_session_key`` nếu có)."""
    return await pool.fetch(
        "SELECT session_key, title, bullets, summary, started_at, updated_at "
        "FROM user_session_notes WHERE telegram_user_id = $1 "
        "AND ($3::bigint IS NULL OR session_key <> $3) "
        "ORDER BY updated_at DESC, id DESC LIMIT $2",
        telegram_user_id,
        limit,
        exclude_session_key,
    )


async def session_note(pool: Any, telegram_user_id: int, session_key: int) -> Any:
    return await pool.fetchrow(
        "SELECT session_key, title, bullets, summary, started_at, updated_at "
        "FROM user_session_notes WHERE telegram_user_id = $1 AND session_key = $2",
        telegram_user_id,
        session_key,
    )


async def rewrite_session_note(
    pool: Any,
    telegram_user_id: int,
    session_key: int,
    *,
    title: str,
    bullets: str,
    summary: str,
) -> None:
    """Ghi ĐÈ title/bullets/summary của 1 phiên (kể cả chuỗi rỗng - khác
    upsert_session_note giữ trường cũ) mà không đổi thứ tự (updated_at giữ
    nguyên); cả 3 rỗng thì xoá luôn ghi chú phiên đó. Dùng khi /quen 1 điều."""
    if not (title or bullets or summary):
        await pool.execute(
            "DELETE FROM user_session_notes WHERE telegram_user_id = $1 AND session_key = $2",
            telegram_user_id,
            session_key,
        )
        return
    await pool.execute(
        "UPDATE user_session_notes SET title = $3, bullets = $4, summary = $5 "
        "WHERE telegram_user_id = $1 AND session_key = $2",
        telegram_user_id,
        session_key,
        title,
        bullets,
        summary,
    )


async def clear_session_notes(pool: Any, telegram_user_id: int) -> None:
    await pool.execute(
        "DELETE FROM user_session_notes WHERE telegram_user_id = $1", telegram_user_id
    )


# ─── chat_messages (cửa sổ trích nhớ) ────────────────────────────────────────


async def chat_rows_after(
    pool: Any, telegram_user_id: int, after_id: int, limit: int, max_chars: int
) -> list[Any]:
    """Các dòng có id > after_id (cũ -> mới), nội dung cắt ``max_chars`` ngay
    trong SQL để không kéo câu trả lời dài vào RAM."""
    return await pool.fetch(
        "SELECT id, role, kind, left(content, $4) AS content, created_at FROM chat_messages "
        "WHERE telegram_user_id = $1 AND id > $2 ORDER BY id ASC LIMIT $3",
        telegram_user_id,
        after_id,
        limit,
        max_chars,
    )


async def recent_chat_rows(
    pool: Any, telegram_user_id: int, limit: int, max_chars: int
) -> list[Any]:
    """``limit`` dòng gần nhất, mới -> cũ (xác định ranh giới phiên)."""
    return await pool.fetch(
        "SELECT id, role, kind, left(content, $3) AS content, created_at FROM chat_messages "
        "WHERE telegram_user_id = $1 ORDER BY id DESC LIMIT $2",
        telegram_user_id,
        limit,
        max_chars,
    )


async def last_model_row(pool: Any, telegram_user_id: int, max_chars: int) -> Any:
    return await pool.fetchrow(
        "SELECT id, kind, left(content, $2) AS content, created_at FROM chat_messages "
        "WHERE telegram_user_id = $1 AND role = 'model' ORDER BY id DESC LIMIT 1",
        telegram_user_id,
        max_chars,
    )


async def users_with_chat_after(pool: Any, since: dt.datetime, limit: int) -> list[int]:
    """Người có tin nhắn từ ``since`` (quét nền trích nhớ khi rảnh)."""
    rows = await pool.fetch(
        "SELECT telegram_user_id, MAX(id) AS last_id FROM chat_messages "
        "WHERE created_at >= $1 GROUP BY telegram_user_id ORDER BY last_id DESC LIMIT $2",
        since,
        limit,
    )
    return [int(row["telegram_user_id"]) for row in rows]


# ─── notes ───────────────────────────────────────────────────────────────────


async def recent_notes(pool: Any, telegram_user_id: int, limit: int, max_chars: int) -> list[Any]:
    """(id, content, created_at) mới nhất trước - quét có trần cho tìm kiếm."""
    return await pool.fetch(
        "SELECT id, left(content, $3) AS content, created_at FROM notes "
        "WHERE telegram_user_id = $1 ORDER BY created_at DESC, id DESC LIMIT $2",
        telegram_user_id,
        limit,
        max_chars,
    )


async def delete_note(pool: Any, telegram_user_id: int, note_id: int) -> bool:
    result = await pool.execute(
        "DELETE FROM notes WHERE telegram_user_id = $1 AND id = $2", telegram_user_id, note_id
    )
    return _count(result) > 0


async def clear_notes(pool: Any, telegram_user_id: int) -> int:
    return _count(
        await pool.execute("DELETE FROM notes WHERE telegram_user_id = $1", telegram_user_id)
    )


# ─── user_events ─────────────────────────────────────────────────────────────


async def add_event(
    pool: Any,
    telegram_user_id: int,
    title: str,
    when_text: str,
    event_date: dt.date,
    *,
    keep_n: int,
) -> bool:
    """Thêm sự kiện (trùng người + ngày + tiêu đề thì bỏ qua) rồi cắt về ``keep_n``
    dòng (bỏ dòng cũ nhất trước). Trả True nếu là dòng mới."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            new_id = await conn.fetchval(
                "INSERT INTO user_events (telegram_user_id, title, when_text, event_date) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (telegram_user_id, event_date, title) DO NOTHING RETURNING id",
                telegram_user_id,
                title,
                when_text,
                event_date,
            )
            await conn.execute(
                """
                DELETE FROM user_events
                WHERE telegram_user_id = $1
                  AND id NOT IN (
                      SELECT id FROM user_events
                      WHERE telegram_user_id = $1
                      ORDER BY (status = 'pending') DESC, created_at DESC, id DESC
                      LIMIT $2
                  )
                """,
                telegram_user_id,
                keep_n,
            )
    return new_id is not None


async def expire_events(pool: Any, before: dt.date) -> int:
    """Sự kiện pending đã qua quá lâu (lỡ hỏi thăm) -> done, không hỏi muộn."""
    return _count(
        await pool.execute(
            "UPDATE user_events SET status = 'done' WHERE status = 'pending' AND event_date < $1",
            before,
        )
    )


async def users_with_due_events(pool: Any, on_or_before: dt.date, limit: int) -> list[int]:
    rows = await pool.fetch(
        "SELECT telegram_user_id, MIN(event_date) AS first_date FROM user_events "
        "WHERE status = 'pending' AND event_date <= $1 "
        "GROUP BY telegram_user_id ORDER BY first_date LIMIT $2",
        on_or_before,
        limit,
    )
    return [int(row["telegram_user_id"]) for row in rows]


async def claim_due_event(pool: Any, telegram_user_id: int, on_or_before: dt.date) -> Any:
    """Claim nguyên tử 1 sự kiện tới hạn (pending -> asked); None nếu không có.
    Lỗi gửi thì release_event trả về pending."""
    return await pool.fetchrow(
        """
        UPDATE user_events SET status = 'asked', claimed_at = now()
        WHERE id = (
            SELECT id FROM user_events
            WHERE telegram_user_id = $1 AND status = 'pending' AND event_date <= $2
            ORDER BY event_date, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, telegram_user_id, title, when_text, event_date
        """,
        telegram_user_id,
        on_or_before,
    )


async def release_event(pool: Any, event_id: int) -> None:
    await pool.execute(
        "UPDATE user_events SET status = 'pending', claimed_at = NULL "
        "WHERE id = $1 AND status = 'asked' AND asked_at IS NULL",
        event_id,
    )


async def mark_event_asked(pool: Any, event_id: int) -> None:
    await pool.execute("UPDATE user_events SET asked_at = now() WHERE id = $1", event_id)


async def finish_events(pool: Any, telegram_user_id: int) -> int:
    """Người dùng bảo "đừng nhắc nữa": mọi sự kiện còn chờ -> done."""
    return _count(
        await pool.execute(
            "UPDATE user_events SET status = 'done' "
            "WHERE telegram_user_id = $1 AND status = 'pending'",
            telegram_user_id,
        )
    )


async def finish_event_ids(pool: Any, telegram_user_id: int, ids: list[int]) -> int:
    """Các sự kiện còn chờ trong ``ids`` (của chính người đó) -> done."""
    if not ids:
        return 0
    return _count(
        await pool.execute(
            "UPDATE user_events SET status = 'done' "
            "WHERE telegram_user_id = $1 AND id = ANY($2::bigint[]) AND status = 'pending'",
            telegram_user_id,
            ids,
        )
    )


async def list_events(pool: Any, telegram_user_id: int, limit: int) -> list[Any]:
    return await pool.fetch(
        "SELECT id, title, when_text, event_date, status FROM user_events "
        "WHERE telegram_user_id = $1 ORDER BY event_date DESC, id DESC LIMIT $2",
        telegram_user_id,
        limit,
    )


async def events_on(pool: Any, telegram_user_id: int, day: dt.date, limit: int) -> list[Any]:
    return await pool.fetch(
        "SELECT id, title, when_text, event_date, status FROM user_events "
        "WHERE telegram_user_id = $1 AND event_date = $2 AND status <> 'done' "
        "ORDER BY id LIMIT $3",
        telegram_user_id,
        day,
        limit,
    )


async def clear_events(pool: Any, telegram_user_id: int) -> None:
    await pool.execute("DELETE FROM user_events WHERE telegram_user_id = $1", telegram_user_id)


def _count(result: Any) -> int:
    """Số dòng từ chuỗi trạng thái asyncpg ("DELETE 3", "UPDATE 1")."""
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0
