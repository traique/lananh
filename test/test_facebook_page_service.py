import pytest

from services import facebook_page_service


def test_settings_require_page_credentials(monkeypatch):
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    monkeypatch.delenv("FACEBOOK_PAGE_ACCESS_TOKEN", raising=False)
    with pytest.raises(facebook_page_service.FacebookPublishError):
        facebook_page_service._settings()


@pytest.mark.asyncio
async def test_publish_feed_is_explicitly_published_and_returns_permalink(monkeypatch):
    calls = []

    monkeypatch.setattr(
        facebook_page_service,
        "_settings",
        lambda: ("page123", "token", "v26.0"),
    )

    async def fake_post(client, url, **kwargs):
        calls.append((url, kwargs))
        return {"id": "page123_post456"}

    async def fake_verify(client, **kwargs):
        return facebook_page_service.FacebookPostStatus(
            post_id="page123_post456",
            is_published=True,
            is_hidden=False,
            timeline_visibility="normal",
            permalink_url="https://www.facebook.com/page123/posts/post456",
            in_published_posts=True,
        )

    monkeypatch.setattr(facebook_page_service, "_graph_post", fake_post)
    monkeypatch.setattr(facebook_page_service, "_verify_new_post", fake_verify)

    result = await facebook_page_service.publish_page_post("hello", [])

    assert calls[0][1]["data"]["published"] == "true"
    assert result.post_id == "page123_post456"
    assert result.visibility_confirmed is True
    assert result.permalink_url.endswith("/post456")


@pytest.mark.asyncio
async def test_multi_photo_final_feed_is_explicitly_published(monkeypatch):
    calls = []
    photo_counter = 0

    monkeypatch.setattr(
        facebook_page_service,
        "_settings",
        lambda: ("page123", "token", "v26.0"),
    )

    async def fake_post(client, url, **kwargs):
        nonlocal photo_counter
        calls.append((url, kwargs))
        if url.endswith("/photos"):
            photo_counter += 1
            return {"id": f"photo{photo_counter}"}
        return {"id": "page123_post456"}

    async def fake_verify(client, **kwargs):
        return facebook_page_service.FacebookPostStatus(
            post_id="page123_post456",
            is_published=True,
            is_hidden=False,
            timeline_visibility="normal",
            permalink_url="https://www.facebook.com/page123/posts/post456",
            in_published_posts=True,
        )

    monkeypatch.setattr(facebook_page_service, "_graph_post", fake_post)
    monkeypatch.setattr(facebook_page_service, "_verify_new_post", fake_verify)

    result = await facebook_page_service.publish_page_post(
        "album", [("image/jpeg", b"a"), ("image/png", b"b")]
    )

    assert calls[0][1]["data"]["published"] == "false"
    assert calls[1][1]["data"]["published"] == "false"
    final_payload = calls[2][1]["data"]
    assert final_payload["published"] == "true"
    assert "attached_media[0]" in final_payload
    assert "attached_media[1]" in final_payload
    assert result.visibility_confirmed is True
