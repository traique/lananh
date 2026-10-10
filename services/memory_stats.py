"""Số liệu RAM cho /admin/api/memory-usage (theo dõi giới hạn 512 MB của Render free).

Chỉ đọc /proc và cgroup, không cần thư viện ngoài. Trường nào không đọc được
(máy local không phải Linux, cgroup v1/v2 khác nhau) trả None thay vì lỗi.
"""

from __future__ import annotations

import gc
import os
import sys
import time
from pathlib import Path

MIB = 1024 * 1024
# Thư viện nặng chỉ nạp khi cần; biết cái nào đã nạp giúp giải thích RAM tăng.
_HEAVY_MODULES = {
    "pandas": "phân tích cổ phiếu",
    "numpy": "phân tích cổ phiếu",
    "vnstock": "dữ liệu cổ phiếu",
    "google.genai": "Google AI Studio (api1/api2)",
    "sklearn": "trend model",
    "PIL": "xử lý ảnh",
    "lxml": "đọc web/tin tức",
}
_STARTED = time.time()


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _status_kb(pid: str, field: str) -> int | None:
    text = _read_text(f"/proc/{pid}/status")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith(f"{field}:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _mib(kb: int | None) -> float | None:
    return None if kb is None else round(kb / 1024, 1)


def _bytes_to_mib(raw: str | None) -> float | None:
    if not raw or not raw.isdigit():
        return None
    value = int(raw)
    # cgroup v1 dùng số cực lớn để biểu thị "không giới hạn".
    return None if value >= 1 << 60 else round(value / MIB, 1)


def container_memory() -> dict[str, float | None]:
    """RAM cả container (Python + Node + supervisord) theo cgroup - đây là con
    số Render dùng để quyết định kill khi vượt 512 MB."""
    current = _read_text("/sys/fs/cgroup/memory.current") or _read_text(
        "/sys/fs/cgroup/memory/memory.usage_in_bytes"
    )
    limit = _read_text("/sys/fs/cgroup/memory.max") or _read_text(
        "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    )
    peak = _read_text("/sys/fs/cgroup/memory.peak") or _read_text(
        "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"
    )
    return {
        "used_mib": _bytes_to_mib(current),
        "limit_mib": _bytes_to_mib(limit),
        "peak_mib": _bytes_to_mib(peak),
    }


def _processes() -> list[dict]:
    """RSS từng tiến trình trong container (python/uvicorn, node gateway...)."""
    result = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return result
    for pid in pids:
        raw = _read_text(f"/proc/{pid}/cmdline")
        if not raw:
            continue
        cmdline = raw.replace("\x00", " ").strip()
        rss = _status_kb(pid, "VmRSS")
        if rss is None:
            continue
        if "node" in cmdline and "zalo-gateway/dist" in cmdline:
            name = "zalo-gateway (node)"
        elif int(pid) == os.getpid():
            name = "web (python)"
        elif "supervisord" in cmdline:
            name = "supervisord"
        else:
            name = cmdline.split(" ", 1)[0].rsplit("/", 1)[-1][:40]
        result.append({"pid": int(pid), "name": name, "rss_mib": _mib(rss)})
    return sorted(result, key=lambda p: p["rss_mib"] or 0, reverse=True)


def snapshot() -> dict:
    pid = str(os.getpid())
    container = container_memory()
    used, limit = container["used_mib"], container["limit_mib"]
    return {
        "python": {
            "rss_mib": _mib(_status_kb(pid, "VmRSS")),
            "peak_rss_mib": _mib(_status_kb(pid, "VmHWM")),
            "threads": _status_kb(pid, "Threads"),
            # gc.get_count() rẻ; gc.get_objects() tạo list khổng lồ nên không dùng.
            "gc_pending": list(gc.get_count()),
            "uptime_min": round((time.time() - _STARTED) / 60, 1),
        },
        "container": {
            **container,
            "used_percent": round(used * 100 / limit, 1) if used and limit else None,
        },
        "processes": _processes(),
        "heavy_modules_loaded": {
            name: purpose for name, purpose in _HEAVY_MODULES.items() if name in sys.modules
        },
    }
