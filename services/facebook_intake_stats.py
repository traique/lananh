"""Thống kê bộ lọc Zalo -> hàng chờ Facebook (chỉ trong RAM, reset khi restart).

Dùng cho lệnh /fb_boloc: bộ lọc đang giữ/bỏ bao nhiêu bài và vì sao, tách theo TỪNG
NHÓM nguồn (tên nhóm, không phải group ID) và bao nhiêu bài trùng với bài đã có ở một
nhóm KHÁC - từ đó chỉnh FACEBOOK_REQUIRE_PHOTO_AND_CAPTION / FACEBOOK_SKIP_VOUCHER_POSTS /
FACEBOOK_SKIP_NON_SALE_POSTS hoặc bỏ bớt nhóm nguồn toàn bài đăng lại.
"""

from __future__ import annotations

import time
from collections import Counter, deque

QUEUED = "đã vào hàng chờ"
PRUNED = "tự xoá do vượt ngưỡng bài chờ"
DUPLICATE = "trùng lặp"
# Trần số nhóm theo dõi riêng (nhóm nguồn thực tế vài chục): không lớn dần mãi.
_MAX_GROUPS = 50

_counts: Counter[str] = Counter()
_groups: dict[str, Counter[str]] = {}
_cross_group = 0
_recent: deque[tuple[float, str, str, str]] = deque(maxlen=10)
_started = time.time()


def duplicate_reason(reason: str) -> str:
    """Nhãn thống kê của một bài trùng ("trùng lặp: <lý do>")."""
    return f"{DUPLICATE}: {reason}"


def record(
    reason: str,
    text: str = "",
    *,
    amount: int = 1,
    group: str = "",
    cross_group: bool = False,
) -> None:
    """Ghi 1 kết quả lọc. ``group``: tên hiển thị của nhóm nguồn; ``cross_group``: bài
    trùng với bài đã có ở nhóm khác."""
    global _cross_group
    _counts[reason] += amount
    if cross_group:
        _cross_group += amount
    if group and reason != PRUNED:
        bucket = _groups.get(group)
        if bucket is None and len(_groups) < _MAX_GROUPS:
            bucket = _groups[group] = Counter()
        if bucket is not None:
            if reason == QUEUED:
                bucket["queued"] += amount
            elif reason.startswith(DUPLICATE):
                bucket["duplicate"] += amount
                if cross_group:
                    bucket["cross"] += amount
            else:
                bucket["skipped"] += amount
    if reason not in (QUEUED, PRUNED):
        _recent.appendleft((time.time(), reason, " ".join((text or "").split())[:80], group))


def reset() -> None:
    global _started, _cross_group
    _counts.clear()
    _groups.clear()
    _recent.clear()
    _cross_group = 0
    _started = time.time()


def _group_line(name: str, counts: Counter[str]) -> str:
    parts = [f"{counts['queued']} vào hàng chờ"]
    if counts["duplicate"]:
        cross = f" ({counts['cross']} trùng nhóm khác)" if counts["cross"] else ""
        parts.append(f"{counts['duplicate']} trùng{cross}")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} bỏ")
    return f"• {name}: " + " · ".join(parts)


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
    if _cross_group:
        lines.append(f"🔁 Trùng với bài đã có ở nhóm khác: {_cross_group} bài")
    if _groups:
        lines.extend(["", "Theo nhóm nguồn:"])
        ranked = sorted(_groups.items(), key=lambda item: -sum(item[1].values()))
        lines.extend(_group_line(name, counts) for name, counts in ranked)
    if _recent:
        lines.extend(["", "Bài bị bỏ gần đây:"])
        for ts, reason, text, group in _recent:
            clock = time.strftime("%H:%M", time.localtime(ts))
            where = f" ({group})" if group else ""
            lines.append(f"• {clock} [{reason}]{where} {text or '(không có chữ)'}")
    return "\n".join(lines)
