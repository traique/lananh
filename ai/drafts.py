"""Bản nháp chữ đang sinh (Telegram sendMessageDraft, TELEGRAM_STREAM_DRAFTS).

Kênh đặt 1 "sink" (hàm đồng bộ, rẻ - chỉ lưu chữ mới nhất) cho lượt bằng
``sink_scope()``; services/channel_chat_service.run_chat_turn bật ``drafting()``
CHỈ quanh lượt gọi orchestrator.chat cuối. Nhờ vậy lượt gọi phụ (router tool,
viết lại truy vấn, trích trí nhớ) không bao giờ đổ JSON của chúng vào bản nháp.
openai_compatible.post_chat_completion thấy ``partial_sink()`` khác None thì đọc
SSE theo từng dòng và đẩy chữ đã gom vào sink. Câu trả lời CUỐI vẫn đi qua mọi
bước lọc (CJK, trích dẫn, link lạ, policy cổ phiếu) như cũ - bản nháp chỉ để xem.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

Sink = Callable[[str], None]

_sink: ContextVar[Sink | None] = ContextVar("draft_sink", default=None)
_active: ContextVar[bool] = ContextVar("draft_active", default=False)


@contextmanager
def sink_scope(sink: Sink | None) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


@contextmanager
def drafting(enabled: bool = True) -> Iterator[None]:
    token = _active.set(enabled)
    try:
        yield
    finally:
        _active.reset(token)


def partial_sink() -> Sink | None:
    """Sink nhận chữ đang sinh, hoặc None nếu lượt gọi này không phát nháp."""
    return _sink.get() if _active.get() else None


def push(text: str) -> None:
    """Đẩy chữ đã gom tới sink (lỗi của sink không bao giờ làm hỏng lượt gọi AI)."""
    sink = partial_sink()
    if sink is None or not text:
        return
    try:
        sink(text)
    except Exception:  # pragma: no cover - sink do kênh cung cấp, chỉ phòng hờ
        pass
