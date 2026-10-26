"""Người nhận câu trả lời của lượt hiện tại (cách xưng hô riêng, có phải chủ bot).

Kênh đặt 1 lần ở điểm vào bằng ``use()`` (router Zalo sau khi resolve người
gửi); tầng service đọc qua ``current()`` - không phải truyền thêm tham số qua
cả chuỗi dispatch -> lệnh -> chat -> ảnh (cùng kiểu
services.reminder_delivery.notification_target). ContextVar cô lập theo từng
asyncio Task nên nhiều tin Zalo chạy song song không lẫn người nhận của nhau;
task con (asyncio.gather) thừa hưởng giá trị của lượt.

Không đặt gì (Telegram, Zoom - chỉ chủ bot dùng) = mặc định: không có cách
xưng hô riêng, chủ bot nhận ra qua user_id (config.is_owner_user_id).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import messages
from core import address, config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplyAudience:
    # Cách gọi người nhận do chủ bot đặt (core/address.py); None = mặc định persona.
    address_form: str | None = None
    # Kiểm tra người nhận có phải chủ bot không (Zalo: tài khoản controller -
    # KHÁC role admin, /zaloadmin cấp được cho người khác). Gọi LƯỜI, chỉ ở nhánh
    # lỗi/câu trả lời dự phòng, để lượt bình thường không tốn thêm 1 lần đọc DB.
    owner_check: Callable[[], Awaitable[bool]] | None = None


_DEFAULT = ReplyAudience()
_current: ContextVar[ReplyAudience] = ContextVar("reply_audience", default=_DEFAULT)


def current() -> ReplyAudience:
    return _current.get()


@contextmanager
def use(audience: ReplyAudience) -> Iterator[None]:
    token = _current.set(audience)
    try:
        yield
    finally:
        _current.reset(token)


def forms(address_form: str | None = None) -> tuple[str, str]:
    """(cách gọi người nhận, Lan Anh tự xưng) cho câu do CODE viết: ``address_form``
    nếu có, không thì cách xưng hô riêng của lượt (Zalo /zalopair), mặc định
    "anh"/"em". Viết hoa đầu câu bằng ``.capitalize()``."""
    you = address.normalize(address_form or current().address_form) or "anh"
    return you, address.self_word(you)


async def is_owner(user_id: int) -> bool:
    """Người nhận được xem chi tiết kỹ thuật (loại lỗi, nhãn ⚙️ API...) không:
    chủ bot Telegram/Zoom (ALLOWED_USER_ID) hoặc owner_check của kênh trả True.
    Lỗi khi kiểm -> False (thà ẩn chi tiết còn hơn lộ cho thành viên)."""
    if config.is_owner_user_id(user_id):
        return True
    check = _current.get().owner_check
    if check is None:
        return False
    try:
        return bool(await check())
    except Exception:
        logger.warning("Không kiểm được người nhận có phải chủ bot không.", exc_info=True)
        return False


async def error_text(kind: str, user_id: int, detail: str = "") -> str:
    """messages.friendly_error cho người nhận của lượt hiện tại."""
    return messages.friendly_error(
        kind,
        owner=await is_owner(user_id),
        address_form=current().address_form,
        detail=detail,
    )
