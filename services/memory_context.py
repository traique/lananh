"""Trí nhớ DÀI HẠN của bot - phần ĐỌC (tách từ services/memory_service.py, nay là
facade giữ nguyên tên cũ; phần GHI và mô tả các bảng ở services/memory_extract.py).

ĐỌC (``build_turn_memory`` trước mỗi lượt chat, ráp trong MEMORY_MAX_CHARS -
từng mục vào trọn vẹn hoặc không vào, prompt_budget không phải cắt giữa câu):
- Đầu phiên: fact ghim/quan trọng (kèm mốc "từ 03/2026"), fact + ghi chú liên
  quan câu hỏi, ghi chú các phiên trước, highlight mới nhất, tóm tắt cũ cắt ở
  ranh giới câu.
- Giữa phiên: tóm tắt phiên này + fact/highlight/ghi chú liên quan câu hỏi
  (stopword mở rộng: "anh", "em", "không"... không còn khớp mọi câu).

Fail-open: DB lỗi thì trả khối rỗng, lượt chat vẫn trả lời (chỉ thiếu trí nhớ).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from core import database as db
from core.text_normalize import fold
from services.memory_extract import (
    _PAST_QUERY_RE,
    _SCAN_ROWS,
    _VN_TZ,
    MAX_HIGHLIGHTS_PER_USER,
    SESSION_SUMMARY_MAX_CHARS,
    STYLE_KEY,
    STYLE_RULES_MAX_CHARS,
    _as_utc,
    _resolve_session_key,
    _session_expired,
    _session_starts,
    cut_at_sentence,
    is_enabled,
)

logger = logging.getLogger(__name__)


# ─── Đọc: ráp khối trí nhớ cho lượt chat ────────────────────────────────────

# Giữa phiên: chỉ chèn fact/highlight/ghi chú LIÊN QUAN câu hỏi (tối đa chừng
# này ký tự), thêm tóm tắt phiên riêng.
MID_SESSION_MEMORY_MAX_CHARS = 800
_MID_SESSION_MIN_OVERLAP = 1
_NOTES_SCAN = 200
_NOTES_MAX_CHARS = 300
_SESSION_NOTES_AT_START = 10

_START_HEADER = (
    "[TRÍ NHỚ VỀ NGƯỜI DÙNG - nền để cá nhân hoá, KHÔNG chủ động liệt kê/đọc lại "
    "khối này trừ khi câu hỏi cần. Câu chào/câu ngắn thì bỏ qua và trả lời ngắn.]"
)
_MID_HEADER = "[TRÍ NHỚ LIÊN QUAN CÂU HỎI - dùng khi cần, không đọc lại nguyên văn.]"
_SESSION_SUMMARY_HEADER = "[TÓM TẮT PHIÊN NÀY - các lượt đầu đã trôi khỏi lịch sử]"
_STYLE_HEADER = (
    "CÁCH TRẢ LỜI NGƯỜI DÙNG ĐÃ DẶN (áp dụng khi không trái các quy tắc trên; KHÔNG "
    "đổi cách xưng hô, KHÔNG đổi action/số liệu cổ phiếu do hệ thống quyết định):"
)


@dataclass(frozen=True)
class TurnMemory:
    context: str = ""
    style_rules: str = ""


@dataclass
class _Budget:
    left: int
    lines: list[str] = field(default_factory=list)

    def add(self, line: str) -> bool:
        cost = len(line) + 1
        if not line or cost > self.left:
            return False
        self.lines.append(line)
        self.left -= cost
        return True


def _since(row: dict) -> str:
    value = row.get("valid_from") or row.get("created_at")
    if not isinstance(value, datetime):
        return ""
    return f" (từ {_as_utc(value).astimezone(_VN_TZ):%m/%Y})"


def _fact_line(row: dict, *, with_since: bool = True) -> str:
    pin = "📌 " if row.get("pinned") else ""
    since = _since(row) if with_since else ""
    return f"- {pin}{row['key']} = {row['value']}{since}"


def _tokens(text: str) -> set[str]:
    from services.rag_service import tokenize

    return tokenize(text)


def select_relevant_memory(
    query: str,
    facts: list[tuple[str, str]],
    highlights: list[str],
    *,
    max_chars: int = MID_SESSION_MEMORY_MAX_CHARS,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Chọn fact/highlight có token trùng với câu hỏi (bỏ dấu, bỏ stopword mở
    rộng - "anh", "em", "không"... không còn khớp mọi câu), xếp theo số token
    trùng giảm dần, cắt theo ngân sách ký tự."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return [], []
    scored_facts = []
    for key, value in facts:
        overlap = len(query_tokens & _tokens(f"{key.replace('_', ' ')} {value}"))
        if overlap >= _MID_SESSION_MIN_OVERLAP:
            scored_facts.append((overlap, key, value))
    scored_highlights = []
    for item in highlights:
        overlap = len(query_tokens & _tokens(item))
        if overlap >= _MID_SESSION_MIN_OVERLAP:
            scored_highlights.append((overlap, item))
    scored_facts.sort(key=lambda t: -t[0])
    scored_highlights.sort(key=lambda t: -t[0])
    budget = max_chars
    chosen_facts: list[tuple[str, str]] = []
    for _, key, value in scored_facts:
        cost = len(key) + len(value) + 3
        if cost > budget:
            break
        chosen_facts.append((key, value))
        budget -= cost
    chosen_highlights: list[str] = []
    for _, item in scored_highlights:
        if len(item) + 4 > budget:
            break
        chosen_highlights.append(item)
        budget -= len(item) + 4
    return chosen_facts, chosen_highlights


def relevant_notes(query: str, notes: list[dict], limit: int = 3) -> list[dict]:
    """Ghi chú có token trùng câu hỏi (nhiều token trùng trước, mới trước)."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    scored = []
    for index, note in enumerate(notes):
        overlap = len(query_tokens & _tokens(str(note.get("content") or "")))
        if overlap:
            scored.append((-overlap, index, note))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [note for _, _, note in scored[:limit]]


