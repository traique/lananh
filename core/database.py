"""Supabase Postgres - bền hơn SQLite vì ổ đĩa local trên Render free tier là ephemeral."""

import asyncio
import contextvars
import functools
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from core import config

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.pool.Pool] = None
_pool_lock = asyncio.Lock()

_CONNECTION_ERRORS = (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, OSError)


async def _reset_pool(failed_pool: Optional[asyncpg.pool.Pool]) -> None:
    """Đóng pool bị lỗi rồi bỏ nó đi, ĐỂ get_pool() tạo pool mới ở lần gọi
    kế tiếp. Chỉ đóng nếu `_pool` hiện tại VẪN CÒN LÀ `failed_pool` - nếu 2
    coroutine cùng gặp lỗi kết nối song song, coroutine chạy sau (pool nó
    thấy bị lỗi đã cũ) sẽ không được phép đóng pool MỚI mà coroutine chạy
    trước vừa tạo xong (race condition "domino" phá pool đang sống)."""
    global _pool
    if failed_pool is None:
        return
    async with _pool_lock:
        if _pool is not failed_pool:
            return
        if _pool is not None:
            try:
                await _pool.close()
            except Exception:
                pass
            _pool = None


# Pool mà lượt gọi DB hiện tại THỰC SỰ dùng (get_pool() ghi lại). Nhờ vậy
# _with_reconnect chỉ reset đúng pool vừa lỗi, không đóng nhầm pool MỚI mà
# một coroutine khác vừa tạo (bản cũ đọc biến global `_pool` lúc bắt lỗi nên
# cơ chế chống race của _reset_pool bị vô hiệu).
_pool_in_use: contextvars.ContextVar[Optional[asyncpg.pool.Pool]] = contextvars.ContextVar(
    "_pool_in_use", default=None
)


def _safe_to_retry(exc: BaseException, idempotent: bool) -> bool:
    """Câu lệnh idempotent (SELECT/UPSERT/DELETE theo khoá) luôn retry được.

    Câu INSERT không idempotent chỉ retry khi chắc chắn lệnh CHƯA tới server:
    ``ConnectionDoesNotExistError`` ("connection was closed in the middle of
    operation") có thể xảy ra SAU khi server đã commit, retry sẽ tạo bản ghi
    trùng - lỗi đó được ném ra cho nơi gọi.
    """
    if idempotent:
        return True
    return not isinstance(exc, asyncpg.ConnectionDoesNotExistError)


def _with_reconnect(func=None, *, idempotent: bool = True):
    # Supabase free tier tự pause DB sau ~1 tuần không hoạt động, khiến pool
    # cũ giữ connection đã chết -> raise lỗi kết nối. Bắt lỗi, reset ĐÚNG pool
    # vừa lỗi rồi thử lại đúng 1 lần (nếu an toàn) để tự phục hồi.
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            token = _pool_in_use.set(None)
            try:
                return await fn(*args, **kwargs)
            except _CONNECTION_ERRORS as exc:
                # TimeoutError là lớp con của OSError (Python 3.11+): câu lệnh
                # chạy quá command_timeout KHÔNG phải mất kết nối - đóng cả pool
                # rồi chạy lại chỉ nhân đôi thời gian chờ.
                if isinstance(exc, TimeoutError):
                    raise
                await _reset_pool(_pool_in_use.get())
                if not _safe_to_retry(exc, idempotent):
                    raise
                return await fn(*args, **kwargs)
            finally:
                _pool_in_use.reset(token)

        return wrapper

    return decorate(func) if func is not None else decorate


# Tên công khai cho các repository ngoài module này (idempotency, webhook inbox,
# zalo_users...) dùng chung đúng cơ chế retry an toàn ở trên.
with_reconnect = _with_reconnect


# Chỉ giữ tối đa N prompt gần nhất / user - results liên quan tự xoá theo
# (ON DELETE CASCADE). Tránh bảng phình vô hạn theo thời gian (đặc biệt vì
# đây là bot 1 người dùng nhưng chạy 24/7).
HISTORY_RETENTION_LIMIT = 20


