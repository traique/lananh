"""Mức đốt quota miễn phí của từng provider + cảnh báo khi chạm ngưỡng (mặc định 80%).

Đếm từ bảng provider_calls (lượt bị 429 không tính là lượt đã dùng) theo ĐÚNG cửa
sổ quota của từng nơi, không theo ngày Việt Nam:

- Gemini (api1/api2): giới hạn tính theo PROJECT Google Cloud, không theo key, và
  reset lúc 0h giờ Thái Bình Dương (America/Los_Angeles). Hai key cùng 1 project
  (GEMINI_KEYS_SAME_PROJECT=1) thì cộng chung 1 dòng "gemini".
- OpenRouter: model :free giới hạn theo NGÀY (UTC) - 50 lượt khi tài khoản chưa
  từng nạp 10 USD, 1000 khi đã nạp (FREE_TIER_RPD_OPENROUTER).
- Groq: số còn lại chính xác lấy từ header x-ratelimit-remaining-requests (quota
  ngày) / -tokens (quota phút) của response Groq gần nhất (ai/call_log.py) - không
  cần đoán giới hạn; FREE_TIER_RPD_GROQ chỉ dùng khi chưa có header.
- Tavily: credit theo THÁNG (gói free 1000; tìm "advanced" tính 2 credit).
- Agnes: chỉ hiển thị số lượt (không rõ hạn mức).

Giới hạn 0 = chưa đặt -> chỉ hiển thị, không cảnh báo. Cảnh báo đi qua core.alerts
tối đa 1 lần/provider/cửa sổ quota (đánh dấu ở bảng settings, sống qua restart);
kiểm tra tối đa mỗi CHECK_INTERVAL_SEC trong vòng services/monitor_service. Luật
cố định, không gọi AI.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ai import call_log
from core import alerts
from core import database as db
from core.envutil import env_int

logger = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")
CHECK_INTERVAL_SEC = 900
_ALERT_KEY = "quota_burn:alerted:{name}"
# Header Groq cũ hơn ngần này thì coi như không còn đúng (quota ngày đã có thể reset).
_HEADER_MAX_AGE_SEC = 6 * 3600
_last_check = 0.0


@dataclass(frozen=True)
class BurnSpec:
    name: str  # khoá hiển thị/cảnh báo
    label: str
    providers: tuple[str, ...]  # giá trị cột provider_calls.provider
    period: str  # "day" | "month"
    tz: ZoneInfo | timezone
    limit_env: str
    default_limit: int
    unit: str = "lượt"  # "lượt" | "credit"


@dataclass
class Burn:
    name: str
    label: str
    period: str
    window_start: str
    requests: int = 0
    rate_limited: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    used: int = 0
    limit: int = 0
    unit: str = "lượt"
    pct: float | None = None
    source: str = "provider_calls"
    headers: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _same_gemini_project() -> bool:
    return os.getenv("GEMINI_KEYS_SAME_PROJECT", "0").strip() == "1"


def specs() -> list[BurnSpec]:
    gemini_providers = (("gemini", "Gemini (api1+api2)", ("api1", "api2")),)
    if not _same_gemini_project():
        gemini_providers = (
            ("api1", "Gemini API 1", ("api1",)),
            ("api2", "Gemini API 2", ("api2",)),
        )
    items = [
        BurnSpec(name, label, providers, "day", PACIFIC, "FREE_TIER_RPD_GEMINI", 0)
        for name, label, providers in gemini_providers
    ]
    items += [
        BurnSpec("groq", "Groq", ("groq",), "day", timezone.utc, "FREE_TIER_RPD_GROQ", 0),
        BurnSpec(
            "openrouter",
            "OpenRouter",
            ("openrouter",),
            "day",
            timezone.utc,
            "FREE_TIER_RPD_OPENROUTER",
            50,
        ),
        BurnSpec(
            "tavily",
            "Tavily",
            ("tavily",),
            "month",
            timezone.utc,
            "FREE_TIER_TAVILY_MONTHLY",
            1000,
            unit="credit",
        ),
        BurnSpec("agnes", "Agnes (ảnh)", ("agnes",), "day", timezone.utc, "", 0),
    ]
    return items


def window_start(spec: BurnSpec, now: datetime) -> datetime:
    local = now.astimezone(spec.tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if spec.period == "month":
        start = start.replace(day=1)
    return start


def _limit(spec: BurnSpec) -> int:
    if not spec.limit_env:
        return 0
    return env_int(spec.limit_env, spec.default_limit, 0, 10_000_000)


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


async def _groq_chat_model() -> str:
    """Model chat của Groq (override trang admin/`/model` hoặc GROQ_MODEL) - quota
    Groq tính theo model, trang quota đọc bucket của model chat."""
    try:
        from ai import groq_client

        return await groq_client.model_name()
    except Exception:
        logger.warning("Không đọc được model Groq đang dùng, lấy GROQ_MODEL.", exc_info=True)
        from core import config

        return config.GROQ_MODEL


def _apply_groq_headers(burn: Burn, now_epoch: float, model: str) -> None:
    snapshot = call_log.latest_ratelimit("groq", model)
    if snapshot is None or now_epoch - snapshot[0] > _HEADER_MAX_AGE_SEC:
        return
    headers = snapshot[1]
    burn.headers = dict(headers)
    limit = _int(headers.get("x-ratelimit-limit-requests"))
    remaining = _int(headers.get("x-ratelimit-remaining-requests"))
    if limit and remaining is not None:
        burn.limit = limit
        burn.used = max(0, limit - remaining)
        burn.source = "header"


async def snapshot(now: datetime | None = None) -> list[Burn]:
    """Mức dùng hiện tại của mọi provider (mỗi cửa sổ quota 1 câu SQL)."""
    now = now or datetime.now(timezone.utc)
    items = specs()
    by_start: dict[datetime, list[BurnSpec]] = {}
    for spec in items:
        by_start.setdefault(window_start(spec, now), []).append(spec)
    counts: dict[tuple[datetime, str], dict] = {}
    for start, group in by_start.items():
        providers = sorted({p for spec in group for p in spec.providers})
        for row in await db.provider_burn_since(providers, start):
            counts[(start, row["provider"])] = row
    burns: list[Burn] = []
    for spec in items:
        start = window_start(spec, now)
        burn = Burn(
            name=spec.name,
            label=spec.label,
            period=spec.period,
            window_start=start.isoformat(),
            unit=spec.unit,
            limit=_limit(spec),
        )
        for provider in spec.providers:
            row = counts.get((start, provider)) or {}
            burn.requests += int(row.get("requests") or 0)
            burn.rate_limited += int(row.get("rate_limited") or 0)
            burn.prompt_tokens += int(row.get("prompt_tokens") or 0)
            burn.completion_tokens += int(row.get("completion_tokens") or 0)
            burn.used += int(row.get("credits" if spec.unit == "credit" else "requests") or 0)
        if spec.name == "groq":
            _apply_groq_headers(burn, now.timestamp(), await _groq_chat_model())
        if burn.limit:
            burn.pct = round(burn.used * 100 / burn.limit, 1)
        burns.append(burn)
    return burns


def alert_pct() -> int:
    return env_int("QUOTA_BURN_ALERT_PCT", 80, 0, 100)


def _window_id(burn: Burn) -> str:
    return burn.window_start[:7] if burn.period == "month" else burn.window_start[:10]


def alert_text(burn: Burn) -> str:
    window = "tháng này" if burn.period == "month" else "hôm nay (theo cửa sổ quota)"
    extra = ""
    if burn.name.startswith(("api", "gemini")):
        extra = " Quota Gemini reset lúc 0h giờ Thái Bình Dương (~14-15h giờ VN)."
    elif burn.source == "header":
        extra = " (số chính xác từ header Groq)"
    return (
        f"⛽ {burn.label} đã dùng {burn.used}/{burn.limit} {burn.unit} {window} "
        f"({burn.pct:.0f}%).{extra} Bot sẽ tự chuyển provider khác khi hết; xem "
        "/admin → Thống kê AI."
    )


async def check_and_alert(now: datetime | None = None, *, force: bool = False) -> list[str]:
    """Cảnh báo provider vượt QUOTA_BURN_ALERT_PCT (1 lần/cửa sổ). Trả tên đã báo."""
    global _last_check
    threshold = alert_pct()
    if threshold <= 0:
        return []
    if not force and time.monotonic() - _last_check < CHECK_INTERVAL_SEC:
        return []
    _last_check = time.monotonic()
    alerted: list[str] = []
    for burn in await snapshot(now):
        if not burn.limit or burn.pct is None or burn.pct < threshold:
            continue
        key = _ALERT_KEY.format(name=burn.name)
        window = _window_id(burn)
        if await db.get_setting(key) == window:
            continue
        await db.set_setting(key, window)
        alerts.send(alert_text(burn), key=f"quota_burn:{burn.name}", cooldown=3600)
        alerted.append(burn.name)
    return alerted
