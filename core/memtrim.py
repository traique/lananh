"""Trả RAM heap rảnh về hệ điều hành sau việc nặng (Render free 512 MB).

Vì sao cần: CPython cấp phát object nhỏ trong arena của pymalloc, object lớn
(chuỗi JSON, bytes âm thanh, DataFrame của vnstock...) qua malloc của glibc. Khi
việc nặng xong, bộ nhớ được giải phóng về heap nhưng glibc thường KHÔNG trả lại
cho kernel (vùng rảnh nằm giữa heap, hoặc dưới ngưỡng trim tự động) - RSS của
tiến trình đứng ở mức đỉnh dù đã rảnh, và cgroup vẫn tính phần đó.
``malloc_trim(0)`` của glibc nhả mọi trang rảnh ở cuối VÀ giữa các arena
(madvise DONTNEED). ``gc.collect()`` chạy trước để object nằm trong vòng tham
chiếu (DataFrame, traceback giữ frame...) thật sự được giải phóng.

- ``trim_now()``: chạy ngay (gc.collect + malloc_trim), trả số đo RSS trước/sau.
- ``request(reason)``: hẹn 1 lần trim sau vài giây (gộp nhiều yêu cầu liền nhau,
  tối đa 1 lần mỗi ``MEMORY_TRIM_MIN_GAP_SEC``); gọi sau lượt trả lời, phân tích
  cổ phiếu, trang thị trường, đọc tài liệu, tin thoại, việc nền.
  ``MEMORY_TRIM_AFTER_WORK=0`` tắt phần này (test đặt 0 để không để lại task hẹn).
- ``maybe_periodic()``: monitor gọi mỗi chu kỳ, trim nếu đã quá
  ``MEMORY_TRIM_INTERVAL_SEC`` (mặc định 600 = 10 phút; 0 = tắt) kể từ lần trước.
- ``freeze_startup_heap()``: gọi 1 lần ở CUỐI khởi động. gc.collect() đồng bộ trên
  event loop và quét MỌI object được theo dõi - phần lớn là object sống suốt đời
  tiến trình tạo lúc import (module, hàm, class, bảng hằng: ~126 nghìn object sau
  ``import web``). gc.freeze() chuyển chúng sang thế hệ vĩnh viễn: các lần collect
  sau chỉ quét object mới (đo trên máy dev: ~60 ms -> ~0 ms mỗi lần; Render free
  ~0,1 CPU chậm hơn nhiều). Object đóng băng vẫn được giải phóng theo đếm tham
  chiếu như thường, chỉ vòng tham chiếu giữa chúng không được gom - chấp nhận được
  vì đó là object khởi động sống mãi.

Lần trim nào mất hơn ``SLOW_TRIM_MS`` (100 ms - cả bot đứng chừng đó) thì log INFO.

Không phải glibc (musl, macOS) -> malloc_trim không có: bỏ qua, chỉ gc.collect.
Không bao giờ raise. Trạng thái trong RAM chỉ vài con số.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import gc
import logging
import time
from dataclasses import dataclass

from core.envutil import env_int, env_on

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024
# Chờ chừng này giây sau yêu cầu rồi mới trim: lượt kế tiếp (tin trả lời đang gửi,
# lượt chat nối tiếp) không bị gc.collect chen giữa.
REQUEST_DELAY_SEC = 3.0
# Trim chạy đồng bộ trên event loop: chậm hơn ngưỡng này (ms) thì log INFO.
SLOW_TRIM_MS = 100

_libc_trim = None
_libc_checked = False
_pending: asyncio.Task | None = None
_last_at = 0.0


@dataclass
class TrimStats:
    runs: int = 0
    last_reason: str = ""
    last_at: float = 0.0
    last_before_mib: float | None = None
    last_after_mib: float | None = None
    last_ms: int = 0
    total_freed_mib: float = 0.0
    malloc_trim: bool = False


stats = TrimStats()


def interval_sec() -> int:
    return env_int("MEMORY_TRIM_INTERVAL_SEC", 600, 0, 86_400)


def min_gap_sec() -> int:
    return env_int("MEMORY_TRIM_MIN_GAP_SEC", 30, 0, 3_600)


def _malloc_trim():
    """Hàm malloc_trim của glibc (ctypes) hoặc None nếu không có."""
    global _libc_trim, _libc_checked
    if _libc_checked:
        return _libc_trim
    _libc_checked = True
    try:
        name = ctypes.util.find_library("c") or "libc.so.6"
        libc = ctypes.CDLL(name)
        func = libc.malloc_trim
        func.argtypes = [ctypes.c_size_t]
        func.restype = ctypes.c_int
        _libc_trim = func
    except (OSError, AttributeError):
        _libc_trim = None
    return _libc_trim


def rss_mib() -> float | None:
    """RSS hiện tại của tiến trình (VmRSS) - None ngoài Linux."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as file:
            for line in file:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def trim_now(reason: str = "", *, collect: bool = True) -> TrimStats:
    """gc.collect() (nếu ``collect``) rồi malloc_trim(0). Đồng bộ, vài chục ms."""
    global _last_at
    started = time.monotonic()
    before = rss_mib()
    try:
        if collect:
            gc.collect()
        func = _malloc_trim()
        if func is not None:
            func(0)
        stats.malloc_trim = func is not None
    except Exception:  # pragma: no cover - phòng thủ, không bao giờ làm hỏng caller
        logger.debug("memtrim lỗi", exc_info=True)
    after = rss_mib()
    _last_at = time.monotonic()
    stats.runs += 1
    stats.last_reason = reason
    stats.last_at = time.time()
    stats.last_before_mib = before
    stats.last_after_mib = after
    stats.last_ms = int((_last_at - started) * 1000)
    if before is not None and after is not None and before > after:
        stats.total_freed_mib = round(stats.total_freed_mib + before - after, 1)
    level = logging.INFO if stats.last_ms > SLOW_TRIM_MS else logging.DEBUG
    logger.log(
        level,
        "memtrim(%s): RSS %s -> %s MiB trong %d ms (gc.collect + malloc_trim chặn event loop).",
        reason,
        before,
        after,
        stats.last_ms,
    )
    return stats


