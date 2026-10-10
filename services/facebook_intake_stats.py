"""Thống kê bộ lọc Zalo -> hàng chờ Facebook (chỉ trong RAM, reset khi restart).

Dùng cho lệnh /fb_boloc để biết bộ lọc đang giữ/bỏ bao nhiêu bài và vì sao,
từ đó chỉnh FACEBOOK_REQUIRE_PHOTO_AND_CAPTION / FACEBOOK_SKIP_VOUCHER_POSTS.
"""

from __future__ import annotations

import time
from collections import Counter, deque

QUEUED = "đã vào hàng chờ"
PRUNED = "tự xoá do vượt ngưỡng bài chờ"

_counts: Counter[str] = Counter()
_recent: deque[tuple[float, str, str]] = deque(maxlen=10)
_started = time.time()


def record(reason: str, text: str = "", *, amount: int = 1) -> None:
    _counts[reason] += amount
    if reason not in (QUEUED, PRUNED):
        _recent.appendleft((time.time(), reason, " ".join((text or "").split())[:80]))


def reset() -> None:
    global _started
    _counts.clear()
    _recent.clear()
    _started = time.time()


def summary() -> str:
    hours = max(0.0, (time.time() - _started) / 3600)
    lines = [f"🧮 BỘ LỌC ZALO → FACEBOOK ({hours:.1f} giờ gần nhất, từ lần khởi động)"]
    if not _counts:
        lines.append("Chưa có bài nào từ nhóm nguồn.")
        return "\n".join(lines)
    total = sum(v for k, v in _counts.items() if k != PRUNED)
    for reason, value in _counts.most_common():
        share = f" ({value * 100 // total}%)" if total and reason != PRUNED else ""
        lines.append(f"- {reason}: {value}{share}")
    if _recent:
        lines.extend(["", "Bài bị bỏ gần đây:"])
        for ts, reason, text in _recent:
            clock = time.strftime("%H:%M", time.localtime(ts))
            lines.append(f"• {clock} [{reason}] {text or '(không có chữ)'}")
    return "\n".join(lines)
