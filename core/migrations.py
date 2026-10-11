"""Versioned PostgreSQL schema migrations.

All application DDL lives in ``migrations/*.sql``. Runtime repositories only
ensure migrations have run; they do not CREATE/ALTER their own tables.

Mỗi version đã áp được lưu kèm checksum SHA-256 nội dung file. Version áp từ
trước khi có cột checksum được điền bù (backfill) bằng nội dung hiện tại ở lần
chạy đầu. Sau đó nếu file của 1 version đã áp bị sửa (checksum lệch) thì runner
KHÔNG áp lại (giống trước) nhưng log lỗi + báo chủ bot, thay vì im lặng bỏ qua
như sự cố 005/007 - và không làm sập bot đang chạy production.

Runner chỉ duyệt các file đang có trong migrations/: version đã ghi trong
schema_migrations mà file đã bị xoá (006, 003_vector.sql của tính năng pgvector
đã bỏ - bảng được dọn bởi 014) được bỏ qua, không báo lệch checksum.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from core import alerts

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_ADVISORY_LOCK_KEY = 718_026_091
_lock = asyncio.Lock()
_ran = False


def _migration_files() -> list[tuple[int, Path]]:
    items: list[tuple[int, Path]] = []
    for path in sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        items.append((int(path.name.split("_", 1)[0]), path))
    return items


def checksum(sql: str) -> str:
    """SHA-256 nội dung migration, chuẩn hoá xuống dòng (CRLF/LF) để checkout
    trên Windows không bị coi là đã sửa file."""
    normalized = sql.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _ensure_table(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            checksum TEXT
        )
        """
    )
    # Bảng tạo trước khi có checksum: thêm cột (metadata của runner, không phải
    # schema ứng dụng).
    await conn.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT")


async def _apply_pending(conn) -> list[str]:
    """Áp các migration chưa chạy; trả danh sách mô tả version bị sửa sau khi áp."""
    await _ensure_table(conn)
    applied = {
        int(row["version"]): row["checksum"]
        for row in await conn.fetch("SELECT version, checksum FROM schema_migrations")
    }
    mismatches: list[str] = []
    for version, path in _migration_files():
        sql = path.read_text(encoding="utf-8")
        digest = checksum(sql)
        if version in applied:
            stored = applied[version]
            if stored is None:
                await conn.execute(
                    "UPDATE schema_migrations SET checksum = $2 "
                    "WHERE version = $1 AND checksum IS NULL",
                    version,
                    digest,
                )
            elif stored != digest:
                mismatches.append(f"{version:03d} ({path.name})")
            continue
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
                version,
                path.name,
                digest,
            )
        logger.info("Applied DB migration %03d: %s", version, path.name)
    return mismatches


def _report_mismatches(mismatches: list[str]) -> None:
    if not mismatches:
        return
    listed = ", ".join(mismatches)
    logger.error(
        "Migration đã áp nhưng file bị sửa sau đó (KHÔNG áp lại): %s. "
        "Thay đổi schema phải nằm trong file migration MỚI.",
        listed,
    )
    alerts.send(
        f"⚠️ Migration DB đã áp nhưng file bị sửa sau đó: {listed}. "
        "Nội dung mới KHÔNG được áp - hãy đưa thay đổi vào 1 file migration mới."
    )


async def run(pool) -> None:
    """Apply pending migrations once, serialised across processes/instances."""
    global _ran
    if _ran:
        return

    async with _lock:
        if _ran:
            return
        async with pool.acquire() as conn:
            # Cross-instance lock: only one deployment migrates a shared DB at once.
            # Advisory lock theo session - cần Session Pooler (xem README).
            await conn.execute(f"SELECT pg_advisory_lock({_ADVISORY_LOCK_KEY})")
            try:
                mismatches = await _apply_pending(conn)
            finally:
                await conn.execute(f"SELECT pg_advisory_unlock({_ADVISORY_LOCK_KEY})")
        _report_mismatches(mismatches)
        _ran = True