def freeze_startup_heap() -> int:
    """Gọi 1 lần ở CUỐI khởi động (mọi module đã nạp, vòng nền đã start): gom rác
    khởi động rồi gc.freeze() phần còn sống, để mọi gc.collect() về sau (trim_now
    sau mỗi lượt và mỗi 10 phút, GC tự động) không quét lại heap khởi động. Trả số
    object đã đóng băng (0 nếu lỗi). Không raise."""
    try:
        gc.collect()
        gc.freeze()
        count = gc.get_freeze_count()
    except Exception:  # pragma: no cover - phòng thủ, không bao giờ làm hỏng khởi động
        logger.debug("memtrim: gc.freeze lỗi", exc_info=True)
        return 0
    logger.info("memtrim: đóng băng %d object của heap khởi động (gc.freeze).", count)
    return count


async def _delayed(reason: str, delay: float) -> None:
    global _pending
    try:
        await asyncio.sleep(delay)
        trim_now(reason)
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover
        logger.debug("memtrim hẹn giờ lỗi", exc_info=True)
    finally:
        _pending = None


def request(reason: str, *, delay: float = REQUEST_DELAY_SEC) -> bool:
    """Hẹn 1 lần trim sau ``delay`` giây; True nếu đã hẹn. Không chặn caller.

    Gộp: đang có lần hẹn chưa chạy thì thôi; vừa trim chưa quá MEMORY_TRIM_MIN_GAP_SEC
    thì hẹn vào cuối khoảng nghỉ đó (việc nặng liên tiếp vẫn được dọn 1 lần sau cùng)."""
    global _pending
    if not env_on("MEMORY_TRIM_AFTER_WORK"):
        return False
    if _pending is not None and not _pending.done():
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    wait = max(delay, _last_at + min_gap_sec() - time.monotonic())
    _pending = loop.create_task(_delayed(reason, wait))
    return True


def maybe_periodic(now: float | None = None) -> bool:
    """Monitor gọi mỗi chu kỳ: trim nếu đã quá MEMORY_TRIM_INTERVAL_SEC từ lần trước."""
    every = interval_sec()
    if every <= 0:
        return False
    now = time.monotonic() if now is None else now
    if now - _last_at < every:
        return False
    trim_now("định kỳ")
    return True


def snapshot() -> dict:
    """Số liệu cho /admin (services/memory_stats.py)."""
    return {
        "runs": stats.runs,
        "malloc_trim": stats.malloc_trim or _malloc_trim() is not None,
        "last_reason": stats.last_reason,
        "last_before_mib": stats.last_before_mib,
        "last_after_mib": stats.last_after_mib,
        "last_ms": stats.last_ms,
        "total_freed_mib": stats.total_freed_mib,
        "interval_sec": interval_sec(),
        # Số object heap khởi động đã đóng băng (freeze_startup_heap); 0 = chưa gọi.
        "frozen_objects": gc.get_freeze_count(),
    }


def reset_for_tests() -> None:
    global _pending, _last_at
    if _pending is not None and not _pending.done():
        try:
            _pending.cancel()
        except RuntimeError:  # loop của test trước đã đóng
            pass
    _pending = None
    _last_at = 0.0
    for name, value in vars(TrimStats()).items():
        setattr(stats, name, value)
