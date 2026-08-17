import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import zalo_scheduler


@pytest.mark.asyncio
async def test_controller_id_falls_back_to_database(monkeypatch):
    monkeypatch.delenv("ZALO_CONTROLLER_ID", raising=False)

    async def fake_load_controller():
        return "controller-from-db"

    monkeypatch.setattr(zalo_scheduler.zalo_session, "load_controller", fake_load_controller)

    assert await zalo_scheduler._controller_id() == "controller-from-db"


@pytest.mark.asyncio
async def test_controller_env_overrides_database(monkeypatch):
    monkeypatch.setenv("ZALO_CONTROLLER_ID", "controller-from-env")

    async def should_not_load():
        raise AssertionError("DB không được đọc khi env đã có controller")

    monkeypatch.setattr(zalo_scheduler.zalo_session, "load_controller", should_not_load)

    assert await zalo_scheduler._controller_id() == "controller-from-env"


def test_scheduler_can_start_without_controller_env(monkeypatch):
    monkeypatch.setenv("ZALO_ENABLED", "true")
    monkeypatch.setenv("ZALO_BRIDGE_SECRET", "bridge-secret")
    monkeypatch.delenv("ZALO_CONTROLLER_ID", raising=False)

    assert zalo_scheduler._enabled() is True


@pytest.mark.asyncio
async def test_digest_saves_summary_and_outbox_atomically(monkeypatch):
    monkeypatch.delenv("ZALO_CONTROLLER_ID", raising=False)
    monkeypatch.setenv("ZALO_BOT_ACCOUNT_ID", "zalo-bot")

    async def fake_controller_id():
        return "controller-from-db"

    async def fake_list_groups(account_id):
        assert account_id == "zalo-bot"
        return [("group-1", "nhom-1")]

    async def fake_summary_exists(*args):
        return False

    async def fake_summarize(*args):
        return None, None, "Nội dung tổng kết"

    saved = []

    async def fake_save_and_enqueue(*args):
        saved.append(args)
        return True

    async def fake_cleanup(account_id):
        return None

    class FakeDateTime:
        @classmethod
        def now(cls, tz):
            return datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    monkeypatch.setattr(zalo_scheduler, "datetime", FakeDateTime)
    monkeypatch.setattr(zalo_scheduler, "_controller_id", fake_controller_id)
    monkeypatch.setattr(zalo_scheduler.zalo_repository, "list_groups", fake_list_groups)
    monkeypatch.setattr(zalo_scheduler.zalo_repository, "summary_exists", fake_summary_exists)
    monkeypatch.setattr(zalo_scheduler, "summarize_group", fake_summarize)
    monkeypatch.setattr(
        zalo_scheduler.zalo_repository,
        "save_summary_and_enqueue",
        fake_save_and_enqueue,
    )
    monkeypatch.setattr(
        zalo_scheduler.zalo_repository,
        "cleanup_old_messages",
        fake_cleanup,
    )

    await zalo_scheduler._run_due_digest()

    assert len(saved) == 1
    assert saved[0][0:3] == ("zalo-bot", "group-1", "daily")
    assert saved[0][-2:] == ("Nội dung tổng kết", "controller-from-db")