def _ssl_setting():
    """Giá trị ``ssl=`` cho asyncpg theo DATABASE_SSL_MODE.

    "require": như libpq - mã hoá đường truyền, không xác thực chứng chỉ (dễ
    cấu hình, chống nghe lén thụ động nhưng không chống MITM có chứng chỉ giả).
    "verify-full": SSLContext xác thực chứng chỉ server + hostname, dùng CA hệ
    thống hoặc DATABASE_SSL_CA. "disable": không SSL (chỉ cho DB local/test)."""
    mode = config.DATABASE_SSL_MODE
    if mode == "disable":
        return False
    if mode in ("verify-full", "verify-ca"):
        import ssl

        ctx = ssl.create_default_context(cafile=config.DATABASE_SSL_CA)
        ctx.check_hostname = mode == "verify-full"
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    if mode != "require":
        logger.warning("DATABASE_SSL_MODE=%r không hợp lệ, dùng 'require'.", mode)
    return "require"


async def get_pool() -> asyncpg.pool.Pool:
    pool = await _get_or_create_pool()
    _pool_in_use.set(pool)
    return pool


async def _get_or_create_pool() -> asyncpg.pool.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                config.DATABASE_URL,
                min_size=1,
                max_size=5,
                # Đóng connection rảnh sau 60s: Supavisor/Supabase hay cắt
                # connection idle, giữ lại chỉ tạo lỗi "connection is closed" ở
                # lượt kế tiếp (và tốn RAM vô ích).
                max_inactive_connection_lifetime=60,
                command_timeout=30,
                ssl=_ssl_setting(),
                # PgBouncer/Supavisor transaction mode không giữ prepared statement
                statement_cache_size=0,
            )
    return _pool


@_with_reconnect
async def ensure_migrations() -> None:
    """Apply all pending versioned schema migrations."""
    from core import migrations

    await migrations.run(await get_pool())


