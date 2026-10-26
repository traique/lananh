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
