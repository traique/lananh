"""Phiên bản bot (core/version.py) đọc từ CHANGELOG.md và lệnh /phienban."""

from datetime import date
from pathlib import Path

import pytest

from core import version
from services import release_notes

ROOT = Path(__file__).resolve().parent.parent


def _key(release: version.Release) -> tuple[int, ...]:
    return tuple(int(part) for part in release.version.split("."))


@pytest.fixture(autouse=True)
def _fresh_cache():
    version.releases.cache_clear()
    yield
    version.releases.cache_clear()


def test_changelog_that_hop_le_moi_nhat_o_tren():
    items = version.releases()
    assert items, "CHANGELOG.md phải có ít nhất 1 mục ## X.Y.Z - YYYY-MM-DD"
    keys = [_key(release) for release in items]
    assert keys == sorted(keys, reverse=True) and len(set(keys)) == len(keys)
    days = [release.released for release in items]
    assert days == sorted(days, reverse=True)
    for release in items:
        assert release.notes, release.version
        assert all(len(note) <= 300 for note in release.notes), release.version
    assert version.current() == items[0]
    assert version.version() == items[0].version


def test_image_docker_giu_changelog():
    """.dockerignore bỏ *.md: thiếu dòng !CHANGELOG.md thì bản chạy trên Render mất
    số phiên bản và tin báo bản mới không có nội dung."""
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "*.md" in lines
    assert lines.index("!CHANGELOG.md") > lines.index("*.md")


def test_parse_noi_dong_va_bo_muc_khong_phai_phien_ban():
    text = """# Lịch sử

Giới thiệu - bị bỏ qua.

## Chưa phát hành

- việc đang làm

## 2.1.0 - 2026-11-02

- Dòng một
  nối tiếp dòng một
* Dòng hai

## v2.0.10 - 2026-11-01

- Sửa lỗi
"""
    items = version.parse_changelog(text)
    assert [(r.version, r.released) for r in items] == [
        ("2.1.0", date(2026, 11, 2)),
        ("2.0.10", date(2026, 11, 1)),
    ]
    assert items[0].notes == ("Dòng một nối tiếp dòng một", "Dòng hai")
    assert _key(items[1]) == (2, 0, 10) and _key(items[0]) > _key(items[1])


def test_changelog_hong_thi_khong_raise(tmp_path, monkeypatch):
    broken = tmp_path / "CHANGELOG.md"
    broken.write_text("## 1.0.0 - 2026-13-45\n- ngày sai\n", encoding="utf-8")
    monkeypatch.setattr(version, "CHANGELOG_PATH", broken)
    assert version.releases() == () and version.version() == version.UNKNOWN
    version.releases.cache_clear()
    monkeypatch.setattr(version, "CHANGELOG_PATH", tmp_path / "khong-co.md")
    assert version.current() is None
    assert "chưa đọc được" in release_notes.history_text()


def test_nhan_phien_ban_kem_commit(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567890")
    assert version.label() == f"{version.version()} (abcdef1)"
    monkeypatch.delenv("RENDER_GIT_COMMIT")
    assert version.label() == version.version()


def _sample(tmp_path, monkeypatch, count: int) -> None:
    blocks = [
        f"## 1.{i}.0 - 2026-10-{10 + i:02d}\n\n- Tính năng {i}\n" for i in range(count, 0, -1)
    ]
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# L\n\n" + "\n".join(blocks), encoding="utf-8")
    monkeypatch.setattr(version, "CHANGELOG_PATH", path)


def test_phienban_mac_dinh_3_ban_all_toi_da_10(tmp_path, monkeypatch):
    _sample(tmp_path, monkeypatch, 12)
    text = release_notes.history_text()
    assert text.startswith("🏷️ Lan Anh đang chạy bản 1.12.0")
    assert "1.10.0 — 20/10/2026" in text and "• Tính năng 10" in text
    assert "1.9.0" not in text and "Còn 9 bản cũ hơn - gõ /phienban all" in text
    full = release_notes.history_text("all")
    assert "1.3.0" in full and "1.2.0" not in full
    # Đã là "all" thì không bảo gõ lại "/phienban all" (in y hệt).
    assert "/phienban all" not in full and "CHANGELOG.md" in full
    assert "1.11.0" in release_notes.history_text("2") and "1.10.0" not in (
        release_notes.history_text("2")
    )
    assert "1.12.0" in release_notes.history_text("0")


def test_phienban_khong_vuot_tran_tin(tmp_path, monkeypatch):
    long_note = "x" * 290
    blocks = [
        f"## 1.{i}.0 - 2026-10-{10 + i:02d}\n" + f"- {long_note}\n" * 8 for i in range(9, 0, -1)
    ]
    path = tmp_path / "CHANGELOG.md"
    path.write_text("\n".join(blocks), encoding="utf-8")
    monkeypatch.setattr(version, "CHANGELOG_PATH", path)
    text = release_notes.history_text("all")
    assert len(text) <= 3700 and "bản cũ hơn" in text and "/phienban all" not in text
    # Mặc định 3 bản nhưng chạm trần độ dài: "/phienban all" cũng không giúp gì.
    assert "/phienban all" not in release_notes.history_text()
