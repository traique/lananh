"""State machine của provider-chain (router9|api1|api2). KHÔNG chứa logic gọi
HTTP - chỉ đọc/ghi trạng thái, bền qua restart qua core.database.get_setting/
set_setting, cache trong RAM cho request nhanh.

active_provider chỉ mang tính tham khảo/hiển thị (/status) - quyết định thật
sự ở mỗi request dựa trên router9_dead_since/api_exhausted_until (xem
ai/orchestrator.py: _run_provider_chain).
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional, TypedDict

import messages
from core import alerts, config
from core import database as db

logger = logging.getLogger(__name__)

_STATE_ACTIVE_PROVIDER = "provider_active"
_STATE_ROUTER9_DEAD_SINCE = "provider_router9_dead_since"
_STATE_ROUTER9_ENABLED = "provider_router9_enabled"
_STATE_API_EXHAUSTED_PREFIX = "provider_api_exhausted_until_"
# Lý do cooldown/chết gần nhất (hiện ở /status): "rate limit theo phút...",
# "hết quota NGÀY...", "timeout"... Lưu bảng settings, không cần cột mới.
_STATE_COOLDOWN_REASON_PREFIX = "provider_cooldown_reason_"
# Provider có cooldown-on-quota. router9 dùng cơ chế dead/probe riêng (xem
# mark_router9_dead/alive bên dưới) nhưng lỗi 429 của router9 chỉ là cooldown
# ngắn theo Retry-After, KHÔNG khai tử.
_COOLDOWN_PROVIDERS = ("groq", "openrouter", "api1", "api2", "router9")
# router9 lỗi 5xx/mất kết nối: chỉ khai tử khi đủ ROUTER9_HEALTH_STRIKES lỗi
# trong ROUTER9_HEALTH_WINDOW_SEC (timeout và 401/403 vẫn khai tử ngay).
ROUTER9_HEALTH_STRIKES = 2
ROUTER9_HEALTH_WINDOW_SEC = 300.0


class ProviderStateSnapshot(TypedDict):
    active_provider: str
    router9_dead_since: Optional[float]
    router9_enabled: bool
    groq_exhausted_until: float
    openrouter_exhausted_until: float
    api1_exhausted_until: float
    api2_exhausted_until: float
    router9_exhausted_until: float
    cooldown_reasons: dict[str, str]


# ─── Cảnh báo Telegram khi 9Router chết/sống lại ─────────────────────────────
# Module này không có sẵn bot/chat_id (tránh vòng import với handlers/bot_app)
# - tầng khởi tạo bot đăng ký 1 callback async nhận text, gọi
# set_alert_callback(fn) đúng 1 lần lúc bot_app.build_application(). Callback
# được chuyển sang core.alerts để webhook inbox/migrations (tầng core) dùng
# chung, không cần bot_app đăng ký thêm chỗ nào.
_background_tasks: set[asyncio.Task] = alerts.tasks


def set_alert_callback(fn: Callable[[str], Awaitable[None]]) -> None:
    alerts.set_callback(fn)


def send_alert(text: str) -> None:
    alerts.send(text)


class ProviderChainState:
    def __init__(self) -> None:
        self.active_provider: str = "router9"
        self.router9_dead_since: Optional[float] = None  # epoch seconds, None = sống/chưa biết
        self.router9_enabled: bool = True  # tắt thủ công qua /router9 off, độc lập với dead_since
        # key: "groq"|"openrouter"|"api1"|"api2"|"router9", value: epoch seconds
        self.api_exhausted_until: dict[str, float] = {name: 0.0 for name in _COOLDOWN_PROVIDERS}
        self.cooldown_reasons: dict[str, str] = {}
        # Mốc (monotonic) các lỗi sức khoẻ gần đây của router9 - chỉ trong RAM.
        self._router9_strikes: list[float] = []
        self._lock = asyncio.Lock()
        self._loaded = False

    async def ensure_loaded(self) -> None:
        if not self._loaded:
            await self.load()

    async def load(self) -> None:
        """Nạp state từ DB lúc khởi động. Nếu chưa gọi, các hàm dùng state
        sẽ tự nạp lười ở lần đầu cần tới (qua ensure_loaded())."""
        async with self._lock:
            if self._loaded:
                return
            raw_active = await db.get_setting(_STATE_ACTIVE_PROVIDER)
            # Tương thích ngược: state cũ còn ghi "cookie" (trước khi đổi sang
            # 9Router) -> coi như "router9".
            if raw_active == "cookie":
                raw_active = "router9"
            self.active_provider = (
                raw_active if raw_active in ("router9", *_COOLDOWN_PROVIDERS) else "router9"
            )

            raw_dead = await db.get_setting(_STATE_ROUTER9_DEAD_SINCE)
            try:
                self.router9_dead_since = float(raw_dead) if raw_dead else None
            except ValueError:
                self.router9_dead_since = None

            for name in _COOLDOWN_PROVIDERS:
                raw = await db.get_setting(f"{_STATE_API_EXHAUSTED_PREFIX}{name}")
                try:
                    self.api_exhausted_until[name] = float(raw) if raw else 0.0
                except ValueError:
                    self.api_exhausted_until[name] = 0.0
                reason = await db.get_setting(f"{_STATE_COOLDOWN_REASON_PREFIX}{name}")
                if reason:
                    self.cooldown_reasons[name] = reason

            raw_enabled = await db.get_setting(_STATE_ROUTER9_ENABLED)
            self.router9_enabled = raw_enabled != "0"

            self._loaded = True
            logger.info(
                "Provider-chain state đã nạp: active=%s, router9_dead_since=%s, router9_enabled=%s, api_exhausted=%s",
                self.active_provider,
                self.router9_dead_since,
                self.router9_enabled,
                self.api_exhausted_until,
            )

    async def _persist(self, key: str, value: str) -> None:
        """Ghi state xuống DB kiểu best-effort: state trong RAM mới là nguồn
        quyết định của provider-chain. Supabase chập chờn KHÔNG được biến 1 câu
        trả lời đã sinh xong thành "provider lỗi" (trước đây set_setting raise
        ngay sau lượt gọi thành công -> chain gọi tiếp provider khác, đốt quota
        nhiều lần rồi vẫn báo lỗi)."""
        try:
            await db.set_setting(key, value)
        except Exception:
            logger.warning(
                "Không ghi được state provider %s xuống DB (bỏ qua).", key, exc_info=True
            )

    async def set_active_provider(self, name: str) -> None:
        if self.active_provider == name:
            return  # không đổi -> không ghi DB mỗi lượt gọi
        logger.info("Provider-chain: chuyển active_provider -> %s", name)
        self.active_provider = name
        await self._persist(_STATE_ACTIVE_PROVIDER, name)

    async def mark_router9_dead(self, reason: str = "") -> None:
        just_died = self.router9_dead_since is None
        if not just_died:
            return
        now = time.time()
        self.router9_dead_since = now
        self._router9_strikes.clear()
        if reason:
            self.cooldown_reasons["router9_dead"] = reason
        logger.warning("Provider-chain: khai tử 9Router (%s).", reason or "lỗi")
        await self._persist(_STATE_ROUTER9_DEAD_SINCE, str(now))
        send_alert(messages.ROUTER9_DEAD_ALERT)

    async def mark_router9_alive(self) -> None:
        self._router9_strikes.clear()
        if self.router9_dead_since is None:
            return  # đang sống -> không ghi DB mỗi lượt gọi
        self.router9_dead_since = None
        self.cooldown_reasons.pop("router9_dead", None)
        await self._persist(_STATE_ROUTER9_DEAD_SINCE, "")
        send_alert(messages.ROUTER9_ALIVE_ALERT)

    def note_router9_health_failure(self) -> bool:
        """Ghi 1 lỗi sức khoẻ (5xx, mất kết nối, lỗi lạ) của router9; True nếu
        đã đủ ROUTER9_HEALTH_STRIKES lỗi trong ROUTER9_HEALTH_WINDOW_SEC -> nên
        khai tử. Lỗi theo REQUEST (400/413/422, nội dung bị lọc, output lẫn
        CJK...) không đi qua hàm này."""
        now = time.monotonic()
        self._router9_strikes = [
            ts for ts in self._router9_strikes if now - ts <= ROUTER9_HEALTH_WINDOW_SEC
        ]
        self._router9_strikes.append(now)
        return len(self._router9_strikes) >= ROUTER9_HEALTH_STRIKES

    async def set_router9_enabled(self, enabled: bool) -> None:
        self.router9_enabled = enabled
        await db.set_setting(_STATE_ROUTER9_ENABLED, "1" if enabled else "0")
        logger.info("Provider-chain: 9Router %s thủ công.", "bật" if enabled else "tắt")

    async def mark_api_exhausted(
        self, provider: str, seconds: Optional[float] = None, reason: str = ""
    ) -> None:
        """Cooldown ``seconds`` giây (mặc định API_QUOTA_COOLDOWN_SEC) - caller
        tính theo gợi ý của provider qua ai/cooldown.py::cooldown_for()."""
        duration = float(config.API_QUOTA_COOLDOWN_SEC if seconds is None else seconds)
        until = time.time() + max(0.0, duration)
        self.api_exhausted_until[provider] = until
        if reason:
            self.cooldown_reasons[provider] = reason
        logger.warning(
            "%s hết quota/rate limit (429), cooldown %.0fs (%s).",
            provider,
            duration,
            reason or "mặc định",
        )
        await self._persist(f"{_STATE_API_EXHAUSTED_PREFIX}{provider}", str(until))
        if reason:
            await self._persist(f"{_STATE_COOLDOWN_REASON_PREFIX}{provider}", reason)

    async def reset_api_cooldown(self, provider: str) -> None:
        self.api_exhausted_until[provider] = 0.0
        self.cooldown_reasons.pop(provider, None)
        await db.set_setting(f"{_STATE_API_EXHAUSTED_PREFIX}{provider}", "")
        logger.info("%s: cooldown reset thủ công.", provider)

    def api_in_cooldown(self, provider: str) -> bool:
        return time.time() < self.api_exhausted_until.get(provider, 0.0)

    def snapshot(self) -> ProviderStateSnapshot:
        """Snapshot state hiện tại (RAM) - dùng cho /status. Không await DB
        để /status trả lời tức thì phần này."""
        return ProviderStateSnapshot(
            active_provider=self.active_provider,
            router9_dead_since=self.router9_dead_since,
            router9_enabled=self.router9_enabled,
            groq_exhausted_until=self.api_exhausted_until.get("groq", 0.0),
            openrouter_exhausted_until=self.api_exhausted_until.get("openrouter", 0.0),
            api1_exhausted_until=self.api_exhausted_until.get("api1", 0.0),
            api2_exhausted_until=self.api_exhausted_until.get("api2", 0.0),
            router9_exhausted_until=self.api_exhausted_until.get("router9", 0.0),
            cooldown_reasons=dict(self.cooldown_reasons),
        )


provider_state = ProviderChainState()


async def init_provider_state() -> None:
    """Nạp state provider-chain từ DB lúc khởi động (bot_app._post_init gọi 1 lần)."""
    await provider_state.load()
