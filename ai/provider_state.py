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
from core import config
from core import database as db

logger = logging.getLogger(__name__)

_STATE_ACTIVE_PROVIDER = "provider_active"
_STATE_ROUTER9_DEAD_SINCE = "provider_router9_dead_since"
_STATE_API_EXHAUSTED_PREFIX = "provider_api_exhausted_until_"


class ProviderStateSnapshot(TypedDict):
    active_provider: str
    router9_dead_since: Optional[float]
    api1_exhausted_until: float
    api2_exhausted_until: float


# ─── Cảnh báo Telegram khi 9Router chết/sống lại ─────────────────────────────
# Module này không có sẵn bot/chat_id (tránh vòng import với handlers/bot_app)
# - tầng khởi tạo bot đăng ký 1 callback async nhận text, gọi
# set_alert_callback(fn) đúng 1 lần lúc bot_app.build_application().
_alert_callback: Optional[Callable[[str], Awaitable[None]]] = None
_background_tasks: set[asyncio.Task] = set()


def set_alert_callback(fn: Callable[[str], Awaitable[None]]) -> None:
    global _alert_callback
    _alert_callback = fn


def send_alert(text: str) -> None:
    if _alert_callback is None:
        return
    try:
        task = asyncio.create_task(_alert_callback(text))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception:
        logger.warning("Không gửi được cảnh báo qua Telegram.", exc_info=True)


class ProviderChainState:
    def __init__(self) -> None:
        self.active_provider: str = "router9"
        self.router9_dead_since: Optional[float] = None  # epoch seconds, None = sống/chưa biết
        self.api_exhausted_until: dict[int, float] = {1: 0.0, 2: 0.0}  # epoch seconds
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
            self.active_provider = raw_active if raw_active in ("router9", "api1", "api2") else "router9"

            raw_dead = await db.get_setting(_STATE_ROUTER9_DEAD_SINCE)
            try:
                self.router9_dead_since = float(raw_dead) if raw_dead else None
            except ValueError:
                self.router9_dead_since = None

            for idx in (1, 2):
                raw = await db.get_setting(f"{_STATE_API_EXHAUSTED_PREFIX}{idx}")
                try:
                    self.api_exhausted_until[idx] = float(raw) if raw else 0.0
                except ValueError:
                    self.api_exhausted_until[idx] = 0.0

            self._loaded = True
            logger.info(
                "Provider-chain state đã nạp: active=%s, router9_dead_since=%s, api_exhausted=%s",
                self.active_provider, self.router9_dead_since, self.api_exhausted_until,
            )

    async def set_active_provider(self, name: str) -> None:
        if self.active_provider != name:
            logger.info("Provider-chain: chuyển active_provider -> %s", name)
        self.active_provider = name
        await db.set_setting(_STATE_ACTIVE_PROVIDER, name)

    async def mark_router9_dead(self) -> None:
        just_died = self.router9_dead_since is None
        now = time.time()
        self.router9_dead_since = now
        await db.set_setting(_STATE_ROUTER9_DEAD_SINCE, str(now))
        if just_died:
            send_alert(messages.ROUTER9_DEAD_ALERT)

    async def mark_router9_alive(self) -> None:
        was_dead = self.router9_dead_since is not None
        self.router9_dead_since = None
        await db.set_setting(_STATE_ROUTER9_DEAD_SINCE, "")
        if was_dead:
            send_alert(messages.ROUTER9_ALIVE_ALERT)

    async def mark_api_exhausted(self, idx: int) -> None:
        until = time.time() + config.API_QUOTA_COOLDOWN_SEC
        self.api_exhausted_until[idx] = until
        await db.set_setting(f"{_STATE_API_EXHAUSTED_PREFIX}{idx}", str(until))
        logger.warning("api%s hết quota (429), cooldown %ss.", idx, config.API_QUOTA_COOLDOWN_SEC)

    def api_in_cooldown(self, idx: int) -> bool:
        return time.time() < self.api_exhausted_until.get(idx, 0.0)

    def snapshot(self) -> ProviderStateSnapshot:
        """Snapshot state hiện tại (RAM) - dùng cho /status. Không await DB
        để /status trả lời tức thì phần này."""
        return ProviderStateSnapshot(
            active_provider=self.active_provider,
            router9_dead_since=self.router9_dead_since,
            api1_exhausted_until=self.api_exhausted_until.get(1, 0.0),
            api2_exhausted_until=self.api_exhausted_until.get(2, 0.0),
        )


provider_state = ProviderChainState()


async def init_provider_state() -> None:
    """Nạp state provider-chain từ DB lúc khởi động (bot_app._post_init gọi 1 lần)."""
    await provider_state.load()


def get_provider_state_snapshot() -> ProviderStateSnapshot:
    return provider_state.snapshot()
