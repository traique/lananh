"""Supabase Postgres - bền hơn SQLite vì ổ đĩa local trên Render free tier là ephemeral.

Mặt tiền (facade) của tầng DB: pool/breaker/``with_reconnect`` ở core/db/pool.py (re-export
dưới đây), SQL ở core/repositories/*. Mọi nơi gọi ``db.xxx``; test thay ``db.get_pool`` hoặc
hàm repository thì các hàm ở đây thấy ngay (tra tên lúc gọi).
"""

import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Concatenate, Optional, ParamSpec, TypeVar, overload

from core import config
from core.db.pool import DatabaseUnavailableError as DatabaseUnavailableError
from core.db.pool import _ssl_setting as _ssl_setting  # test/test_security_contracts.py
from core.db.pool import _with_reconnect
from core.db.pool import circuit_open as circuit_open
from core.db.pool import close_pool as close_pool
from core.db.pool import get_pool as get_pool
from core.db.pool import pool_stats as pool_stats
from core.db.pool import reset_circuit_breaker as reset_circuit_breaker
from core.db.pool import statement_cache_size as statement_cache_size
from core.db.pool import with_reconnect as with_reconnect
from core.repositories import chat_history as _chat_history
from core.repositories import feedback as _feedback
from core.repositories import memory as _memory
from core.repositories import observability as _observability
from core.repositories import prompts as _prompts
from core.repositories import quota as _quota

logger = logging.getLogger(__name__)

HISTORY_RETENTION_LIMIT = _prompts.HISTORY_RETENTION_LIMIT

_P = ParamSpec("_P")
_R = TypeVar("_R")
_T = TypeVar("_T")


@overload
def _bind(
    fn: Callable[Concatenate[Any, _P], Awaitable[_R]], *, idempotent: bool = True
) -> Callable[_P, Awaitable[_R]]: ...


@overload
def _bind(
    fn: Callable[Concatenate[Any, _P], Awaitable[Any]],
    *,
    idempotent: bool = True,
    post: Callable[[Any], _T],
) -> Callable[_P, Awaitable[_T]]: ...


def _bind(fn, *, idempotent=True, post=None):
    """``db.xxx = _bind(repo.fn)``: hàm gọi ``repo.fn(await get_pool(), *args, **kwargs)``
    có with_reconnect (``idempotent=False``: lệnh có thể đã commit thì không chạy lại).
    Hàm repository và ``get_pool`` được tra theo tên LÚC GỌI - test thay ``db.get_pool``
    hay hàm trong core/repositories vẫn có hiệu lực như với wrapper viết tay. ``post`` đổi
    kết quả (vd Record -> dict). Chữ ký (pyright) lấy từ hàm repository, bỏ tham số pool."""
    module, name = sys.modules[fn.__module__], fn.__name__

    async def bound(*args, **kwargs):
        result = await getattr(module, name)(await get_pool(), *args, **kwargs)
        return result if post is None else post(result)

    bound.__name__ = bound.__qualname__ = name
    bound.__doc__ = fn.__doc__
    return _with_reconnect(bound, idempotent=idempotent)


def _dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


def _dict_or_none(row) -> dict | None:
    return dict(row) if row else None


@_with_reconnect
async def ensure_migrations() -> None:
    """Apply all pending versioned schema migrations."""
    from core import migrations

    pool = await get_pool()
    if await migrations.run(pool):
        # Schema vừa đổi: thay mọi kết nối (lúc được mượn/trả lại) để không kết nối nào
        # còn giữ prepared statement soạn theo schema cũ.
        expire = getattr(pool, "expire_connections", None)
        if expire is not None:
            await expire()


@_with_reconnect
async def init_db() -> None:
    await ensure_migrations()
    # Dọn dữ liệu là việc vận hành, không phải migration schema.
    await _prompts.prune_all(await get_pool())


# ─── Lịch sử lệnh /history: prompts + results (core/repositories/prompts.py) ─
save_prompt = _bind(_prompts.save, idempotent=False)
save_result = _bind(_prompts.save_result, idempotent=False)
get_history = _bind(_prompts.history)


# ─── Số liệu lượt gọi / provider / truy vết lượt (migrations/015, 021, 025) ──
# SQL ở core/repositories/{observability,quota,feedback}.py. INSERT số liệu không
# idempotent; ghi đánh giá 👍/👎 là UPSERT theo (người, prompt) nên thử lại an toàn.
usage_by_user = _bind(_observability.usage_by_user)
usage_by_model = _bind(_observability.usage_by_model)
provider_outcomes = _bind(_observability.provider_outcomes)
record_provider_call = _bind(_observability.insert_provider_call, idempotent=False)
provider_latency_summary = _bind(_observability.latency_by_provider)
record_turn_metrics = _bind(_observability.insert_turn_metrics, idempotent=False)
recent_turn_metrics = _bind(_observability.recent_turns)
turn_stage_summary = _bind(_observability.turn_stage_summary)
provider_calls_for_turn = _bind(_observability.provider_calls_for_turn)
provider_burn_since = _bind(_observability.burn_since)
user_calls_timeline = _bind(_observability.calls_timeline)

# Quota ngày thành viên Zalo (services/usage_quota.py, migrations/025).
user_calls_since = _bind(_quota.count_since)
user_calls_by_user_since = _bind(_quota.counts_by_user_since)
record_user_call = _bind(_quota.insert, idempotent=False)
# Không idempotent: UPDATE chọn lượt from_kind MỚI NHẤT, chạy lại sau khi đã commit sẽ
# đổi luôn lượt trước đó (lượt nặng +2) - chỉ retry khi lệnh chưa gửi đi.
relabel_last_user_call = _bind(_quota.relabel_latest, idempotent=False)

# Đánh giá câu trả lời (services/answer_feedback.py, migrations/021).
answer_feedback_upsert = _bind(_feedback.upsert)
answer_feedback_turn = _bind(_feedback.turn_snapshot)
answer_feedback_last_chat = _bind(_feedback.last_chat_snapshot)
answer_feedback_stats = _bind(_feedback.stats)
answer_feedback_recent_down = _bind(_feedback.recent_down)
answer_feedback_owner_down = _bind(_feedback.owner_down_for_evals)
answer_feedback_clear = _bind(_feedback.clear)


# ─── Lịch sử chat ngắn hạn: chat_messages (core/repositories/chat_history.py) ─
# Giữ tối đa N tin nhắn gần nhất / user (prompt chỉ cần CHAT_HISTORY_TURNS*2 dòng;
# phần dư là bộ đệm cho trích trí nhớ theo lô và tool search_history). Mặc định 1000,
# env CHAT_MESSAGES_RETENTION_LIMIT (kẹp 200-2000). Hàm ghi đọc hằng này LÚC GỌI.
CHAT_MESSAGES_RETENTION_LIMIT = config.CHAT_MESSAGES_RETENTION_LIMIT


