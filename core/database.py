"""Supabase Postgres - bền hơn SQLite vì ổ đĩa local trên Render free tier là ephemeral."""
import asyncio
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

# pgvector (Bước 7 - semantic recall, xem services/memory_service.py, ai/official_client.embed_text()).
# Tự tắt êm nếu DB không bật được extension "vector" (vd thiếu quyền trên 1
# số gói Postgres managed) - init_db() sẽ set lại giá trị thật lúc khởi động.
VECTOR_ENABLED = False


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Đăng ký codec kiểu `vector` cho MỖI connection mới trong pool - bắt
    buộc để asyncpg hiểu cột `vector(768)` (xem chat_embeddings). Lỗi ở đây
    (vd extension chưa bật) chỉ log, KHÔNG raise - để pool vẫn khởi tạo bình
    thường cho các bảng khác không liên quan tới pgvector."""
    try:
        from pgvector.asyncpg import register_vector

        await register_vector(conn)
    except Exception:
        logger.debug("Không đăng ký được codec pgvector cho connection (có thể chưa bật extension).")


async def _reset_pool(failed_pool: Optional[asyncpg.pool.Pool]) -> None:
    """Đóng pool bị lỗi rồi bỏ nó đi, ĐỂ get_pool() tạo pool mới ở lần gọi
    kế tiếp. Chỉ đóng nếu `_pool` hiện tại VẪN CÒN LÀ `failed_pool` - nếu 2
    coroutine cùng gặp lỗi kết nối song song, coroutine chạy sau (pool nó
    thấy bị lỗi đã cũ) sẽ không được phép đóng pool MỚI mà coroutine chạy
    trước vừa tạo xong (race condition "domino" phá pool đang sống)."""
    global _pool
    async with _pool_lock:
        if _pool is not failed_pool:
            return
        if _pool is not None:
            try:
                await _pool.close()
            except Exception:
                pass
            _pool = None


def _with_reconnect(func):
    # Supabase free tier tự pause DB sau ~1 tuần không hoạt động, khiến pool
    # cũ giữ connection đã chết -> raise lỗi kết nối. Bắt lỗi, reset pool rồi
    # thử lại đúng 1 lần để tự phục hồi thay vì crash.
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except _CONNECTION_ERRORS:
            await _reset_pool(_pool)
            return await func(*args, **kwargs)
    return wrapper

# Chỉ giữ tối đa N prompt gần nhất / user - results liên quan tự xoá theo
# (ON DELETE CASCADE). Tránh bảng phình vô hạn theo thời gian (đặc biệt vì
# đây là bot 1 người dùng nhưng chạy 24/7).
HISTORY_RETENTION_LIMIT = 20


async def get_pool() -> asyncpg.pool.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                config.DATABASE_URL,
                min_size=1,
                max_size=5,
                command_timeout=30,
                ssl="require",
                # PgBouncer/Supavisor transaction mode không giữ prepared statement
                statement_cache_size=0,
                init=_init_connection,
            )
    return _pool


async def _ensure_vector_extension() -> bool:
    """Bật extension `vector` (pgvector) bằng 1 connection ĐỘC LẬP, KHÔNG qua
    pool, và PHẢI chạy trước get_pool() lần đầu. Lý do: pool mở sẵn
    min_size=1 connection ngay khi tạo, mỗi connection tự chạy
    _init_connection() (register_vector) ĐÚNG 1 LẦN lúc được tạo - nếu extension
    chưa tồn tại lúc đó, connection này "kẹt" không có codec vector suốt vòng
    đời của nó, dù CREATE EXTENSION chạy thành công ngay sau bằng 1 connection
    khác trong cùng pool. Tạo extension trước, ngoài pool, tránh hẳn race này.

    Trả về True nếu extension đã sẵn sàng (hoặc đã có sẵn), False nếu không
    bật được (thiếu quyền...) - KHÔNG raise, để không kéo sập init_db()."""
    try:
        conn = await asyncpg.connect(config.DATABASE_URL, ssl="require")
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        finally:
            await conn.close()
        return True
    except Exception:
        logger.warning(
            "Không bật được extension 'vector' (pgvector) trên DB này - "
            "semantic recall (Bước 7) sẽ tự tắt. Bật thủ công qua Supabase "
            "Dashboard > Database > Extensions > vector rồi khởi động lại bot "
            "nếu muốn dùng tính năng này.",
            exc_info=True,
        )
        return False


@_with_reconnect
async def init_db() -> None:
    # QUAN TRỌNG: phải chạy TRƯỚC get_pool() (xem docstring _ensure_vector_extension).
    global VECTOR_ENABLED
    VECTOR_ENABLED = await _ensure_vector_extension()

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                command_type TEXT NOT NULL,
                prompt TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # channel: 'telegram'|'zalo'|'zoom' - thêm sau khi bảng đã chạy production
        # nên dùng ALTER thay vì sửa CREATE TABLE (không re-run trên bảng có sẵn).
        # Dùng cho thống kê lượt gọi AI theo kênh/người dùng ở trang admin.
        await conn.execute(
            "ALTER TABLE prompts ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'telegram'"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prompts_channel ON prompts (channel, created_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prompts_user_id ON prompts (telegram_user_id, id DESC)"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id SERIAL PRIMARY KEY,
                prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                result_type TEXT NOT NULL,
                content_text TEXT,
                file_path TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

        # Lượt gọi thành công tới từng provider/model trong provider-chain
        # (router9/groq/openrouter/api1/api2, xem ai/orchestrator.py -
        # _run_provider_chain ghi 1 dòng mỗi lần 1 provider trả lời thành
        # công) - dùng cho thống kê "lượt gọi theo model" ở trang admin.
        # Tách khỏi bảng prompts (đó là lượt gọi theo user/kênh, không biết
        # provider/model nào đã thực sự trả lời).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_calls (
                id SERIAL PRIMARY KEY,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_calls_provider ON provider_calls (provider, created_at DESC)"
        )

        # Trí nhớ hội thoại (Phương án B - cửa sổ trượt + session timeout).
        # Ghi lại MỌI lượt chat bất kể đang dùng provider nào (router9/api1/
        # api2), để khi provider-chain đổi provider giữa chừng, nhánh API vẫn
        # có ngữ cảnh gần nhất nạp lại từ đây (xem ai.orchestrator.chat()).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                role TEXT NOT NULL,          -- 'user' | 'model'
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_msg_user ON chat_messages (telegram_user_id, id DESC)"
        )

        # Trí nhớ DÀI HẠN (khác chat_messages là trí nhớ NGẮN HẠN theo phiên):
        # - user_facts: các "sự thật" bền về người dùng (tên, sở thích, danh
        #   mục đầu tư...), 1 dòng / key, upsert theo (telegram_user_id, key).
        #   Được trích xuất tự động bằng Gemini sau mỗi lượt chat (xem services/memory_service.py).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_facts (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (telegram_user_id, key)
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_facts_user ON user_facts (telegram_user_id, updated_at DESC)"
        )

        # - user_memory_summary: 1 đoạn tóm tắt "rolling" / user, được Gemini
        #   hợp nhất dần (tóm tắt cũ + lượt mới) mỗi lượt chat, thay cho việc
        #   giữ toàn bộ lịch sử -> trí nhớ gần như vô hạn mà không phình token.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_memory_summary (
                telegram_user_id BIGINT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

        # "Nhật ký ý quan trọng": thay cho semantic recall (chat_embeddings, đã
        # ngưng dùng - xem services/memory_service.py) - N dòng gần nhất/user,
        # trích cùng lượt gọi generate_utility_json() đã có sẵn (không tốn
        # thêm lượt gọi AI nào), KHÔNG bị "nén" lại như user_memory_summary
        # nên giữ nguyên chi tiết cụ thể (số liệu, ngày tháng, tên mã...).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_memory_highlights (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_memory_highlights_user "
            "ON user_memory_highlights (telegram_user_id, created_at DESC)"
        )

        # Function calling (xem services/tools.py): ghi chú tự do + nhắc việc,
        # do Gemini tự quyết định gọi qua tools.maybe_run_tool().
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notes_user ON notes (telegram_user_id, created_at DESC)"
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                message TEXT NOT NULL,
                due_at TIMESTAMPTZ NOT NULL,
                sent BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (due_at) WHERE sent = false"
        )

        # pgvector semantic recall (Bước 7 - nâng cao, làm sau cùng). Extension
        # đã được thử bật ở _ensure_vector_extension() TRƯỚC khi mở pool (xem
        # đầu init_db()) - ở đây chỉ tạo bảng/index NẾU đã bật thành công.
        if VECTOR_ENABLED:
            try:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_embeddings (
                        id SERIAL PRIMARY KEY,
                        telegram_user_id BIGINT NOT NULL,
                        content TEXT NOT NULL,
                        embedding vector(768) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_embeddings_user ON chat_embeddings (telegram_user_id)"
                )
                logger.info("pgvector đã sẵn sàng - semantic recall (Bước 7) khả dụng.")
            except Exception:
                # Extension bật được nhưng tạo bảng lỗi (hiếm) -> tắt tính
                # năng thay vì để lỗi này kéo sập toàn bộ init_db().
                VECTOR_ENABLED = False
                logger.warning("Extension 'vector' đã bật nhưng tạo bảng chat_embeddings lỗi.", exc_info=True)

        # Dọn 1 lần lúc khởi động: xoá phần vượt quá HISTORY_RETENTION_LIMIT
        # cho MỌI user đã có sẵn trong bảng (không chỉ user mới ghi thêm),
        # để dữ liệu tồn đọng từ trước khi có giới hạn này cũng được dọn.
        await conn.execute(
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


@_with_reconnect
async def save_prompt(telegram_user_id: int, command_type: str, prompt: str, channel: str = "telegram") -> int:
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
            # Chỉ giữ HISTORY_RETENTION_LIMIT prompt gần nhất của user này -
            # chạy ngay sau mỗi insert nên bảng không bao giờ phình quá giới
            # hạn, dù bot chạy liên tục trong thời gian dài.
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
    """Số lượt gọi AI (bảng prompts) theo (channel, telegram_user_id), dùng
    cho trang admin. telegram_user_id âm = tài khoản Zalo đã pair (xem
    channels/zalo_users.py); dương = chủ bot (Telegram/Zoom dùng chung
    config.ALLOWED_USER_ID vì Zoom hiện chỉ có 1 pairing)."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT channel, telegram_user_id, COUNT(*) AS calls, MAX(created_at) AS last_call_at
        FROM prompts
        WHERE created_at >= now() - ($1 || ' hours')::interval
        GROUP BY channel, telegram_user_id
        ORDER BY calls DESC
        """,
        str(since_hours),
    )
    return [dict(row) for row in rows]


@_with_reconnect
async def record_provider_call(provider: str, model: str) -> None:
    """Ghi 1 lượt gọi thành công của provider/model (bảng provider_calls) -
    gọi từ ai/orchestrator.py::_run_provider_chain ngay sau mỗi lần 1
    provider trả lời thành công. Lỗi DB ở đây KHÔNG được để văng lên caller
    (chỉ là số liệu thống kê cho trang admin, không phải luồng chính) -
    caller (orchestrator) tự bọc try/except khi gọi hàm này."""
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO provider_calls (provider, model) VALUES ($1, $2)",
        provider,
        model,
    )


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
        GROUP BY provider, model
        ORDER BY calls DESC
        """,
        str(since_hours),
    )
    return [dict(row) for row in rows]


