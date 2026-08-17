"""Chuẩn hoá text trước khi gửi sang Zalo. Zalo chỉ hiển thị plain text."""

import re

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{2,3})(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_EXTRA_BLANK_LINES = re.compile(r"\n{3,}")


def to_plain_text(text: str) -> str:
    """Bỏ markdown để Zalo không hiển thị ký tự thô như ** hay ###."""
    cleaned = _HEADING.sub("", text or "")
    cleaned = _EMPHASIS.sub(r"\2", cleaned)
    cleaned = _BULLET.sub(r"\1• ", cleaned)
    cleaned = _EXTRA_BLANK_LINES.sub("\n\n", cleaned)
    return cleaned.strip()
