"""Ghi lượt gọi provider (bảng provider_calls) - 1 chỗ cho mọi đường gọi AI.

Provider-chain (ai/orchestrator.py), Gemini utility dự phòng, /agent, Agnes và
Tavily đều ghi qua ``record()``/``timed()`` ở đây: provider, model, kết quả (ok |
error | timeout | rate_limited | contaminated...), độ trễ, giai đoạn (chat, search,
utility, agent, image...), mã lượt (core/trace.py), số ký tự prompt và token do
provider báo. Ghi lỗi DB không bao giờ văng lên lượt trả lời (chỉ là số liệu).

Token đi qua 1 "kênh phụ" thay vì đổi kiểu trả về của client (post_chat_completion
trả str, test và caller dựa vào đó): caller mở ``collect_usage()`` quanh 1 lượt gọi,
client gọi ``note_openai_usage``/``note_gemini_usage``/``note_prompt_chars`` khi đọc
xong response. Ô chứa là 1 object mutable trong ContextVar nên client chạy trong
task con (asyncio.wait_for, gather) vẫn ghi được vào đúng ô của lượt gọi đó.

Header rate-limit (x-ratelimit-remaining-requests/-tokens của Groq...) được giữ
bản mới nhất theo (provider, model) (``latest_ratelimit`` - Groq tính quota THEO
MODEL: model tra web khác bucket model chat) cho trang quota (services/
quota_burn.py) - chỉ vài provider đã biết, có trần số bản.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from core import database as db
from core import trace

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Giai đoạn của lượt gọi (chat/ask/utility/search...) - caller đặt qua
# ``call_stage()`` để provider_calls phân biệt lượt nào đốt thời gian.
_call_stage: ContextVar[str | None] = ContextVar("provider_call_stage", default=None)


@contextmanager
def call_stage(stage: str) -> Iterator[None]:
    token = _call_stage.set(stage)
    try:
        yield
    finally:
        _call_stage.reset(token)


def current_stage() -> str | None:
    return _call_stage.get()


# ─── Kênh phụ token / số ký tự prompt ───────────────────────────────────────


@dataclass
class CallUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_chars: int | None = None
    ratelimit: dict[str, str] | None = None


_usage_slot: ContextVar[CallUsage | None] = ContextVar("provider_call_usage", default=None)


@contextmanager
def collect_usage() -> Iterator[CallUsage]:
    """Mở 1 ô chứa token cho đúng 1 lượt gọi provider (lồng nhau: ô trong cùng)."""
    usage = CallUsage()
    token = _usage_slot.set(usage)
    try:
        yield usage
    finally:
        _usage_slot.reset(token)


def _as_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _add(current: int | None, value: int | None) -> int | None:
    if value is None:
        return current
    return (current or 0) + value


def note_tokens(prompt_tokens: Any, completion_tokens: Any) -> None:
    """Cộng dồn token vào ô hiện tại (1 lượt gọi có thể gồm nhiều request)."""
    usage = _usage_slot.get()
    if usage is None:
        return
    usage.prompt_tokens = _add(usage.prompt_tokens, _as_int(prompt_tokens))
    usage.completion_tokens = _add(usage.completion_tokens, _as_int(completion_tokens))


def note_prompt_chars(chars: int) -> None:
    usage = _usage_slot.get()
    if usage is not None and chars > 0:
        usage.prompt_chars = _add(usage.prompt_chars, int(chars))


def messages_chars(messages: list[dict]) -> int:
    """Tổng ký tự nội dung chữ của danh sách messages chuẩn OpenAI."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(
                len(str(part.get("text") or "")) for part in content if isinstance(part, dict)
            )
    return total


def openai_usage(payload: Any) -> dict | None:
    """Khối usage của 1 response/chunk chuẩn OpenAI. Groq stream đặt ở
    ``x_groq.usage`` của chunk cuối (choices rỗng), OpenRouter ở ``usage``."""
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        x_groq = payload.get("x_groq")
        usage = x_groq.get("usage") if isinstance(x_groq, dict) else None
    return usage if isinstance(usage, dict) else None


def note_openai_usage(payload: Any) -> None:
    usage = openai_usage(payload)
    if usage:
        note_tokens(usage.get("prompt_tokens"), usage.get("completion_tokens"))


def note_gemini_usage(usage_metadata: Any) -> None:
    """Gemini: completion = candidates + thoughts (token suy luận cũng bị tính)."""
    if usage_metadata is None:
        return
    prompt = _as_int(getattr(usage_metadata, "prompt_token_count", None))
    candidates = _as_int(getattr(usage_metadata, "candidates_token_count", None))
    thoughts = _as_int(getattr(usage_metadata, "thoughts_token_count", None))
    completion = (
        None if candidates is None and thoughts is None else (candidates or 0) + (thoughts or 0)
    )
    note_tokens(prompt, completion)