@_with_reconnect
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


@_with_reconnect
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


@_with_reconnect
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


@_with_reconnect
async def add_note(telegram_user_id: int, content: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO notes (telegram_user_id, content) VALUES ($1, $2)",
        telegram_user_id,
        content,
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
async def add_reminder(telegram_user_id: int, message: str, due_at) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO reminders (telegram_user_id, message, due_at) VALUES ($1, $2, $3)",
        telegram_user_id,
        message,
        due_at,
    )


@_with_reconnect
async def get_due_reminders() -> list[tuple[int, int, str]]:
    """Trả về [(id, telegram_user_id, message), ...] các reminder đã tới hạn
    và CHƯA gửi. Không lọc theo user vì bot chỉ phục vụ 1 user, nhưng để
    nguyên user_id trong kết quả cho rõ ràng nếu sau này mở rộng đa user."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id, telegram_user_id, message FROM reminders WHERE due_at <= now() AND sent = false"
    )
    return [(r["id"], r["telegram_user_id"], r["message"]) for r in rows]


@_with_reconnect
async def mark_reminder_sent(reminder_id: int) -> None:
    pool = await get_pool()
    await pool.execute("UPDATE reminders SET sent = true WHERE id = $1", reminder_id)


# ─── pgvector semantic recall (Bước 7, xem services/memory_service.py) ─────


# Giữ tối đa N embedding gần nhất / user - bảng này trước đó insert thẳng
# không trim, phình vô hạn theo thời gian (mỗi lượt chat 1 dòng, chạy 24/7).
CHAT_EMBEDDINGS_RETENTION_LIMIT = 500

# Ngưỡng cosine distance (`<=>` của pgvector, 0 = giống hệt) để loại kết quả
# không đủ liên quan thay vì luôn trả đủ top_k dù đoạn gần nhất cũng không
# thật sự gần nghĩa - top_k chỉ đảm bảo THỨ TỰ, không đảm bảo CHẤT LƯỢNG.
SEMANTIC_SEARCH_MAX_DISTANCE = 0.5


@_with_reconnect
async def add_chat_embedding(telegram_user_id: int, content: str, embedding: list[float]) -> None:
    if not VECTOR_ENABLED:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO chat_embeddings (telegram_user_id, content, embedding) VALUES ($1, $2, $3)",
                telegram_user_id,
                content,
                embedding,
            )
            await conn.execute(
                """
                DELETE FROM chat_embeddings
                WHERE telegram_user_id = $1
                  AND id NOT IN (
                      SELECT id FROM chat_embeddings
                      WHERE telegram_user_id = $1
                      ORDER BY id DESC
                      LIMIT $2
                  )
                """,
                telegram_user_id,
                CHAT_EMBEDDINGS_RETENTION_LIMIT,
            )


@_with_reconnect
async def clear_chat_embeddings(telegram_user_id: int) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM chat_embeddings WHERE telegram_user_id = $1", telegram_user_id)


@_with_reconnect
async def semantic_search(
    telegram_user_id: int,
    query_embedding: list[float],
    top_k: int = 3,
    max_distance: float = SEMANTIC_SEARCH_MAX_DISTANCE,
) -> list[str]:
    """Trả về nội dung các lượt chat cũ GẦN NGHĨA NHẤT với query_embedding,
    đã lọc bớt kết quả có distance >= max_distance (không đủ liên quan) -
    mới -> cũ không quan trọng thứ tự ở đây vì đã sắp theo độ tương đồng."""
    if not VECTOR_ENABLED:
        return []
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT content FROM chat_embeddings
        WHERE telegram_user_id = $1
          AND embedding <=> $2 < $4
        ORDER BY embedding <=> $2
        LIMIT $3
        """,
        telegram_user_id,
        query_embedding,
        top_k,
        max_distance,
    )
    return [r["content"] for r in rows]


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
