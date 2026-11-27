"""Unit test cho core/migrations.py: áp migration theo thứ tự, checksum (điền bù
cho version cũ, phát hiện file bị sửa sau khi áp), bỏ qua version đã áp nhưng file
đã xoá (003_vector.sql của pgvector)."""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from core import migrations

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


class FakeConn:
    """Mô phỏng đúng các câu lệnh runner dùng; ghi lại SQL migration đã chạy."""

    def __init__(self, applied=None):
        # version -> checksum (None = áp từ trước khi có cột checksum)
        self.applied: dict[int, str | None] = dict(applied or {})
        self.executed: list[str] = []
        self.timeouts: list[float | None] = []  # timeout của từng câu migration
        self.locks: list[tuple[str, float | None]] = []
        self.tx_settings: list[str] = []
        self.in_tx = False
        self.lock_free = True  # pg_try_advisory_lock thành công ngay?
        self.fail_on: str | None = None

    async def fetchval(self, sql, *args):
        assert sql.startswith("SELECT pg_try_advisory_lock")
        self.locks.append((sql, None))
        return self.lock_free

    async def execute(self, sql, *args, **kwargs):
        timeout = kwargs.get("timeout")  # như asyncpg: execute(sql, *args, timeout=None)
        if sql.startswith("SELECT pg_advisory"):
            self.locks.append((sql, timeout))
            return "SELECT 1"
        if sql.startswith("SET LOCAL"):
            assert self.in_tx, "SET LOCAL phải nằm trong transaction của migration"
            self.tx_settings.append(sql)
            return "SET"
        head = sql.lstrip()
        if head.startswith(
            ("CREATE TABLE IF NOT EXISTS schema_migrations", "ALTER TABLE schema_migrations")
        ):
            return "OK"
        if sql.startswith("UPDATE schema_migrations"):
            version, digest = args
            if self.applied.get(version) is None:
                self.applied[version] = digest
            return "UPDATE 1"
        if sql.startswith("INSERT INTO schema_migrations"):
            version, _name, digest = args
            self.applied[version] = digest
            return "INSERT 0 1"
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("migration lỗi mô phỏng")
        self.executed.append(sql)
        self.timeouts.append(timeout)
        return "OK"

    async def fetch(self, sql, *args):
        return [{"version": v, "checksum": c} for v, c in self.applied.items()]

    @asynccontextmanager
    async def transaction(self):
        snapshot = dict(self.applied)
        self.in_tx = True
        try:
            yield
        except BaseException:
            self.applied = snapshot
            raise
        finally:
            self.in_tx = False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture(autouse=True)
def fresh_runner(monkeypatch):
    monkeypatch.setattr(migrations, "_ran", False)


@pytest.fixture
def sent_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(migrations.alerts, "send", lambda text: sent.append(text) or True)
    return sent


def _files():
    return {version: path for version, path in migrations._migration_files()}


@pytest.mark.asyncio
async def test_fresh_db_applies_all_with_checksums(sent_alerts, caplog):
    caplog.set_level("INFO", logger="core.migrations")
    conn = FakeConn()
    assert await migrations.run(FakePool(conn)) == len(_files())
    files = _files()
    assert 3 not in files  # 003_vector.sql đã xoá: DB mới không còn tạo chat_embeddings
    expected = sorted(files)
    assert sorted(conn.applied) == expected
    for version in expected:
        assert conn.applied[version] == migrations.checksum(files[version].read_text("utf-8"))
    assert len(conn.executed) == len(expected)
    assert conn.locks[0][0].startswith("SELECT pg_try_advisory_lock")
    assert "unlock" in conn.locks[-1][0] and len(conn.locks) == 2  # không phải chờ khoá
    # Mỗi migration: DDL dài được tới 600 s (không phải command_timeout 30 s của pool),
    # chờ khoá bảng tối đa 10 s - đặt trong CHÍNH transaction của migration.
    assert conn.timeouts == [600] * len(expected)
    assert len(conn.tx_settings) == len(expected)
    assert all("lock_timeout = '10s'" in s for s in conn.tx_settings)
    # Danh sách version sắp áp được log TRƯỚC khi áp (deploy treo thì biết file nào).
    messages = [r.getMessage() for r in caplog.records]
    pending = next(i for i, m in enumerate(messages) if m.startswith("Sắp áp"))
    applied = next(i for i, m in enumerate(messages) if m.startswith("Applied DB migration"))
    assert pending < applied and files[expected[-1]].name in messages[pending]
    assert not sent_alerts
    assert await migrations.run(FakePool(conn)) == 0  # đã chạy trong process này


