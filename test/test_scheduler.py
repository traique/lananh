"""Tests for reminder delivery ordering and retry leases."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scheduler  # noqa: E402
from core import database as db  # noqa: E402
from core import idempotency  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_notify_callback():
    yield
    scheduler._notify_callback = None


@pytest.mark.asyncio
async def test_notify_tra_true_khi_gui_thanh_cong():
    async def fake_callback(uid, text):
        return None

    scheduler.set_notify_callback(fake_callback)
    assert await scheduler._notify(1, "xin chao") is True


@pytest.mark.asyncio
async def test_notify_tra_false_khi_gui_loi():
    async def fake_callback(uid, text):
        raise RuntimeError("mô phỏng lỗi Telegram")

    scheduler.set_notify_callback(fake_callback)
    assert await scheduler._notify(1, "xin chao") is False


@pytest.mark.asyncio
async def test_notify_tra_false_khi_chua_dang_ky_callback():
    assert await scheduler._notify(1, "xin chao") is False


@pytest.mark.asyncio
async def test_reminder_gui_that_bai_duoc_release_de_thu_lai(monkeypatch):
    mark_calls = []
    release_calls = []

    async def fake_mark_reminder_sent(reminder_id):
        mark_calls.append(reminder_id)

    async def fake_release(reminder_id):
        release_calls.append(reminder_id)

    async def failing_callback(uid, text):
        raise RuntimeError("Telegram tạm thời lỗi")

    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark_reminder_sent)
    monkeypatch.setattr(idempotency, "release_reminder_claim", fake_release)
    scheduler.set_notify_callback(failing_callback)

    await scheduler._process_due_reminders([(101, 1, "uống thuốc")])

    assert mark_calls == []
    assert release_calls == [101]


@pytest.mark.asyncio
async def test_reminder_gui_thanh_cong_moi_mark_sent(monkeypatch):
    mark_calls = []

    async def fake_mark_reminder_sent(reminder_id):
        mark_calls.append(reminder_id)

    async def ok_callback(uid, text):
        return None

    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark_reminder_sent)
    scheduler.set_notify_callback(ok_callback)

    await scheduler._process_due_reminders([(102, 1, "họp lúc 3h")])

    assert mark_calls == [102]


@pytest.mark.asyncio
async def test_reminder_nhieu_reminder_doc_lap_nhau(monkeypatch):
    mark_calls = []
    release_calls = []

    async def fake_mark_reminder_sent(reminder_id):
        mark_calls.append(reminder_id)

    async def fake_release(reminder_id):
        release_calls.append(reminder_id)

    async def flaky_callback(uid, text):
        if "loi" in text:
            raise RuntimeError("mô phỏng lỗi")

    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark_reminder_sent)
    monkeypatch.setattr(idempotency, "release_reminder_claim", fake_release)
    scheduler.set_notify_callback(flaky_callback)

    await scheduler._process_due_reminders(
        [
            (1, 1, "binh thuong 1"),
            (2, 1, "loi"),
            (3, 1, "binh thuong 2"),
        ]
    )

    assert mark_calls == [1, 3]
    assert release_calls == [2]
