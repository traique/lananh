"""Regressions for delivery loss, duplicate execution, SSRF, and RAM boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import bot_app
import scheduler
import web
from channels import router, facebook_commands
from channels.contracts import ZaloMessageRequest
from core import database as db, idempotency, webhook_inbox
from handlers import commands
from services import (
    concurrency,
    shopee_affiliate_browser as shopee,
    facebook_page_service as facebook,
)
from services.channel_chat_service import ChannelResult
from services.reminder_delivery import current_target, NotificationTarget, notification_target
from services.request_limits import RequestLimitsMiddleware, MIB
from services.telegram_processing import record_error
from stock import analysis, portfolio


@pytest.mark.asyncio
async def test_real_bot_notify_failure_does_not_mark_reminder_sent(monkeypatch):
    monkeypatch.setattr(bot_app.config, "TELEGRAM_TOKEN", "123:fake-token")
    monkeypatch.setattr(
        bot_app.tg_format, "send_rich", AsyncMock(side_effect=RuntimeError("offline"))
    )
    app = bot_app.build_application()
    mark, release = AsyncMock(), AsyncMock()
    monkeypatch.setattr(db, "mark_reminder_sent", mark)
    monkeypatch.setattr(idempotency, "release_reminder_claim", release)
    try:
        await scheduler._process_due_reminders([(1, 2, "test")])
        mark.assert_not_awaited()
        release.assert_awaited_once_with(1)
    finally:
        scheduler.set_notify_callback(None)
        for request in app.bot._request:
            await request.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["zalo", "zoom"])
async def test_reminder_delivery_uses_authenticated_channel_recipient(monkeypatch, channel):
    from channels import zalo_repository, zoom

    target = NotificationTarget(channel, "destination", "account", "user-jid")
    with notification_target(target):
        assert current_target(-7) == target
    with pytest.raises(ValueError):
        current_target(-7)
    send = AsyncMock()
    mark, telegram = AsyncMock(), AsyncMock()
    monkeypatch.setattr(zalo_repository, "enqueue_reminder", send)
    monkeypatch.setattr(zoom, "send_message", send)
    monkeypatch.setattr(db, "mark_reminder_sent", mark)
    monkeypatch.setattr(scheduler, "_notify", telegram)
    await scheduler._process_due_reminders(
        [idempotency.DueReminder(7, -7, "test", channel, "destination", "account", "user-jid")]
    )
    telegram.assert_not_awaited()
    if channel == "zalo":
        send.assert_awaited_once_with(7, "account", "destination", "⏰ Nhắc việc: test")
        mark.assert_not_awaited()
    else:
        send.assert_awaited_once_with(
            "destination", "⏰ Nhắc việc: test", user_jid="user-jid", account_id="account"
        )
        mark.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_legacy_zalo_reminder_resolves_active_account(monkeypatch):
    from channels import zalo_repository, zalo_session

    send = AsyncMock()
    monkeypatch.setattr(zalo_repository, "enqueue_reminder", send)
    monkeypatch.setattr(zalo_session, "load_account_id", AsyncMock(return_value="actual-account"))
    await scheduler._process_due_reminders(
        [idempotency.DueReminder(1, -1, "test", "zalo", "z-user")]
    )
    send.assert_awaited_once_with(1, "actual-account", "z-user", "⏰ Nhắc việc: test")


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["telegram", "zoom"])
@pytest.mark.parametrize("db_failure", [False, True])
async def test_webhook_ack_waits_for_durable_insert(monkeypatch, channel, db_failure):
    enqueue = AsyncMock(side_effect=RuntimeError("db down") if db_failure else None)
    monkeypatch.setattr(webhook_inbox, "enqueue", enqueue)
    monkeypatch.setattr(web, "application", SimpleNamespace(bot=None))
    monkeypatch.setattr(web.config, "WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(web.config, "ZOOM_ENABLED", True)
    monkeypatch.setattr(web.config, "ZOOM_SECRET_TOKEN", "")
    monkeypatch.setattr(web.config, "ZOOM_VERIFICATION_TOKEN", "secret")
    payload = (
        {"update_id": 123}
        if channel == "telegram"
        else {"payload": {"messageId": "msg-1", "userJid": "u", "cmd": "hello"}}
    )
    req = SimpleNamespace(
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret", "authorization": "secret"},
        json=AsyncMock(return_value=payload),
        body=AsyncMock(return_value=b"{}"),
    )
    result = await (web.telegram_webhook(req) if channel == "telegram" else web.zoom_webhook(req))
    assert result.status_code == (503 if db_failure else 200)
    assert enqueue.await_args.args == (
        channel,
        "123" if channel == "telegram" else "msg-1",
        payload,
    )
    assert not web._background_tasks


@pytest.mark.asyncio
async def test_ptb_error_handler_failure_is_visible_to_inbox(monkeypatch):
    error = RuntimeError("send failed")

    async def ptb_swallows(update):
        record_error(error)

    monkeypatch.setattr(web, "application", SimpleNamespace(process_update=ptb_swallows))
    with pytest.raises(RuntimeError, match="send failed"):
        await web._process_update(
            SimpleNamespace(update_id=1, effective_chat=SimpleNamespace(id=123))
        )


@pytest.mark.asyncio
async def test_inbox_keeps_failed_payload_for_retry(monkeypatch):
    row = {
        "channel": "telegram",
        "event_id": "1",
        "payload": '{"update_id":1}',
        "lease_token": "token",
        "attempts": 1,
    }
    finish, retry = AsyncMock(), AsyncMock()
    monkeypatch.setattr(webhook_inbox, "finish", finish)
    monkeypatch.setattr(webhook_inbox, "retry", retry)
    await webhook_inbox.deliver(row, AsyncMock(side_effect=RuntimeError("network")))
    finish.assert_not_awaited()
    assert retry.await_args.args[0] == row
    success = AsyncMock()
    await webhook_inbox.deliver(row, success)
    success.assert_awaited_once_with("telegram", {"update_id": 1})
    finish.assert_awaited_once_with(row)


@pytest.mark.asyncio
async def test_inbox_workers_are_bounded_and_cancel_cleanly(monkeypatch):
    monkeypatch.setattr(webhook_inbox, "claim", AsyncMock(return_value=None))
    webhook_inbox.start(AsyncMock(), workers=2)
    webhook_inbox.start(AsyncMock(), workers=4)
    assert len(webhook_inbox._tasks) == 2
    await webhook_inbox.stop()
    assert not webhook_inbox._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("facebook_command", [False, True])
async def test_overlapping_zalo_retries_execute_once_and_wait_for_cache_save(
    monkeypatch, facebook_command
):
    monkeypatch.setattr(router, "_secret", lambda: "secret")
    monkeypatch.setattr(
        router.zalo_users,
        "resolve",
        AsyncMock(return_value=SimpleNamespace(is_active=True, is_admin=True, internal_user_id=-1)),
    )
    monkeypatch.setattr(router, "maybe_handle_group_command", AsyncMock(return_value=None))
    saved = {}
    entered, allow_save = asyncio.Event(), asyncio.Event()

    async def load(*args):
        return saved.get(args)

    async def save(*args):
        entered.set()
        await allow_save.wait()
        saved[args[:3]] = args[3]

    calls = AsyncMock(return_value=ChannelResult(["done"]))
    monkeypatch.setattr(router.idempotency, "get_zalo_response", load)
    monkeypatch.setattr(router.idempotency, "save_zalo_response", save)
    monkeypatch.setattr(
        router,
        "maybe_handle_facebook_command",
        calls if facebook_command else AsyncMock(return_value=None),
    )
    monkeypatch.setattr(router, "handle_channel_text", calls)
    payload = ZaloMessageRequest(
        account_id="acc",
        sender_id="u",
        conversation_id="u",
        message_id="same",
        text="/fb_check 1" if facebook_command else "hello",
    )
    first = asyncio.create_task(router.receive(payload, "secret"))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(router.receive(payload, "secret"))
    await asyncio.sleep(0)
    assert calls.await_count == 1
    allow_save.set()
    replies = await asyncio.gather(first, second)
    assert [r.messages for r in replies] == [["done"], ["done"]]
    assert calls.await_count == 1
    assert not concurrency._message_locks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect",
    [
        "http://127.0.0.1/private",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://user:pass@example.com/",
    ],
)
async def test_link_checks_never_request_private_redirect(monkeypatch, redirect):
    from services import web_reader

    monkeypatch.setattr(
        web_reader.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )
    requested = []

    def response(req):
        requested.append(str(req.url))
        return httpx.Response(302, headers={"location": redirect})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        assert await commands._check_url(client, "https://example.com/start") is None
    assert requested == ["https://example.com/start"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "holding,closed,expected", [(True, False, True), (False, True, False), (False, False, True)]
)
async def test_structured_holdings_override_old_memory(monkeypatch, holding, closed, expected):
    monkeypatch.setattr(
        portfolio,
        "get_holding",
        AsyncMock(return_value=SimpleNamespace(quantity=100) if holding else None),
    )
    monkeypatch.setattr(portfolio, "was_closed", AsyncMock(return_value=closed))
    monkeypatch.setattr(db, "get_facts", AsyncMock(return_value=[("danh_muc", "VCB")]))
    assert await analysis._is_holding_symbol(1, "VCB") is expected


@pytest.mark.asyncio
async def test_failed_facebook_readback_preserves_created_id_before_verification(monkeypatch):
    events = []
    monkeypatch.setattr(facebook, "_settings", lambda key="default": ("page", "token", "v26.0"))
    monkeypatch.setattr(facebook, "_graph_post", AsyncMock(return_value={"id": "page_post"}))

    async def verify(*args, **kw):
        assert events == ["creating", "page_post"]
        raise httpx.ReadTimeout("readback failed")

    monkeypatch.setattr(facebook, "_verify_new_post", verify)

    async def before():
        events.append("creating")

    async def created(post_id):
        events.append(post_id)

    result = await facebook.publish_page_post("hello", [], before_create=before, on_created=created)
    assert result.post_id == "page_post" and not result.visibility_confirmed


@pytest.mark.asyncio
async def test_timeout_creating_facebook_post_is_uncertain(monkeypatch):
    monkeypatch.setattr(facebook, "_settings", lambda key="default": ("page", "token", "v26.0"))
    monkeypatch.setattr(
        facebook, "_graph_post", AsyncMock(side_effect=httpx.ReadTimeout("response lost"))
    )
    with pytest.raises(facebook.FacebookPublicationUncertain):
        await facebook.publish_page_post("hello", [])


@pytest.mark.asyncio
async def test_unknown_facebook_target_is_never_reposted(monkeypatch):
    repo = facebook_commands.facebook_repository
    monkeypatch.setattr(repo, "ensure_targets", AsyncMock())
    monkeypatch.setattr(
        repo,
        "list_targets",
        AsyncMock(
            return_value=[{"page_key": "default", "status": "UNKNOWN", "facebook_post_id": None}]
        ),
    )
    finalize = AsyncMock(return_value="ERROR")
    monkeypatch.setattr(repo, "finalize_post_status", finalize)
    publish, media = AsyncMock(), AsyncMock()
    monkeypatch.setattr(facebook_commands, "publish_page_post", publish)
    monkeypatch.setattr(repo, "get_media", media)
    result = await facebook_commands._publish_claimed("acc", 1, {}, ["default"], "claim")
    publish.assert_not_awaited()
    media.assert_not_awaited()
    assert "/fb_reconcile" in result.messages[0]
    finalize.assert_awaited_once_with("acc", 1, claim_token="claim")


@pytest.mark.asyncio
async def test_stale_shopee_link_is_ignored(monkeypatch):
    monkeypatch.setattr(
        shopee,
        "_affiliate_candidates",
        AsyncMock(return_value=["https://s.shopee.vn/old", "https://s.shopee.vn/new"]),
    )
    assert (
        await shopee._extract_affiliate_url(None, "source", {"https://s.shopee.vn/old"})
        == "https://s.shopee.vn/new"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,valid",
    [
        ("https://shopee.vn/product/1/2", True),
        ("https://shopee.vn/product/1/3", False),
        ("https://shopee.vn/verify/traffic/error", False),
        ("http://127.0.0.1/", False),
    ],
)
async def test_shopee_affiliate_destination_must_match_before_cache(monkeypatch, target, valid):
    handler = None
    actions = []

    async def route(pattern, callback):
        nonlocal handler
        handler = callback

    async def abort():
        actions.append("abort")

    async def proceed():
        actions.append("continue")

    async def goto(*args, **kw):
        request = SimpleNamespace(url=target, is_navigation_request=lambda: True)
        await handler(SimpleNamespace(request=request, abort=abort, continue_=proceed))
        raise RuntimeError("navigation aborted")

    page = SimpleNamespace(route=route, goto=goto, close=AsyncMock())

    def check_url(url):
        if "127.0.0.1" in url:
            raise shopee.ShopeeAffiliateError("private")
        return url

    monkeypatch.setattr(shopee, "_require_shopee_url", check_url)
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    if valid:
        await shopee._verify_affiliate_destination(context, "https://s.shopee.vn/new", "item:1:2")
        assert actions == ["abort"]  # no product payload downloaded
    else:
        with pytest.raises(shopee.ShopeeAffiliateError, match="chưa lưu"):
            await shopee._verify_affiliate_destination(
                context, "https://s.shopee.vn/new", "item:1:2"
            )
    page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_chunked_body_is_rejected_before_downstream_allocation():
    app = AsyncMock()
    middleware = RequestLimitsMiddleware(app)
    incoming = iter(
        [
            {"type": "http.request", "body": b"a" * (MIB // 2), "more_body": True},
            {"type": "http.request", "body": b"b" * MIB, "more_body": False},
        ]
    )
    receive = AsyncMock(side_effect=lambda: next(incoming))
    sent = []

    async def send(msg):
        sent.append(msg)

    await middleware(
        {"type": "http", "method": "POST", "path": "/webhook", "headers": []}, receive, send
    )
    assert sent[0]["status"] == 413
    app.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_large_media_receives_retryable_backpressure():
    started, finish = asyncio.Event(), asyncio.Event()

    async def app(*args):
        started.set()
        await finish.wait()

    middleware = RequestLimitsMiddleware(app)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/internal/zalo/facebook-group-post",
        "headers": [],
    }
    receive = AsyncMock(return_value={"type": "http.request", "body": b"{}", "more_body": False})
    first = asyncio.create_task(middleware(scope, receive, AsyncMock()))
    await started.wait()
    sent = []

    async def send(msg):
        sent.append(msg)

    await middleware(scope, receive, send)
    assert sent[0]["status"] == 503 and (b"retry-after", b"5") in sent[0]["headers"]
    finish.set()
    await first
    assert not middleware.heavy_active


def test_ambiguous_legacy_caption_never_maps_one_shortlink_to_two_products():
    old = "https://s.shopee.vn/old"
    previous = {"https://shopee.vn/product/1/2": old, "https://shopee.vn/product/3/4": old}
    with pytest.raises(ValueError, match="cache cũ trùng"):
        facebook_commands._replace_caption_links(
            old, {"https://shopee.vn/product/1/2": "https://s.shopee.vn/new"}, previous
        )
    assert (
        facebook_commands._replace_caption_links(
            "edited caption " + old,
            {"https://shopee.vn/product/1/2": "https://s.shopee.vn/new"},
            {"https://shopee.vn/product/1/2": old},
        )
        == "edited caption https://s.shopee.vn/new"
    )


def test_long_lived_caches_are_bounded_and_expired_entries_are_pruned(monkeypatch):
    import time
    from stock import providers, vci_direct, fundamentals

    cache = {str(i): (time.monotonic(), object()) for i in range(300)}
    providers._evict_expired(cache, 90)
    assert len(cache) < 256
    verification = {
        "old": (time.monotonic() - 100, False, 10),
        "new": (time.monotonic(), True, 86400),
    }
    providers._evict_expired(verification, 90)
    assert set(verification) == {"new"}  # three-field verification cache also works
    local = {str(i): (time.monotonic(), None) for i in range(128)}
    vci_direct._cached(local, "VCB", lambda: [1])
    assert len(local) == 128 and local["VCB"][1] == [1]
    monkeypatch.setattr(commands, "_PRICE_CACHE", {})
    for i in range(200):
        commands._set_cached_price(str(i), "quote")
    assert len(commands._PRICE_CACHE) == 128


@pytest.mark.asyncio
async def test_generated_image_stream_has_hard_limit_without_content_length(monkeypatch):
    from ai import agnes_client

    class ImageStream(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False

        async def __aiter__(self):
            yield b"a" * 3
            yield b"b" * 3
            raise AssertionError("must stop before reading more bytes")

        async def aclose(self):
            self.closed = True

    stream = ImageStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream))
    ) as client:
        monkeypatch.setattr(agnes_client, "_get_client", lambda: client)
        with pytest.raises(agnes_client.AgnesError, match="giới hạn"):
            await agnes_client._request_bytes("GET", "https://image.example/image", 5)
    assert stream.closed


@pytest.mark.asyncio
async def test_generated_base64_image_limit_preserves_normal_images(monkeypatch):
    import base64, json
    from ai import agnes_client

    monkeypatch.setattr(agnes_client, "get_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(agnes_client, "_api_key", AsyncMock(return_value="test"))
    monkeypatch.setattr(agnes_client, "_model", AsyncMock(return_value="image-model"))
    monkeypatch.setattr(agnes_client, "_MAX_IMAGE_BYTES", 3)
    monkeypatch.setattr(db, "record_provider_call", AsyncMock())
    response = AsyncMock(
        return_value=json.dumps(
            {"data": [{"b64_json": base64.b64encode(b"123").decode()}]}
        ).encode()
    )
    monkeypatch.setattr(agnes_client, "_request_bytes", response)
    assert (await agnes_client.generate_image("test")).data == b"123"
    response.return_value = json.dumps(
        {"data": [{"b64_json": base64.b64encode(b"12345").decode()}]}
    ).encode()
    with pytest.raises(agnes_client.AgnesError, match="giới hạn"):
        await agnes_client.generate_image("test")


@pytest.mark.asyncio
async def test_remote_only_image_reports_missing_cdp_configuration(monkeypatch):
    monkeypatch.setenv("SHOPEE_LOCAL_BROWSERS_INSTALLED", "false")
    monkeypatch.setattr(shopee.config, "SHOPEE_BROWSER_CDP_URL", "")
    with pytest.raises(shopee.ShopeeAffiliateError, match="SHOPEE_BROWSER_CDP_URL"):
        await shopee._launch_and_convert([])
