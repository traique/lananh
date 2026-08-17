"""Helpers for draining and cancelling application-owned asyncio tasks."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import MutableSet


async def stop_tracked_tasks(
    tasks: MutableSet[asyncio.Task],
    *,
    timeout: float = 15.0,
    cancel_immediately: bool = False,
    logger: logging.Logger | None = None,
    label: str = "background",
) -> None:
    """Stop a tracked task set without leaking exceptions during shutdown.

    User work is allowed to finish for ``timeout`` seconds by default. Infinite
    loops should pass ``cancel_immediately=True``. Any remaining tasks are then
    cancelled and awaited so the event loop does not report destroyed tasks.
    """
    current = asyncio.current_task()
    pending = [task for task in list(tasks) if task is not current and not task.done()]
    if not pending:
        tasks.clear()
        return

    if cancel_immediately:
        for task in pending:
            task.cancel()
    else:
        _, still_pending = await asyncio.wait(pending, timeout=max(0.0, timeout))
        for task in still_pending:
            task.cancel()
        pending = list(still_pending)

    if pending:
        results = await asyncio.gather(*pending, return_exceptions=True)
        if logger is not None:
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    logger.warning(
                        "%s task failed during shutdown: %r", label, result
                    )

    tasks.difference_update(task for task in list(tasks) if task.done())
    tasks.clear()
