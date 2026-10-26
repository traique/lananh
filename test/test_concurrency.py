import asyncio

import pytest

from services import concurrency


@pytest.fixture(autouse=True)
def fresh_slots(monkeypatch):
    """Semaphore mới cho mỗi test (semaphore asyncio gắn với event loop của test
    đầu tiên phải chờ nó). Mặc định như production: MAX=2, dành riêng 1 chỗ."""
    monkeypatch.setattr(concurrency, "_turn_slots", asyncio.Semaphore(2))
    monkeypatch.setattr(concurrency, "_member_slots", asyncio.Semaphore(1))


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


async def test_different_users_run_in_parallel(monkeypatch):
    # Không dành riêng chỗ cho chủ bot (OWNER_RESERVED_SLOTS=0): 2 thành viên song song.
    monkeypatch.setattr(concurrency, "_member_slots", None)
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


async def test_chu_bot_khong_bao_gio_bi_hai_thanh_vien_chan():
    """MAX=2, dành riêng 1: 2 thành viên đang chạy/đang chờ thì lượt chủ bot vẫn vào ngay."""
    release = asyncio.Event()
    first_started = asyncio.Event()
    order = []

    async def member(key):
        async with concurrency.assistant_turn(key):
            order.append(key)
            first_started.set()
            await release.wait()

    t1 = asyncio.create_task(member("zalo:-1"))
    await first_started.wait()
    t2 = asyncio.create_task(member("zalo:-2"))
    await asyncio.sleep(0.01)
    assert order == ["zalo:-1"]  # thành viên thứ 2 chờ chỗ thành viên
    async with asyncio.timeout(1):
        async with concurrency.assistant_turn(concurrency.OWNER_TURN_KEY):
            order.append("owner")
    release.set()
    await asyncio.gather(t1, t2)
    assert order == ["zalo:-1", "owner", "zalo:-2"]


async def test_tong_luot_khong_vuot_max_khi_co_chu_bot(monkeypatch):
    active = peak = 0

    async def turn(key):
        nonlocal active, peak
        async with concurrency.assistant_turn(key):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(turn(f"zalo:-{i}") for i in range(4)), turn(concurrency.OWNER_TURN_KEY))
    assert peak == 2


def test_max_1_khong_danh_rieng_de_khoi_chan_chet(monkeypatch):
    monkeypatch.setenv("OWNER_RESERVED_SLOTS", "1")
    assert concurrency._owner_reserved_slots(1) == 0
    assert concurrency._owner_reserved_slots(2) == 1
    assert concurrency._owner_reserved_slots(4) == 1
    monkeypatch.setenv("OWNER_RESERVED_SLOTS", "9")
    assert concurrency._owner_reserved_slots(3) == 2
    monkeypatch.setenv("OWNER_RESERVED_SLOTS", "0")
    assert concurrency._owner_reserved_slots(2) == 0
    monkeypatch.setenv("OWNER_RESERVED_SLOTS", "abc")
    assert concurrency._owner_reserved_slots(2) == 1


async def test_max_1_thanh_vien_van_chay_duoc(monkeypatch):
    monkeypatch.setattr(concurrency, "_turn_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(concurrency, "_member_slots", None)
    async with asyncio.timeout(1):
        async with concurrency.assistant_turn("zalo:-1"):
            pass
        async with concurrency.assistant_turn(concurrency.OWNER_TURN_KEY):
            pass


async def test_huy_van_huy_duoc_khi_thanh_vien_khac_dang_cho_cho():
    """/huy (cancel_turn) chỉ huỷ task con của chính người đó; thành viên đang chờ chỗ
    không bị ảnh hưởng và vào lượt ngay khi chỗ trống."""
    started = asyncio.Event()
    entered = []

    async def long_member():
        async with concurrency.assistant_turn("zalo:-1"):

            async def work():
                started.set()
                await asyncio.sleep(10)

            with pytest.raises(concurrency.TurnCancelled):
                await concurrency.run_cancellable("zalo:-1", work)

    async def waiting_member():
        async with concurrency.assistant_turn("zalo:-2"):
            entered.append("zalo:-2")

    t1 = asyncio.create_task(long_member())
    await started.wait()
    t2 = asyncio.create_task(waiting_member())
    await asyncio.sleep(0.01)
    assert entered == [] and not concurrency.cancel_turn("zalo:-2")
    assert concurrency.cancel_turn("zalo:-1")
    async with asyncio.timeout(1):
        await asyncio.gather(t1, t2)
    assert entered == ["zalo:-2"]
