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

Cùng vòng lặp chạy watchdog vận hành (services/watchdog.py: RAM, dung lượng
DB, inbox ứ đọng, job quá hạn, nhịp tim), lưu bộ đếm lỗi và gửi bản tổng hợp
lỗi hằng ngày (services/error_digest.py); lúc khởi động còn phân loại lần
restart vừa rồi và hoàn tất xoay khoá settings nếu có SETTINGS_ENC_KEY_OLD
(services/key_rotation.py). Tất cả vẫn là luật cố định, không gọi AI.
"""

import asyncio
import logging
import time

import messages
from ai.provider_state import provider_state, send_alert
from core import config
from services import error_digest, key_rotation, quota_burn, watchdog

logger = logging.getLogger(__name__)

_monitor_task: asyncio.Task | None = None
_startup_task: asyncio.Task | None = None
# Trần thời gian ghi nhịp tim "tắt êm" lúc shutdown (không kéo dài drain).
_SHUTDOWN_STEP_SEC = 3.0
# None = đang bình thường (ít nhất 1 provider dùng được); float = epoch lúc
# phát hiện TOÀN BỘ provider down - dùng để chỉ báo 1 lần lúc bắt đầu down và
# 1 lần lúc hồi phục, không spam mỗi chu kỳ.
_all_down_since: float | None = None


def _is_provider_down(name: str, now: float) -> bool:
    if name == "router9":
        return (
            (not provider_state.router9_enabled)
            or (provider_state.router9_dead_since is not None)
            or provider_state.api_exhausted_until.get("router9", 0.0) > now
        )
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
            extra={"alerted": True},
        )
        send_alert(messages.ALL_PROVIDERS_DOWN_ALERT)
    elif not all_down and _all_down_since is not None:
        downtime_min = (now - _all_down_since) / 60
        logger.info("Provider-chain hồi phục sau %.1f phút down toàn bộ.", downtime_min)
        _all_down_since = None
        send_alert(messages.ALL_PROVIDERS_RECOVERED_ALERT)


async def _run_step(label: str, step) -> None:
    try:
        await step()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("monitor: bước %s lỗi.", label, exc_info=True)


async def _startup_checks() -> None:
    """Chạy 1 lần ở nền lúc khởi động (không chặn bot nhận tin)."""
    await _run_step("gộp bộ đếm lỗi đã lưu", error_digest.load)
    await _run_step("phân loại lần khởi động", watchdog.detect_restart)
    # Có SETTINGS_ENC_KEY_OLD: mã hoá lại giá trị còn ở khoá cũ, báo số còn lại.
    await _run_step("xoay khoá settings", key_rotation.on_startup)


async def _monitor_loop() -> None:
    while True:
        await asyncio.sleep(config.MONITOR_INTERVAL_SEC)
        try:
            await _check_all_providers_down()
        except Exception:
            logger.warning("Lỗi trong vòng giám sát provider-chain.", exc_info=True)
        await _run_step("watchdog", watchdog.run_checks)
        # Quota miễn phí từng provider >= QUOTA_BURN_ALERT_PCT (tối đa 15 phút/lần).
        await _run_step("quota miễn phí", quota_burn.check_and_alert)
        await _run_step("lưu bộ đếm lỗi", error_digest.persist)
        await _run_step("tổng hợp lỗi hằng ngày", error_digest.send_daily_digest_if_due)


def start() -> None:
    global _monitor_task, _startup_task
    error_digest.bind_loop()
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_monitor_loop())
    if _startup_task is None:
        _startup_task = asyncio.create_task(_startup_checks())


async def _cancel(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def stop() -> None:
    global _monitor_task, _startup_task
    await _cancel(_monitor_task)
    await _cancel(_startup_task)
    _monitor_task = None
    _startup_task = None
    for label, step in (
        ("ghi nhịp tim", lambda: watchdog.write_heartbeat(clean=True)),
        ("lưu bộ đếm lỗi", error_digest.persist),
    ):
        try:
            await asyncio.wait_for(step(), _SHUTDOWN_STEP_SEC)
        except Exception:
            logger.warning("Không %s được lúc tắt.", label, exc_info=True)
