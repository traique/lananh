"""Mã lượt (turn id) xuyên suốt 1 câu trả lời và số đo từng giai đoạn của lượt.

- ``turn(turn_id, channel=..., user_kind=...)``: đặt ở điểm vào của mỗi kênh, dùng
  lại event_key sẵn có - Telegram "telegram:<update_id>" (web._process_update),
  Zalo "zalo:<account>:<message_id>" và Zoom "zoom:<account>:<event_id>"
  (services/channel_dispatch.dispatch, route ảnh/thoại/file Zalo). Job nền dùng
  ``job(name)`` -> "job:<tên>:<YYYY-MM-DD>" trong MỖI vòng lặp. Lồng nhau thì giữ
  mã ngoài cùng (lượt Zoom đi qua dispatch không đổi mã giữa chừng).
- ``TurnIdFilter``: gắn mã lượt vào mọi dòng log (handler gốc, như _RedactFilter
  của logging_setup) - dòng không thuộc lượt nào có ``turn_id`` mặc định "-".
- ``metrics()``: số đo của lượt hiện tại (chờ khoá người dùng, chờ slot
  MAX_CONCURRENT_TURNS, gom ngữ cảnh, sinh câu trả lời, gửi tin, số lượt gọi
  provider, provider cuối, có phải chuyển provider không). Hết lượt thì ghi 1 dòng
  bảng turn_metrics (migrations/025) ở NỀN, best-effort, số task đang ghi có trần.
  Chỉ ghi lượt đã qua khoá lượt (assistant_turn) hoặc có gọi provider - lệnh chỉ
  đọc, vòng job không gọi AI không sinh dòng nào.
- Task nền sống lâu được khởi động LƯỜI bên trong 1 lượt (asyncio.create_task chép
  context) phải gọi ``detach()`` ở đầu task, nếu không mọi log/lượt gọi provider
  về sau của task đó mang mãi mã lượt cũ.

Chỉ lưu metadata (mã lượt, kênh, thời gian, tên provider) - không bao giờ lưu
nội dung tin nhắn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
# Trần số task ghi turn_metrics đang chờ DB (DB chậm/sập thì bỏ bớt, không dồn RAM).
MAX_PENDING_WRITES = 16
# Giai đoạn đo được bằng ``stage()``.
STAGES = ("context", "llm", "send")
# Giai đoạn gọi provider không tính là "câu trả lời chính" khi chọn final_provider.
_AUX_STAGES = frozenset({"utility", "stt"})

_turn_id: ContextVar[str | None] = ContextVar("trace_turn_id", default=None)
_metrics: ContextVar["TurnMetrics | None"] = ContextVar("trace_turn_metrics", default=None)
_pending: set[asyncio.Task] = set()


@dataclass
class TurnMetrics:
    """Số đo của 1 lượt (mutable, dùng chung giữa task cha và task con của lượt)."""

    turn_id: str
    channel: str = ""
    user_kind: str = ""
    started: float = field(default_factory=time.monotonic)
    lock_wait_ms: int | None = None
    slot_wait_ms: int | None = None
    context_ms: int | None = None
    llm_ms: int | None = None
    send_ms: int | None = None
    llm_calls: int = 0
    failed_calls: int = 0
    provider_ms: int = 0
    final_provider: str | None = None
    outcome: str = "ok"
    # Đã qua assistant_turn (lượt AI thật) - quyết định có ghi turn_metrics không.
    locked: bool = False

    def note_call(self, provider: str, outcome: str, latency_ms: int | None, stage: str | None):
        self.llm_calls += 1
        self.provider_ms += max(0, int(latency_ms or 0))
        if outcome != "ok":
            self.failed_calls += 1
        elif stage not in _AUX_STAGES:
            self.final_provider = provider

    def add_stage(self, name: str, elapsed_ms: int) -> None:
        attr = f"{name}_ms"
        current = getattr(self, attr, None)
        setattr(self, attr, (current or 0) + max(0, elapsed_ms))

    def should_record(self) -> bool:
        return self.locked or self.llm_calls > 0

    def row(self) -> dict:
        total = int((time.monotonic() - self.started) * 1000)
        return {
            "turn_id": self.turn_id[:120],
            "channel": self.channel[:20],
            "user_kind": self.user_kind[:20],
            "lock_wait_ms": self.lock_wait_ms,
            "slot_wait_ms": self.slot_wait_ms,
            "context_ms": self.context_ms,
            # Lượt không qua run_chat_turn (lệnh AI, job): tổng độ trễ provider.
            "llm_ms": self.llm_ms if self.llm_ms is not None else (self.provider_ms or None),
            "send_ms": self.send_ms,
            "total_ms": total,
            "llm_calls": min(self.llm_calls, 32767),
            "final_provider": self.final_provider,
            "fallback_used": self.failed_calls > 0,
            "outcome": self.outcome[:20],
        }


def current() -> str | None:
    """Mã lượt hiện tại (None ngoài lượt/job)."""
    return _turn_id.get()


def metrics() -> TurnMetrics | None:
    return _metrics.get()


@contextmanager
def turn(turn_id: str | None, *, channel: str = "", user_kind: str = "") -> Iterator[None]:
    """Đặt mã lượt + bộ số đo cho khối lệnh. Đã có lượt bao ngoài -> giữ nguyên."""
    if not turn_id or _turn_id.get() is not None:
        yield
        return
    data = TurnMetrics(turn_id=turn_id, channel=channel, user_kind=user_kind)
    id_token = _turn_id.set(turn_id)
    metrics_token = _metrics.set(data)
    try:
        yield
    except BaseException as exc:
        if data.outcome == "ok":
            data.outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        raise
    finally:
        _metrics.reset(metrics_token)
        _turn_id.reset(id_token)
        if data.should_record():
            _schedule_write(data.row())


def job_id(name: str, now: datetime | None = None) -> str:
    day = (now or datetime.now(_VN_TZ)).strftime("%Y-%m-%d")
    return f"job:{name}:{day}"


@contextmanager
def job(name: str) -> Iterator[None]:
    """1 vòng job nền: mã "job:<tên>:<ngày VN>" (ghi đè mã lượt lỡ thừa hưởng)."""
    id_token = _turn_id.set(None)
    metrics_token = _metrics.set(None)
    try:
        with turn(job_id(name), channel="job", user_kind="job"):
            yield
    finally:
        _metrics.reset(metrics_token)
        _turn_id.reset(id_token)


def detach() -> None:
    """Bỏ mã lượt thừa hưởng trong task hiện tại (gọi ở đầu task nền sống lâu).

    Chỉ đổi context của CHÍNH task đang chạy (asyncio.create_task đã chép riêng),
    không ảnh hưởng lượt đã tạo ra task."""
    _turn_id.set(None)
    _metrics.set(None)


def adopt(turn_id: str | None) -> None:
    """Đặt MÃ LƯỢT (không kèm số đo) cho context hiện tại - dùng khi tạo task nền
    với context rỗng mà vẫn muốn log/provider_calls gom về lượt đã sinh ra nó."""
    _turn_id.set(turn_id)
    _metrics.set(None)


def set_outcome(outcome: str) -> None:
    data = _metrics.get()
    if data is not None:
        data.outcome = outcome


def mark_locked(lock_wait_ms: int, slot_wait_ms: int) -> None:
    """services/concurrency.assistant_turn báo thời gian chờ khoá người dùng / slot."""
    data = _metrics.get()
    if data is None:
        return
    data.locked = True
    data.lock_wait_ms = (data.lock_wait_ms or 0) + max(0, lock_wait_ms)
    data.slot_wait_ms = (data.slot_wait_ms or 0) + max(0, slot_wait_ms)


def note_call(provider: str, outcome: str, latency_ms: int | None, stage: str | None) -> None:
    data = _metrics.get()
    if data is not None:
        data.note_call(provider, outcome, latency_ms, stage)


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Đo 1 giai đoạn của lượt ("context" | "llm" | "send"), cộng dồn nếu lặp."""
    started = time.monotonic()
    try:
        yield
    finally:
        data = _metrics.get()
        if data is not None and name in STAGES:
            data.add_stage(name, int((time.monotonic() - started) * 1000))


