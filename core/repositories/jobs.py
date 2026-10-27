"""SQL việc nền (/viec) và tác vụ định kỳ (/lichtacvu) - migrations/027,
services/background_jobs.py, services/routines.py.

Hàm nhận ``pool`` (asyncpg pool hoặc pool PGlite của test); wrapper có retry kết
nối nằm ở core/database.py. Mọi truy vấn theo người dùng lọc ``telegram_user_id``
của chính người gọi - Zalo (id âm) không đụng dữ liệu Telegram và ngược lại.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

ACTIVE = ("queued", "running")
_JOB_COLUMNS = (
    "id, telegram_user_id, channel, kind, routine_id, prompt, status, result, error, "
    "no_reply, attempts, tokens, provider, scheduled_for, created_at, started_at, finished_at"
)


# ─── Việc nền ────────────────────────────────────────────────────────────────


async def insert_job(
    pool: Any,
    user_id: int,
    channel: str,
    prompt: str,
    *,
    user_cap: int,
    global_cap: int,
) -> tuple[int | None, str]:
    """(id mới, "") hoặc (None, "user_cap" | "global_cap"). Đếm trần và INSERT trong
    1 transaction (lượt /viec của 1 người đã nối tiếp nhau nhờ khoá lượt)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            mine = await conn.fetchval(
                "SELECT count(*) FROM bg_jobs WHERE telegram_user_id = $1 AND kind = 'job' "
                "AND status = ANY($2::text[])",
                user_id,
                list(ACTIVE),
            )
            if int(mine or 0) >= user_cap:
                return None, "user_cap"
            queued = await conn.fetchval("SELECT count(*) FROM bg_jobs WHERE status = 'queued'")
            if int(queued or 0) >= global_cap:
                return None, "global_cap"
            new_id = await conn.fetchval(
                "INSERT INTO bg_jobs (telegram_user_id, channel, kind, prompt) "
                "VALUES ($1, $2, 'job', $3) RETURNING id",
                user_id,
                channel,
                prompt,
            )
    return int(new_id), ""


async def claim_next(pool: Any, lease_sec: int) -> dict | None:
    """Nhận việc đang chờ cũ nhất (SKIP LOCKED: 2 instance không nhận trùng)."""
    row = await pool.fetchrow(
        f"""
        UPDATE bg_jobs
        SET status = 'running', attempts = attempts + 1, started_at = now(),
            lease_until = now() + $1::int * interval '1 second'
        WHERE id = (
            SELECT id FROM bg_jobs WHERE status = 'queued' ORDER BY id LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING {_JOB_COLUMNS}
        """,
        lease_sec,
    )
    return dict(row) if row else None


async def renew_lease(pool: Any, job_id: int, lease_sec: int) -> bool:
    """Gia hạn lease; False = việc không còn ở trạng thái running (đã bị huỷ)."""
    found = await pool.fetchval(
        "UPDATE bg_jobs SET lease_until = now() + $2::int * interval '1 second' "
        "WHERE id = $1 AND status = 'running' RETURNING id",
        job_id,
        lease_sec,
    )
    return found is not None


async def finish(
    pool: Any,
    job_id: int,
    status: str,
    *,
    result: str | None,
    error: str | None,
    no_reply: bool,
    tokens: int | None,
    provider: str | None,
) -> bool:
    """Ghi kết quả; chỉ khi việc còn running (đã bị huỷ thì giữ 'cancelled')."""
    found = await pool.fetchval(
        """
        UPDATE bg_jobs
        SET status = $2, result = $3, error = $4, no_reply = $5, tokens = $6, provider = $7,
            finished_at = now(), lease_until = NULL
        WHERE id = $1 AND status = 'running'
        RETURNING id
        """,
        job_id,
        status,
        result,
        error,
        no_reply,
        tokens,
        provider,
    )
    return found is not None


async def recover_stale(pool: Any, max_attempts: int) -> list[dict]:
    """Việc running đã hết lease (process chết, redeploy): chạy lại nếu chưa đủ
    ``max_attempts`` lần, không thì failed. Trả các dòng vừa chuyển failed."""
    rows = await pool.fetch(
        f"""
        UPDATE bg_jobs
        SET status = CASE WHEN attempts >= $1 THEN 'failed' ELSE 'queued' END,
            error = CASE WHEN attempts >= $1 THEN 'interrupted' ELSE error END,
            finished_at = CASE WHEN attempts >= $1 THEN now() ELSE NULL END,
            lease_until = NULL
        WHERE status = 'running' AND lease_until < now()
        RETURNING {_JOB_COLUMNS}
        """,
        max_attempts,
    )
    return [dict(r) for r in rows if r["status"] == "failed"]


