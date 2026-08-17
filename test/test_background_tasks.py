import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.background_tasks import stop_tracked_tasks


@pytest.mark.asyncio
async def test_stop_tracked_tasks_allows_quick_task_to_finish():
    finished = asyncio.Event()

    async def quick_work():
        await asyncio.sleep(0)
        finished.set()

    task = asyncio.create_task(quick_work())
    tasks = {task}

    await stop_tracked_tasks(tasks, timeout=1.0)

    assert finished.is_set()
    assert task.done()
    assert tasks == set()


@pytest.mark.asyncio
async def test_stop_tracked_tasks_cancels_task_after_timeout():
    cancelled = asyncio.Event()

    async def hanging_work():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(hanging_work())
    tasks = {task}
    await asyncio.sleep(0)

    await stop_tracked_tasks(tasks, timeout=0.0)

    assert task.cancelled()
    assert cancelled.is_set()
    assert tasks == set()


@pytest.mark.asyncio
async def test_stop_tracked_tasks_can_cancel_loop_immediately():
    task = asyncio.create_task(asyncio.Event().wait())
    tasks = {task}
    await asyncio.sleep(0)

    await stop_tracked_tasks(tasks, cancel_immediately=True)

    assert task.cancelled()
    assert tasks == set()
