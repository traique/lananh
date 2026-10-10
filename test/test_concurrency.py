import asyncio

from services import concurrency


async def test_same_user_turns_are_serialized():
    order = []

    async def turn(tag):
        async with concurrency.assistant_turn("zalo:1"):
            order.append(f"{tag}-start")
            await asyncio.sleep(0.01)
            order.append(f"{tag}-end")

    await asyncio.gather(turn("a"), turn("b"))
    assert order in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )
    assert not concurrency._turn_locks


async def test_different_users_run_in_parallel():
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        async with concurrency.assistant_turn("zalo:1"):
            started.set()
            await release.wait()

    task = asyncio.create_task(slow())
    await started.wait()
    # User khác KHÔNG phải chờ user đang chạy lượt dài.
    async with asyncio.timeout(1):
        async with concurrency.assistant_turn("zalo:2"):
            pass
    release.set()
    await task


async def test_global_slot_limit_bounds_parallel_turns(monkeypatch):
    monkeypatch.setattr(concurrency, "_turn_slots", asyncio.Semaphore(1))
    active = 0
    peak = 0

    async def turn(key):
        nonlocal active, peak
        async with concurrency.assistant_turn(key):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(turn(f"u{i}") for i in range(4)))
    assert peak == 1
