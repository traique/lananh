"""Che bí mật trong prompt TRƯỚC khi gửi cho provider AI (đều là gói miễn phí của
bên thứ ba: 9Router, Groq, OpenRouter, Gemini).

services/memory_guard chỉ chặn khi LƯU trí nhớ; câu hỏi, lịch sử chat, trí nhớ,
grounding vẫn đi nguyên văn tới provider. Module này thay phần GIÁ TRỊ bí mật
(mã OTP, mật khẩu/mk/pass, mã PIN, STK, CCCD/CMND/hộ chiếu, số thẻ qua Luhn,
khoá bí mật, tiền tố API key - mẫu neo theo từ khoá của
memory_guard.prompt_secret_spans) bằng nhãn "[đã ẩn: mã OTP]", "[đã ẩn: số tài
khoản]"... và bỏ ký tự vô hình. Cố ý không bắt dãy số trần: giá/khối lượng cổ
phiếu, số điện thoại, số tiền đi nguyên văn.

Ráp ở chỗ gửi provider (ai/orchestrator chat/ask/utility_json/analyze_image/
analyze_document, ai/agent_service) - DB vẫn lưu như cũ. Khi có ẩn, model nhận
thêm 1 dòng hệ thống (SYSTEM_NOTE): biết thông tin đã được che, không đoán và
không hỏi người dùng gửi lại.

Router tool của chat (services/tools.py) chạy trong ``scope()`` rồi
``restore_obj`` tham số model trả về: "ghi chú giúp anh: mật khẩu wifi là
hoa-sen-2026" vẫn lưu đúng ghi chú vào DB của bot (provider chỉ thấy nhãn).

Tắt bằng PROMPT_REDACTION=0 (mặc định bật).
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from core import untrusted
from core.envutil import env_on
from services import memory_guard

logger = logging.getLogger(__name__)

SYSTEM_NOTE = (
    "(Hệ thống: vài thông tin nhạy cảm trong tin nhắn/ngữ cảnh đã được che trước khi gửi "
    "cho bạn, thay bằng nhãn [đã ẩn: ...]. Đừng đoán hay nhắc lại giá trị đó và KHÔNG hỏi "
    "người dùng gửi lại qua chat.)"
)
# Trần số bí mật nhớ để khôi phục trong 1 lượt (RAM có trần).
_MAX_SECRETS = 64


def enabled() -> bool:
    return env_on("PROMPT_REDACTION")


class Redactor:
    """Che bí mật cho MỘT lượt gửi provider. Cùng giá trị -> cùng nhãn; nhiều giá trị
    cùng loại được đánh số ("[đã ẩn: mật khẩu 2]") để khôi phục đúng chỗ."""

    __slots__ = ("active", "_by_value", "_by_placeholder", "_counts")

    def __init__(self, active: bool | None = None) -> None:
        self.active = enabled() if active is None else active
        self._by_value: dict[tuple[str, str], str] = {}
        self._by_placeholder: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    @property
    def redacted(self) -> bool:
        return bool(self._counts)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(self._counts)

    def _placeholder(self, label: str, value: str) -> str:
        existing = self._by_value.get((label, value))
        if existing is not None:
            return existing
        number = self._counts.get(label, 0) + 1
        self._counts[label] = number
        placeholder = f"[đã ẩn: {label}]" if number == 1 else f"[đã ẩn: {label} {number}]"
        if len(self._by_placeholder) < _MAX_SECRETS:
            self._by_value[(label, value)] = placeholder
            self._by_placeholder[placeholder] = value
        return placeholder

    def text(self, text: str) -> str:
        """Bản đã che của 1 đoạn (rỗng/không có bí mật -> chỉ bỏ ký tự vô hình)."""
        if not self.active or not text:
            return text
        text = untrusted.strip_invisible(text)
        spans = memory_guard.prompt_secret_spans(text)
        if not spans:
            return text
        parts: list[str] = []
        last = 0
        for start, end, label in spans:
            parts.append(text[last:start])
            parts.append(self._placeholder(label, text[start:end]))
            last = end
        parts.append(text[last:])
        return "".join(parts)

    def history(self, rows: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        return [(role, self.text(content)) for role, content in rows]

    def note(self) -> str:
        return SYSTEM_NOTE if self.redacted else ""

    def with_note(self, prompt: str) -> str:
        """Prompt 1 lượt (không có system prompt riêng): đặt dòng hệ thống lên đầu
        khi chính prompt này có nhãn đã ẩn."""
        return f"{SYSTEM_NOTE}\n\n{prompt}" if self.redacted and "[đã ẩn:" in prompt else prompt

    def log(self, where: str) -> None:
        if self.redacted:
            # Chỉ log LOẠI bí mật, không bao giờ log giá trị.
            logger.info("Đã che %s trong prompt %s.", ", ".join(self.labels), where)

    def restore(self, text: str) -> str:
        """Đổi nhãn về giá trị gốc - CHỈ dùng cho dữ liệu chạy trong bot (tham số
        tool), không bao giờ cho prompt gửi đi."""
        if not self._by_placeholder or not text or "[đã ẩn:" not in text:
            return text
        for placeholder, value in self._by_placeholder.items():
            text = text.replace(placeholder, value)
        return text

    def restore_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.restore(obj)
        if isinstance(obj, dict):
            return {key: self.restore_obj(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [self.restore_obj(item) for item in obj]
        return obj


_scope: contextvars.ContextVar[Redactor | None] = contextvars.ContextVar(
    "prompt_redactor", default=None
)


@contextmanager
def scope() -> Iterator[Redactor]:
    """Mọi lượt gọi AI bên trong dùng CHUNG 1 Redactor - caller khôi phục được
    nhãn trong kết quả (router tool)."""
    redactor = Redactor()
    token = _scope.set(redactor)
    try:
        yield redactor
    finally:
        _scope.reset(token)


def redactor() -> Redactor:
    """Redactor của scope() đang mở, hoặc 1 bản mới cho riêng lượt gọi này."""
    return _scope.get() or Redactor()