@_with_reconnect(idempotent=False)
async def add_chat_message(telegram_user_id: int, role: str, content: str) -> None:
    await _chat_history.add_message(
        await get_pool(), telegram_user_id, role, content, retention=CHAT_MESSAGES_RETENTION_LIMIT
    )


@_with_reconnect(idempotent=False)
async def add_chat_turn(
    telegram_user_id: int, user_text: str, model_text: str, *, kind: str = "chat"
) -> list[int]:
    """Lưu CẢ lượt (tin người dùng + câu trả lời) trong 1 transaction - không
    bao giờ còn nửa lượt (chỉ có câu hỏi) khi DB lỗi giữa 2 lệnh INSERT.
    ``kind``: loại lượt (services/conversation_log.py) - trích nhớ chỉ đọc lượt
    chat/rag/agent, lượt "stock" giúp chặn câu chat sau mâu thuẫn policy. Trả
    id 2 dòng vừa lưu (🔄/✂️ xoá đúng cặp này sau khi làm lại)."""
    return await _chat_history.add_turn(
        await get_pool(),
        telegram_user_id,
        user_text,
        model_text,
        retention=CHAT_MESSAGES_RETENTION_LIMIT,
        kind=kind,
    )


@_with_reconnect
async def search_chat_history(
    telegram_user_id: int, terms: list[str], *, scan_limit: int = 300, max_chars: int = 600
) -> list[dict]:
    """Tìm trong lịch sử chat CỦA CHÍNH người này (tool search_history): dòng
    chứa ít nhất 1 từ trong ``terms`` (đã bỏ dấu, chữ thường; từ không phải
    a-z0-9 bị bỏ - không có ký tự đại diện LIKE), kèm câu cùng lượt; mới -> cũ."""
    patterns = [f"%{term}%" for term in terms if term.isascii() and term.isalnum()]
    if not patterns:
        return []
    rows = await _chat_history.search(
        await get_pool(), telegram_user_id, patterns, scan_limit=scan_limit, max_chars=max_chars
    )
    return [dict(row) for row in rows]


@_with_reconnect
async def get_session_messages(
    telegram_user_id: int, k: int, session_timeout_sec: int
) -> list[tuple[str, str]]:
    """Trả về tối đa `k` lượt gần nhất (role, content) THEO THỨ TỰ CŨ -> MỚI,
    chỉ trong phạm vi "phiên hiện tại": nếu khoảng nghỉ giữa 2 tin nhắn liên
    tiếp (hoặc giữa "bây giờ" và tin gần nhất) > session_timeout_sec, coi như
    phiên cũ đã kết thúc và KHÔNG lấy các tin trước điểm nghỉ đó."""
    if k <= 0:
        return []
    rows = await _chat_history.session_messages(
        await get_pool(), telegram_user_id, k, session_timeout_sec
    )
    return [(r["role"], r["content"]) for r in rows]


delete_chat_rows = _bind(_chat_history.delete_rows)
clear_chat = _bind(_chat_history.clear)
# Cửa sổ chưa trích nhớ (id > after_id, cũ -> mới) / lượt gần nhất (mới -> cũ).
get_chat_rows_after = _bind(_memory.chat_rows_after, post=_dicts)
get_recent_chat_rows = _bind(_memory.recent_chat_rows, post=_dicts)
get_last_model_row = _bind(_memory.last_model_row, post=_dict_or_none)
users_with_chat_since = _bind(_memory.users_with_chat_after)


# ─── Trí nhớ dài hạn: user_facts + highlights + ghi chú phiên ─────────────────
# (xem services/memory_service.py; SQL ở core/repositories/memory.py). Từ
# migrations/017: fact có ghim/độ quan trọng/nguồn, giá trị cũ được vô hiệu hoá
# (invalid_at) thay vì xoá - mọi hàm đọc "fact" dưới đây chỉ trả dòng hiện hành.

# Trần giá trị cũ giữ lại (cho câu hỏi "trước đây..."): theo từng key và tổng.
SUPERSEDED_FACTS_PER_KEY = 3
SUPERSEDED_FACTS_TOTAL = 60


@_with_reconnect
async def upsert_fact(telegram_user_id: int, key: str, value: str) -> None:
    """Tương thích cũ: ghi fact tự động (nguồn 'auto', không ghim)."""
    await save_fact(telegram_user_id, key, value)


@_with_reconnect
async def save_fact(
    telegram_user_id: int,
    key: str,
    value: str,
    *,
    importance: int = 2,
    category: str = "",
    source: str = "auto",
    pinned: bool = False,
    protect: bool = True,
) -> str:
    """Thêm/cập nhật fact (giá trị đổi -> bản cũ vô hiệu hoá, giữ làm lịch sử).
    Trả "added" | "updated" | "unchanged" | "protected" (protect=True và fact
    đang ghim/do người dùng tự nhập). Chạy lại sau lỗi kết nối an toàn: lần 2
    thấy đúng giá trị đã ghi -> "unchanged"."""
    from core.repositories import memory

    return await memory.save_fact(
        await get_pool(),
        telegram_user_id,
        key,
        value,
        importance=importance,
        category=category,
        source=source,
        pinned=pinned,
        protect=protect,
    )


@_with_reconnect
async def get_facts(telegram_user_id: int) -> list[tuple[str, str]]:
    """[(key, value), ...] fact HIỆN HÀNH theo thứ tự ưu tiên (ghim > quan trọng >
    mới cập nhật)."""
    from core.repositories import memory

    rows = await memory.current_facts(await get_pool(), telegram_user_id)
    return [(r["key"], r["value"]) for r in rows]


@_with_reconnect
async def get_fact_rows(telegram_user_id: int) -> list[dict]:
    """Fact hiện hành kèm id/ghim/độ quan trọng/nhóm/nguồn/mốc thời gian."""
    from core.repositories import memory

    return [dict(r) for r in await memory.current_facts(await get_pool(), telegram_user_id)]


@_with_reconnect
async def get_superseded_facts(telegram_user_id: int, limit: int = 20) -> list[dict]:
    from core.repositories import memory

    rows = await memory.superseded_facts(await get_pool(), telegram_user_id, limit)
    return [dict(r) for r in rows]


@_with_reconnect
async def invalidate_fact(
    telegram_user_id: int, fact_id: int, *, protect: bool = True
) -> str | None:
    """Vô hiệu hoá 1 fact theo id (giữ lịch sử); trả key hoặc None."""
    from core.repositories import memory

    return await memory.invalidate_fact(
        await get_pool(), telegram_user_id, fact_id, protect=protect
    )


