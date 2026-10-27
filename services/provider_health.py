"""Độ trễ p50/p95 và tỉ lệ lỗi của từng provider trong 24 giờ qua (bảng
provider_calls) - thêm vào /status trên Telegram, Zalo/Zoom.

Chỉ đọc số liệu đã ghi (không ping, không gọi AI). DB lỗi -> bỏ phần này,
/status vẫn hiện trạng thái provider như cũ. Kèm vài dòng "Gemini Live" (cấu hình,
cooldown riêng, số lượt trong ngày quota) - ``live_lines``.
"""

from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ai import live_client
from ai.provider_state import provider_state
from core import config
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
    "live_api1": "Gemini Live (API 1)",
    "live_api2": "Gemini Live (API 2)",
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


# ─── Gemini Live (ai/live_client.py) ─────────────────────────────────────────
# Không ping Live (mỗi lần mở phiên tốn 1 lượt quota chưa rõ hạn mức): chỉ hiện
# cấu hình, cooldown và số lượt đã gọi trong ngày quota đọc từ provider_calls.
LIVE_RATE_LIMIT_URL = "aistudio.google.com/rate-limit"


def _fmt_until(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%H:%M %d/%m")


async def live_usage_today(now=None) -> tuple[int, int]:
    """(số lượt Live, số lượt bị 429) từ 0h giờ Thái Bình Dương (ngày quota Gemini)."""
    from services import quota_burn

    now = now or datetime.now(timezone.utc)
    start = now.astimezone(quota_burn.PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await db.provider_burn_since(list(live_client.PROVIDER_NAMES), start)
    requests = sum(int(row.get("requests") or 0) for row in rows)
    limited = sum(int(row.get("rate_limited") or 0) for row in rows)
    return requests, limited


async def live_lines(*, use_html: bool = False) -> list[str]:
    """Các dòng "Gemini Live" cho /status (Telegram HTML hoặc chữ thuần Zalo/Zoom)."""

    def esc(text: str) -> str:
        return html.escape(text) if use_html else text

    try:
        model = await live_client.model_name()
        admin_on = await live_client.admin_enabled()
        configured = await live_client.configured()
    except Exception:
        logger.warning("Không đọc được cấu hình Gemini Live cho /status.", exc_info=True)
        return []
    from services import voice_reply

    if not admin_on:
        state = "⛔ đang TẮT ở trang admin"
    elif not configured:
        state = "chưa có key api1/api2"
    else:
        state = "bật"
    chat = (
        "dự phòng cuối chuỗi chat"
        if config.LIVE_CHAT_FALLBACK and "live" in config.PROVIDER_ORDER
        else "không dùng cho chat (LIVE_CHAT_FALLBACK=0)"
    )
    lines = [f"🎙️ Gemini Live ({esc(model)}): {state} · {chat} · /noi: {voice_reply.voice_engine()}"]
    now = time.time()
    reasons = provider_state.cooldown_reasons
    for name in live_client.PROVIDER_NAMES:
        until = provider_state.api_exhausted_until.get(name, 0.0)
        if until > now:
            reason = reasons.get(name)
            lines.append(
                f"   ⥅ {name} cooldown tới {_fmt_until(until)}"
                + (f" ({esc(reason)})" if reason else "")
            )
    try:
        requests, limited = await live_usage_today()
        usage = f"   ⥅ ngày quota này (từ 0h giờ Mỹ): {requests} lượt"
        if limited:
            usage += f", {limited} lượt bị từ chối vì quota"
        if live_client.active_sessions():
            usage += f" · đang mở {live_client.active_sessions()} phiên"
        lines.append(usage)
    except Exception:
        logger.warning("Không đếm được lượt Gemini Live cho /status.", exc_info=True)
    lines.append(
        "   ⥅ Google chưa công bố hạn mức free tier của Live - xem số thật tại "
        f"{LIVE_RATE_LIMIT_URL}"
    )
    return lines