async def cancel(pool: Any, user_id: int, job_id: int) -> str | None:
    """Huỷ việc của CHÍNH người gọi. Trả trạng thái trước khi huỷ (queued/running),
    None nếu không có việc đó hoặc việc đã xong."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchval(
                "SELECT status FROM bg_jobs WHERE id = $1 AND telegram_user_id = $2 FOR UPDATE",
                job_id,
                user_id,
            )
            if old not in ACTIVE:
                return None
            await conn.execute(
                "UPDATE bg_jobs SET status = 'cancelled', finished_at = now(), lease_until = NULL "
                "WHERE id = $1",
                job_id,
            )
    return str(old)


async def get_job(pool: Any, user_id: int, job_id: int) -> dict | None:
    row = await pool.fetchrow(
        f"SELECT {_JOB_COLUMNS} FROM bg_jobs WHERE id = $1 AND telegram_user_id = $2",
        job_id,
        user_id,
    )
    return dict(row) if row else None


async def list_jobs(pool: Any, user_id: int, limit: int) -> list[dict]:
    """Việc /viec của 1 người: đang chạy/chờ trước, rồi mới nhất."""
    rows = await pool.fetch(
        f"""
        SELECT {_JOB_COLUMNS} FROM bg_jobs
        WHERE telegram_user_id = $1 AND kind = 'job'
        ORDER BY (status IN ('queued', 'running')) DESC, id DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def active_jobs(pool: Any, user_id: int) -> list[dict]:
    rows = await pool.fetch(
        f"SELECT {_JOB_COLUMNS} FROM bg_jobs WHERE telegram_user_id = $1 "
        "AND status = ANY($2::text[]) ORDER BY id LIMIT 20",
        user_id,
        list(ACTIVE),
    )
    return [dict(r) for r in rows]


async def queue_position(pool: Any, job_id: int) -> int:
    """Số việc đang chạy/chờ đứng trước ``job_id`` (toàn bot)."""
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM bg_jobs WHERE id < $1 AND status = ANY($2::text[])",
            job_id,
            list(ACTIVE),
        )
        or 0
    )