@_with_reconnect
async def delete_fact(telegram_user_id: int, key: str) -> None:
    """XOÁ HẲN mọi phiên bản của 1 key (người dùng chủ động bảo quên)."""
    from core.repositories import memory

    await memory.forget_key(await get_pool(), telegram_user_id, key)


@_with_reconnect
async def set_fact_pinned(telegram_user_id: int, fact_id: int, pinned: bool) -> bool:
    from core.repositories import memory

    return await memory.set_pinned(await get_pool(), telegram_user_id, fact_id, pinned)


@_with_reconnect
async def trim_facts(
    telegram_user_id: int,
    keep_n: int,
    *,
    superseded_per_key: int = SUPERSEDED_FACTS_PER_KEY,
    superseded_total: int = SUPERSEDED_FACTS_TOTAL,
) -> None:
    """Giữ tối đa ``keep_n`` fact hiện hành theo thứ tự ưu tiên (fact ghim không
    bị cắt), kèm trần riêng cho giá trị cũ - chặn user_facts phình vô hạn."""
    from core.repositories import memory

    await memory.trim_facts(
        await get_pool(),
        telegram_user_id,
        keep_current=keep_n,
        keep_superseded_per_key=superseded_per_key,
        keep_superseded_total=superseded_total,
    )


@_with_reconnect
async def clear_facts(telegram_user_id: int) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM user_facts WHERE telegram_user_id = $1", telegram_user_id)


@_with_reconnect(idempotent=False)
async def add_highlight(telegram_user_id: int, content: str, keep_n: int) -> None:
    """Thêm 1 dòng "ý quan trọng" mới, rồi cắt về `keep_n` dòng mới nhất/user
    (dòng cũ nhất tự rơi ra) - xem services/memory_service.py."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO user_memory_highlights (telegram_user_id, content) VALUES ($1, $2)",
                telegram_user_id,
                content,
            )
            await conn.execute(
                """
                DELETE FROM user_memory_highlights
                WHERE telegram_user_id = $1
                  AND id NOT IN (
                      SELECT id FROM user_memory_highlights
                      WHERE telegram_user_id = $1
                      ORDER BY created_at DESC
                      LIMIT $2
                  )
                """,
                telegram_user_id,
                keep_n,
            )


@_with_reconnect
async def get_highlights(telegram_user_id: int, limit: int) -> list[str]:
    """Trả về nội dung, CŨ -> MỚI (thứ tự đọc tự nhiên khi chèn vào prompt)."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT content FROM user_memory_highlights WHERE telegram_user_id = $1 "
        "ORDER BY created_at DESC LIMIT $2",
        telegram_user_id,
        limit,
    )
    return [r["content"] for r in reversed(rows)]


@_with_reconnect
async def get_highlight_rows(telegram_user_id: int, limit: int) -> list[dict]:
    """(id, content, created_at) mới nhất trước - /memory đánh số, /quen xoá theo id."""
    from core.repositories import memory

    rows = await memory.highlight_rows(await get_pool(), telegram_user_id, limit)
    return [dict(r) for r in rows]


@_with_reconnect
async def delete_highlights(telegram_user_id: int, ids: list[int]) -> int:
    from core.repositories import memory

    return await memory.delete_highlights(await get_pool(), telegram_user_id, ids)


@_with_reconnect
async def clear_highlights(telegram_user_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM user_memory_highlights WHERE telegram_user_id = $1", telegram_user_id
    )


@_with_reconnect
async def get_summary(telegram_user_id: int) -> str:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT summary FROM user_memory_summary WHERE telegram_user_id = $1",
        telegram_user_id,
    )
    return row["summary"] if row else ""


@_with_reconnect
async def set_summary(telegram_user_id: int, summary: str) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO user_memory_summary (telegram_user_id, summary, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (telegram_user_id)
        DO UPDATE SET summary = EXCLUDED.summary, updated_at = now()
        """,
        telegram_user_id,
        summary,
    )


# Ghi chú theo phiên: tối đa N phiên gần nhất / người (cắt khi ghi).
SESSION_NOTES_RETENTION = 100


@_with_reconnect
async def upsert_session_note(
    telegram_user_id: int,
    session_key: int,
    *,
    title: str = "",
    bullets: str = "",
    summary: str = "",
    started_at: datetime,
) -> None:
    from core.repositories import memory

    await memory.upsert_session_note(
        await get_pool(),
        telegram_user_id,
        session_key,
        title=title,
        bullets=bullets,
        summary=summary,
        started_at=started_at,
        keep_n=SESSION_NOTES_RETENTION,
    )


@_with_reconnect
async def get_session_notes(
    telegram_user_id: int, limit: int = 10, *, exclude_session_key: int | None = None
) -> list[dict]:
    from core.repositories import memory

    rows = await memory.session_notes(
        await get_pool(), telegram_user_id, limit, exclude_session_key=exclude_session_key
    )
    return [dict(r) for r in rows]


@_with_reconnect
async def get_session_note(telegram_user_id: int, session_key: int) -> dict | None:
    from core.repositories import memory

    row = await memory.session_note(await get_pool(), telegram_user_id, session_key)
    return dict(row) if row else None


@_with_reconnect
async def rewrite_session_note(
    telegram_user_id: int, session_key: int, *, title: str, bullets: str, summary: str
) -> None:
    """Ghi đè (kể cả rỗng) ghi chú 1 phiên; cả 3 trường rỗng thì xoá phiên đó."""
    from core.repositories import memory

    await memory.rewrite_session_note(
        await get_pool(),
        telegram_user_id,
        session_key,
        title=title,
        bullets=bullets,
        summary=summary,
    )


@_with_reconnect
async def search_session_notes(
    telegram_user_id: int, query: str = "", *, limit: int = 20, offset: int = 0
) -> tuple[int, list[dict]]:
    """Trang /admin "Nhật ký": (tổng số khớp, 1 trang ghi chú phiên)."""
    from core.repositories import memory

    total, rows = await memory.search_session_notes(
        await get_pool(), telegram_user_id, query, limit, offset
    )
    return total, [dict(r) for r in rows]


@_with_reconnect
async def get_summary_row(telegram_user_id: int) -> dict | None:
    """Tóm tắt dài hạn kèm thời điểm cập nhật (trang /admin "Nhật ký")."""
    from core.repositories import memory

    row = await memory.summary_row(await get_pool(), telegram_user_id)
    return dict(row) if row else None


@_with_reconnect
async def clear_session_notes(telegram_user_id: int) -> None:
    from core.repositories import memory

    await memory.clear_session_notes(await get_pool(), telegram_user_id)


# ─── Function calling: notes + reminders (xem services/tools.py, scheduler.py) ──────


@_with_reconnect(idempotent=False)
async def add_note(telegram_user_id: int, content: str, *, event_key: str | None = None) -> None:
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO notes (telegram_user_id, content, event_key) VALUES ($1, $2, $3)
        ON CONFLICT (event_key) DO NOTHING""",
        telegram_user_id,
        content,
        event_key,
    )


