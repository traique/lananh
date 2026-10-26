"""Chuẩn hoá text trước khi gửi sang Zalo.

- ``to_plain_text``: bỏ markdown (Zalo không render ** hay ###).
- ``to_zalo_rich``: cùng chữ thuần đó + danh sách style kiểu zca-js 2.1.2
  (sendMessage({msg, styles})): chỗ nào markdown in đậm/nghiêng/tiêu đề thì gắn
  style tương ứng; tin cổ phiếu/danh mục (colorize=True) tô xanh % tăng, đỏ % giảm.
- ``zalo_rich_chunks``: cắt tin như services.channel_chat_service.split_for_zalo
  (CÙNG các đoạn chữ) và đổi style về toạ độ của từng đoạn.

Hình dạng style (khớp type ``Style`` trong zca-js/dist/apis/sendMessage.d.ts):
``{"start": int, "len": int, "st": str}`` - start/len tính theo ĐƠN VỊ UTF-16
(như JavaScript String.length: emoji ngoài BMP = 2), st là 1 giá trị TextStyle
("b", "i", "u", "s", "c_db342e" đỏ, "c_15a85f" xanh, "c_f27806" cam,
"c_f7b503" vàng, "f_13" chữ nhỏ, "f_18" chữ to). Không dùng lst_1/lst_2: gạch
đầu dòng vẫn là ký tự "• " trong chữ để gateway gửi chữ thuần (không style)
hiển thị y như cũ.
"""

from __future__ import annotations