@_with_reconnect
async def init_db() -> None:
    await ensure_migrations()

    # Data retention is operational cleanup, not schema migration.
    await (await get_pool()).execute(
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


@_with_reconnect(idempotent=False)
async def save_prompt(
    telegram_user_id: int, command_type: str, prompt: str, channel: str = "telegram"
) -> int:
    pool = await get_pool()
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
            # Ghi song song vào user_calls (KHÔNG bị dọn theo số lượng như
            # `prompts` bên dưới) - đây là nguồn số liệu thật cho
            # usage_by_user()/`/thongke`, xem docstring bảng user_calls trong
            # init_db().
            await conn.execute(
                "INSERT INTO user_calls (channel, telegram_user_id) VALUES ($1, $2)",
                channel,
                telegram_user_id,
            )
            # Chỉ giữ HISTORY_RETENTION_LIMIT prompt gần nhất của user này -
            # chạy ngay sau mỗi insert nên bảng không bao giờ phình quá giới
            # hạn, dù bot chạy liên tục trong thời gian dài. Việc dọn này CHỈ
            # phục vụ /history (xem lại nội dung prompt gần đây) - KHÔNG được
            # dùng để đếm usage (xem user_calls ở trên).
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


@_with_reconnect
async def usage_by_user(since_hours: int = 24 * 7) -> list[dict]:
    """Số lượt gọi AI (bảng user_calls - KHÔNG phải `prompts`, xem docstring
    bảng user_calls trong init_db() để biết lý do) theo (channel,
    telegram_user_id), dùng cho trang admin và /thongke. telegram_user_id âm =
    tài khoản Zalo đã pair (xem channels/zalo_users.py); dương = chủ bot
    (Telegram/Zoom dùng chung config.ALLOWED_USER_ID vì Zoom hiện chỉ có 1
    pairing)."""
    pool = await get_pool()
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


@_with_reconnect(idempotent=False)
async def record_provider_call(
    provider: str,
    model: str,
    *,
    outcome: str = "ok",
    latency_ms: Optional[int] = None,
    stage: Optional[str] = None,
    prompt_chars: Optional[int] = None,
) -> None:
    """Ghi 1 lượt gọi provider/model (bảng provider_calls). ``outcome``: ok |
    error | timeout | rate_limited | contaminated. Lỗi DB ở đây KHÔNG được để
    văng lên caller (chỉ là số liệu thống kê, không phải luồng chính) - caller
    (orchestrator) tự bọc try/except khi gọi hàm này."""
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO provider_calls (provider, model, outcome, latency_ms, stage, prompt_chars)
        VALUES ($1, $2, $3, $4, $5, $6)""",
        provider,
        model,
        outcome,
        latency_ms,
        stage,
        prompt_chars,
    )


@_with_reconnect
async def provider_outcomes(since_hours: int = 24 * 7) -> list[dict]:
    """Số lượt theo (provider, outcome) + độ trễ trung vị/p95 - trang admin dùng
    để thấy provider nào chậm/lỗi/hết quota thay vì chỉ đếm lượt thành công."""
    pool = await get_pool()
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


@_with_reconnect
async def usage_by_model(since_hours: int = 24 * 7) -> list[dict]:
    """Số lượt gọi thành công (bảng provider_calls) theo (provider, model),
    dùng cho trang admin - khác usage_by_user() ở chỗ đây là góc nhìn theo
    HẠ TẦNG AI đang trả lời, không phải theo user/kênh nào đang hỏi."""
    pool = await get_pool()
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


@_with_reconnect(idempotent=False)
async def save_result(
    prompt_id: int,
    result_type: str,
    content_text: Optional[str] = None,
    file_path: Optional[str] = None,
) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO results (prompt_id, result_type, content_text, file_path) "
        "VALUES ($1, $2, $3, $4)",
        prompt_id,
        result_type,
        content_text,
        file_path,
    )


@_with_reconnect
async def get_history(telegram_user_id: int, limit: int = 10):
    pool = await get_pool()
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


# Giữ tối đa N tin nhắn gần nhất / user trong chat_messages, để bảng không
# phình vô hạn (K lượt trượt tối đa cần dùng chỉ CHAT_HISTORY_TURNS*2 dòng,
# nhưng giữ dư một chút phòng khi anh tăng CHAT_HISTORY_TURNS sau này).
CHAT_MESSAGES_RETENTION_LIMIT = 200


@_with_reconnect(idempotent=False)
async def add_chat_message(telegram_user_id: int, role: str, content: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO chat_messages (telegram_user_id, role, content) VALUES ($1, $2, $3)",
                telegram_user_id,
                role,
                content,
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
                CHAT_MESSAGES_RETENTION_LIMIT,
            )


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
    pool = await get_pool()
    # Lấy dư hơn k để có đủ dữ liệu xác định điểm cắt phiên, rồi mới trim về k.
    rows = await pool.fetch(
        """
        SELECT role, content, created_at
        FROM chat_messages
        WHERE telegram_user_id = $1
        ORDER BY id DESC
        LIMIT $2
        """,
        telegram_user_id,
        max(k * 4, 40),
    )
    if not rows:
        return []

    now = datetime.now(timezone.utc)
    if (now - rows[0]["created_at"]).total_seconds() > session_timeout_sec:
        # Tin gần nhất đã quá cũ -> phiên trước đã hết hạn, bắt đầu phiên mới trống.
        return []

    session_rows = [rows[0]]
    for i in range(1, len(rows)):
        gap = (rows[i - 1]["created_at"] - rows[i]["created_at"]).total_seconds()
        if gap > session_timeout_sec:
            break
        session_rows.append(rows[i])

    session_rows = session_rows[:k]
    session_rows.reverse()  # cũ -> mới
    return [(r["role"], r["content"]) for r in session_rows]


@_with_reconnect
async def clear_chat(telegram_user_id: int) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM chat_messages WHERE telegram_user_id = $1", telegram_user_id)


# ─── Trí nhớ dài hạn: user_facts + rolling summary (xem services/memory_service.py) ─────────


@_with_reconnect
async def upsert_fact(telegram_user_id: int, key: str, value: str) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO user_facts (telegram_user_id, key, value, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (telegram_user_id, key)
        DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        telegram_user_id,
        key,
        value,
    )


@_with_reconnect
async def get_facts(telegram_user_id: int) -> list[tuple[str, str]]:
    """Trả về [(key, value), ...] mới cập nhật nhất trước."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT key, value FROM user_facts WHERE telegram_user_id = $1 ORDER BY updated_at DESC",
        telegram_user_id,
    )
    return [(r["key"], r["value"]) for r in rows]