@_with_reconnect
async def get_notes(telegram_user_id: int, limit: int = 10) -> list[tuple[str, datetime]]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT content, created_at FROM notes
        WHERE telegram_user_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        telegram_user_id,
        limit,
    )
    return [(r["content"], r["created_at"]) for r in rows]


@_with_reconnect
async def get_recent_notes(
    telegram_user_id: int, limit: int = 200, max_chars: int = 400
) -> list[dict]:
    """(id, content, created_at) mới nhất trước, có trần - tìm/xoá ghi chú."""
    from core.repositories import memory

    rows = await memory.recent_notes(await get_pool(), telegram_user_id, limit, max_chars)
    return [dict(r) for r in rows]


@_with_reconnect
async def delete_note(telegram_user_id: int, note_id: int) -> bool:
    from core.repositories import memory

    return await memory.delete_note(await get_pool(), telegram_user_id, note_id)


@_with_reconnect
async def clear_notes(telegram_user_id: int) -> int:
    from core.repositories import memory

    return await memory.clear_notes(await get_pool(), telegram_user_id)


# ─── Sự kiện hỏi thăm chủ động (migrations/018, services/proactive_service.py) ──

USER_EVENTS_PER_USER = 50


@_with_reconnect
async def add_user_event(telegram_user_id: int, title: str, when_text: str, event_date) -> bool:
    """Thêm sự kiện (trùng người + ngày + tiêu đề thì bỏ qua - chạy lại an toàn)."""
    from core.repositories import memory

    return await memory.add_event(
        await get_pool(),
        telegram_user_id,
        title,
        when_text,
        event_date,
        keep_n=USER_EVENTS_PER_USER,
    )


@_with_reconnect
async def expire_user_events(before) -> int:
    from core.repositories import memory

    return await memory.expire_events(await get_pool(), before)


@_with_reconnect
async def users_with_due_events(on_or_before, limit: int = 20) -> list[int]:
    from core.repositories import memory

    return await memory.users_with_due_events(await get_pool(), on_or_before, limit)


@_with_reconnect(idempotent=False)
async def claim_due_user_event(telegram_user_id: int, on_or_before) -> dict | None:
    """Claim 1 sự kiện tới hạn (pending -> asked). Không retry sau khi mất kết nối
    giữa chừng: lần 2 sẽ claim THÊM 1 sự kiện khác."""
    from core.repositories import memory

    row = await memory.claim_due_event(await get_pool(), telegram_user_id, on_or_before)
    return dict(row) if row else None


@_with_reconnect
async def release_user_event(event_id: int) -> None:
    from core.repositories import memory

    await memory.release_event(await get_pool(), event_id)


@_with_reconnect
async def mark_user_event_asked(event_id: int) -> None:
    from core.repositories import memory

    await memory.mark_event_asked(await get_pool(), event_id)


@_with_reconnect
async def finish_user_events(telegram_user_id: int) -> int:
    from core.repositories import memory

    return await memory.finish_events(await get_pool(), telegram_user_id)


@_with_reconnect
async def finish_user_event_ids(telegram_user_id: int, ids: list[int]) -> int:
    """Sự kiện còn chờ trong ``ids`` -> done (lặp lại an toàn)."""
    from core.repositories import memory

    return await memory.finish_event_ids(await get_pool(), telegram_user_id, ids)


@_with_reconnect
async def list_user_events(telegram_user_id: int, limit: int = 20) -> list[dict]:
    from core.repositories import memory

    return [dict(r) for r in await memory.list_events(await get_pool(), telegram_user_id, limit)]


@_with_reconnect
async def user_events_on(telegram_user_id: int, day, limit: int = 5) -> list[dict]:
    from core.repositories import memory

    rows = await memory.events_on(await get_pool(), telegram_user_id, day, limit)
    return [dict(r) for r in rows]


@_with_reconnect
async def clear_user_events(telegram_user_id: int) -> None:
    from core.repositories import memory

    await memory.clear_events(await get_pool(), telegram_user_id)


@_with_reconnect(idempotent=False)
async def add_reminder(
    telegram_user_id: int,
    message: str,
    due_at,
    *,
    channel: str = "telegram",
    recipient_id: str | None = None,
    account_id: str = "",
    user_jid: str = "",
    event_key: str | None = None,
):
    if channel not in {"telegram", "zalo", "zoom"}:
        raise ValueError("Kênh nhắc việc không hợp lệ.")
    recipient_id = recipient_id or str(telegram_user_id)
    if channel == "zalo" and not account_id:
        raise ValueError("Thiếu tài khoản Zalo nhận nhắc việc.")
    pool = await get_pool()
    return await pool.fetchval(
        """INSERT INTO reminders
        (telegram_user_id, message, due_at, channel, recipient_id, account_id, user_jid, event_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (event_key) DO UPDATE SET event_key = EXCLUDED.event_key
        RETURNING due_at""",
        telegram_user_id,
        message,
        due_at,
        channel,
        recipient_id,
        account_id,
        user_jid,
        event_key,
    )


@_with_reconnect
async def mark_reminder_sent(reminder_id: int) -> None:
    pool = await get_pool()
    # fired_at: lúc nhắc thật - "hoãn 10p"/"xong" trong 30 phút tìm theo cột này.
    await pool.execute(
        "UPDATE reminders SET sent = true, fired_at = COALESCE(fired_at, now()) WHERE id = $1",
        reminder_id,
    )


# ─── Nhắc việc v2: lặp lại, ngày nhớ, huỷ/hoãn (services/reminder_service.py) ──
# SQL ở core/repositories/reminders.py. Tạo chuỗi INSERT nhiều dòng trong 1
# transaction -> idempotent=False; chèn lần kế tiếp dùng ON CONFLICT nên thử lại an toàn.


@_with_reconnect
async def reminder_count_pending_once(telegram_user_id: int) -> int:
    from core.repositories import reminders

    return await reminders.count_pending_once(await get_pool(), telegram_user_id)


@_with_reconnect
async def reminder_count_series(telegram_user_id: int) -> int:
    from core.repositories import reminders

    return await reminders.count_series(await get_pool(), telegram_user_id)


@_with_reconnect(idempotent=False)
async def reminder_create_series(telegram_user_id: int, message: str, **kw) -> tuple[int, bool]:
    from core.repositories import reminders

    return await reminders.create_series(await get_pool(), telegram_user_id, message, **kw)


@_with_reconnect
async def reminder_get_series(series_id: int) -> dict | None:
    from core.repositories import reminders

    return await reminders.get_series(await get_pool(), series_id)