@pytest.mark.asyncio
async def test_instance_khac_dang_giu_khoa_thi_cho_co_han(sent_alerts, caplog):
    caplog.set_level("INFO", logger="core.migrations")
    files = _files()
    conn = FakeConn({v: migrations.checksum(files[v].read_text("utf-8")) for v in files})
    conn.lock_free = False
    assert await migrations.run(FakePool(conn)) == 0
    assert [sql.split("(")[0] for sql, _ in conn.locks] == [
        "SELECT pg_try_advisory_lock",
        "SELECT pg_advisory_lock",
        "SELECT pg_advisory_unlock",
    ]
    assert conn.locks[1][1] == 600  # chờ instance kia áp xong, có trần
    assert any("Instance khác" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_legacy_rows_get_checksum_backfilled_without_reapplying(sent_alerts):
    files = _files()
    conn = FakeConn({v: None for v in files})
    await migrations.run(FakePool(conn))
    assert conn.executed == []  # không áp lại gì
    assert all(
        conn.applied[v] == migrations.checksum(files[v].read_text("utf-8")) for v in conn.applied
    )
    assert not sent_alerts


@pytest.mark.asyncio
async def test_edited_applied_migration_is_reported_not_reapplied(sent_alerts, caplog):
    files = _files()
    applied = {v: migrations.checksum(files[v].read_text("utf-8")) for v in files}
    applied[8] = "stale-checksum-from-old-file"
    conn = FakeConn(applied)
    await migrations.run(FakePool(conn))
    assert conn.executed == []
    assert conn.applied[8] == "stale-checksum-from-old-file"  # không ghi đè, để còn phát hiện
    assert len(sent_alerts) == 1 and "008" in sent_alerts[0]
    assert any("008" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


@pytest.mark.asyncio
async def test_applied_version_without_file_is_ignored(sent_alerts):
    """DB cũ đã áp 003_vector.sql: file đã xoá nhưng dòng schema_migrations còn -
    runner không báo lệch, không áp lại gì; 014 (nếu chưa áp) dọn bảng."""
    files = _files()
    applied = {v: migrations.checksum(files[v].read_text("utf-8")) for v in files if v != 14}
    applied[3] = "checksum-of-deleted-003"
    conn = FakeConn(applied)
    await migrations.run(FakePool(conn))
    assert len(conn.executed) == 1 and "DROP TABLE IF EXISTS chat_embeddings" in conn.executed[0]
    assert conn.applied[3] == "checksum-of-deleted-003"
    assert not sent_alerts


@pytest.mark.asyncio
async def test_failed_migration_is_not_recorded_and_lock_is_released():
    conn = FakeConn()
    conn.fail_on = "webhook_inbox_status_check"  # nằm trong 013
    with pytest.raises(RuntimeError):
        await migrations.run(FakePool(conn))
    assert 13 not in conn.applied and 11 in conn.applied
    assert "unlock" in conn.locks[-1][0]
    assert migrations._ran is False  # lần sau còn thử lại


def test_checksum_ignores_line_ending_style():
    assert migrations.checksum("a\r\nb\n") == migrations.checksum("a\nb\n")
    assert migrations.checksum("a\nb") != migrations.checksum("a\nc")


def test_every_migration_statement_set_is_rerunnable():
    """Quy ước: câu ADD CONSTRAINT phải có DROP ... IF EXISTS đi trước (chạy lại an toàn)."""
    for path in MIGRATIONS.glob("*.sql"):
        lines = path.read_text("utf-8").splitlines()
        for index, line in enumerate(lines):
            if "ADD CONSTRAINT" in line:
                name = line.split("ADD CONSTRAINT", 1)[1].split()[0]
                before = "\n".join(lines[:index])
                assert (
                    f"DROP CONSTRAINT IF EXISTS {name}" in before or "DROP CONSTRAINT %I" in before
                ), f"{path.name}: ADD CONSTRAINT {name} không có DROP IF EXISTS trước đó"


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("TEST_PGLITE") != "1", reason="set TEST_PGLITE=1 để chạy trên PGlite")
async def test_runner_on_real_postgres_is_idempotent_and_013_allows_dead(sent_alerts):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "sql_support"))
    from pglite_pool import Pool

    pool = await Pool.create()
    try:
        await migrations.run(pool)
        migrations._ran = False
        await migrations.run(pool)  # chạy lại: không áp gì thêm, không báo lệch
        assert not sent_alerts
        rows = await pool.fetch("SELECT version, checksum FROM schema_migrations ORDER BY version")
        assert [r["version"] for r in rows] == sorted(_files())
        assert all(r["checksum"] for r in rows)
        await pool.execute(
            "INSERT INTO webhook_inbox (channel, event_id, status) VALUES ('telegram', 'x', 'DEAD')"
        )
        # 008 chạy lại được (ADD CONSTRAINT đã có DROP IF EXISTS).
        await pool.execute((MIGRATIONS / "008_delivery_recovery.sql").read_text("utf-8"))
    finally:
        await pool.close()