async def recent_all(pool: Any, limit: int) -> list[dict]:
    """Trang /admin: việc gần nhất của mọi người (đang chạy/chờ trước)."""
    rows = await pool.fetch(
        f"""
        SELECT {_JOB_COLUMNS} FROM bg_jobs
        ORDER BY (status IN ('queued', 'running')) DESC, id DESC
        LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def clear_user(pool: Any, user_id: int) -> None:
    """/xoadulieu của chủ bot: bỏ việc đã xong (prompt + kết quả); việc đang chạy giữ."""
    await pool.execute(
        "DELETE FROM bg_jobs WHERE telegram_user_id = $1 AND status <> ALL($2::text[])",
        user_id,
        list(ACTIVE),
    )


# ─── Tác vụ định kỳ ──────────────────────────────────────────────────────────

_ROUTINE_COLUMNS = (
    "r.id, r.telegram_user_id, r.channel, r.prompt, r.recurrence, r.status, r.next_run_at, "
    "r.last_run_at, r.created_at"
)


def _routine(row) -> dict:
    item = dict(row)
    rule = item.get("recurrence")
    if isinstance(rule, str):
        item["recurrence"] = json.loads(rule)
    return item


async def count_routines(pool: Any, user_id: int) -> int:
    return int(
        await pool.fetchval("SELECT count(*) FROM routines WHERE telegram_user_id = $1", user_id)
        or 0
    )


async def insert_routine(
    pool: Any, user_id: int, channel: str, prompt: str, rule: dict, next_run_at: datetime
) -> int:
    return int(
        await pool.fetchval(
            "INSERT INTO routines (telegram_user_id, channel, prompt, recurrence, next_run_at) "
            "VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING id",
            user_id,
            channel,
            prompt,
            json.dumps(rule, ensure_ascii=False),
            next_run_at,
        )
    )


async def list_routines(pool: Any, user_id: int) -> list[dict]:
    """Tác vụ của 1 người + lần chạy gần nhất (trạng thái, có báo hay không)."""
    rows = await pool.fetch(
        f"""
        SELECT {_ROUTINE_COLUMNS}, j.status AS last_status, j.no_reply AS last_no_reply,
               j.created_at AS last_job_at
        FROM routines r
        LEFT JOIN LATERAL (
            SELECT status, no_reply, created_at FROM bg_jobs
            WHERE routine_id = r.id ORDER BY id DESC LIMIT 1
        ) j ON true
        WHERE r.telegram_user_id = $1
        ORDER BY r.id
        LIMIT 50
        """,
        user_id,
    )
    return [_routine(r) for r in rows]


async def all_routines(pool: Any, limit: int) -> list[dict]:
    rows = await pool.fetch(
        f"SELECT {_ROUTINE_COLUMNS} FROM routines r ORDER BY r.id LIMIT $1", limit
    )
    return [_routine(r) for r in rows]


async def delete_routine(pool: Any, user_id: int, routine_id: int) -> bool:
    found = await pool.fetchval(
        "DELETE FROM routines WHERE id = $1 AND telegram_user_id = $2 RETURNING id",
        routine_id,
        user_id,
    )
    return found is not None


async def set_routine_status(
    pool: Any, user_id: int, routine_id: int, status: str, next_run_at: datetime | None
) -> bool:
    found = await pool.fetchval(
        "UPDATE routines SET status = $3, next_run_at = COALESCE($4, next_run_at) "
        "WHERE id = $1 AND telegram_user_id = $2 RETURNING id",
        routine_id,
        user_id,
        status,
        next_run_at,
    )
    return found is not None


async def get_routine(pool: Any, user_id: int, routine_id: int) -> dict | None:
    row = await pool.fetchrow(
        f"SELECT {_ROUTINE_COLUMNS} FROM routines r WHERE r.id = $1 AND r.telegram_user_id = $2",
        routine_id,
        user_id,
    )
    return _routine(row) if row else None


async def due_routines(pool: Any, now: datetime, limit: int) -> list[dict]:
    rows = await pool.fetch(
        f"""
        SELECT {_ROUTINE_COLUMNS} FROM routines r
        WHERE r.status = 'active' AND r.next_run_at IS NOT NULL AND r.next_run_at <= $1
        ORDER BY r.next_run_at
        LIMIT $2
        """,
        now,
        limit,
    )
    return [_routine(r) for r in rows]


async def fire_routine(
    pool: Any,
    routine: dict,
    scheduled: datetime,
    next_run_at: datetime | None,
    *,
    enqueue: bool,
) -> int | None:
    """Nhận 1 lần chạy: dời next_run_at (chỉ khi vẫn là giá trị cũ - instance khác
    chưa nhận) và xếp 1 dòng bg_jobs trong CÙNG transaction. Trả id việc vừa xếp,
    None nếu đã có instance khác nhận hoặc ``enqueue`` False (chỉ dời lịch)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            moved = await conn.fetchval(
                """
                UPDATE routines SET next_run_at = $3,
                    last_run_at = CASE WHEN $4 THEN now() ELSE last_run_at END
                WHERE id = $1 AND status = 'active' AND next_run_at = $2
                RETURNING id
                """,
                routine["id"],
                scheduled,
                next_run_at,
                enqueue,
            )
            if moved is None or not enqueue:
                return None
            job_id = await conn.fetchval(
                """
                INSERT INTO bg_jobs (telegram_user_id, channel, kind, routine_id, prompt,
                                     scheduled_for, dedup_key)
                VALUES ($1, $2, 'routine', $3, $4, $5, $6)
                ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING
                RETURNING id
                """,
                routine["telegram_user_id"],
                routine["channel"],
                routine["id"],
                routine["prompt"],
                scheduled,
                f"routine:{routine['id']}:{scheduled.isoformat()}",
            )
    return int(job_id) if job_id is not None else None


async def routine_runs_since(pool: Any, since: datetime) -> int:
    """Số lần chạy tác vụ định kỳ toàn bot từ ``since`` (trần mỗi ngày)."""
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM bg_jobs WHERE kind = 'routine' AND created_at >= $1", since
        )
        or 0
    )


async def routine_jobs_since(pool: Any, user_id: int, since: datetime) -> list[dict]:
    rows = await pool.fetch(
        f"SELECT {_JOB_COLUMNS} FROM bg_jobs WHERE telegram_user_id = $1 AND kind = 'routine' "
        "AND created_at >= $2 ORDER BY id LIMIT 30",
        user_id,
        since,
    )
    return [dict(r) for r in rows]