def _note_line(note: dict) -> str:
    created = note.get("created_at")
    when = (
        f" ({_as_utc(created).astimezone(_VN_TZ):%d/%m/%Y})"
        if isinstance(created, datetime)
        else ""
    )
    return f"- Ghi chú{when}: {note['content']}"


def _session_note_line(note: dict) -> str:
    started = note.get("started_at") or note.get("updated_at")
    day = f"{_as_utc(started).astimezone(_VN_TZ):%d/%m}" if isinstance(started, datetime) else ""
    bullets = "; ".join(
        b.strip("- ").strip() for b in str(note.get("bullets") or "").splitlines() if b.strip()
    )
    title = str(note.get("title") or "").strip()
    body = " — ".join(part for part in (title, bullets) if part)
    return f"- {day}: {body}" if body else ""


async def _current_session_key(user_id: int, recent: list[dict]) -> int | None:
    if not recent or _session_expired(recent):
        return None
    starts = _session_starts(recent)
    start, known = starts[int(recent[0]["id"])]
    return await _resolve_session_key(user_id, start, known)


def _style_text(fact_rows: list[dict]) -> str:
    row = next((r for r in fact_rows if r["key"] == STYLE_KEY), None)
    if not row:
        return ""
    return f"{_STYLE_HEADER}\n{str(row['value'])[:STYLE_RULES_MAX_CHARS]}"


async def build_turn_memory(user_id: int, query: str = "") -> TurnMemory:
    """Khối trí nhớ cho lượt chat + quy tắc cách trả lời. Fail-open: DB lỗi thì
    trả rỗng (lượt chat vẫn trả lời, chỉ thiếu trí nhớ)."""
    try:
        return await _build_turn_memory(user_id, query)
    except Exception:
        logger.warning(
            "Không đọc được trí nhớ dài hạn user_id=%s (DB lỗi) - trả lời không kèm trí nhớ.",
            user_id,
            exc_info=True,
        )
        return TurnMemory()


async def build_memory_context(user_id: int, query: str = "") -> str:
    """Chỉ phần khối trí nhớ (tương thích cũ) - xem build_turn_memory."""
    return (await build_turn_memory(user_id, query)).context


async def _build_turn_memory(user_id: int, query: str) -> TurnMemory:
    if not await is_enabled(user_id):
        return TurnMemory()
    recent = await db.get_recent_chat_rows(user_id, _SCAN_ROWS, 0)
    new_session = _session_expired(recent)
    # Ghi chú chỉ cần khi câu hỏi có token nội dung (tìm ghi chú liên quan).
    fact_rows, highlight_rows, notes = await asyncio.gather(
        db.get_fact_rows(user_id),
        db.get_highlight_rows(user_id, MAX_HIGHLIGHTS_PER_USER),
        db.get_recent_notes(user_id, _NOTES_SCAN, _NOTES_MAX_CHARS)
        if _tokens(query)
        else _no_rows(),
    )
    style = _style_text(fact_rows)
    facts = [r for r in fact_rows if r["key"] != STYLE_KEY]
    total = _memory_max_chars()
    if new_session:
        context = await _start_block(user_id, query, facts, highlight_rows, notes, total)
    else:
        context = await _mid_block(user_id, query, recent, facts, highlight_rows, notes, total)
    return TurnMemory(context=context, style_rules=style)