_RATELIMIT_PREFIX = "x-ratelimit-"
_MAX_RATELIMIT_HEADERS = 8


def note_ratelimit_headers(headers: Any) -> None:
    """Giữ các header x-ratelimit-* của response (Groq: remaining-requests là
    quota NGÀY, remaining-tokens là quota PHÚT) vào ô hiện tại."""
    usage = _usage_slot.get()
    if usage is None or headers is None:
        return
    found: dict[str, str] = {}
    try:
        items = headers.items()
    except AttributeError:
        return
    for key, value in items:
        name = str(key).lower()
        if name.startswith(_RATELIMIT_PREFIX) and len(found) < _MAX_RATELIMIT_HEADERS:
            found[name] = str(value)[:40]
    if found:
        usage.ratelimit = found


# Bản header rate-limit mới nhất theo (provider, model): (epoch, headers). Chỉ
# provider trong danh sách; quá trần thì bỏ bản cũ nhất.
_RATELIMIT_PROVIDERS = frozenset({"groq", "openrouter", "router9"})
_MAX_RATELIMIT_SNAPSHOTS = 12
_latest_ratelimit: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


def latest_ratelimit(
    provider: str, model: str | None = None
) -> tuple[float, dict[str, str]] | None:
    """Bản header mới nhất của ``provider`` - đúng ``model`` khi truyền (Groq: quota
    theo model), không thì bản mới nhất của bất kỳ model nào."""
    if model is not None:
        return _latest_ratelimit.get((provider, model))
    found = [value for (name, _model), value in _latest_ratelimit.items() if name == provider]
    return max(found, key=lambda item: item[0], default=None)


def _keep_ratelimit(provider: str, model: str, headers: dict[str, str]) -> None:
    key = (provider, model)
    if key not in _latest_ratelimit and len(_latest_ratelimit) >= _MAX_RATELIMIT_SNAPSHOTS:
        oldest = min(_latest_ratelimit, key=lambda item: _latest_ratelimit[item][0])
        del _latest_ratelimit[oldest]
    _latest_ratelimit[key] = (time.time(), dict(headers))


# ─── Ghi provider_calls ─────────────────────────────────────────────────────


async def record(
    provider: str,
    model: str | None,
    *,
    outcome: str = "ok",
    latency_ms: int | None = None,
    stage: str | None = None,
    usage: CallUsage | None = None,
) -> None:
    """Ghi 1 lượt gọi. ``stage`` None -> giai đoạn đang đặt qua ``call_stage()``.
    Không bao giờ raise."""
    stage = stage if stage is not None else _call_stage.get()
    trace.note_call(provider, outcome, latency_ms, stage)
    if usage is not None and usage.ratelimit and provider in _RATELIMIT_PROVIDERS:
        _keep_ratelimit(provider, model or provider, usage.ratelimit)
    try:
        await db.record_provider_call(
            provider,
            model or provider,
            outcome=outcome,
            latency_ms=latency_ms,
            stage=stage,
            prompt_chars=usage.prompt_chars if usage else None,
            turn_id=trace.current(),
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
        )
    except Exception:
        logger.warning("Không ghi được lượt gọi provider=%s vào DB.", provider, exc_info=True)


def classify(exc: BaseException) -> str:
    """Phân loại lỗi cho cột outcome (không phụ thuộc orchestrator)."""
    from ai.timeouts import TurnTimeoutError

    if isinstance(exc, TurnTimeoutError):
        return "turn_timeout"
    if isinstance(exc, (TimeoutError,)):
        return "timeout"
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    text = str(exc)
    if status == 429 or "RESOURCE_EXHAUSTED" in text or "HTTP 429" in text:
        return "rate_limited"
    return "error"


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def timed(
    provider: str,
    model: str | None,
    call: Callable[[], Awaitable[T]],
    *,
    stage: str | None = None,
    classify_error: Callable[[BaseException], str] | None = None,
) -> T:
    """Chạy ``call()`` trong 1 ô token, ghi provider_calls cho CẢ lượt lỗi lẫn lượt
    thành công (kèm độ trễ). Lỗi được raise lại nguyên vẹn cho caller xử lý."""
    started = time.monotonic()
    with collect_usage() as usage:
        try:
            result = await call()
        except Exception as exc:
            outcome = (classify_error or classify)(exc)
            await record(
                provider,
                model,
                outcome=outcome,
                latency_ms=elapsed_ms(started),
                stage=stage,
                usage=usage,
            )
            raise
    await record(provider, model, latency_ms=elapsed_ms(started), stage=stage, usage=usage)
    return result
