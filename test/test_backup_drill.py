"""Diễn tập khôi phục (scripts/backup_drill.py, chạy trên GitHub Actions): đối chiếu
bảng sau khi khôi phục, lệch checksum migration là lỗi, không in số dòng."""

import json

import pytest

from scripts import backup_drill


def test_doi_chieu_bang():
    required = {t: 1 for t in backup_drill.REQUIRED_TABLES}
    source = {**required, "chat_messages": 5, "notes": 0, "webhook_inbox": 9, "zalo_groups": 2}
    restored = {**required, "chat_messages": 5, "notes": 0, "webhook_inbox": 0, "zalo_groups": 2}
    assert backup_drill.compare_counts(source, restored, {"webhook_inbox"}) == []
    restored["zalo_groups"] = 0
    restored["user_facts"] = None
    problems = backup_drill.compare_counts(source, restored, {"webhook_inbox"})
    assert problems == [
        "user_facts: thiếu bảng sau khi khôi phục",
        "zalo_groups: rỗng sau khi khôi phục dù DB nguồn có dữ liệu",
    ]


class _Conn:
    def __init__(self, counts):
        self.counts = counts

    async def fetch(self, sql, *args):
        return [{"table_name": name} for name in self.counts]

    async def fetchval(self, sql, *args):
        if "to_regclass" in sql:
            return "schema_migrations" in self.counts
        return self.counts[sql.rsplit(".", 1)[1].strip('"')]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_run_lech_checksum_la_loi_va_khong_in_so_dong(tmp_path, monkeypatch, capsys):
    import asyncpg

    counts = {t: 7 for t in backup_drill.REQUIRED_TABLES}
    path = tmp_path / "row_counts.json"
    path.write_text(json.dumps(counts), "utf-8")

    async def create_pool(dsn, **kwargs):
        return _Pool(_Conn(counts))

    mismatches: list[str] = []

    async def apply_locked(pool):
        return list(mismatches)

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(backup_drill.migrations, "apply_locked", apply_locked)
    source = backup_drill.load_counts(path)
    assert await backup_drill.run("postgresql://drill", source, set()) == 0
    out = capsys.readouterr().out
    assert "ĐẠT" in out and "7" not in out  # chỉ tên bảng + OK/LỖI

    mismatches.append("008 (008_delivery_recovery.sql)")
    assert await backup_drill.run("postgresql://drill", source, set()) == 1
    captured = capsys.readouterr()
    assert "KHÔNG ĐẠT" in captured.out and "008_delivery_recovery.sql" in captured.err


def test_quote_ten_bang():
    assert backup_drill._quote('a"b') == '"a""b"'
