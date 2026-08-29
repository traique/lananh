"""Giám sát chủ động cho provider-chain AI - phần "3" trong yêu cầu "biến
lananh thành agent": bot tự phát hiện sự cố nghiêm trọng và chủ động báo,
thay vì chủ bot phải tự gõ /status định kỳ để kiểm tra.

CỐ Ý dùng luật cố định (rule-based), KHÔNG nhờ AI phán đoán có nên báo hay
không: logic báo lỗi provider mà lại phụ thuộc chính provider đang có thể đã
chết là một vòng lặp phụ thuộc nguy hiểm (provider chết -> hỏi AI có nên báo
không -> AI cũng không trả lời được vì chính nó đang chết). Mọi kiểm tra ở
đây chỉ đọc state trong RAM (ai.provider_state), không gọi AI nào.

Khác với ai/provider_state.py::mark_router9_dead() (chỉ báo RIÊNG router9
chết - lananh vẫn trả lời bình thường qua api1/api2), module này báo tình
huống nặng hơn: router9 CỘNG mọi provider cooldown còn lại trong
config.PROVIDER_ORDER đều đang exhausted cùng lúc - nghĩa là KHÔNG provider
nào trả lời được, mọi lệnh AI sẽ lỗi.
"""
import asyncio
import logging
import time

import messages
from ai.provider_state import provider_state, send_alert
from core import config

logger = logging.getLogger(__name__)

_monitor_task: asyncio.Task | None = None
# None = đang bình thường (ít nhất 1 provider dùng được); float = epoch lúc
# phát hiện TOÀN BỘ provider down - dùng để chỉ báo 1 lần lúc bắt đầu down và
# 1 lần lúc hồi phục, không spam mỗi chu kỳ.
_all_down_since: float | None = None


def _is_provider_down(name: str, now: float) -> bool:
    if name == "router9":
        return (not provider_state.router9_enabled) or (provider_state.router9_dead_since is not None)
    return provider_state.api_exhausted_until.get(name, 0.0) > now


async def _check_all_providers_down() -> None:
    global _all_down_since

    await provider_state.ensure_loaded()
    now = time.time()
    order = config.PROVIDER_ORDER
    if not order:
        return

    all_down = all(_is_provider_down(name, now) for name in order)

    if all_down and _all_down_since is None:
        _all_down_since = now
        logger.error(
            "Toàn bộ provider trong PROVIDER_ORDER=%s hiện đều không dùng được.",
            order,
        )
        send_alert(messages.ALL_PROVIDERS_DOWN_ALERT)
    elif not all_down and _all_down_since is not None:
        downtime_min = (now - _all_down_since) / 60
        logger.info("Provider-chain hồi phục sau %.1f phút down toàn bộ.", downtime_min)
        _all_down_since = None
        send_alert(messages.ALL_PROVIDERS_RECOVERED_ALERT)


async def _monitor_loop() -> None:
    while True:
        await asyncio.sleep(config.MONITOR_INTERVAL_SEC)
        try:
            await _check_all_providers_down()
        except Exception:
            logger.warning("Lỗi trong vòng giám sát provider-chain.", exc_info=True)


def start() -> None:
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_monitor_loop())


async def stop() -> None:
    global _monitor_task
    if _monitor_task is not None and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except (asyncio.CancelledError, Exception):
            pass
    _monitor_task = None
