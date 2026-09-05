"""Test telemetry fail-open: DB lỗi KHÔNG được làm chết tính năng chính."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import telemetry as telemetry_module  # noqa: E402
from services.telemetry import telemetry  # noqa: E402


@pytest.mark.asyncio
async def test_start_tra_none_khi_db_loi(monkeypatch):
    async def broken_save_prompt(*args, **kwargs):
        raise ConnectionRefusedError("DB die")

    monkeypatch.setattr(telemetry_module.db, "save_prompt", broken_save_prompt)
    assert await telemetry.start(1, "chat", "hello") is None


@pytest.mark.asyncio
async def test_success_failure_noop_voi_prompt_id_none(monkeypatch):
    calls = []

    async def broken_save_result(*args, **kwargs):
        calls.append("called")
        raise RuntimeError("DB die")

    monkeypatch.setattr(telemetry_module.db, "save_result", broken_save_result)
    # KHÔNG được raise (trước đây prompt_id=None sẽ trôi xuống save_result).
    await telemetry.success(None, "chat", "reply")
    await telemetry.failure(None, "chat", RuntimeError("x"))
    assert calls == []


@pytest.mark.asyncio
async def test_success_loi_db_khong_raise(monkeypatch):
    async def broken_save_result(*args, **kwargs):
        raise RuntimeError("DB die")

    monkeypatch.setattr(telemetry_module.db, "save_result", broken_save_result)
    await telemetry.success(123, "chat", "reply")
