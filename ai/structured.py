"""Đầu ra JSON có schema cho lượt phụ (router tool của chat...).

Caller mở ``json_schema(schema, name)`` quanh ``orchestrator.utility_json``: Groq gửi
``response_format`` json_schema (thay json_object), Gemini gửi
``response_json_schema`` (google-genai 2.11) - model phải trả đúng khuôn (vd tên
tool nằm trong danh sách), bớt lượt JSON hỏng/tên tool bịa phải gọi lại.

Model/endpoint từ chối schema (HTTP 400 nhắc tới schema/response_format, hoặc JSON
không khớp schema) thì lượt đó tự lùi về JSON thường (json_object / JSON mime) và
cặp (provider, model) được nhớ trong RAM (có trần) để lượt sau khỏi tốn request hỏng.

Schema đi qua ContextVar (như ``call_log.call_stage``) để chữ ký
utility_json/generate_json/generate_utility_json giữ nguyên - nhiều nơi gọi và giả
lập chúng theo chữ ký cũ.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

Schema = tuple[str, dict[str, Any]]

_schema: ContextVar[Schema | None] = ContextVar("json_output_schema", default=None)
# (provider, model) đã từ chối schema trong process này.
_unsupported: set[tuple[str, str]] = set()
_MAX_UNSUPPORTED = 16
_REJECT_RE = re.compile(r"schema|response_format|json_validate", re.IGNORECASE)


@contextmanager
def json_schema(schema: dict[str, Any], name: str = "ket_qua") -> Iterator[None]:
    """Lượt JSON trong khối này xin provider trả đúng ``schema`` (JSON Schema)."""
    token = _schema.set((name, schema))
    try:
        yield
    finally:
        _schema.reset(token)


def current() -> Schema | None:
    return _schema.get()


def for_model(provider: str, model: str) -> Schema | None:
    """Schema của lượt hiện tại nếu ``provider``/``model`` chưa từng từ chối schema."""
    spec = _schema.get()
    if spec is None or (provider, model) in _unsupported:
        return None
    return spec


def rejected(exc: BaseException) -> bool:
    """Lỗi 400 vì schema/response_format (không nhận, hoặc JSON không khớp)."""
    current_exc: BaseException | None = exc
    seen: set[int] = set()
    while current_exc is not None and id(current_exc) not in seen:
        seen.add(id(current_exc))
        status = getattr(current_exc, "status_code", None) or getattr(current_exc, "code", None)
        if status == 400 and _REJECT_RE.search(str(current_exc)):
            return True
        current_exc = current_exc.__cause__ or current_exc.__context__
    return False


def mark_unsupported(provider: str, model: str, exc: BaseException) -> None:
    if len(_unsupported) < _MAX_UNSUPPORTED:
        _unsupported.add((provider, model))
    logger.warning(
        "%s/%s từ chối JSON schema (%s) - dùng JSON thường.", provider, model, str(exc)[:200]
    )
