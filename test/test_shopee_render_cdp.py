import asyncio
import logging
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services import shopee_affiliate_browser as shopee


class PageScope:
    def __init__(self, url, text="", frames=()):
        self.url = url
        self.frames = list(frames)
        self.main_frame = None
        self.body = SimpleNamespace(inner_text=AsyncMock(return_value=text))

    def locator(self, selector):
        assert selector == "body"
        return self.body


@pytest.mark.asyncio
@pytest.mark.parametrize("in_frame", [False, True])
async def test_traffic_error_is_verification_even_when_body_is_empty(monkeypatch, in_frame):
    blocked = PageScope("https://shopee.vn/verify/traffic/error?is_logged_in=true")
    page = (
        PageScope("https://affiliate.shopee.vn/offer/custom_link", frames=[blocked])
        if in_frame
        else blocked
    )
    probe = AsyncMock(side_effect=AssertionError("must stop before probing the field"))
    monkeypatch.setattr(shopee, "_visible_input", probe)

    with pytest.raises(shopee.ShopeeVerificationRequired, match="tăng timeout"):
        await shopee._wait_for_custom_link_field(page)

    probe.assert_not_awaited()
    blocked.body.inner_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_verification_path_on_unrelated_host_does_not_block_custom_link():
    page = PageScope(
        "https://affiliate.shopee.vn/offer/custom_link",
        frames=[PageScope("https://other.example/verify/traffic/error")],
    )
    await shopee._assert_logged_in(page)


@pytest.mark.asyncio
async def test_captcha_text_in_child_frame_is_verification():
    page = PageScope(
        "https://affiliate.shopee.vn/offer/custom_link",
        frames=[PageScope("https://shopee.vn/", "Xác minh bảo mật")],
    )
    with pytest.raises(shopee.ShopeeVerificationRequired):
        await shopee._assert_logged_in(page)


@pytest.mark.asyncio
async def test_detached_frame_does_not_prevent_reading_other_frames():
    detached = PageScope("https://shopee.vn/")
    detached.body.inner_text.side_effect = RuntimeError("frame detached")
    page = PageScope(
        "https://affiliate.shopee.vn/offer/custom_link",
        frames=[detached, PageScope("https://shopee.vn/", "Đăng nhập vào Shopee")],
    )
    with pytest.raises(shopee.ShopeeSessionExpired):
        await shopee._assert_logged_in(page)