@_with_reconnect
async def reminder_add_occurrence(series: dict, due_at, message: str) -> bool:
    from core.repositories import reminders

    return await reminders.add_occurrence(await get_pool(), series, due_at, message)


@_with_reconnect
async def reminder_list_pending_once(telegram_user_id: int, limit: int = 20) -> list[dict]:
    from core.repositories import reminders

    return await reminders.list_pending_once(await get_pool(), telegram_user_id, limit)


@_with_reconnect
async def reminder_list_series(
    telegram_user_id: int, kind: str = "reminder", limit: int = 50
) -> list[dict]:
    from core.repositories import reminders

    return await reminders.list_series(await get_pool(), telegram_user_id, kind, limit)


@_with_reconnect
async def reminder_delete(telegram_user_id: int, reminder_id: int) -> bool:
    from core.repositories import reminders

    return await reminders.delete_reminder(await get_pool(), telegram_user_id, reminder_id)


@_with_reconnect
async def reminder_delete_series(telegram_user_id: int, series_id: int) -> bool:
    from core.repositories import reminders

    return await reminders.delete_series(await get_pool(), telegram_user_id, series_id)


@_with_reconnect
async def reminder_reschedule(telegram_user_id: int, reminder_id: int, due_at) -> bool:
    from core.repositories import reminders

    return await reminders.reschedule(await get_pool(), telegram_user_id, reminder_id, due_at)


@_with_reconnect
async def reminder_last_fired(telegram_user_id: int, within) -> dict | None:
    from core.repositories import reminders

    return await reminders.last_fired(await get_pool(), telegram_user_id, within)


@_with_reconnect
async def reminder_get(telegram_user_id: int, reminder_id: int) -> dict | None:
    """1 nhắc việc của chính người đó (nút hoãn dưới tin nhắc Telegram)."""
    from core.repositories import reminders

    return await reminders.get_owned(await get_pool(), telegram_user_id, reminder_id)


@_with_reconnect
async def reminder_mark_done(reminder_id: int) -> None:
    from core.repositories import reminders

    await reminders.mark_done(await get_pool(), reminder_id)


@_with_reconnect
async def reminder_mark_fired(reminder_id: int) -> None:
    from core.repositories import reminders

    await reminders.mark_fired(await get_pool(), reminder_id)


# ─── Chi tiêu cá nhân (services/expense_service.py) ─────────────────────────
# SQL ở core/repositories/expenses.py; ngày truyền dạng text YYYY-MM-DD. Khoản chi
# không có event_key (lệnh ngoài webhook) INSERT không idempotent -> không retry.


@_with_reconnect(idempotent=False)
async def expense_add(telegram_user_id: int, amount_vnd: int, **kw) -> tuple[int, bool]:
    from core.repositories import expenses

    return await expenses.insert(await get_pool(), telegram_user_id, amount_vnd, **kw)


@_with_reconnect
async def expense_count_since(telegram_user_id: int, within) -> int:
    from core.repositories import expenses

    return await expenses.count_since(await get_pool(), telegram_user_id, within)


@_with_reconnect
async def expense_delete(telegram_user_id: int, expense_id: int) -> dict | None:
    from core.repositories import expenses

    return await expenses.delete(await get_pool(), telegram_user_id, expense_id)


@_with_reconnect
async def expense_last(telegram_user_id: int, within) -> dict | None:
    from core.repositories import expenses

    return await expenses.last_created(await get_pool(), telegram_user_id, within)


@_with_reconnect
async def expense_totals(telegram_user_id: int, start: str, end: str) -> list[dict]:
    from core.repositories import expenses

    return await expenses.totals(await get_pool(), telegram_user_id, start, end)


@_with_reconnect
async def expense_recent(telegram_user_id: int, limit: int = 10) -> list[dict]:
    from core.repositories import expenses

    return await expenses.recent(await get_pool(), telegram_user_id, limit)


# ─── Ảnh avatar (/admin/avatar, services/avatar_photo.py) ────────────────────
# SQL ở core/repositories/avatar.py; UPSERT/DELETE theo khoá kind nên chạy lại an toàn.


@_with_reconnect
async def avatar_photo_meta() -> dict[str, dict]:
    from core.repositories import avatar

    return await avatar.meta(await get_pool())


@_with_reconnect
async def avatar_photo_get(kind: str) -> dict | None:
    from core.repositories import avatar

    return await avatar.photo(await get_pool(), kind)


@_with_reconnect
async def avatar_photo_put(kind: str, **kw) -> None:
    from core.repositories import avatar

    await avatar.put(await get_pool(), kind, **kw)


@_with_reconnect
async def avatar_photo_delete(kind: str) -> int:
    from core.repositories import avatar

    return await avatar.delete(await get_pool(), kind)


# ─── Nhật ký ăn uống (services/nutrition_service.py) ────────────────────────
# SQL ở core/repositories/meals.py; ngày truyền dạng text YYYY-MM-DD. Món không có
# event_key (lệnh ngoài webhook) INSERT không idempotent -> không retry.


@_with_reconnect(idempotent=False)
async def meal_add(telegram_user_id: int, **kw) -> tuple[int, bool]:
    from core.repositories import meals

    return await meals.insert(await get_pool(), telegram_user_id, **kw)


@_with_reconnect
async def meal_count_since(telegram_user_id: int, within) -> int:
    from core.repositories import meals

    return await meals.count_since(await get_pool(), telegram_user_id, within)


@_with_reconnect
async def meal_day(telegram_user_id: int, day: str, limit: int = 40) -> list[dict]:
    from core.repositories import meals

    return await meals.day_rows(await get_pool(), telegram_user_id, day, limit)


@_with_reconnect
async def meal_day_totals(telegram_user_id: int, start: str, end: str) -> list[dict]:
    from core.repositories import meals

    return await meals.day_totals(await get_pool(), telegram_user_id, start, end)


@_with_reconnect
async def meal_delete(telegram_user_id: int, meal_id: int) -> dict | None:
    from core.repositories import meals

    return await meals.delete(await get_pool(), telegram_user_id, meal_id)


@_with_reconnect
async def meal_recent(telegram_user_id: int, limit: int = 200) -> list[dict]:
    from core.repositories import meals

    return await meals.recent(await get_pool(), telegram_user_id, limit)


@_with_reconnect
async def meal_clear(telegram_user_id: int) -> int:
    from core.repositories import meals

    return await meals.clear(await get_pool(), telegram_user_id)


@_with_reconnect
async def list_reminders_between(
    telegram_user_id: int, start: datetime, end: datetime, limit: int = 10
) -> list[tuple[datetime, str]]:
    """Nhắc việc chưa gửi đến hạn trong [start, end) - mục nhắc việc của bản tin sáng."""
    from core.repositories import brief as brief_repository

    return await brief_repository.list_reminders_between(
        await get_pool(), telegram_user_id, start, end, limit
    )


