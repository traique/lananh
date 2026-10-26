"""SQL giám sát vận hành / sao lưu / xoay khoá trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng
một lần). Dùng chung fixture ``delivery_db`` của file đó.
"""

from datetime import timedelta

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db

# Dùng lại fixture DB của test_delivery_sql.py (pytest tìm fixture theo tên trong module).
sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db


@pytest.mark.asyncio
async def test_database_bytes_doc_duoc(delivery_db):
    assert await db.database_bytes() > 0


@pytest.mark.asyncio
async def test_webhook_backlog_chi_dem_pending_da_toi_han_qua_lau(delivery_db):
    pool = delivery_db
    await pool.execute(
        """
        INSERT INTO webhook_inbox (channel, event_id, status, available_at) VALUES
            ('telegram', 'cu', 'PENDING', now() - interval '30 minutes'),
            ('telegram', 'moi', 'PENDING', now() - interval '1 minute'),
            ('zoom', 'backoff', 'PENDING', now() + interval '20 minutes'),
            ('zoom', 'dang-xu-ly', 'PROCESSING', now() - interval '40 minutes'),
            ('telegram', 'xong', 'DONE', now() - interval '50 minutes')
        """
    )
    stale, oldest = await db.webhook_backlog(timedelta(minutes=10))
    assert stale == 1
    assert oldest is not None and 29 * 60 <= oldest <= 31 * 60
    assert await db.webhook_backlog(timedelta(hours=2)) == (0, None)


@pytest.mark.asyncio
async def test_backup_runs_lay_luot_moi_nhat_theo_loai(delivery_db):
    pool = delivery_db
    assert await db.latest_backup_run("backup") is None
    await pool.execute(
        """
        INSERT INTO backup_runs (run_key, kind, finished_at, size_bytes) VALUES
            ('1-1-backup', 'backup', now() - interval '2 days', 100),
            ('2-1-backup', 'backup', now() - interval '1 day', 200),
            ('2-1-drill', 'drill', now() - interval '1 day', NULL)
        """
    )
    latest = await db.latest_backup_run("backup")
    assert latest["run_key"] == "2-1-backup" and latest["size_bytes"] == 200
    assert (await db.latest_backup_run("drill"))["run_key"] == "2-1-drill"
    with pytest.raises(Exception, match="backup_runs_kind_check"):
        await pool.execute("INSERT INTO backup_runs (run_key, kind) VALUES ('x', 'khac')")


@pytest.mark.asyncio
async def test_migration_023_chay_lai_duoc(delivery_db):
    sql = (_delivery_sql.MIGRATIONS / "023_backup_runs.sql").read_text("utf-8")
    await delivery_db.execute(sql)  # đã áp qua migrate(): chạy lại không lỗi
    assert await delivery_db.fetchval("SELECT to_regclass('backup_runs') IS NOT NULL")
