import pytest

from channels import facebook_commands


@pytest.mark.asyncio
async def test_facebook_group_commands_use_separate_repository(monkeypatch):
    saved = {}

    async def fake_add(account_id, group_id, alias):
        saved.update(account_id=account_id, group_id=group_id, alias=alias)

    monkeypatch.setattr(facebook_commands.facebook_repository, "add_group", fake_add)
    result = await facebook_commands.maybe_handle_facebook_command(
        "B", "/fb_themnhom 123 Deal Team"
    )

    assert saved == {"account_id": "B", "group_id": "123", "alias": "deal team"}
    assert "Không ảnh hưởng /tongket" in result.messages[0]


@pytest.mark.asyncio
async def test_regular_group_command_is_not_claimed():
    result = await facebook_commands.maybe_handle_facebook_command(
        "B", "/themnhom 123 Summary Team"
    )
    assert result is None


@pytest.mark.asyncio
async def test_fb_ok_requires_affiliate_for_original_shopee_link(monkeypatch):
    row = {
        "id": 7,
        "status": "PENDING_APPROVAL",
        "original_content": "Deal https://shopee.vn/product/1/2",
        "processed_content": "Deal https://shopee.vn/product/1/2",
    }

    async def fake_get_post(account_id, post_id):
        return row

    async def fake_links(account_id, urls):
        return {}

    monkeypatch.setattr(facebook_commands.facebook_repository, "get_post", fake_get_post)
    monkeypatch.setattr(
        facebook_commands.facebook_repository, "get_affiliate_links", fake_links
    )

    result = await facebook_commands.maybe_handle_facebook_command("B", "/fb_ok 7")
    assert "chưa có affiliate" in result.messages[0]


@pytest.mark.asyncio
async def test_fb_link_manual_fallback_replaces_original_with_official_short_link(monkeypatch):
    row = {
        "id": 7,
        "status": "PENDING_APPROVAL",
        "original_content": "Deal https://shopee.vn/product/1/2",
        "processed_content": "Deal https://shopee.vn/product/1/2",
        "group_id": "g1",
        "sender_name": "Lan",
        "sender_id": "u1",
    }
    updated = {}

    async def fake_get_post(account_id, post_id):
        if "content" in updated:
            return {**row, "processed_content": updated["content"]}
        return row

    async def fake_set_link(account_id, source_url, affiliate_url, **kwargs):
        updated["source"] = source_url
        updated["affiliate"] = affiliate_url
        updated["canonical_key"] = kwargs.get("canonical_key")

    async def fake_update(account_id, post_id, content):
        updated["content"] = content
        return True

    async def fake_media(post_id):
        return []

    async def fake_links(account_id, urls):
        return {updated["source"]: updated["affiliate"]} if "source" in updated else {}

    monkeypatch.setattr(facebook_commands.facebook_repository, "get_post", fake_get_post)
    monkeypatch.setattr(facebook_commands.facebook_repository, "set_affiliate_link", fake_set_link)
    monkeypatch.setattr(facebook_commands.facebook_repository, "update_content", fake_update)
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_media", fake_media)
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_affiliate_links", fake_links)
    async def fake_resolve(url):
        return facebook_commands.shopee_affiliate_browser.ResolvedShopeeUrl(
            source_url=url, destination_url=url, canonical_key="item:1:2"
        )

    monkeypatch.setattr(facebook_commands.shopee_affiliate_browser, "resolve_shopee_url", fake_resolve)

    result = await facebook_commands.maybe_handle_facebook_command(
        "B", "/fb_link 7 https://s.shopee.vn/affiliate123"
    )

    assert updated["content"] == "Deal https://s.shopee.vn/affiliate123"
    assert updated["canonical_key"] == "item:1:2"
    assert "https://s.shopee.vn/affiliate123" in result.messages[0]
    assert "/r/" not in result.messages[0]


@pytest.mark.asyncio
async def test_preview_includes_original_shopee_link_for_easy_copy(monkeypatch):
    source_url = "https://shopee.vn/product/1/2"
    row = {
        "id": 25,
        "status": "PENDING_APPROVAL",
        "original_content": f"Deal {source_url}",
        "processed_content": f"Deal {source_url}",
        "group_id": "g1",
        "sender_name": "Lan",
        "sender_id": "u1",
    }

    async def fake_get_post(account_id, post_id):
        return row

    async def fake_media(post_id):
        return []

    async def fake_links(account_id, urls):
        return {}

    monkeypatch.setattr(facebook_commands.facebook_repository, "get_post", fake_get_post)
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_media", fake_media)
    monkeypatch.setattr(
        facebook_commands.facebook_repository, "get_affiliate_links", fake_links
    )

    preview = await facebook_commands._preview("B", 25)

    assert "Link Shopee gốc (chưa chuyển đổi):" in preview
    assert source_url in preview
    assert "/fb_link 25" in preview


