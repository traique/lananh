"""Diễn tập khôi phục bản sao lưu DB - CHỈ chạy trong GitHub Actions
(.github/workflows/backup.yml, job restore-drill), không chạy trên Render.

Sau khi pg_restore bản dump vào 1 Postgres tạm, script này:

1. Chạy migration runner thật (``core.migrations.apply_locked``): migration đã áp
   mà file trong repo có checksum khác là LỖI; migration mới hơn bản dump được
   áp thêm (đúng như khi khôi phục sang project Supabase mới rồi deploy).
2. Kiểm tra các bảng lõi (trí nhớ, chat, ghi chú, nhắc việc, danh mục...) có mặt.
3. Đối chiếu với số dòng đếm lúc sao lưu (row_counts.json, mã hoá cùng bản dump):
   bảng có dữ liệu ở DB nguồn thì sau khi khôi phục cũng phải có dữ liệu - trừ
   các bảng cố ý bỏ dữ liệu (--exclude).

Repo public, log Actions ai đăng nhập GitHub cũng xem được: chỉ in tên bảng và
OK/LỖI, KHÔNG in số dòng hay nội dung.

Chạy: python -m scripts.backup_drill --dsn URL --counts row_counts.json --exclude "a b"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from core import migrations

# Bảng phải có mặt sau khi khôi phục (dữ liệu bền của trợ lý).
REQUIRED_TABLES = (
    "schema_migrations",
    "settings",
    "chat_messages",
    "user_facts",
    "user_memory_summary",
    "user_memory_highlights",
    "notes",
    "reminders",
    "stock_holdings",
    "zalo_users",
)


def compare_counts(
    source: dict[str, int], restored: dict[str, int | None], excluded: set[str]
) -> list[str]:
    """Danh sách vấn đề (rỗng = đạt). ``restored[bảng] is None`` = thiếu bảng."""
    problems: list[str] = []
    for table in REQUIRED_TABLES:
        if restored.get(table) is None:
            problems.append(f"{table}: thiếu bảng sau khi khôi phục")
    for table, count in sorted(source.items()):
        if table in excluded or (table in REQUIRED_TABLES and restored.get(table) is None):
            continue
        got = restored.get(table)
        if got is None:
            problems.append(f"{table}: thiếu bảng sau khi khôi phục")
        elif count > 0 and got == 0:
            problems.append(f"{table}: rỗng sau khi khôi phục dù DB nguồn có dữ liệu")
    return problems


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def restored_counts(conn, tables: set[str]) -> dict[str, int | None]:
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    existing = {row["table_name"] for row in rows}
    counts: dict[str, int | None] = {}
    for table in sorted(tables):
        counts[table] = (
            int(await conn.fetchval(f"SELECT count(*) FROM public.{_quote(table)}"))
            if table in existing
            else None
        )
    return counts


def load_counts(path: Path) -> dict[str, int]:
    return {str(k): int(v) for k, v in json.loads(path.read_text("utf-8")).items()}


async def run(dsn: str, source: dict[str, int], excluded: set[str]) -> int:
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            had_runner_table = await conn.fetchval(
                "SELECT to_regclass('public.schema_migrations') IS NOT NULL"
            )
        mismatches = await migrations.apply_locked(pool)
        async with pool.acquire() as conn:
            restored = await restored_counts(conn, set(source) | set(REQUIRED_TABLES))
    finally:
        await pool.close()

    problems = [] if had_runner_table else ["bản dump không có bảng schema_migrations"]
    problems += [
        f"migration {item}: file trong repo khác checksum lúc đã áp vào DB nguồn"
        for item in mismatches
    ]
    problems += compare_counts(source, restored, excluded)
    checked = sorted(t for t in restored if t not in excluded)
    bad = {line.split(":", 1)[0] for line in problems}
    for table in checked:
        print(f"{'LỖI' if table in bad else 'OK '} {table}")
    for problem in problems:
        print(f"LỖI {problem}", file=sys.stderr)
    print(
        "Diễn tập khôi phục: ĐẠT."
        if not problems
        else f"Diễn tập khôi phục: KHÔNG ĐẠT ({len(problems)} vấn đề)."
    )
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diễn tập khôi phục bản sao lưu DB.")
    parser.add_argument("--dsn", required=True, help="Postgres tạm đã pg_restore bản dump")
    parser.add_argument("--counts", required=True, type=Path, help="row_counts.json đã giải mã")
    parser.add_argument("--exclude", default="", help="bảng bỏ dữ liệu, cách nhau dấu cách")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.dsn, load_counts(args.counts), set(args.exclude.split())))


if __name__ == "__main__":
    raise SystemExit(main())