import bisect
import re

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{2,3})(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_EXTRA_BLANK_LINES = re.compile(r"\n{3,}")
# % có dấu (+2,5% / -1.3% / −0,8 %): chỉ tô màu trong tin cổ phiếu/danh mục.
_SIGNED_PERCENT = re.compile(r"(?<![\w.,])([+\-−])\d+(?:[.,]\d+)?\s?%")

BOLD = "b"
ITALIC = "i"
RED = "c_db342e"
GREEN = "c_15a85f"

Span = tuple[int, int, str]  # (vị trí đầu, vị trí cuối - theo ký tự Python, style)


def _sub_tracked(
    pattern: re.Pattern, text: str, spans: list[Span], build
) -> tuple[str, list[Span]]:
    """Như pattern.sub() nhưng giữ được toạ độ style. ``build(match)`` trả
    (các phần thay thế, style mới theo toạ độ CŨ); mỗi phần là chuỗi chèn thêm
    hoặc cặp (đầu, cuối) giữ nguyên 1 đoạn của text cũ. Vị trí rơi vào phần bị
    xoá dồn về mép phần còn lại gần nhất."""
    out: list[str] = []
    segments: list[tuple[int, int, int]] = []  # (đầu cũ, cuối cũ, đầu mới)
    added: list[Span] = []
    size = 0
    pos = 0

    def keep(a: int, b: int) -> None:
        nonlocal size
        segments.append((a, b, size))
        out.append(text[a:b])
        size += b - a

    for match in pattern.finditer(text):
        if match.start() > pos:
            keep(pos, match.start())
        parts, extra = build(match)
        for part in parts:
            if isinstance(part, str):
                out.append(part)
                size += len(part)
            else:
                keep(*part)
        added.extend(extra)
        pos = match.end()
    if pos < len(text):
        keep(pos, len(text))
    starts = [segment[0] for segment in segments]

    def remap(p: int) -> int:
        index = bisect.bisect_right(starts, p) - 1
        if index < 0:
            return 0
        a, b, new_a = segments[index]
        return new_a + min(p, b) - a

    moved = [(remap(s), remap(e), st) for s, e, st in spans + added]
    return "".join(out), [(s, e, st) for s, e, st in moved if e > s]


def _heading(text: str):
    def build(match: re.Match):
        line_end = text.find("\n", match.end())
        line_end = len(text) if line_end < 0 else line_end
        title = text[match.end() : line_end].rstrip()
        return [], ([(match.end(), match.end() + len(title), BOLD)] if title else [])

    return build


def _emphasis(match: re.Match):
    marker = match.group(1)
    a, b = match.start(2), match.end(2)
    if len(marker) == 3:
        styles = [(a, b, BOLD), (a, b, ITALIC)]
    elif marker == "*":
        styles = [(a, b, ITALIC)]
    else:
        styles = [(a, b, BOLD)]
    return [(a, b)], styles


def _bullet(match: re.Match):
    return [(match.start(1), match.end(1)), "• "], []


def _blank_lines(match: re.Match):
    return [(match.start(), match.start() + 2)], []


def _rich_spans(text: str) -> tuple[str, list[Span]]:
    """(chữ thuần như to_plain_text cũ, style theo toạ độ ký tự Python)."""
    text = text or ""
    spans: list[Span] = []
    text, spans = _sub_tracked(_HEADING, text, spans, _heading(text))
    text, spans = _sub_tracked(_EMPHASIS, text, spans, _emphasis)
    text, spans = _sub_tracked(_BULLET, text, spans, _bullet)
    text, spans = _sub_tracked(_EXTRA_BLANK_LINES, text, spans, _blank_lines)
    lead = len(text) - len(text.lstrip())
    plain = text.strip()
    clipped = [(max(0, s - lead), min(len(plain), e - lead), st) for s, e, st in spans]
    return plain, [(s, e, st) for s, e, st in clipped if e > s]


def _color_spans(plain: str) -> list[Span]:
    return [
        (m.start(), m.end(), GREEN if m.group(1) == "+" else RED)
        for m in _SIGNED_PERCENT.finditer(plain)
    ]


def _utf16_prefix(text: str) -> list[int]:
    """prefix[i] = số đơn vị UTF-16 của text[:i]."""
    prefix = [0]
    for char in text:
        prefix.append(prefix[-1] + (2 if ord(char) > 0xFFFF else 1))
    return prefix


def _styles_for(spans: list[Span], prefix: list[int], lo: int, hi: int) -> list[dict]:
    """Style của đoạn plain[lo:hi], toạ độ UTF-16 tính từ đầu đoạn."""
    styles = []
    for start, end, st in sorted(set(spans)):
        a, b = max(start, lo), min(end, hi)
        if b > a:
            styles.append({"start": prefix[a] - prefix[lo], "len": prefix[b] - prefix[a], "st": st})
    return styles


def to_zalo_rich(text: str, *, colorize: bool = False) -> tuple[str, list[dict]]:
    """(chữ thuần, styles zca-js) - chữ thuần GIỐNG HỆT to_plain_text(text)."""
    plain, spans = _rich_spans(text)
    if colorize:
        spans += _color_spans(plain)
    return plain, _styles_for(spans, _utf16_prefix(plain), 0, len(plain))


def to_plain_text(text: str) -> str:
    """Bỏ markdown để Zalo không hiển thị ký tự thô như ** hay ###."""
    return _rich_spans(text)[0]


def _split_spans(text: str, limit: int) -> list[tuple[int, int]]:
    """Vị trí các đoạn mà split_for_zalo(text, limit) trả về (text đã strip)."""
    if not text:
        return []
    if len(text) <= limit:
        return [(0, len(text))]

    def stripped(a: int, b: int) -> tuple[int, int]:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        return a, b

    spans: list[tuple[int, int]] = []
    start, end = 0, len(text)
    while end - start > limit:
        remaining = text[start:end]
        cut = remaining.rfind("\n\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        spans.append(stripped(start, start + cut))
        start, end = stripped(start + cut, end)
    if end > start:
        spans.append((start, end))
    return spans


def zalo_rich_chunks(
    messages: list[str], limit: int = 1800, *, colorize: bool = False
) -> tuple[list[str], list[list[dict]]]:
    """Cắt từng tin như split_for_zalo(to_plain_text(tin)) và kèm styles của mỗi
    đoạn (cùng chỉ số). Chữ các đoạn trùng khớp bản chỉ có chữ thuần."""
    chunks: list[str] = []
    styles: list[list[dict]] = []
    for message in messages:
        plain, spans = _rich_spans(message)
        if colorize:
            spans += _color_spans(plain)
        prefix = _utf16_prefix(plain)
        for lo, hi in _split_spans(plain, limit):
            chunks.append(plain[lo:hi])
            styles.append(_styles_for(spans, prefix, lo, hi))
    return chunks, styles