@pytest.mark.asyncio
async def test_prepare_post_notifies_secondary_admin_channels(monkeypatch):
    row = {
        "id": 25,
        "status": "PENDING_APPROVAL",
        "original_content": "Deal mới",
        "processed_content": "Deal mới",
        "group_id": "g1",
        "sender_name": "Lan",
        "sender_id": "u1",
    }
    notifications = []

    async def fake_get_post(account_id, post_id):
        return row

    async def fake_links(account_id, urls):
        return {}

    async def fake_media(post_id):
        return []

    async def fake_controller():
        return ""

    async def notify(text):
        notifications.append(text)

    monkeypatch.delenv("ZALO_CONTROLLER_ID", raising=False)
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_post", fake_get_post)
    monkeypatch.setattr(
        facebook_commands.facebook_repository, "get_affiliate_links", fake_links
    )
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_media", fake_media)
    from channels import zalo_session

    monkeypatch.setattr(zalo_session, "load_controller", fake_controller)
    facebook_commands.set_admin_notification_callback(notify)
    try:
        await facebook_commands.prepare_post("B", 25)
    finally:
        facebook_commands.set_admin_notification_callback(None)

    assert len(notifications) == 1
    assert "BÀI FACEBOOK CHỜ DUYỆT #25" in notifications[0]

@pytest.mark.asyncio
async def test_fb_reset_deletes_saved_posts_and_reports_sequence_reset(monkeypatch):
    seen = {}

    async def fake_reset(account_id):
        seen["account_id"] = account_id
        return 12, True

    monkeypatch.setattr(facebook_commands.facebook_repository, "reset_posts", fake_reset)

    result = await facebook_commands.maybe_handle_facebook_command("B", "/fb_reset")

    assert seen == {"account_id": "B"}
    assert "12 bài" in result.messages[0]
    assert "#1" in result.messages[0]


@pytest.mark.asyncio
async def test_fb_link_auto_converts_all_urls_in_one_batch(monkeypatch):
    first = "https://s.shopee.vn/source1"
    second = "https://shopee.vn/product/3/4"
    row = {
        "id": 9,
        "status": "PENDING_APPROVAL",
        "original_content": f"A {first} B {second}",
        "processed_content": f"A {first} B {second}",
        "group_id": "g1",
        "sender_name": "Lan",
        "sender_id": "u1",
    }
    updated = {}

    async def fake_get_post(account_id, post_id):
        return {**row, "processed_content": updated.get("content", row["processed_content"])}

    async def fake_convert(account_id, urls):
        assert urls == [first, second]
        return [
            facebook_commands.shopee_affiliate_browser.AffiliateConversion(first, "https://s.shopee.vn/aff1", "item:1:2", False),
            facebook_commands.shopee_affiliate_browser.AffiliateConversion(second, "https://s.shopee.vn/aff2", "item:3:4", True),
        ]

    async def fake_update(account_id, post_id, content):
        updated["content"] = content
        return True

    async def fake_media(post_id):
        return []

    async def fake_links(account_id, urls):
        return {first: "https://s.shopee.vn/aff1", second: "https://s.shopee.vn/aff2"}

    monkeypatch.setattr(facebook_commands.facebook_repository, "get_post", fake_get_post)
    monkeypatch.setattr(facebook_commands.facebook_repository, "update_content", fake_update)
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_media", fake_media)
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_affiliate_links", fake_links)
    monkeypatch.setattr(facebook_commands.shopee_affiliate_browser, "convert_urls", fake_convert)
    monkeypatch.setattr(facebook_commands.config, "SHOPEE_AFFILIATE_AUTO_ENABLED", True)

    result = await facebook_commands.maybe_handle_facebook_command("B", "/fb_link 9")

    assert updated["content"] == "A https://s.shopee.vn/aff1 B https://s.shopee.vn/aff2"
    assert "1 link mới" in result.messages[0]
    assert "1 link từ cache" in result.messages[0]


@pytest.mark.asyncio
async def test_fb_link_manual_single_url_refuses_multi_product_post(monkeypatch):
    row = {
        "id": 11,
        "status": "PENDING_APPROVAL",
        "original_content": "A https://shopee.vn/product/1/2 B https://shopee.vn/product/3/4",
        "processed_content": "same",
    }

    async def fake_get_post(account_id, post_id):
        return row

    monkeypatch.setattr(facebook_commands.facebook_repository, "get_post", fake_get_post)
    result = await facebook_commands.maybe_handle_facebook_command(
        "B", "/fb_link 11 https://s.shopee.vn/one-aff-link"
    )
    assert "có 2 link Shopee" in result.messages[0]
    assert "<source_url> <affiliate_url>" in result.messages[0]
