"""Phiên bản bot và lịch sử phiên bản.

Nguồn duy nhất là CHANGELOG.md ở gốc repo: mục ``## X.Y.Z - YYYY-MM-DD`` đầu tiên là
bản đang chạy, các dòng ``- ...`` ngay dưới là nội dung cập nhật. Thêm bản mới = thêm
1 mục ở đầu file (không có số phiên bản viết cứng chỗ nào khác). Mã commit đang chạy
lấy từ RENDER_GIT_COMMIT (Render tự đặt; chạy local thì trống).

File được đọc 1 lần rồi cache (nhỏ, chỉ đổi khi deploy). Đọc/parse lỗi thì coi như
chưa có phiên bản ("?"), không bao giờ raise - /status và lúc khởi động không được
hỏng vì file này. .dockerignore phải giữ CHANGELOG.md trong image.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
UNKNOWN = "?"

_HEADING_RE = re.compile(r"^##\s+v?(\d+)\.(\d+)\.(\d+)\s+[-–]\s+(\d{4}-\d{2}-\d{2})\s*$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*\S)\s*$")


@dataclass(frozen=True)
class Release:
    version: str
    released: date
    notes: tuple[str, ...]

    @property
    def key(self) -> tuple[int, ...]:
        return tuple(int(part) for part in self.version.split("."))


def parse_changelog(text: str) -> tuple[Release, ...]:
    """Các mục ``## X.Y.Z - YYYY-MM-DD`` theo thứ tự trong file (mới nhất trước).
    Dòng thụt lề ngay sau 1 gạch đầu dòng là phần nối của gạch đó; nội dung ngoài
    mục (đầu file) bị bỏ qua. Ngày sai định dạng -> ValueError."""
    releases: list[Release] = []
    current: tuple[str, date] | None = None
    notes: list[str] = []

    def close() -> None:
        if current is not None:
            releases.append(Release(current[0], current[1], tuple(notes)))

    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            close()
            major, minor, patch, day = heading.groups()
            current = (f"{int(major)}.{int(minor)}.{int(patch)}", date.fromisoformat(day))
            notes = []
            continue
        if line.startswith("## "):
            # Mục khác (vd "## Chưa phát hành") kết thúc mục phiên bản đang đọc.
            close()
            current = None
            continue
        if current is None:
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            notes.append(bullet.group(1))
        elif notes and line.startswith((" ", "\t")) and line.strip():
            notes[-1] = f"{notes[-1]} {line.strip()}"
    close()
    return tuple(releases)


@lru_cache(maxsize=1)
def releases() -> tuple[Release, ...]:
    try:
        return parse_changelog(CHANGELOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Không đọc được %s - chưa có số phiên bản.", CHANGELOG_PATH, exc_info=True)
        return ()


def current() -> Release | None:
    items = releases()
    return items[0] if items else None


def version() -> str:
    release = current()
    return release.version if release else UNKNOWN


def commit() -> str:
    """Mã commit ngắn đang chạy (Render), trống khi chạy local."""
    return os.getenv("RENDER_GIT_COMMIT", "").strip()[:7]


def label() -> str:
    """Vd "1.0.0 (abc1234)" - dùng cho /status, /phienban, tin báo bản mới."""
    sha = commit()
    return f"{version()} ({sha})" if sha else version()
