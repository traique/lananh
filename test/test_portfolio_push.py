"""Gửi thông báo danh mục chủ động: chủ bot qua Telegram, thành viên Zalo qua
outbox có trần/ngày, mọi lượt gửi được ghi nhật ký chống trùng."""

import pytest

from services import portfolio_push as push


class _ZaloUser:
    def __init__(self, internal_user_id, external_id, active=True):
        self.internal_user_id = internal_user_id
        self.external_id = external_id
        self.is_active = active


@pytest.fixture
def env(monkeypatch):
    from channels import zalo_repository, zalo_users
    from core import config

    state = {"sent": [], "outbox": [], "logged": [], "count": 0}
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)

    async def notify(uid, text):
        state["sent"].append((uid, text))

    async def log(user_id, kind, ref_key, sent_on):
        state["logged"].append((user_id, kind, ref_key, sent_on))

    async def count(user_id, sent_on):
        return state["count"]

    async def users():
        return [_ZaloUser(-5, "z5"), _ZaloUser(-6, "z6", active=False)]

    async def account():
        return "acc"

    async def enqueue(account_id, recipient, text):
        state["outbox"].append((account_id, recipient, text))

    monkeypatch.setattr(push.db, "portfolio_log_alert", log)
    monkeypatch.setattr(push.db, "portfolio_count_alerts_on", count)
    monkeypatch.setattr(zalo_users, "list_users", users)
    monkeypatch.setattr(zalo_repository, "resolve_active_account_id", account)
    monkeypatch.setattr(zalo_repository, "enqueue_outbox", enqueue)
    push.set_notify_callback(notify)
    yield state
    push.set_notify_callback(None)


@pytest.mark.asyncio
async def test_chu_bot_nhan_qua_telegram_va_ghi_nhat_ky(env):
    assert await push.deliver(42, "🔔", kind="price", ref_key="1", today="2026-10-26")
    assert env["sent"] == [(42, "🔔")]
    assert env["logged"] == [(42, "price", "1", "2026-10-26")]
    assert not await push.deliver(7, "lạ", kind="price", ref_key="2", today="2026-10-26")


@pytest.mark.asyncio
async def test_thanh_vien_zalo_qua_outbox_co_tran_moi_ngay(env, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_PUSH_ZALO_DAILY_CAP", "2")
    assert await push.deliver(-5, "🔔", kind="price", ref_key="1", today="2026-10-26")
    assert env["outbox"] == [("acc", "z5", "🔔")]
    env["count"] = 2
    assert not await push.deliver(-5, "🔔", kind="price", ref_key="2", today="2026-10-26")
    env["count"] = 0
    assert not await push.deliver(
        -6, "🔔", kind="price", ref_key="3", today="2026-10-26"
    )  # bị khoá
    monkeypatch.setenv("PORTFOLIO_PUSH_ZALO_DAILY_CAP", "0")
    assert not push.can_deliver(-5)
    assert not await push.deliver(-5, "🔔", kind="price", ref_key="4", today="2026-10-26")
    assert len(env["outbox"]) == 1


@pytest.mark.asyncio
async def test_gui_loi_khong_ghi_nhat_ky(env):
    async def boom(uid, text):
        raise RuntimeError("Telegram lỗi")

    push.set_notify_callback(boom)
    assert not await push.deliver(42, "🔔", kind="price", ref_key="1", today="2026-10-26")
    assert env["logged"] == []
