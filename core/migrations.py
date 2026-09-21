"""Versioned PostgreSQL schema migrations.

All application DDL lives in ``migrations/*.sql``. Runtime repositories only
ensure migrations have run; they do not CREATE/ALTER their own tables.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_lock = asyncio.Lock()
_ran_without_vector = False
_ran_with_vector = False


def _migration_files(*, include_vector: bool) -> list[tuple[int, Path]]:
    items: list[tuple[int, Path]] = []
    for path in sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version == 3 and not include_vector:
            continue
        items.append((version, path))
    return items


async def run(pool, *, include_vector: bool = False) -> None:
    """Apply pending migrations once, serialised across processes/instances.

    Non-vector runtimes still apply later ordinary migrations (for example 004)
    while skipping 003_vector.sql. A later vector-enabled caller can therefore
    safely come back and apply only migration 003.
    """
    global _ran_without_vector, _ran_with_vector
    if include_vector and _ran_with_vector:
        return
    if not include_vector and (_ran_without_vector or _ran_with_vector):
        return

    async with _lock:
        if include_vector and _ran_with_vector:
            return
        if not include_vector and (_ran_without_vector or _ran_with_vector):
            return
        async with pool.acquire() as conn:
            # Cross-instance lock: only one deployment migrates a shared DB at once.
            await conn.execute("SELECT pg_advisory_lock(718_026_091)")
            try:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                applied = await conn.fetch("SELECT version FROM schema_migrations")
                applied_versions = {int(row["version"]) for row in applied}
                for version, path in _migration_files(include_vector=include_vector):
                    if version in applied_versions:
                        continue
                    sql = path.read_text(encoding="utf-8")
                    async with conn.transaction():
                        await conn.execute(sql)
                        await conn.execute(
                            "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                            version,
                            path.name,
                        )
                    logger.info("Applied DB migration %03d: %s", version, path.name)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(718_026_091)")
        if include_vector:
            _ran_with_vector = True
            _ran_without_vector = True
        else:
            _ran_without_vector = True
