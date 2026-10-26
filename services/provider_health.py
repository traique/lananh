"""Độ trễ p50/p95 và tỉ lệ lỗi của từng provider trong 24 giờ qua (bảng
provider_calls) - thêm vào /status trên Telegram, Zalo/Zoom.

Chỉ đọc số liệu đã ghi (không ping, không gọi AI). DB lỗi -> bỏ phần này,
/status vẫn hiện trạng thái provider như cũ.
"""

from __future__ import annotations

import html
import logging

from core import database as db

logger = logging.getLogger(__name__)

DEFAULT_HOURS = 24
_LABELS = {
    "router9": "9Router",
    "groq": "Groq",
    "openrouter": "OpenRouter",
    "api1": "API 1",
    "api2": "API 2",
    "tavily": "Tavily",
    "agnes": "Agnes",
}


def _seconds(ms) -> str:
    if ms is None:
        return "-"
    return f"{float(ms) / 1000:.1f}s".replace(".", ",")


def format_rows(
    rows: list[dict], hours: int = DEFAULT_HOURS, *, use_html: bool = False
) -> list[str]:
    if not rows:
        return []
    title = f"📈 {hours} giờ qua (lượt gọi · p50/p95 lượt thành công · tỉ lệ lỗi):"
    lines = [f"<b>{title}</b>" if use_html else title]
    for row in rows:
        calls = int(row.get("calls") or 0)
        failed = int(row.get("failed") or 0)
        rate = round(failed * 100 / calls) if calls else 0
        label = _LABELS.get(row.get("provider") or "", row.get("provider") or "?")
        if use_html:
            label = html.escape(label)
        lines.append(
            f"• {label}: {calls} lượt · {_seconds(row.get('p50_ms'))}/"
            f"{_seconds(row.get('p95_ms'))} · lỗi {rate}%"
        )
    return lines


async def latency_lines(hours: int = DEFAULT_HOURS, *, use_html: bool = False) -> list[str]:
    """Các dòng cho /status; [] khi chưa có số liệu hoặc DB lỗi."""
    try:
        rows = await db.provider_latency_summary(hours)
    except Exception:
        logger.warning("Không đọc được độ trễ provider cho /status.", exc_info=True)
        return []
    return format_rows(rows, hours, use_html=use_html)