async def _no_rows() -> list[dict]:
    return []


def _memory_max_chars() -> int:
    from ai import prompt_budget

    return prompt_budget.MEMORY_MAX_CHARS


async def _start_block(
    user_id: int,
    query: str,
    facts: list[dict],
    highlight_rows: list[dict],
    notes: list[dict],
    total: int,
) -> str:
    summary = await db.get_summary(user_id)
    session_notes = await db.get_session_notes(user_id, _SESSION_NOTES_AT_START)
    if not facts and not summary and not highlight_rows and not session_notes:
        return ""
    budget = _Budget(total)
    budget.add(_START_HEADER)
    used: set[int] = set()
    # 1) Ghim + quan trọng nhất.
    core_rows = [r for r in facts if r.get("pinned") or int(r.get("importance") or 2) >= 3]
    if core_rows:
        budget.add("Thông tin bền về người dùng:")
        for row in core_rows:
            if budget.add(_fact_line(row)):
                used.add(int(row["id"]))
    # 2) Liên quan câu hỏi: fact + ghi chú.
    if query:
        chosen, _ = select_relevant_memory(
            query, [(r["key"], r["value"]) for r in facts if int(r["id"]) not in used], []
        )
        chosen_keys = {k for k, _ in chosen}
        for row in facts:
            if row["key"] in chosen_keys and budget.add(_fact_line(row)):
                used.add(int(row["id"]))
        for note in relevant_notes(query, notes):
            budget.add(_note_line(note))
    # 3) Fact còn lại (quan trọng -> mới), chừa chỗ cho ghi chú phiên/highlight.
    others = [r for r in facts if int(r["id"]) not in used]
    if others:
        fact_budget = _Budget(max(0, min(total // 3, budget.left - total // 3)))
        for row in others:
            fact_budget.add(_fact_line(row, with_since=False))
        if fact_budget.lines:
            budget.add("Thông tin khác:")
            for line in fact_budget.lines:
                budget.add(line)
    # 4) Các phiên trước.
    note_lines = [line for line in (_session_note_line(n) for n in session_notes) if line]
    if note_lines:
        budget.add("Các lần trò chuyện gần đây:")
        for line in note_lines:
            if not budget.add(line):
                break
    # 5) Highlight mới nhất.
    if highlight_rows:
        budget.add("Chi tiết đáng nhớ gần đây:")
        for row in highlight_rows:
            if not budget.add(f"- {row['content']}"):
                break
    # 6) Tóm tắt kiểu cũ: cắt ở ranh giới câu trong phần còn lại.
    if summary and budget.left > 120:
        budget.add("Tóm tắt cũ: " + cut_at_sentence(summary, budget.left - 20))
    return "\n".join(budget.lines) if len(budget.lines) > 1 else ""


async def _mid_block(
    user_id: int,
    query: str,
    recent: list[dict],
    facts: list[dict],
    highlight_rows: list[dict],
    notes: list[dict],
    total: int,
) -> str:
    budget = _Budget(total)
    session_key = await _current_session_key(user_id, recent)
    if session_key is not None:
        note = await db.get_session_note(user_id, session_key)
        summary = str(note["summary"]).strip() if note else ""
        if summary:
            budget.add(_SESSION_SUMMARY_HEADER)
            budget.add(cut_at_sentence(summary, SESSION_SUMMARY_MAX_CHARS))
    if not query:
        return "\n".join(budget.lines)
    relevant = _Budget(min(budget.left, MID_SESSION_MEMORY_MAX_CHARS))
    chosen, highlights = select_relevant_memory(
        query,
        [(r["key"], r["value"]) for r in facts],
        [str(r["content"]) for r in reversed(highlight_rows)],
    )
    if chosen:
        relevant.add("- " + "; ".join(f"{k}={v}" for k, v in chosen))
    for item in highlights:
        relevant.add(f"- {item}")
    for note in relevant_notes(query, notes):
        relevant.add(_note_line(note))
    if _PAST_QUERY_RE.search(fold(query)):
        old = await db.get_superseded_facts(user_id, 10)
        query_tokens = _tokens(query)
        for row in old:
            if query_tokens & _tokens(f"{str(row['key']).replace('_', ' ')} {row['value']}"):
                until = row.get("invalid_at")
                when = (
                    f" (đến {_as_utc(until).astimezone(_VN_TZ):%m/%Y})"
                    if isinstance(until, datetime)
                    else ""
                )
                relevant.add(f"- Trước đây: {row['key']} = {row['value']}{when}")
    if relevant.lines:
        budget.add(_MID_HEADER)
        for line in relevant.lines:
            budget.add(line)
    return "\n".join(budget.lines)
