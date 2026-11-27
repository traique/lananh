"""Trí nhớ DÀI HẠN của bot - FACADE giữ nguyên mọi tên cũ ``memory_service.X`` cho nơi
gọi và test. Thân module (1.500 dòng) tách làm 2 phần (11/2026):

- services/memory_extract.py - GHI: bật/tắt trí nhớ, khoá đọc-rồi-ghi theo người,
  watermark, trích nhớ theo lô (after_exchange -> maybe_flush -> flush), áp thao tác
  fact/highlight/cách trả lời/ghi chú phiên, /quen, gộp fact, vòng quét nền, xoá trí
  nhớ; kèm mô tả các bảng dữ liệu.
- services/memory_context.py - ĐỌC: ráp khối trí nhớ cho mỗi lượt chat
  (build_turn_memory, select_relevant_memory, relevant_notes...).

Facade mang lại MỌI tên của 2 module con (kể cả tên riêng như ``_pending``,
``_slots``, ``db``) như file cũ. GÁN ``memory_service.X = ...`` (monkeypatch trong
test, đổi hằng số lúc chạy) được chuyển tới MỌI module con đang có tên X - code bên
trong thấy ngay, đúng như khi còn chung 1 file (vd vá ``memory_service.maybe_flush``
thì after_exchange dùng bản vá, vá ``memory_service._utility_json`` thì bộ trích nhớ
gọi bản vá); monkeypatch/mock trả lại giá trị cũ cũng bằng phép gán nên đi cùng
đường. Lưu ý: vá THẲNG module con (``memory_extract.X = ...``) thì code bên trong thấy
nhưng ``memory_service.X`` vẫn là giá trị cũ - nên vá qua facade.
"""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING, Any

from core import database as db  # noqa: F401 - test vá memory_service.db.<hàm>
from services import memory_context, memory_extract

if TYPE_CHECKING:  # tên tĩnh cho pyright; lúc chạy đi qua __getattr__ bên dưới
    from services.memory_context import MID_SESSION_MEMORY_MAX_CHARS as MID_SESSION_MEMORY_MAX_CHARS
    from services.memory_context import TurnMemory as TurnMemory
    from services.memory_context import build_memory_context as build_memory_context
    from services.memory_context import build_turn_memory as build_turn_memory
    from services.memory_context import relevant_notes as relevant_notes
    from services.memory_context import select_relevant_memory as select_relevant_memory
    from services.memory_extract import CATEGORIES as CATEGORIES
    from services.memory_extract import FLUSH_EXCHANGES as FLUSH_EXCHANGES
    from services.memory_extract import IDLE_FLUSH_SEC as IDLE_FLUSH_SEC
    from services.memory_extract import INTERACTIVE_LOCK_WAIT_SEC as INTERACTIVE_LOCK_WAIT_SEC
    from services.memory_extract import MAX_FACTS_PER_USER as MAX_FACTS_PER_USER
    from services.memory_extract import MAX_HIGHLIGHTS_PER_USER as MAX_HIGHLIGHTS_PER_USER
    from services.memory_extract import MAX_PINNED_PER_USER as MAX_PINNED_PER_USER
    from services.memory_extract import MEMORY_BUSY_TEXT as MEMORY_BUSY_TEXT
    from services.memory_extract import SESSION_SUMMARY_MAX_CHARS as SESSION_SUMMARY_MAX_CHARS
    from services.memory_extract import STYLE_KEY as STYLE_KEY
    from services.memory_extract import STYLE_RULES_MAX_CHARS as STYLE_RULES_MAX_CHARS
    from services.memory_extract import SWEEP_INTERVAL_SEC as SWEEP_INTERVAL_SEC
    from services.memory_extract import WINDOW_MAX_CHARS as WINDOW_MAX_CHARS
    from services.memory_extract import MemoryBusy as MemoryBusy
    from services.memory_extract import _normalize_key as _normalize_key
    from services.memory_extract import add_to_forget_list as add_to_forget_list
    from services.memory_extract import after_exchange as after_exchange
    from services.memory_extract import build_extraction_prompt as build_extraction_prompt
    from services.memory_extract import clear_memory as clear_memory
    from services.memory_extract import consolidate as consolidate
    from services.memory_extract import cut_at_sentence as cut_at_sentence
    from services.memory_extract import flush as flush
    from services.memory_extract import flush_before_reset as flush_before_reset
    from services.memory_extract import get_forget_list as get_forget_list
    from services.memory_extract import interactive_lock as interactive_lock
    from services.memory_extract import interactive_lock_timeout as interactive_lock_timeout
    from services.memory_extract import is_enabled as is_enabled
    from services.memory_extract import is_trivial as is_trivial
    from services.memory_extract import maybe_flush as maybe_flush
    from services.memory_extract import scrub_forgotten as scrub_forgotten
    from services.memory_extract import set_enabled as set_enabled
    from services.memory_extract import start as start
    from services.memory_extract import stop as stop
    from services.memory_extract import stop_background_tasks as stop_background_tasks
    from services.memory_extract import sweep_once as sweep_once
    from services.memory_extract import update_memory as update_memory
    from services.memory_extract import user_lock as user_lock
    from services.memory_extract import wants_remember as wants_remember

# Thứ tự tra tên: phần GHI trước (memory_context import lại vài tên dùng chung từ đó).
_PARTS: tuple[types.ModuleType, ...] = (memory_extract, memory_context)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _owners(name: str) -> list[types.ModuleType]:
    """Các module con đang có tên ``name`` (tên dùng chung có ở cả 2)."""
    if _is_dunder(name):
        return []
    return [part for part in _PARTS if name in vars(part)]


def _reexport() -> None:
    """Chép mọi tên (trừ dunder) của module con vào facade; tên trùng lấy của phần
    GHI; tên riêng của facade giữ nguyên."""
    namespace = globals()
    for part in _PARTS:
        for name, value in vars(part).items():
            if not _is_dunder(name):
                namespace.setdefault(name, value)


_reexport()


def __getattr__(name: str) -> Any:
    """Tên module con thêm SAU lúc nạp facade (hiếm): đọc thẳng từ module con."""
    for part in _owners(name):
        return vars(part)[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _Facade(types.ModuleType):
    """Gán thuộc tính qua facade -> gán cả bản ở facade lẫn ở mọi module con đang có
    tên đó (code bên trong module con đọc biến toàn cục của chính nó)."""

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for part in _owners(name):
            setattr(part, name, value)

    def __delattr__(self, name: str) -> None:
        if _owners(name):
            raise AttributeError(
                f"{name!r} thuộc module con của {__name__}: vá bằng monkeypatch.setattr "
                "(trả lại bằng phép gán), không xoá qua facade."
            )
        super().__delattr__(name)


sys.modules[__name__].__class__ = _Facade
