import asyncio

import pytest

from ai.timeouts import with_timeout


@pytest.mark.asyncio
async def test_with_timeout_returns_result():
    async def operation():
        await asyncio.sleep(0)
        return "ok"

    assert await with_timeout(operation(), 1.0, "test operation") == "ok"


@pytest.mark.asyncio
async def test_with_timeout_raises_named_timeout():
    async def operation():
        await asyncio.sleep(0.05)

    with pytest.raises(TimeoutError, match=r"test operation timed out after 0.01s"):
        await with_timeout(operation(), 0.01, "test operation")