@pytest.fixture
def browser_runtime(monkeypatch):
    events = []
    driver_running = False

    async def close_context():
        assert driver_running, "context must close while the driver is alive"
        events.append("context.close")

    async def close_browser():
        assert driver_running, "CDP must disconnect while the driver is alive"
        events.append("browser.close")

    class Driver:
        async def __aenter__(self):
            nonlocal driver_running
            driver_running = True
            events.append("driver.start")
            return playwright

        async def __aexit__(self, *args):
            nonlocal driver_running
            events.append("driver.stop")
            driver_running = False

    page = SimpleNamespace(route=AsyncMock(), close=AsyncMock())
    refreshed_state = {"cookies": [{"name": "refreshed"}], "origins": []}
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        close=AsyncMock(side_effect=close_context),
        set_default_timeout=Mock(),
        storage_state=AsyncMock(return_value=refreshed_state),
    )
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context),
        close=AsyncMock(side_effect=close_browser),
    )
    chromium = SimpleNamespace(
        connect_over_cdp=AsyncMock(return_value=browser),
        launch=AsyncMock(return_value=browser),
    )
    playwright = SimpleNamespace(chromium=chromium, webkit=chromium)
    api = ModuleType("playwright.async_api")
    api.async_playwright = Driver
    api.TimeoutError = type("PlaywrightTimeoutError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    saved_state = {"cookies": [{"name": "saved"}], "origins": []}
    load = AsyncMock(return_value=saved_state)
    save = AsyncMock()
    convert = AsyncMock(return_value="https://s.shopee.vn/affiliate")
    monkeypatch.setattr(shopee.shopee_affiliate_session, "load", load)
    monkeypatch.setattr(shopee.shopee_affiliate_session, "save", save)
    monkeypatch.setattr(shopee, "_convert_on_page", convert)
    monkeypatch.setattr(shopee, "_verify_affiliate_destination", AsyncMock())
    monkeypatch.setattr(shopee, "_log_memory", lambda *_: None)
    monkeypatch.setattr(
        shopee.config, "SHOPEE_BROWSER_CDP_URL", "https://worker.onrender.com/cdp/secret-token"
    )
    monkeypatch.setattr(shopee.config, "SHOPEE_BROWSER_ENGINE", "chromium")
    monkeypatch.setattr(shopee.config, "SHOPEE_BROWSER_LAUNCH_BUDGET_SEC", 20)
    item = shopee.ResolvedShopeeUrl(
        "https://shopee.vn/product/1/2", "https://shopee.vn/product/1/2", "item:1:2"
    )
    return SimpleNamespace(
        events=events,
        browser=browser,
        context=context,
        chromium=chromium,
        load=load,
        save=save,
        convert=convert,
        saved_state=saved_state,
        refreshed_state=refreshed_state,
        item=item,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [True, False])
async def test_session_is_restored_and_resources_close_before_driver(
    browser_runtime, monkeypatch, remote
):
    runtime = browser_runtime
    if not remote:
        monkeypatch.setattr(shopee.config, "SHOPEE_BROWSER_CDP_URL", "")

    converted = await shopee._launch_and_convert([runtime.item])

    assert converted == {"item:1:2": "https://s.shopee.vn/affiliate"}
    assert runtime.browser.new_context.call_args.kwargs["storage_state"] == runtime.saved_state
    runtime.save.assert_awaited_once_with(runtime.refreshed_state)
    assert runtime.events == ["driver.start", "context.close", "browser.close", "driver.stop"]
    if remote:
        assert runtime.chromium.connect_over_cdp.call_args.kwargs["timeout"] == 120_000
        runtime.chromium.launch.assert_not_awaited()
    else:
        runtime.chromium.connect_over_cdp.assert_not_awaited()
        runtime.chromium.launch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_conversion_closes_cdp_and_does_not_save_state(browser_runtime, cancelled):
    runtime = browser_runtime
    failure = (
        asyncio.CancelledError() if cancelled else shopee.ShopeeVerificationRequired("blocked")
    )
    runtime.convert.side_effect = failure

    with pytest.raises(type(failure)):
        await shopee._launch_and_convert([runtime.item])

    runtime.save.assert_not_awaited()
    assert runtime.events == ["driver.start", "context.close", "browser.close", "driver.stop"]


@pytest.mark.asyncio
async def test_context_creation_failure_still_disconnects_browser(browser_runtime):
    runtime = browser_runtime
    runtime.browser.new_context.side_effect = RuntimeError("context creation failed")
    with pytest.raises(shopee.ShopeeAffiliateError, match="context creation failed"):
        await shopee._launch_and_convert([runtime.item])
    runtime.save.assert_not_awaited()
    assert runtime.events == ["driver.start", "browser.close", "driver.stop"]


@pytest.mark.asyncio
async def test_verification_failure_does_not_create_affiliate_cache(browser_runtime, monkeypatch):
    runtime = browser_runtime
    runtime.convert.side_effect = shopee.ShopeeVerificationRequired("blocked")
    monkeypatch.setattr(
        shopee.facebook_repository, "get_affiliate_links", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        shopee.facebook_repository, "get_affiliate_links_by_canonical", AsyncMock(return_value={})
    )
    save_link = AsyncMock()
    monkeypatch.setattr(shopee.facebook_repository, "set_affiliate_link", save_link)
    monkeypatch.setattr(shopee, "resolve_shopee_url", AsyncMock(return_value=runtime.item))

    with pytest.raises(shopee.ShopeeVerificationRequired):
        await shopee.convert_urls("account", [runtime.item.source_url])

    save_link.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdp_token_is_not_exposed_in_success_or_failure_logs(browser_runtime, caplog):
    runtime = browser_runtime
    endpoint = shopee.config.SHOPEE_BROWSER_CDP_URL
    with caplog.at_level(logging.DEBUG, logger=shopee.__name__):
        await shopee._launch_and_convert([runtime.item])
        runtime.chromium.connect_over_cdp.side_effect = RuntimeError(
            f"cannot connect to {endpoint}"
        )
        with pytest.raises(shopee.ShopeeAffiliateError) as captured:
            await shopee._launch_and_convert([runtime.item])
    assert "secret-token" not in caplog.text
    assert "secret-token" not in str(captured.value)
    assert captured.value.__suppress_context__ is True


def test_batch_budget_includes_cdp_cold_start(browser_runtime, monkeypatch):
    remote_budget = shopee._browser_batch_timeout_sec(1)
    monkeypatch.setattr(shopee.config, "SHOPEE_BROWSER_CDP_URL", "")
    local_budget = shopee._browser_batch_timeout_sec(1)
    assert remote_budget == local_budget + 100
    monkeypatch.setattr(shopee.config, "SHOPEE_BROWSER_CDP_URL", "https://worker/cdp/token")
    monkeypatch.setattr(shopee.config, "SHOPEE_BROWSER_LAUNCH_BUDGET_SEC", 180)
    assert shopee._browser_launch_budget_sec() == 180
