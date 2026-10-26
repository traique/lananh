"""Gửi thông báo danh mục CHỦ ĐỘNG (cảnh báo giá, quy đổi quyền, lịch sự kiện
quyền) tới đúng người sở hữu dữ liệu.

- Chủ bot (id Telegram dương = ALLOWED_USER_ID): Telegram qua notify callback do
  bot_app đăng ký. Zoom dùng chung id chủ bot nên tin vẫn về Telegram.
- Thành viên Zalo (id ÂM, kể cả tài khoản Zalo của chính chủ bot - ngữ cảnh tách
  riêng Telegram): xếp vào zalo_outbox để gateway gửi, tối đa
  PORTFOLIO_PUSH_ZALO_DAILY_CAP tin/người/ngày (0 = không gửi qua Zalo) - tin
  chủ động qua zca-js làm tăng rủi ro tài khoản bị khoá.

Mỗi lượt gửi thành công được ghi stock_alert_log (kind, ref_key, ngày VN) - vừa
chống gửi trùng qua restart/redeploy, vừa là bộ đếm trần mỗi ngày cho Zalo.
Không có lời gọi AI nào ở đây (không cần khoá lượt/hạn chót).
"""

from __future__ import annotations

import logging

from core import config, database as db
from core.envutil import env_int

logger = logging.getLogger(__name__)

_notify = None


def set_notify_callback(fn) -> None:
    global _notify
    _notify = fn


def zalo_daily_cap() -> int:
    return env_int("PORTFOLIO_PUSH_ZALO_DAILY_CAP", 5, 0, 50)


def can_deliver(user_id: int) -> bool:
    """Người dùng này có nhận được tin chủ động không (để lệnh đặt cảnh báo báo
    trước thay vì đặt rồi im lặng)."""
    if user_id > 0:
        return user_id == config.ALLOWED_USER_ID
    return user_id < 0 and zalo_daily_cap() > 0


async def already_sent(user_id: int, kind: str, ref_key: str, *, on: str | None = None) -> bool:
    """Đã gửi (kind, ref_key) chưa - trong ngày ``on`` hoặc bất kỳ ngày nào."""
    return await db.portfolio_alert_logged(user_id, kind, ref_key, on)


async def mark_sent(user_id: int, kind: str, ref_key: str, today: str) -> None:
    """Ghi nhật ký cho mục đã nằm trong 1 tin gộp vừa gửi (không gửi thêm gì)."""
    try:
        await db.portfolio_log_alert(user_id, kind, ref_key[:200], today)
    except Exception:
        logger.warning("portfolio_push: không ghi được nhật ký %s.", kind, exc_info=True)


async def _zalo_recipient(user_id: int) -> str | None:
    from channels import zalo_users

    for user in await zalo_users.list_users():
        if user.internal_user_id == user_id:
            return user.external_id if user.is_active else None
    return None


async def _send_zalo(user_id: int, text: str, today: str) -> bool:
    cap = zalo_daily_cap()
    if cap <= 0:
        return False
    if await db.portfolio_count_alerts_on(user_id, today) >= cap:
        logger.info("portfolio_push: user Zalo %s đã đủ %d tin hôm nay, bỏ qua.", user_id, cap)
        return False
    recipient = await _zalo_recipient(user_id)
    if not recipient:
        return False
    from channels import zalo_repository

    account_id = await zalo_repository.resolve_active_account_id()
    await zalo_repository.enqueue_outbox(account_id, recipient, text)
    return True


async def deliver(user_id: int, text: str, *, kind: str, ref_key: str, today: str) -> bool:
    """Gửi 1 thông báo; True nếu đã gửi (Telegram) hoặc đã xếp hàng (Zalo)."""
    try:
        if user_id > 0:
            if user_id != config.ALLOWED_USER_ID or _notify is None:
                return False
            await _notify(user_id, text)
        elif user_id < 0:
            if not await _send_zalo(user_id, text, today):
                return False
        else:
            return False
    except Exception:
        logger.warning("portfolio_push: gửi %s cho user %s lỗi.", kind, user_id, exc_info=True)
        return False
    try:
        await db.portfolio_log_alert(user_id, kind, ref_key[:200], today)
    except Exception:
        # Đã gửi nhưng không ghi được nhật ký: lần sau có thể gửi lặp (chấp nhận).
        logger.warning("portfolio_push: không ghi được nhật ký %s.", kind, exc_info=True)
    return True
