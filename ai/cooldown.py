"""Tính thời gian cooldown cho 1 lỗi hết quota/rate-limit (429) theo gợi ý
của chính provider, thay vì 1 giờ cố định cho mọi lỗi 429.

Trước đây 1 lỗi 429 theo PHÚT (vd Groq 8K token/phút cho gpt-oss-20b) cũng
khoá cả nhánh Groq 1 giờ, đẩy tool-router và trích trí nhớ sang quota Gemini
nhỏ hơn, rồi Gemini cũng 429 theo -> dây chuyền tới "mọi provider đều chết".

Quy tắc:
- Hết quota theo NGÀY (Groq "per day"/RPD/TPD, OpenRouter free-models-per-day,
  Gemini QuotaFailure có quotaId chứa "PerDay"): cooldown dài
  (config.API_QUOTA_COOLDOWN_SEC, mặc định 3600s; Gemini lấy min với thời gian
  tới nửa đêm giờ Thái Bình Dương - lúc RPD reset).
- Còn lại (theo phút/giây): clamp(gợi ý của provider, hoặc 90s, 30s..900s).
  Gợi ý đọc từ Retry-After / X-RateLimit-Reset / "try again in Xs" (xem
  ai/openai_compatible.retry_hint_from_response) hoặc RetryInfo.retryDelay
  của Gemini. KHÔNG dùng câu "quota exceeded" làm dấu hiệu theo ngày - Gemini
  ghi câu đó cả cho lỗi theo phút.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ai import openai_compatible
from core import config

MIN_COOLDOWN_SEC = 30.0
DEFAULT_SHORT_COOLDOWN_SEC = 90.0
MAX_SHORT_COOLDOWN_SEC = 900.0

_PT = ZoneInfo("America/Los_Angeles")
_DAILY_MARKERS_RE = re.compile(
    r"per[\s_-]?day|\bRPD\b|\bTPD\b|requests per day|tokens per day|daily|"
    r"free-models-per-day",
    re.IGNORECASE,
)
_RETRY_DELAY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def _clamp_short(seconds: float | None) -> float:
    value = DEFAULT_SHORT_COOLDOWN_SEC if seconds is None else float(seconds)
    return max(MIN_COOLDOWN_SEC, min(MAX_SHORT_COOLDOWN_SEC, value))


def _daily_cap() -> float:
    return float(max(MIN_COOLDOWN_SEC, config.API_QUOTA_COOLDOWN_SEC))


def seconds_until_pacific_midnight(now: datetime | None = None) -> float:
    current = (now or datetime.now(_PT)).astimezone(_PT)
    midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - current).total_seconds()


def _gemini_details(exc: BaseException) -> list[dict[str, Any]]:
    """Danh sách google.rpc details (QuotaFailure, RetryInfo...) trong APIError."""
    raw = getattr(exc, "details", None)
    if isinstance(raw, dict):
        inner = raw.get("error") if isinstance(raw.get("error"), dict) else raw
        details = inner.get("details") if isinstance(inner, dict) else None
        if isinstance(details, list):
            return [item for item in details if isinstance(item, dict)]
    return []


def gemini_cooldown(exc: BaseException) -> tuple[float, str]:
    details = _gemini_details(exc)
    quota_ids: list[str] = []
    retry_delay: float | None = None
    for item in details:
        kind = str(item.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            for violation in item.get("violations") or []:
                if isinstance(violation, dict):
                    quota_ids.append(str(violation.get("quotaId", "")))
        elif kind.endswith("RetryInfo"):
            match = _RETRY_DELAY_RE.match(str(item.get("retryDelay", "")))
            if match:
                retry_delay = float(match.group(1))
    if any("perday" in quota_id.lower() for quota_id in quota_ids):
        seconds = max(MIN_COOLDOWN_SEC, min(_daily_cap(), seconds_until_pacific_midnight()))
        return seconds, "hết quota NGÀY của Gemini (reset nửa đêm giờ Mỹ)"
    seconds = _clamp_short(retry_delay)
    suffix = f", Gemini báo chờ {retry_delay:g}s" if retry_delay is not None else ""
    return seconds, f"rate limit theo phút{suffix}"


def openai_compatible_cooldown(exc: BaseException) -> tuple[float, str]:
    if _DAILY_MARKERS_RE.search(openai_compatible.error_text(exc)):
        return _daily_cap(), "hết quota NGÀY (provider báo giới hạn theo ngày)"
    hint = openai_compatible.retry_after(exc)
    seconds = _clamp_short(hint)
    suffix = f", provider báo chờ {hint:g}s" if hint is not None else ""
    return seconds, f"rate limit theo phút{suffix}"


def live_cooldown(exc: BaseException) -> tuple[float, str]:
    """Gemini Live (ai/live_client.py): lỗi quota thường đến dưới dạng mã đóng
    WebSocket kèm câu chữ (không có QuotaFailure/RetryInfo) - đọc thêm câu chữ để
    biết là quota NGÀY. Hạn mức free tier của Live chưa được Google công bố."""
    seconds, reason = gemini_cooldown(exc)
    if "NGÀY" in reason:
        return seconds, f"Gemini Live {reason}"
    if _DAILY_MARKERS_RE.search(str(exc)):
        seconds = max(MIN_COOLDOWN_SEC, min(_daily_cap(), seconds_until_pacific_midnight()))
        return seconds, "hết quota NGÀY của Gemini Live (reset nửa đêm giờ Mỹ)"
    return seconds, f"Gemini Live {reason}"


def cooldown_for(provider: str, exc: BaseException) -> tuple[float, str]:
    """(số giây cooldown, lý do tiếng Việt) cho lỗi 429 ``exc`` của ``provider``."""
    if provider.startswith("live_"):
        return live_cooldown(exc)
    if provider in ("api1", "api2"):
        return gemini_cooldown(exc)
    return openai_compatible_cooldown(exc)