# ─── Settings (bảng key-value) ───────────────────────────────────────────────
# Mọi lần ghi qua set_setting/compare_and_set_setting báo cho các bộ đệm đã đăng ký
# (core/settings_cache.py) - khoá đọc qua cache không bao giờ cũ hơn những gì chính
# process này đã ghi. Ghi lỗi (không rõ đã commit chưa) thì báo value=None = bỏ khỏi cache.
_setting_listeners: list[Callable[[str, Optional[str]], None]] = []


def on_setting_write(listener: Callable[[str, Optional[str]], None]) -> None:
    if listener not in _setting_listeners:
        _setting_listeners.append(listener)


def _setting_written(key: str, value: Optional[str]) -> None:
    for listener in tuple(_setting_listeners):
        listener(key, value)


@_with_reconnect
async def get_setting(key: str) -> Optional[str]:
    from core.repositories import settings as settings_repository

    return await settings_repository.get(await get_pool(), key)


@_with_reconnect
async def list_settings_with_prefix(prefix: str, limit: int = 100) -> list[tuple[str, str]]:
    from core.repositories import brief as brief_repository

    return await brief_repository.list_settings_with_prefix(await get_pool(), prefix, limit)


@_with_reconnect
async def set_setting(key: str, value: str) -> None:
    from core.repositories import settings as settings_repository

    try:
        await settings_repository.set(await get_pool(), key, value)
    except BaseException:
        _setting_written(key, None)
        raise
    _setting_written(key, value)


@_with_reconnect
async def list_encrypted_settings(limit: int = 500) -> list[tuple[str, str]]:
    """Các dòng settings đã mã hoá (xoay khoá - services/key_rotation.py)."""
    from core.repositories import settings as settings_repository

    return await settings_repository.list_encrypted(await get_pool(), limit)


@_with_reconnect
async def compare_and_set_setting(key: str, expected: str, value: str) -> bool:
    from core.repositories import settings as settings_repository

    try:
        swapped = await settings_repository.compare_and_set(await get_pool(), key, expected, value)
    except BaseException:
        _setting_written(key, None)
        raise
    if swapped:
        _setting_written(key, value)
    return swapped


# ─── Danh mục cổ phiếu: vị thế, sổ giao dịch, theo dõi/cảnh báo giá ─────────
# SQL ở core/repositories/portfolio.py. Hàm ghi vị thế INSERT dòng sổ giao dịch
# trong cùng transaction -> idempotent=False (không retry khi lệnh có thể đã
# commit). Ngày truyền dạng text YYYY-MM-DD.


@_with_reconnect
async def portfolio_list_holdings(user_id: int):
    from core.repositories import portfolio

    return await portfolio.list_holdings(await get_pool(), user_id)


@_with_reconnect
async def portfolio_get_holding(user_id: int, symbol: str):
    from core.repositories import portfolio

    return await portfolio.get_holding(await get_pool(), user_id, symbol)


@_with_reconnect
async def portfolio_list_all_holdings(limit: int, *, after: tuple[int, str] | None = None):
    from core.repositories import portfolio

    return await portfolio.list_all_holdings(await get_pool(), limit, after=after)


@_with_reconnect(idempotent=False)
async def portfolio_replace_holding(
    user_id: int, symbol: str, quantity: float, average_price: float, **kw
):
    from core.repositories import portfolio

    return await portfolio.replace_holding(
        await get_pool(), user_id, symbol, quantity, average_price, **kw
    )


@_with_reconnect(idempotent=False)
async def portfolio_record_purchase(user_id: int, symbol: str, quantity: float, price: float, **kw):
    from core.repositories import portfolio

    return await portfolio.record_purchase(await get_pool(), user_id, symbol, quantity, price, **kw)


@_with_reconnect(idempotent=False)
async def portfolio_record_sale(
    user_id: int, symbol: str, quantity: float | None, price: float | None, **kw
):
    from core.repositories import portfolio

    return await portfolio.record_sale(await get_pool(), user_id, symbol, quantity, price, **kw)


@_with_reconnect(idempotent=False)
async def portfolio_update_alerts(user_id: int, symbol: str, **kw):
    from core.repositories import portfolio

    return await portfolio.update_alerts(await get_pool(), user_id, symbol, **kw)


@_with_reconnect(idempotent=False)
async def portfolio_rebase_holding(user_id: int, symbol: str, rebase):
    from core.repositories import portfolio

    return await portfolio.rebase_holding(await get_pool(), user_id, symbol, rebase)


@_with_reconnect(idempotent=False)
async def portfolio_delete_holding(user_id: int, symbol: str) -> bool:
    from core.repositories import portfolio

    return await portfolio.delete_holding(await get_pool(), user_id, symbol)


@_with_reconnect
async def portfolio_was_closed(user_id: int, symbol: str) -> bool:
    from core.repositories import portfolio

    return await portfolio.was_closed(await get_pool(), user_id, symbol)


@_with_reconnect
async def portfolio_list_transactions(user_id: int, *, symbol: str | None = None, limit: int = 15):
    from core.repositories import portfolio

    return await portfolio.list_transactions(await get_pool(), user_id, symbol=symbol, limit=limit)


@_with_reconnect
async def portfolio_transactions_between(user_id: int, start, end, *, limit: int = 2000):
    from core.repositories import portfolio

    return await portfolio.transactions_between(await get_pool(), user_id, start, end, limit=limit)


# Không idempotent: chạy lại sau khi đã commit sẽ điền giá cho lệnh bán CŨ HƠN.
@_with_reconnect(idempotent=False)
async def portfolio_fill_sale_price(user_id: int, symbol: str, price: float, **kw):
    from core.repositories import portfolio

    return await portfolio.fill_sale_price(await get_pool(), user_id, symbol, price, **kw)


@_with_reconnect
async def portfolio_watchlist(user_id: int) -> list[str]:
    from core.repositories import portfolio

    return await portfolio.watchlist(await get_pool(), user_id)


@_with_reconnect
async def portfolio_add_watch(user_id: int, symbols: list[str], max_symbols: int) -> list[str]:
    from core.repositories import portfolio

    return await portfolio.add_watch(await get_pool(), user_id, symbols, max_symbols)


@_with_reconnect
async def portfolio_remove_watch(user_id: int, symbol: str) -> tuple[bool, int]:
    from core.repositories import portfolio

    return await portfolio.remove_watch(await get_pool(), user_id, symbol)


@_with_reconnect
async def portfolio_list_price_alerts(user_id: int):
    from core.repositories import portfolio

    return await portfolio.list_price_alerts(await get_pool(), user_id)


@_with_reconnect
async def portfolio_add_price_alert(user_id: int, symbol: str, direction: str, level: float, **kw):
    from core.repositories import portfolio

    return await portfolio.add_price_alert(
        await get_pool(), user_id, symbol, direction, level, **kw
    )


