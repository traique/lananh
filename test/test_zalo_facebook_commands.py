from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
async def test_fb_link_saves_link_then_ai_rewrites_caption_without_link(monkeypatch):
    source = "https://shopee.vn/product/1/2"
    row = {
        "id": 7,
        "status": "PENDING_APPROVAL",
        "original_content": f"Deal {source}",
        "processed_content": f"Deal {source}",
        "group_id": "g1",
        "sender_name": "Lan",
        "sender_id": "u1",
    }
    saved = {}

    async def fake_get_post(account_id, post_id):
        return {**row, "processed_content": saved.get("content", row["processed_content"])}

    async def fake_set_link(account_id, source_url, affiliate_url, **kwargs):
        saved["link"] = (source_url, affiliate_url)

    async def fake_update(account_id, post_id, content):
        saved["content"] = content
        return True

    async def fake_media(post_id):
        return []

    async def fake_links(account_id, urls):
        return {saved["link"][0]: saved["link"][1]} if "link" in saved else {}

    async def fake_rewrite(content):
        assert source in content
        return "Hời quá nè", True

    repo = facebook_commands.facebook_repository
    monkeypatch.setattr(repo, "get_post", fake_get_post)
    monkeypatch.setattr(repo, "set_affiliate_link", fake_set_link)
    monkeypatch.setattr(repo, "update_content", fake_update)
    monkeypatch.setattr(repo, "get_media", fake_media)
    monkeypatch.setattr(repo, "get_affiliate_links", fake_links)
    monkeypatch.setattr(facebook_commands.facebook_caption, "rewrite_caption", fake_rewrite)

    result = await facebook_commands.maybe_handle_facebook_command(
        "B", "/fb_link 7 https://s.shopee.vn/affiliate123"
    )

    assert saved["link"] == (source, "https://s.shopee.vn/affiliate123")
    assert saved["content"] == "Hời quá nè"
    message = result.messages[0]
    assert "AI đã viết lại" in message
    assert "💬 Link sẽ thả ở bình luận đầu tiên:\nhttps://s.shopee.vn/affiliate123" in message
    assert "Hời quá nè\n\n" + facebook_commands.facebook_caption.COMMENT_CTA in message
    assert "/r/" not in message


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
    assert "/fb_link 25 <link-affiliate>" in preview
    # Two "_" on one line get eaten by markdown-italic renderers.
    assert all(line.count("_") < 2 for line in preview.splitlines() if line.startswith("/fb_"))
    assert "\n/fb_ok 25\n/fb_boqua 25" in preview


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
async def test_fb_link_without_affiliate_url_shows_usage_and_never_touches_post(monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("repository must not be touched")

    monkeypatch.setattr(facebook_commands.facebook_repository, "get_post", fail)
    monkeypatch.setattr(facebook_commands.facebook_repository, "set_affiliate_link", fail)

    result = await facebook_commands.maybe_handle_facebook_command("B", "/fb_link 9")

    assert "/fb_link <post_id> <affiliate_url>" in result.messages[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,expected",
    [
        ("/fb_link 9 https://s.shopee.vn/aff1 https://s.shopee.vn/aff2", "không thuộc bài"),
        ("/fb_link 9 https://shopee.vn/product/1/2 https://evil.example/aff", "https://s.shopee.vn/"),
        ("/fb_link 9 https://shopee.vn/product/1/2 https://s.shopee.vn.evil.example/x", "https://s.shopee.vn/"),
    ],
)
async def test_fb_link_rejects_wrong_source_or_non_shopee_affiliate_host(
    monkeypatch, command, expected
):
    row = {
        "id": 9,
        "status": "PENDING_APPROVAL",
        "original_content": "A https://shopee.vn/product/1/2 B https://shopee.vn/product/3/4",
        "processed_content": "A https://shopee.vn/product/1/2 B https://shopee.vn/product/3/4",
    }

    async def fake_get_post(account_id, post_id):
        return row

    async def fail(*args, **kwargs):
        raise AssertionError("invalid link must not be saved")

    monkeypatch.setattr(facebook_commands.facebook_repository, "get_post", fake_get_post)
    monkeypatch.setattr(facebook_commands.facebook_repository, "set_affiliate_link", fail)
    monkeypatch.setattr(facebook_commands.facebook_repository, "update_content", fail)

    result = await facebook_commands.maybe_handle_facebook_command("B", command)

    assert expected in result.messages[0]


@pytest.mark.asyncio
async def test_fb_link_waits_for_every_product_before_rewriting(monkeypatch):
    first = "https://shopee.vn/product/1/2"
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
    links = {}
    rewritten = []

    async def fake_get_post(account_id, post_id):
        return row

    async def fake_set_link(account_id, source_url, affiliate_url):
        links[source_url] = affiliate_url

    async def fake_update(account_id, post_id, content):
        rewritten.append(content)
        return True

    async def fake_media(post_id):
        return []

    async def fake_links(account_id, urls):
        return {u: links[u] for u in urls if u in links}

    async def fake_rewrite(content):
        return "Hai deal hời", True

    repo = facebook_commands.facebook_repository
    monkeypatch.setattr(repo, "get_post", fake_get_post)
    monkeypatch.setattr(repo, "set_affiliate_link", fake_set_link)
    monkeypatch.setattr(repo, "update_content", fake_update)
    monkeypatch.setattr(repo, "get_media", fake_media)
    monkeypatch.setattr(repo, "get_affiliate_links", fake_links)
    monkeypatch.setattr(facebook_commands.facebook_caption, "rewrite_caption", fake_rewrite)

    await facebook_commands.maybe_handle_facebook_command(
        "B", f"/fb_link 9 {second} https://s.shopee.vn/aff2"
    )
    assert links == {second: "https://s.shopee.vn/aff2"} and rewritten == []

    await facebook_commands.maybe_handle_facebook_command(
        "B", f"/fb_link 9 {first} https://s.shopee.vn/aff1"
    )
    assert rewritten == ["Hai deal hời"]


@pytest.mark.asyncio
async def test_fb_ok_posts_to_every_configured_page_and_retries_only_failed_one(monkeypatch):
    row = {
        "id": 42,
        "claim_token": "claim-42",
        "status": "PENDING_APPROVAL",
        "original_content": "Deal không có link Shopee",
        "processed_content": "Deal không có link Shopee",
    }
    targets: dict[str, dict] = {}

    async def fake_get_post(account_id, post_id):
        return row

    async def fake_links(account_id, urls):
        return {}

    async def fake_media(post_id):
        return []

    async def fake_claim(account_id, post_id):
        if row["status"] not in ("PENDING_APPROVAL", "ERROR"):
            return None
        row["status"] = "POSTING"
        return row

    async def fake_ensure_targets(post_id, page_keys):
        for key in page_keys:
            targets.setdefault(key, {"page_key": key, "status": "PENDING", "facebook_post_id": None})

    async def fake_list_targets(post_id):
        return list(targets.values())

    calls = {"page1": 0, "page2": 0}

    async def fake_publish(content, media, page_key, *, before_create, on_created):
        calls[page_key] += 1
        await before_create()
        if page_key == "page2" and calls[page_key] == 1:
            raise facebook_commands.FacebookPublishError("page2 tạm lỗi")
        published = type("P", (), {})()
        published.post_id = f"{page_key}-post-id"
        await on_created(published.post_id)
        published.permalink_url = f"https://facebook.com/{page_key}"
        published.visibility_confirmed = True
        return published

    async def fake_record_posted(post_id, page_key, facebook_post_id, permalink_url, **claim):
        targets[page_key] = {"page_key": page_key, "status": "POSTED", "facebook_post_id": facebook_post_id}

    async def fake_record_error(post_id, page_key, message, **claim):
        targets[page_key] = {"page_key": page_key, "status": "ERROR", "facebook_post_id": None, "error_message": message}

    async def fake_finalize(account_id, post_id, **claim):
        if all(t["status"] == "POSTED" for t in targets.values()):
            row["status"] = "POSTED"
            return "POSTED"
        row["status"] = "ERROR"
        return "ERROR"

    monkeypatch.setattr(facebook_commands.facebook_repository, "get_post", fake_get_post)
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_affiliate_links", fake_links)
    monkeypatch.setattr(facebook_commands.facebook_repository, "get_media", fake_media)
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def fake_keep(*args):
        yield
    async def fake_creating(post_id, page_key, token):
        assert token == "claim-42"
        targets[page_key]["status"] = "POSTING"
    monkeypatch.setattr(facebook_commands.facebook_repository, "keep_post_claim", fake_keep)
    monkeypatch.setattr(facebook_commands.facebook_repository, "mark_target_creating", fake_creating)
    monkeypatch.setattr(facebook_commands.facebook_repository, "claim_post", fake_claim)
    monkeypatch.setattr(facebook_commands.facebook_repository, "ensure_targets", fake_ensure_targets)
    monkeypatch.setattr(facebook_commands.facebook_repository, "list_targets", fake_list_targets)
    monkeypatch.setattr(facebook_commands.facebook_repository, "record_target_posted", fake_record_posted)
    monkeypatch.setattr(facebook_commands.facebook_repository, "record_target_error", fake_record_error)
    monkeypatch.setattr(facebook_commands.facebook_repository, "finalize_post_status", fake_finalize)
    monkeypatch.setattr(facebook_commands, "configured_page_keys", lambda: ["page1", "page2"])
    monkeypatch.setattr(facebook_commands, "publish_page_post", fake_publish)

    first = await facebook_commands.maybe_handle_facebook_command("B", "/fb_ok 42")
    assert "page1" in targets and targets["page1"]["status"] == "POSTED"
    assert targets["page2"]["status"] == "ERROR"
    assert "❌ Page 'page2'" in first.messages[0]
    assert row["status"] == "ERROR"

    second = await facebook_commands.maybe_handle_facebook_command("B", "/fb_ok 42")
    assert targets["page2"]["status"] == "POSTED"
    assert "không tạo lại" in second.messages[0]  # page1 untouched
    assert calls == {"page1": 1, "page2": 2}  # page1 published exactly once, never retried
    assert "tất cả" in second.messages[0].lower() or "tất cả" in second.messages[0]
    assert row["status"] == "POSTED"
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


def _comment_flow(monkeypatch, *, targets, comment_error=None):
    source, affiliate = "https://shopee.vn/product/1/2", "https://s.shopee.vn/aff"
    row = {
        "id": 5,
        "claim_token": "tok",
        "status": "PENDING_APPROVAL",
        "original_content": f"Deal {source}",
        "processed_content": f"Deal hời {source}",
    }
    repo = facebook_commands.facebook_repository
    mocks = {
        name: AsyncMock(return_value=value)
        for name, value in {
            "ensure_targets": None,
            "get_media": [],
            "record_target_posted": None,
            "record_target_comment": None,
            "finalize_post_status": "ERROR",
        }.items()
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(repo, name, mock)
    monkeypatch.setattr(repo, "get_post", AsyncMock(return_value=row))
    monkeypatch.setattr(repo, "get_affiliate_links", AsyncMock(return_value={source: affiliate}))
    monkeypatch.setattr(repo, "claim_post", AsyncMock(return_value=row))
    monkeypatch.setattr(repo, "list_targets", AsyncMock(side_effect=lambda post_id: targets))

    @asynccontextmanager
    async def keep(*args):
        yield

    async def fake_publish(content, media, page_key, **callbacks):
        mocks["captions"].append(content)
        targets[0].update(status="POSTED", facebook_post_id="p1")
        return SimpleNamespace(post_id="p1", permalink_url=None, visibility_confirmed=True)

    mocks["captions"] = []
    mocks["post_comment"] = AsyncMock(side_effect=comment_error, return_value="c1")
    monkeypatch.setattr(repo, "keep_post_claim", keep)
    monkeypatch.setattr(facebook_commands, "configured_page_keys", lambda: ["default"])
    monkeypatch.setattr(facebook_commands, "publish_page_post", fake_publish)
    monkeypatch.setattr(facebook_commands, "post_comment", mocks["post_comment"])
    return mocks, affiliate


@pytest.mark.asyncio
async def test_fb_ok_posts_link_free_caption_then_comments_affiliate_link(monkeypatch):
    targets = [{"page_key": "default", "status": "PENDING", "facebook_post_id": None, "comment_id": None}]
    mocks, affiliate = _comment_flow(monkeypatch, targets=targets)

    result = await facebook_commands.maybe_handle_facebook_command("B", "/fb_ok 5")

    assert mocks["captions"] == ["Deal hời\n\n" + facebook_commands.facebook_caption.COMMENT_CTA]
    mocks["post_comment"].assert_awaited_once_with("p1", affiliate, "default")
    mocks["record_target_comment"].assert_awaited_once_with(5, "default", "c1", "tok")
    assert mocks["finalize_post_status"].await_args.kwargs["needs_comment"] is True
    assert "đã thả link vào bình luận đầu tiên" in result.messages[0]


@pytest.mark.asyncio
async def test_fb_ok_retry_only_comments_when_post_already_created(monkeypatch):
    targets = [{"page_key": "default", "status": "POSTED", "facebook_post_id": "p1", "comment_id": None}]
    mocks, affiliate = _comment_flow(
        monkeypatch, targets=targets,
        comment_error=[facebook_commands.FacebookPublishError("thiếu quyền"), "c2"],
    )

    first = await facebook_commands.maybe_handle_facebook_command("B", "/fb_ok 5")
    assert "chưa bình luận được link — thiếu quyền" in first.messages[0]
    mocks["record_target_comment"].assert_not_awaited()

    await facebook_commands.maybe_handle_facebook_command("B", "/fb_ok 5")
    assert mocks["captions"] == []  # post never recreated
    assert mocks["post_comment"].await_count == 2
    mocks["record_target_comment"].assert_awaited_once_with(5, "default", "c2", "tok")
