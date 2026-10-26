"""Người nhận câu trả lời của lượt hiện tại (cách xưng hô riêng...).

Kênh đặt 1 lần ở điểm vào bằng ``use()`` (router Zalo sau khi resolve người
gửi); tầng service đọc qua ``current()`` - không phải truyền thêm tham số qua
cả chuỗi dispatch -> lệnh -> chat -> ảnh (cùng kiểu
services.reminder_delivery.notification_target). ContextVar cô lập theo từng
asyncio Task nên nhiều tin Zalo chạy song song không lẫn người nhận của nhau;
task con (asyncio.gather) thừa hưởng giá trị của lượt.

Không đặt gì (Telegram, Zoom - chỉ chủ bot dùng) = mặc định: không có cách
xưng hô riêng.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyAudience:
    # Cách gọi người nhận do chủ bot đặt (core/address.py); None = mặc định persona.
    address_form: str | None = None


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
