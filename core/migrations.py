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

``migrations.lock`` (gốc repo) ghim checksum của từng file để CI bắt file đã
áp bị sửa NGAY ở PR thay vì sau khi deploy (test/test_migration_lock.py). Thêm
migration mới thì cập nhật lock bằng 1 lệnh::

    python -m core.migrations --write-lock

Lệnh chỉ THÊM dòng cho file mới (và bỏ dòng của file đã xoá); file đã có trong
lock mà nội dung đổi thì lệnh từ chối - thay đổi schema phải vào file mới.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from pathlib import Path

from core import alerts

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
LOCK_PATH = _MIGRATIONS_DIR.parent / "migrations.lock"
_LOCK_HEADER = (
    "# migrations.lock - SHA-256 (core.migrations.checksum, chuẩn hoá CRLF) của từng migration.\n"
    "# KHÔNG sửa dòng của migration đã deploy: đổi schema = thêm file migrations/NNN_*.sql MỚI.\n"
    "# Thêm/xoá file migration rồi cập nhật lock: python -m core.migrations --write-lock\n"
)
_ADVISORY_LOCK_KEY = 718_026_091
_lock = asyncio.Lock()
_ran = False


def _migration_files(directory: Path | None = None) -> list[tuple[int, Path]]:
    items: list[tuple[int, Path]] = []
    for path in sorted((directory or _MIGRATIONS_DIR).glob("[0-9][0-9][0-9]_*.sql")):
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


async def apply_locked(pool) -> list[str]:
    """Áp migration còn thiếu dưới advisory lock; trả danh sách version đã áp mà
    file bị sửa. KHÔNG báo chủ bot - ``run`` lo phần đó; diễn tập khôi phục
    (scripts/backup_drill.py) dùng trực tiếp để coi lệch checksum là lỗi."""
    async with pool.acquire() as conn:
        # Cross-instance lock: only one deployment migrates a shared DB at once.
        # Advisory lock theo session - cần Session Pooler (xem README).
        await conn.execute(f"SELECT pg_advisory_lock({_ADVISORY_LOCK_KEY})")
        try:
            return await _apply_pending(conn)
        finally:
            await conn.execute(f"SELECT pg_advisory_unlock({_ADVISORY_LOCK_KEY})")


async def run(pool) -> None:
    """Apply pending migrations once, serialised across processes/instances."""
    global _ran
    if _ran:
        return

    async with _lock:
        if _ran:
            return
        mismatches = await apply_locked(pool)
        _report_mismatches(mismatches)
        _ran = True


# ─── migrations.lock: ghim checksum để CI bắt file đã áp bị sửa ──────────────


def lock_entries(directory: Path | None = None) -> dict[str, str]:
    """{tên file: checksum} của mọi migration hiện có (cùng hàm checksum với runner)."""
    return {
        path.name: checksum(path.read_text(encoding="utf-8"))
        for _, path in _migration_files(directory)
    }


def read_lock(path: Path = LOCK_PATH) -> dict[str, str]:
    """Đọc lock (dòng ``<tên file> <sha256>``; bỏ dòng trống và dòng ``#``)."""
    if not path.exists():
        return {}
    entries: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, digest = line.partition(" ")
        entries[name] = digest.strip()
    return entries


def lock_problems(locked: dict[str, str], current: dict[str, str]) -> list[str]:
    """Các điểm lệch giữa lock và thư mục migrations/ (rỗng = khớp)."""
    problems = [
        f"{name}: nội dung khác checksum đã ghim - KHÔNG sửa migration đã áp, "
        "hãy đưa thay đổi vào 1 file migration mới"
        for name, digest in sorted(current.items())
        if name in locked and locked[name] != digest
    ]
    problems += [
        f"{name}: migration mới chưa có trong migrations.lock - chạy "
        "`python -m core.migrations --write-lock`"
        for name in sorted(current)
        if name not in locked
    ]
    problems += [
        f"{name}: có trong migrations.lock nhưng file đã bị xoá - nếu cố ý, chạy "
        "`python -m core.migrations --write-lock`"
        for name in sorted(locked)
        if name not in current
    ]
    return problems


def write_lock(path: Path = LOCK_PATH, directory: Path | None = None) -> list[str]:
    """Thêm dòng cho file mới, bỏ dòng của file đã xoá; trả danh sách file có
    nội dung khác lock (khi đó KHÔNG ghi gì - phải tạo migration mới)."""
    current = lock_entries(directory)
    locked = read_lock(path)
    changed = sorted(name for name in current if name in locked and locked[name] != current[name])
    if changed:
        return changed
    body = "".join(f"{name} {digest}\n" for name, digest in sorted(current.items()))
    path.write_text(_LOCK_HEADER + body, encoding="utf-8")
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.migrations",
        description="Kiểm tra hoặc cập nhật migrations.lock (checksum từng migration).",
    )
    parser.add_argument(
        "--write-lock",
        action="store_true",
        help="thêm file migration mới vào lock (từ chối nếu file đã ghim bị sửa)",
    )
    args = parser.parse_args(argv)
    if args.write_lock:
        changed = write_lock()
        if changed:
            print(
                "Từ chối cập nhật lock - migration đã ghim bị sửa nội dung: "
                + ", ".join(changed)
                + ". Hoàn tác thay đổi và đưa nó vào 1 file migration mới.",
                file=sys.stderr,
            )
            return 1
        print(f"Đã cập nhật {LOCK_PATH.name} ({len(lock_entries())} migration).")
        return 0
    problems = lock_problems(read_lock(), lock_entries())
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"{LOCK_PATH.name} khớp {len(lock_entries())} migration.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
