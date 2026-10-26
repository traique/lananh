"""migrations.lock ghim checksum từng migration: sửa file đã áp bị bắt ngay ở PR
(trước đây chỉ lộ ra lúc chạy sau deploy - runner báo lệch rồi bỏ qua thay đổi).

Thêm migration mới: ``python -m core.migrations --write-lock`` rồi commit lock.
"""

from pathlib import Path

from core import migrations


def test_lock_khop_moi_file_migration():
    problems = migrations.lock_problems(migrations.read_lock(), migrations.lock_entries())
    assert not problems, "\n".join(problems)


def test_lock_dung_cung_checksum_voi_runner():
    """Lock phải ghim đúng hàm checksum mà runner lưu vào schema_migrations
    (chuẩn hoá CRLF), không phải sha256 thô của bytes."""
    locked = migrations.read_lock()
    for _, path in migrations._migration_files():
        assert locked[path.name] == migrations.checksum(path.read_text("utf-8"))


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "001_a.sql").write_text("CREATE TABLE a (id int);\n", "utf-8")
    return directory, tmp_path / "migrations.lock"


def test_write_lock_chi_them_file_moi_va_bo_file_da_xoa(tmp_path):
    directory, lock = _setup(tmp_path)
    assert migrations.write_lock(lock, directory) == []
    assert set(migrations.read_lock(lock)) == {"001_a.sql"}
    assert lock.read_text("utf-8").startswith("# migrations.lock")

    (directory / "002_b.sql").write_text("CREATE TABLE b (id int);\n", "utf-8")
    problems = migrations.lock_problems(
        migrations.read_lock(lock), migrations.lock_entries(directory)
    )
    assert len(problems) == 1 and "002_b.sql" in problems[0] and "--write-lock" in problems[0]
    assert migrations.write_lock(lock, directory) == []
    assert set(migrations.read_lock(lock)) == {"001_a.sql", "002_b.sql"}

    (directory / "002_b.sql").unlink()
    assert migrations.write_lock(lock, directory) == []
    assert set(migrations.read_lock(lock)) == {"001_a.sql"}


def test_write_lock_tu_choi_khi_file_da_ghim_bi_sua(tmp_path):
    directory, lock = _setup(tmp_path)
    migrations.write_lock(lock, directory)
    before = lock.read_text("utf-8")
    (directory / "001_a.sql").write_text("CREATE TABLE a (id bigint);\n", "utf-8")
    assert migrations.write_lock(lock, directory) == ["001_a.sql"]
    assert lock.read_text("utf-8") == before  # không ghi đè để "im" lỗi
    problems = migrations.lock_problems(
        migrations.read_lock(lock), migrations.lock_entries(directory)
    )
    assert problems and "file migration mới" in problems[0]


def test_crlf_khong_bi_coi_la_sua_file(tmp_path):
    directory, lock = _setup(tmp_path)
    migrations.write_lock(lock, directory)
    (directory / "001_a.sql").write_bytes(b"CREATE TABLE a (id int);\r\n")
    assert (
        migrations.lock_problems(migrations.read_lock(lock), migrations.lock_entries(directory))
        == []
    )


def test_cli_check_va_write_lock(tmp_path, monkeypatch, capsys):
    directory, lock = _setup(tmp_path)
    monkeypatch.setattr(migrations, "_MIGRATIONS_DIR", directory)
    monkeypatch.setattr(migrations, "LOCK_PATH", lock)
    monkeypatch.setattr(migrations.write_lock, "__defaults__", (lock, None))
    monkeypatch.setattr(migrations.read_lock, "__defaults__", (lock,))
    assert migrations.main([]) == 1  # chưa có lock
    assert migrations.main(["--write-lock"]) == 0
    assert migrations.main([]) == 0
    (directory / "001_a.sql").write_text("ALTER TABLE a ADD COLUMN x int;\n", "utf-8")
    assert migrations.main(["--write-lock"]) == 1
    assert "001_a.sql" in capsys.readouterr().err
