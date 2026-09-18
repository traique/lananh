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
async def test_fb_link_replaces_original_shopee_link_and_creates_short_link(monkeypatch):
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

    async def fake_set_link(account_id, source_url, affiliate_url):
        updated["source"] = source_url
        updated["affiliate"] = affiliate_url

    async def fake_update(account_id, post_id, content):
        updated["content"] = content
        return True

    async def fake_short(url):
        return "abc123"

    async def fake_media(post_id):
        return []

    async def fake_links(account_id, urls):
        return {updated["source"]: updated["affiliate"]} if "source" in updated else {}

    monkeypatch.setattr(facebook_commands.facebook_repository, "get_post", fake_get_post)
    monkeypatch.setattr(facebook_commands.facebook_repository, "set_affiliate_link", fake_set_link)
    monkeypatch.setattr(facebook_commands.facebook_repository, "update_content", fake_update)
    monkeypatch.setattr(facebook_commands.facebook_repository, "create_short_link", fake_short)
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_media", fake_media)
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_affiliate_links", fake_links)
    monkeypatch.setenv("AFFILIATE_SHORT_BASE_URL", "https://go.example.com")

    result = await facebook_commands.maybe_handle_facebook_command(
        "B", "/fb_link 7 https://s.shopee.vn/affiliate123"
    )

    assert updated["content"] == "Deal https://s.shopee.vn/affiliate123"
    assert "https://go.example.com/r/abc123" in result.messages[0]


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
    assert "/fb_link 25 <link_mới>" in preview


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