@_with_reconnect
async def portfolio_delete_price_alerts(user_id: int, symbol: str, level: float | None) -> int:
    from core.repositories import portfolio

    return await portfolio.delete_price_alerts(await get_pool(), user_id, symbol, level)


@_with_reconnect
async def portfolio_active_price_alerts(limit: int):
    from core.repositories import portfolio

    return await portfolio.active_price_alerts(await get_pool(), limit)


@_with_reconnect
async def portfolio_set_price_alert_state(alert_id: int, state: str, *, notified: bool) -> None:
    from core.repositories import portfolio

    await portfolio.set_price_alert_state(await get_pool(), alert_id, state, notified=notified)


@_with_reconnect
async def portfolio_alert_logged(
    user_id: int, kind: str, ref_key: str, sent_on: str | None = None
) -> bool:
    from core.repositories import portfolio

    return await portfolio.alert_logged(await get_pool(), user_id, kind, ref_key, sent_on)


@_with_reconnect
async def portfolio_log_alert(user_id: int, kind: str, ref_key: str, sent_on: str) -> None:
    from core.repositories import portfolio

    await portfolio.log_alert(await get_pool(), user_id, kind, ref_key, sent_on)


@_with_reconnect
async def portfolio_count_alerts_on(user_id: int, sent_on: str) -> int:
    from core.repositories import portfolio

    return await portfolio.count_alerts_on(await get_pool(), user_id, sent_on)


@_with_reconnect
async def portfolio_prune_alert_log(keep_days: int, batch: int = 5000) -> int:
    from core.repositories import portfolio

    return await portfolio.prune_alert_log(await get_pool(), keep_days, batch)


# ─── Hàng đợi thông báo gom (migrations/026, services/notify_hub.py) ────────
# SQL nằm ở core/repositories/notify.py.


@_with_reconnect(idempotent=False)
async def notify_enqueue(
    channel: str,
    user_id: int,
    level: str,
    source: str,
    content: str,
    dedup_key: str | None,
    cap: int,
) -> tuple[int | None, int]:
    """Xếp 1 tin vào hàng đợi; (id mới hoặc None nếu trùng dedup_key, số tin cũ bị
    bỏ vì vượt trần). Không retry sau khi mất kết nối: tin không có dedup_key có thể
    đã được xếp ở lần đầu."""
    from core.repositories import notify

    return await notify.enqueue(
        await get_pool(), channel, user_id, level, source, content, dedup_key, cap
    )


@_with_reconnect
async def notify_pending_recipients(priority_sources: list[str], limit: int = 100) -> list[dict]:
    from core.repositories import notify

    return await notify.pending_recipients(await get_pool(), limit, priority_sources)


@_with_reconnect(idempotent=False)
async def notify_claim(channel: str, user_id: int, limit: int, lease_sec: int) -> list[dict]:
    from core.repositories import notify

    return await notify.claim(await get_pool(), channel, user_id, limit, lease_sec)


@_with_reconnect
async def notify_mark_sent(ids: list[int]) -> None:
    from core.repositories import notify

    await notify.mark_sent(await get_pool(), ids)


@_with_reconnect
async def notify_release(ids: list[int]) -> None:
    from core.repositories import notify

    await notify.release(await get_pool(), ids)


@_with_reconnect(idempotent=False)
async def notify_take_dropped(channel: str, user_id: int) -> int:
    from core.repositories import notify

    return await notify.take_dropped(await get_pool(), channel, user_id)


@_with_reconnect(idempotent=False)
async def notify_flush_zalo(
    user_id: int, limit: int, account_id: str, recipient_id: str, compose
) -> int:
    from core.repositories import notify

    return await notify.flush_zalo(
        await get_pool(), user_id, limit, account_id, recipient_id, compose
    )


@_with_reconnect
async def notify_drop_pending(channel: str, user_id: int) -> int:
    from core.repositories import notify

    return await notify.drop_pending(await get_pool(), channel, user_id)


@_with_reconnect
async def notify_count_pending(user_id: int) -> int:
    from core.repositories import notify

    return await notify.count_pending(await get_pool(), user_id)


@_with_reconnect
async def notify_clear_user(user_id: int) -> None:
    from core.repositories import notify

    await notify.clear_user(await get_pool(), user_id)


# ─── Việc nền + tác vụ định kỳ (migrations/027, services/background_jobs.py,
# services/routines.py). SQL ở core/repositories/jobs.py.


@_with_reconnect(idempotent=False)
async def bg_job_insert(
    user_id: int, channel: str, prompt: str, *, user_cap: int, global_cap: int
) -> tuple[int | None, str]:
    from core.repositories import jobs

    return await jobs.insert_job(
        await get_pool(), user_id, channel, prompt, user_cap=user_cap, global_cap=global_cap
    )


@_with_reconnect(idempotent=False)
async def bg_job_claim(lease_sec: int) -> dict | None:
    from core.repositories import jobs

    return await jobs.claim_next(await get_pool(), lease_sec)


@_with_reconnect
async def bg_job_renew(job_id: int, lease_sec: int) -> bool:
    from core.repositories import jobs

    return await jobs.renew_lease(await get_pool(), job_id, lease_sec)


@_with_reconnect
async def bg_job_finish(job_id: int, status: str, **fields) -> bool:
    from core.repositories import jobs

    return await jobs.finish(await get_pool(), job_id, status, **fields)


@_with_reconnect(idempotent=False)
async def bg_job_recover(max_attempts: int) -> list[dict]:
    from core.repositories import jobs

    return await jobs.recover_stale(await get_pool(), max_attempts)


@_with_reconnect
async def bg_job_cancel(user_id: int, job_id: int) -> str | None:
    from core.repositories import jobs

    return await jobs.cancel(await get_pool(), user_id, job_id)


@_with_reconnect
async def bg_job_get(user_id: int, job_id: int) -> dict | None:
    from core.repositories import jobs

    return await jobs.get_job(await get_pool(), user_id, job_id)


@_with_reconnect
async def bg_job_list(user_id: int, limit: int = 10) -> list[dict]:
    from core.repositories import jobs

    return await jobs.list_jobs(await get_pool(), user_id, limit)


@_with_reconnect
async def bg_job_active(user_id: int) -> list[dict]:
    from core.repositories import jobs

    return await jobs.active_jobs(await get_pool(), user_id)


@_with_reconnect
async def bg_job_position(job_id: int) -> int:
    from core.repositories import jobs

    return await jobs.queue_position(await get_pool(), job_id)


@_with_reconnect
async def bg_job_recent_all(limit: int = 30) -> list[dict]:
    from core.repositories import jobs

    return await jobs.recent_all(await get_pool(), limit)