@_with_reconnect
async def delete_fact(telegram_user_id: int, key: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM user_facts WHERE telegram_user_id = $1 AND key = $2",
        telegram_user_id,
        key,
    )


@_with_reconnect
async def trim_facts(telegram_user_id: int, keep_n: int) -> None:
    """Giữ lại tối đa `keep_n` fact mới cập nhật nhất / user, xoá phần dư -
    chặn user_facts phình vô hạn nếu Gemini trích xuất quá tay qua thời gian."""
    pool = await get_pool()
    await pool.execute(
        """
        DELETE FROM user_facts
        WHERE telegram_user_id = $1
          AND id NOT IN (
              SELECT id FROM user_facts
              WHERE telegram_user_id = $1
              ORDER BY updated_at DESC
              LIMIT $2
          )
        """,
        telegram_user_id,
        keep_n,
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
    await pool.execute("UPDATE reminders SET sent = true WHERE id = $1", reminder_id)


@_with_reconnect
async def get_setting(key: str) -> Optional[str]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", key)
    return row["value"] if row else None


@_with_reconnect
async def set_setting(key: str, value: str) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        key,
        value,
    )


# ─── Giám sát vận hành (services/watchdog.py) ───────────────────────────────
# SQL nằm ở core/repositories/ops.py; đây chỉ là wrapper có retry kết nối.


@_with_reconnect
async def database_bytes() -> int:
    from core.repositories import ops

    return await ops.database_bytes(await get_pool())


@_with_reconnect
async def webhook_backlog(stale_after) -> tuple[int, float | None]:
    from core.repositories import ops

    return await ops.webhook_backlog(await get_pool(), stale_after)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ─── Zoom Team Chat pairing (bot 1 chủ, dùng chung ALLOWED_USER_ID) ─────────
# Zoom không có khái niệm "user Telegram" - chỉ có ĐÚNG 1 jid Zoom được phép
# nói chuyện với bot (như ALLOWED_USER_ID của Telegram). Lưu qua bảng settings
# key-value sẵn có thay vì tạo bảng users/roles riêng như vietassist, vì kiến
# trúc Gemini là bot 1 chủ (ALLOWED_USER_ID), không phải multi-user/multi-role.
_SETTING_ZOOM_JID = "zoom_paired_jid"
_SETTING_ZOOM_NAME = "zoom_paired_name"


async def zoom_get_pairing() -> Optional[tuple[str, str]]:
    """Trả về (jid, display_name) đã pair, hoặc None nếu chưa pair ai."""
    jid = await get_setting(_SETTING_ZOOM_JID)
    if not jid:
        return None
    name = await get_setting(_SETTING_ZOOM_NAME) or ""
    return jid, name


async def zoom_set_pairing(jid: str, display_name: str = "") -> None:
    await set_setting(_SETTING_ZOOM_JID, jid)
    await set_setting(_SETTING_ZOOM_NAME, display_name)


async def zoom_clear_pairing() -> None:
    await set_setting(_SETTING_ZOOM_JID, "")
    await set_setting(_SETTING_ZOOM_NAME, "")
