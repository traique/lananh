"""Webhook inbox: giới hạn số lần thử -> DEAD (báo chủ bot 1 lần), gia hạn lease
chịu được lỗi DB tạm thời, handler biết lần thử hiện tại."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update

from core import webhook_inbox
from handlers import commands


def _row(attempts=1):
    return {
        "channel": "telegram",
        "event_id": "42",
        "payload": '{"update_id": 42}',
        "lease_token": "tok",
        "attempts": attempts,
    }


class _Pool:
    def __init__(self, results=None):
        self.queries = []
        self.results = list(results or [])

    async def execute(self, query, *args):
        self.queries.append((query, args))
        return self.results.pop(0) if self.results else "UPDATE 1"

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return self.results.pop(0) if self.results else []


@pytest.fixture
def pool(monkeypatch):
    fake = _Pool()

    async def get_pool():
        return fake

    monkeypatch.setattr(webhook_inbox.db, "get_pool", get_pool)
    return fake


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(webhook_inbox.alerts, "send", lambda text: sent.append(text) or True)
    return sent


@pytest.mark.asyncio
async def test_processor_sees_attempt_number():
    seen = []

    async def processor(channel, payload):
        seen.append((webhook_inbox.current_attempt(), webhook_inbox.is_first_attempt()))

    finish = AsyncMock()
    original = webhook_inbox.finish
    webhook_inbox.finish = finish
    try:
        await webhook_inbox.deliver(_row(1), processor)
        await webhook_inbox.deliver(_row(3), processor)
    finally:
        webhook_inbox.finish = original
    assert seen == [(1, True), (3, False)]
    assert webhook_inbox.current_attempt() is None and webhook_inbox.is_first_attempt()


@pytest.mark.asyncio
async def test_retry_before_limit_reschedules_with_backoff(pool, alerts, monkeypatch):
    monkeypatch.setattr(webhook_inbox.config, "WEBHOOK_MAX_ATTEMPTS", 3)
    await webhook_inbox.retry(_row(2), RuntimeError("x"))
    query, args = pool.queries[-1]
    assert "status = 'PENDING'" in query and args[4] == "RuntimeError"
    assert not alerts


@pytest.mark.asyncio
async def test_last_attempt_goes_dead_and_alerts_owner_exactly_once(pool, alerts, monkeypatch):
    monkeypatch.setattr(webhook_inbox.config, "WEBHOOK_MAX_ATTEMPTS", 3)
    pool.results = ["UPDATE 1", "UPDATE 0"]
    await webhook_inbox.retry(_row(3), ValueError("boom"))
    # Lượt trùng (đã mất lease / đã DEAD) không báo lại.
    await webhook_inbox.retry(_row(3), ValueError("boom"))
    assert all("status = 'DEAD'" in q for q, _ in pool.queries)
    assert len(alerts) == 1 and "telegram/42" in alerts[0] and "ValueError" in alerts[0]


@pytest.mark.asyncio
async def test_claim_never_picks_exhausted_or_dead_rows(pool, monkeypatch):
    monkeypatch.setattr(webhook_inbox.config, "WEBHOOK_MAX_ATTEMPTS", 5)
    pool.fetchrow = AsyncMock(return_value=None)

    async def fetchrow(query, *args):
        pool.queries.append((query, args))
        return None

    pool.fetchrow = fetchrow
    await webhook_inbox.claim()
    query, args = pool.queries[-1]
    assert "status IN ('PENDING', 'PROCESSING')" in query and "attempts < $3" in query
    assert args[2] == 5


@pytest.mark.asyncio
async def test_reap_marks_crashed_exhausted_events_dead_and_alerts(pool, alerts):
    pool.results = [[{"channel": "zoom", "event_id": "e1", "attempts": 8, "last_error": None}]]
    assert await webhook_inbox.reap_exhausted() == 1
    assert len(alerts) == 1 and "zoom/e1" in alerts[0]


@pytest.mark.asyncio
async def test_lease_renewal_survives_transient_db_error(monkeypatch):
    monkeypatch.setattr(webhook_inbox, "_RENEW_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(webhook_inbox, "_RENEW_RETRY_SEC", 0.01)
    outcomes = iter([OSError("db blip"), True, True])
    calls = []

    async def extend(row):
        calls.append(1)
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        if len(calls) == 3:
            owner.cancel()  # kết thúc test sau 3 lần gia hạn
        return value

    owner = asyncio.create_task(asyncio.sleep(10))
    monkeypatch.setattr(webhook_inbox, "_extend_lease", extend)
    renew = asyncio.create_task(webhook_inbox._renew(_row(), owner))
    with pytest.raises(asyncio.CancelledError):
        await owner
    renew.cancel()
    await asyncio.gather(renew, return_exceptions=True)
    assert len(calls) == 3  # lỗi tạm lần 1 KHÔNG huỷ lượt xử lý


@pytest.mark.asyncio
async def test_lease_renewal_cancels_when_ownership_lost(monkeypatch):
    monkeypatch.setattr(webhook_inbox, "_RENEW_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(webhook_inbox, "_extend_lease", AsyncMock(return_value=False))
    owner = asyncio.create_task(asyncio.sleep(10))
    await webhook_inbox._renew(_row(), owner)
    with pytest.raises(asyncio.CancelledError):
        await owner


@pytest.mark.asyncio
async def test_lease_renewal_cancels_when_errors_last_until_lease_expiry(monkeypatch):
    from datetime import timedelta

    monkeypatch.setattr(webhook_inbox, "_LEASE", timedelta(seconds=0.2))
    monkeypatch.setattr(webhook_inbox, "_LEASE_SAFETY_SEC", 0.05)
    monkeypatch.setattr(webhook_inbox, "_RENEW_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(webhook_inbox, "_RENEW_RETRY_SEC", 0.02)
    monkeypatch.setattr(webhook_inbox, "_extend_lease", AsyncMock(side_effect=OSError("down")))
    owner = asyncio.create_task(asyncio.sleep(10))
    await asyncio.wait_for(webhook_inbox._renew(_row(), owner), 2)
    with pytest.raises(asyncio.CancelledError):
        await owner


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt,replies", [(None, 1), (1, 1), (2, 0), (8, 0)])
async def test_error_reply_is_sent_only_on_first_attempt(monkeypatch, attempt, replies):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = Update.__new__(Update)
    monkeypatch.setattr(Update, "effective_message", property(lambda self: message))
    token = webhook_inbox._current_attempt.set(attempt)
    try:
        await commands.error_handler(update, SimpleNamespace(error=RuntimeError("x")))
    finally:
        webhook_inbox._current_attempt.reset(token)
    assert message.reply_text.await_count == replies


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TypeError("x"), KeyError("k"), AttributeError("a")])
async def test_loi_lap_trinh_khong_thu_lai_ket_thuc_va_bao_1_lan(pool, alerts, error):
    await webhook_inbox.retry(_row(1), error)
    query, args = pool.queries[-1]
    assert "status = 'DONE'" in query and "payload = NULL" in query
    assert args[3] == type(error).__name__
    assert len(alerts) == 1 and type(error).__name__ in alerts[0]


@pytest.mark.asyncio
async def test_turn_timeout_va_telegram_bad_request_khong_thu_lai(pool, alerts):
    from telegram.error import BadRequest, Forbidden, TimedOut

    from ai.timeouts import TurnTimeoutError

    assert webhook_inbox.is_non_retriable(TurnTimeoutError("hết giờ"))
    assert webhook_inbox.is_non_retriable(BadRequest("Message is too long"))
    assert webhook_inbox.is_non_retriable(Forbidden("bot was blocked"))
    # Lỗi tạm thời vẫn được thử lại.
    assert not webhook_inbox.is_non_retriable(TimedOut())
    assert not webhook_inbox.is_non_retriable(RuntimeError("Telegram application is not ready"))
    assert not webhook_inbox.is_non_retriable(ValueError("Expecting value"))
    assert not webhook_inbox.is_non_retriable(ConnectionError("db"))


@pytest.mark.asyncio
async def test_loi_tam_thoi_van_hen_thu_lai(pool, alerts, monkeypatch):
    monkeypatch.setattr(webhook_inbox.config, "WEBHOOK_MAX_ATTEMPTS", 3)
    await webhook_inbox.retry(_row(1), RuntimeError("Telegram application is not ready"))
    query, _args = pool.queries[-1]
    assert "status = 'PENDING'" in query
    assert alerts == []
