"""Carry the authenticated channel address into reminder tools."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class NotificationTarget:
    channel: str
    recipient_id: str
    account_id: str = ""
    user_jid: str = ""
    event_key: str | None = None


_target: ContextVar[NotificationTarget | None] = ContextVar("notification_target", default=None)


@contextmanager
def notification_target(target: NotificationTarget):
    token = _target.set(target)
    try:
        yield
    finally:
        _target.reset(token)


def current_target(user_id: int) -> NotificationTarget:
    target = _target.get()
    if target is not None:
        return target
    if user_id <= 0:
        raise ValueError("Thiếu địa chỉ kênh nhận nhắc việc.")
    return NotificationTarget("telegram", str(user_id))