@_with_reconnect
async def bg_job_clear_user(user_id: int) -> None:
    from core.repositories import jobs

    await jobs.clear_user(await get_pool(), user_id)


@_with_reconnect
async def routine_count(user_id: int) -> int:
    from core.repositories import jobs

    return await jobs.count_routines(await get_pool(), user_id)


@_with_reconnect(idempotent=False)
async def routine_insert(
    user_id: int, channel: str, prompt: str, rule: dict, next_run_at: datetime
) -> int:
    from core.repositories import jobs

    return await jobs.insert_routine(await get_pool(), user_id, channel, prompt, rule, next_run_at)


@_with_reconnect
async def routine_list(user_id: int) -> list[dict]:
    from core.repositories import jobs

    return await jobs.list_routines(await get_pool(), user_id)


@_with_reconnect
async def routine_all(limit: int = 50) -> list[dict]:
    from core.repositories import jobs

    return await jobs.all_routines(await get_pool(), limit)


@_with_reconnect
async def routine_get(user_id: int, routine_id: int) -> dict | None:
    from core.repositories import jobs

    return await jobs.get_routine(await get_pool(), user_id, routine_id)


@_with_reconnect
async def routine_delete(user_id: int, routine_id: int) -> bool:
    from core.repositories import jobs

    return await jobs.delete_routine(await get_pool(), user_id, routine_id)


@_with_reconnect
async def routine_set_status(
    user_id: int, routine_id: int, status: str, next_run_at: datetime | None = None
) -> bool:
    from core.repositories import jobs

    return await jobs.set_routine_status(await get_pool(), user_id, routine_id, status, next_run_at)


@_with_reconnect
async def routine_due(now: datetime, limit: int = 20) -> list[dict]:
    from core.repositories import jobs

    return await jobs.due_routines(await get_pool(), now, limit)


# Idempotent nhờ điều kiện next_run_at = giá trị cũ + dedup_key của bg_jobs.
@_with_reconnect
async def routine_fire(
    routine: dict, scheduled: datetime, next_run_at: datetime | None, *, enqueue: bool
) -> int | None:
    from core.repositories import jobs

    return await jobs.fire_routine(
        await get_pool(), routine, scheduled, next_run_at, enqueue=enqueue
    )


@_with_reconnect
async def routine_runs_since(since: datetime) -> int:
    from core.repositories import jobs

    return await jobs.routine_runs_since(await get_pool(), since)


@_with_reconnect
async def routine_jobs_since(user_id: int, since: datetime) -> list[dict]:
    from core.repositories import jobs

    return await jobs.routine_jobs_since(await get_pool(), user_id, since)


@_with_reconnect
async def portfolio_alert_log_since(user_id: int, since: str) -> list[tuple[str, str, str]]:
    """(kind, ref_key, ngày) nhật ký thông báo danh mục từ ngày ``since``."""
    from core.repositories import portfolio

    return await portfolio.alert_log_since(await get_pool(), user_id, since)


# ─── Giám sát vận hành / sao lưu (services/watchdog.py, backup_status.py) ───
# SQL nằm ở core/repositories/ops.py; đây chỉ là wrapper có retry kết nối.


@_with_reconnect
async def database_bytes() -> int:
    from core.repositories import ops

    return await ops.database_bytes(await get_pool())


@_with_reconnect
async def webhook_backlog(stale_after) -> tuple[int, float | None]:
    from core.repositories import ops

    return await ops.webhook_backlog(await get_pool(), stale_after)


@_with_reconnect
async def latest_backup_run(kind: str = "backup") -> dict | None:
    from core.repositories import ops

    return await ops.latest_backup_run(await get_pool(), kind)


# ─── Zoom Team Chat pairing (bot 1 chủ, dùng chung ALLOWED_USER_ID) ─────────
# Zoom không có khái niệm "user Telegram" - chỉ có ĐÚNG 1 jid Zoom được phép
# nói chuyện với bot (như ALLOWED_USER_ID của Telegram). Lưu qua bảng settings
# key-value sẵn có thay vì tạo bảng users/roles riêng như vietassist, vì kiến
# trúc Gemini là bot 1 chủ (ALLOWED_USER_ID), không phải multi-user/multi-role.
_SETTING_ZOOM_JID = "zoom_paired_jid"
_SETTING_ZOOM_NAME = "zoom_paired_name"


async def zoom_get_pairing() -> Optional[tuple[str, str]]:
    """Trả về (jid, display_name) đã pair, hoặc None nếu chưa pair ai. Đọc qua
    core.settings_cache: mỗi webhook Zoom/thông báo chủ động không còn 2 lượt đọc DB."""
    from core import settings_cache

    jid = await settings_cache.get(_SETTING_ZOOM_JID)
    if not jid:
        return None
    name = await settings_cache.get(_SETTING_ZOOM_NAME) or ""
    return jid, name


async def zoom_set_pairing(jid: str, display_name: str = "") -> None:
    from core import settings_cache

    await settings_cache.set(_SETTING_ZOOM_JID, jid)
    await settings_cache.set(_SETTING_ZOOM_NAME, display_name)


async def zoom_clear_pairing() -> None:
    from core import settings_cache

    await settings_cache.set(_SETTING_ZOOM_JID, "")
    await settings_cache.set(_SETTING_ZOOM_NAME, "")


# ─── Thiết bị app màn hình thông tin (/app, services/app_devices.py) ─────────
# SQL ở core/repositories/app_devices.py; chỉ nhận HASH của mã ghép/token.


# Không idempotent: chạy lại sau khi đã commit sẽ thêm 1 dòng chờ ghép thứ hai.
@_with_reconnect(idempotent=False)
async def app_device_create_pending(
    user_id: int, name: str, code_hash: str, ttl_sec: int, max_devices: int
) -> int | None:
    from core.repositories import app_devices

    return await app_devices.create_pending(
        await get_pool(), user_id, name, code_hash, ttl_sec, max_devices
    )


# Không idempotent: chạy lại sau khi đã commit thì mã ghép đã bị xoá -> báo sai "mã hết hạn".
@_with_reconnect(idempotent=False)
async def app_device_pair(code_hash: str, token_hash: str) -> dict | None:
    from core.repositories import app_devices

    return await app_devices.pair(await get_pool(), code_hash, token_hash)


@_with_reconnect
async def app_device_by_token(token_hash: str) -> dict | None:
    from core.repositories import app_devices

    return await app_devices.by_token(await get_pool(), token_hash)


@_with_reconnect
async def app_device_list(user_id: int) -> list[dict]:
    from core.repositories import app_devices

    return await app_devices.list_for_user(await get_pool(), user_id)


@_with_reconnect
async def app_device_revoke(user_id: int, device_id: int) -> bool:
    from core.repositories import app_devices

    return await app_devices.revoke(await get_pool(), user_id, device_id)
