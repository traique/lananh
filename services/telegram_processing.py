"""Expose handler failures swallowed by python-telegram-bot to the inbox worker."""

from contextvars import ContextVar

update_error: ContextVar[BaseException | None] = ContextVar("telegram_update_error", default=None)


def record_error(error: BaseException) -> None:
    update_error.set(error)