def _schedule_write(row: dict) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if len(_pending) >= MAX_PENDING_WRITES:
        logger.info("Bỏ ghi turn_metrics %s: quá nhiều lượt ghi đang chờ DB.", row["turn_id"])
        return
    task = loop.create_task(_write(row))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _write(row: dict) -> None:
    from core import database as db

    detach()
    try:
        await db.record_turn_metrics(**row)
    except Exception:
        logger.warning("Không ghi được turn_metrics (bỏ qua, chỉ là số liệu).", exc_info=True)


async def drain(limit_sec: float = 3.0) -> None:
    """Chờ các lượt ghi turn_metrics còn dở (lúc tắt app), tối đa ``limit_sec`` giây."""
    tasks = [task for task in _pending if not task.done()]
    if not tasks:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), limit_sec)
    except Exception:
        logger.info("Hết giờ chờ ghi turn_metrics lúc tắt (%d lượt).", len(tasks))


class TurnIdFilter(logging.Filter):
    """Gắn ``turn_id`` ("-" ngoài lượt) và ``turn_tag`` ("[<mã>] " hoặc rỗng) vào
    mọi LogRecord - format của logging_setup dùng ``%(turn_tag)s``."""

    def filter(self, record: logging.LogRecord) -> bool:
        tid = _turn_id.get()
        record.turn_id = tid or "-"
        record.turn_tag = f"[{tid}] " if tid else ""
        return True
