"""bot_app._post_init: đóng băng heap khởi động (core.memtrim.freeze_startup_heap) ở CUỐI,
sau khi mọi vòng nền đã bật; bản memtrim chưa có hàm này thì bỏ qua."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot_app
from core import memtrim


@pytest.fixture
def startup(monkeypatch):
    """Thay mọi bước khởi động bằng hàm ghi lại tên bước (không DB, không mạng)."""
    steps: list[str] = []

    def sync(name):
        return lambda *args, **kwargs: steps.append(name)

    def async_(name):
        async def step(*args, **kwargs):
            steps.append(name)

        return step

    for target, name in (
        (bot_app.db, "init_db"),
        (bot_app.idempotency, "ensure_schema"),
        (bot_app.idempotency, "cleanup_expired"),
        (bot_app.portfolio, "ensure_schema"),
        (bot_app.zalo_users, "ensure_schema"),
        (bot_app.orchestrator, "init_provider_state"),
    ):
        monkeypatch.setattr(target, name, async_(f"{target.__name__}.{name}"))
    for target, name in (
        (bot_app.idempotency, "start_cleanup_task"),
        (bot_app.orchestrator, "start_background_tasks"),
        (bot_app.notify_hub, "start"),
        (bot_app.monitor_service, "start"),
        (bot_app.scheduler, "start"),
        (bot_app.portfolio_alerts, "start"),
        (bot_app.portfolio_events, "start"),
        (bot_app.price_alerts, "start"),
        (bot_app.morning_brief, "start"),
        (bot_app.memory_service, "start"),
        (bot_app.proactive_service, "start"),
        (bot_app.background_jobs, "start"),
    ):
        monkeypatch.setattr(target, name, sync(f"{target.__name__}.{name}"))
    app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=async_("set_my_commands")))
    return app, steps


@pytest.mark.asyncio
async def test_post_init_dong_bang_heap_o_cuoi(startup, monkeypatch):
    app, steps = startup
    monkeypatch.setattr(
        memtrim, "freeze_startup_heap", lambda: steps.append("freeze"), raising=False
    )
    await bot_app._post_init(app)
    assert steps[-1] == "freeze" and steps.count("freeze") == 1
    assert steps[-2] == "services.background_jobs.start"


@pytest.mark.asyncio
async def test_post_init_chay_duoc_khi_memtrim_chua_co_ham_dong_bang(startup, monkeypatch):
    app, steps = startup
    monkeypatch.delattr(memtrim, "freeze_startup_heap", raising=False)
    await bot_app._post_init(app)
    assert steps[-1] == "services.background_jobs.start" and "freeze" not in steps
