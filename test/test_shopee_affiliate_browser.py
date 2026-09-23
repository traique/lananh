import pytest

from services import shopee_affiliate_browser as shopee


def test_canonical_key_extracts_product_ids():
    assert shopee.canonical_key_from_url("https://shopee.vn/product/123/456?x=1") == "item:123:456"
    assert shopee.canonical_key_from_url("https://shopee.vn/ao-dep-i.123.456?sp_atk=x") == "item:123:456"


def test_canonical_key_drops_tracking_query_for_non_product_url():
    assert shopee.canonical_key_from_url("https://shopee.vn/shop/abc?utm_source=x#top") == "https://shopee.vn/shop/abc"


def test_canonical_key_keeps_functional_query_to_avoid_wrong_product_cache():
    one = shopee.canonical_key_from_url("https://shopee.vn/universal-link/product?target=A&utm_source=x")
    two = shopee.canonical_key_from_url("https://shopee.vn/universal-link/product?target=B&utm_source=x")
    assert one != two


@pytest.mark.asyncio
async def test_convert_urls_direct_cache_never_resolves_or_launches_browser(monkeypatch):
    source = "https://s.shopee.vn/source"
    affiliate = "https://s.shopee.vn/affiliate"

    async def fake_direct(account_id, urls):
        return {source: affiliate}

    async def forbidden(*args, **kwargs):
        raise AssertionError("network/browser should not run on direct cache hit")

    monkeypatch.setattr(shopee.facebook_repository, "get_affiliate_links", fake_direct)
    monkeypatch.setattr(shopee, "resolve_shopee_url", forbidden)
    monkeypatch.setattr(shopee, "_launch_and_convert", forbidden)

    result = await shopee.convert_urls("A", [source])
    assert result[0].affiliate_url == affiliate
    assert result[0].from_cache is True


@pytest.mark.asyncio
async def test_convert_urls_uses_canonical_cache_without_browser(monkeypatch):
    source = "https://s.shopee.vn/source"
    affiliate = "https://s.shopee.vn/affiliate"
    saved = {}

    async def fake_direct(account_id, urls):
        return {}

    async def fake_resolve(url):
        return shopee.ResolvedShopeeUrl(url, "https://shopee.vn/product/1/2", "item:1:2")

    async def fake_canonical(account_id, keys):
        return {"item:1:2": affiliate}

    async def fake_set(account_id, source_url, affiliate_url, **kwargs):
        saved.update(source=source_url, affiliate=affiliate_url, canonical=kwargs.get("canonical_key"))

    async def forbidden(*args, **kwargs):
        raise AssertionError("browser should not run on canonical cache hit")

    monkeypatch.setattr(shopee.facebook_repository, "get_affiliate_links", fake_direct)
    monkeypatch.setattr(shopee.facebook_repository, "get_affiliate_links_by_canonical", fake_canonical)
    monkeypatch.setattr(shopee.facebook_repository, "set_affiliate_link", fake_set)
    monkeypatch.setattr(shopee, "resolve_shopee_url", fake_resolve)
    monkeypatch.setattr(shopee, "_launch_and_convert", forbidden)

    result = await shopee.convert_urls("A", [source])
    assert result[0].affiliate_url == affiliate
    assert result[0].from_cache is True
    assert saved == {"source": source, "affiliate": affiliate, "canonical": "item:1:2"}

@pytest.mark.asyncio
async def test_convert_urls_browser_has_hard_timeout(monkeypatch):
    source = "https://s.shopee.vn/source-timeout"

    async def fake_direct(account_id, urls):
        return {}

    async def fake_resolve(url):
        return shopee.ResolvedShopeeUrl(url, "https://shopee.vn/product/9/10", "item:9:10")

    async def fake_canonical(account_id, keys):
        return {}

    async def slow_browser(items):
        import asyncio
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(shopee.facebook_repository, "get_affiliate_links", fake_direct)
    monkeypatch.setattr(shopee.facebook_repository, "get_affiliate_links_by_canonical", fake_canonical)
    monkeypatch.setattr(shopee, "resolve_shopee_url", fake_resolve)
    monkeypatch.setattr(shopee, "_launch_and_convert", slow_browser)
    monkeypatch.setattr(shopee, "_browser_batch_timeout_sec", lambda item_count: 0.01)

    with pytest.raises(shopee.ShopeeAffiliateError, match="vượt quá thời gian"):
        await shopee.convert_urls("A", [source])


@pytest.mark.asyncio
async def test_wait_for_custom_link_field_retries_until_spa_renders(monkeypatch):
    class FakePage:
        url = "https://affiliate.shopee.vn/offer/custom_link"
        frames = []

    page = FakePage()
    expected = object()
    attempts = 0

    async def fake_assert_logged_in(_page):
        return None

    async def fake_visible_input(_page):
        nonlocal attempts
        attempts += 1
        return expected if attempts >= 3 else None

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(shopee, "_assert_logged_in", fake_assert_logged_in)
    monkeypatch.setattr(shopee, "_visible_input", fake_visible_input)
    monkeypatch.setattr(shopee.asyncio, "sleep", fast_sleep)

    field = await shopee._wait_for_custom_link_field(page)
    assert field is expected
    assert attempts == 3
