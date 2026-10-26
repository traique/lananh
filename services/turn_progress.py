"""Giai đoạn của lượt trả lời đang chạy, để kênh báo người dùng "em đang tra
web..." thay vì im lặng 20-200 giây.

Kênh (handlers/turn_feedback.py cho Telegram) đặt 1 ``Tracker`` cho lượt bằng
``tracking()``; tầng service gọi ``report(phase)`` ở chỗ bắt đầu việc chậm (tra
web, đọc trang, lục kho rag/, số liệu cổ phiếu). ContextVar: task con của
asyncio.gather thừa hưởng CÙNG tracker (object dùng chung) nên report từ task
con vẫn tới được kênh. Không có tracker (Zalo/Zoom, job nền) -> report no-op.
Service không bao giờ import handlers/.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

THINK = "think"
SEARCH = "search"
READ = "read"
RAG = "rag"
STOCK = "stock"
PHASES = (THINK, SEARCH, READ, RAG, STOCK)

# Tool của router chat (services/tools.py) -> giai đoạn; tool nhanh không báo.
TOOL_PHASES = {"rag_search": RAG, "read_link": READ, "get_portfolio": STOCK}


@dataclass
class Tracker:
    phase: str = THINK
    # Thứ tự các giai đoạn đã qua (tối đa vài phần tử, chỉ để test/log).
    history: list[str] = field(default_factory=list)

    def set(self, phase: str) -> None:
        if phase not in PHASES or phase == self.phase:
            return
        self.phase = phase
        if len(self.history) < 16:
            self.history.append(phase)


_tracker: ContextVar[Tracker | None] = ContextVar("turn_progress_tracker", default=None)


@contextmanager
def tracking(tracker: Tracker) -> Iterator[Tracker]:
    token = _tracker.set(tracker)
    try:
        yield tracker
    finally:
        _tracker.reset(token)


def report(phase: str) -> None:
    """Báo lượt hiện tại đang ở giai đoạn ``phase`` (không chặn, không raise)."""
    tracker = _tracker.get()
    if tracker is not None:
        tracker.set(phase)


def report_tool(tool_name: str) -> None:
    phase = TOOL_PHASES.get(tool_name)
    if phase:
        report(phase)


def current_phase() -> str | None:
    tracker = _tracker.get()
    return tracker.phase if tracker is not None else None
