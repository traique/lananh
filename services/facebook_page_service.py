"""Facebook Page publisher using the Graph API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os

import httpx


class FacebookPublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class FacebookPostStatus:
    post_id: str
    is_published: bool | None
    is_hidden: bool | None
    timeline_visibility: str | None
    permalink_url: str | None
    in_published_posts: bool | None

    @property
    def public_visibility_confirmed(self) -> bool:
        return (
            self.is_published is True
            and self.is_hidden is not True
            and (self.timeline_visibility or "").lower() not in {"hidden"}
            and self.in_published_posts is not False
        )


@dataclass(frozen=True)
class FacebookPublishedPost:
    post_id: str
    permalink_url: str | None
    visibility_confirmed: bool
    status: FacebookPostStatus


def _settings() -> tuple[str, str, str]:
    page_id = os.getenv("FACEBOOK_PAGE_ID", "").strip()
    token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
    version = os.getenv("FACEBOOK_GRAPH_VERSION", "v26.0").strip() or "v26.0"
    if not page_id or not token:
        raise FacebookPublishError(
            "Chưa cấu hình FACEBOOK_PAGE_ID và FACEBOOK_PAGE_ACCESS_TOKEN."
        )
    return page_id, token, version


async def _graph_post(client: httpx.AsyncClient, url: str, **kwargs) -> dict:
    response = await client.post(url, **kwargs)
    if response.is_error:
        try:
            detail = response.json().get("error", {}).get("message")
        except Exception:
            detail = None
        raise FacebookPublishError(detail or f"Facebook Graph API HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise FacebookPublishError("Facebook Graph API trả dữ liệu không hợp lệ")
    return data


async def _graph_get(client: httpx.AsyncClient, url: str, **kwargs) -> dict:
    response = await client.get(url, **kwargs)
    if response.is_error:
        try:
            detail = response.json().get("error", {}).get("message")
        except Exception:
            detail = None
        raise FacebookPublishError(detail or f"Facebook Graph API HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise FacebookPublishError("Facebook Graph API trả dữ liệu không hợp lệ")
    return data


async def _read_post_status(
    client: httpx.AsyncClient,
    *,
    base: str,
    page_id: str,
    token: str,
    post_id: str,
    check_published_edge: bool = True,
) -> FacebookPostStatus:
    """Read back the post after publish instead of trusting HTTP 200 alone.

    Facebook's UI can lag behind the Graph API. ``is_published`` plus the
    Page ``published_posts`` edge gives us a much stronger signal that the
    story is a normal public Page post, while ``is_hidden`` and
    ``timeline_visibility`` help diagnose posts that only appear under Photos.
    """
    fields = "id,is_published,is_hidden,timeline_visibility,permalink_url"
    try:
        data = await _graph_get(
            client,
            f"{base}/{post_id}",
            params={"fields": fields, "access_token": token},
        )
    except FacebookPublishError:
        # Some Graph versions/tokens may reject one of the diagnostic fields.
        # Fall back to the core fields so a successful publication is not
        # turned into an error just because a diagnostic field changed.
        data = await _graph_get(
            client,
            f"{base}/{post_id}",
            params={
                "fields": "id,is_published,permalink_url",
                "access_token": token,
            },
        )

    in_published_posts: bool | None = None
    if check_published_edge:
        try:
            edge = await _graph_get(
                client,
                f"{base}/{page_id}/published_posts",
                params={"fields": "id", "limit": "50", "access_token": token},
            )
            items = edge.get("data")
            if isinstance(items, list):
                in_published_posts = any(
                    isinstance(item, dict) and str(item.get("id") or "") == post_id
                    for item in items
                )
        except FacebookPublishError:
            # A read-back permission/API change should not erase the stronger
            # per-post ``is_published`` signal. Surface it as unknown instead.
            in_published_posts = None

    return FacebookPostStatus(
        post_id=post_id,
        is_published=data.get("is_published") if isinstance(data.get("is_published"), bool) else None,
        is_hidden=data.get("is_hidden") if isinstance(data.get("is_hidden"), bool) else None,
        timeline_visibility=(str(data["timeline_visibility"]) if data.get("timeline_visibility") is not None else None),
        permalink_url=(str(data["permalink_url"]) if data.get("permalink_url") else None),
        in_published_posts=in_published_posts,
    )


async def inspect_page_post(post_id: str) -> FacebookPostStatus:
    """Inspect a previously-created Page post for public/timeline visibility."""
    page_id, token, version = _settings()
    base = f"https://graph.facebook.com/{version}"
    timeout = httpx.Timeout(30.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await _read_post_status(
            client,
            base=base,
            page_id=page_id,
            token=token,
            post_id=post_id,
        )


async def _verify_new_post(
    client: httpx.AsyncClient,
    *,
    base: str,
    page_id: str,
    token: str,
    post_id: str,
) -> FacebookPostStatus:
    # New Page Experience can be eventually consistent. A few short retries
    # avoid declaring a healthy post "missing" just because published_posts
    # has not indexed it yet.
    status: FacebookPostStatus | None = None
    for delay in (0.0, 1.5, 3.0, 5.0):
        if delay:
            await asyncio.sleep(delay)
        status = await _read_post_status(
            client,
            base=base,
            page_id=page_id,
            token=token,
            post_id=post_id,
        )
        if status.public_visibility_confirmed:
            break
    assert status is not None
    return status


async def publish_page_post(
    content: str, media: list[tuple[str, bytes]]
) -> FacebookPublishedPost:
    page_id, token, version = _settings()
    base = f"https://graph.facebook.com/{version}"
    timeout = httpx.Timeout(60.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if not media:
            data = await _graph_post(
                client,
                f"{base}/{page_id}/feed",
                # Be explicit. The Graph API normally defaults to published,
                # but this prevents accidental draft/dark-post semantics when
                # API behaviour changes or extra parameters are introduced.
                data={
                    "message": content,
                    "published": "true",
                    "access_token": token,
                },
            )
            post_id = data.get("id")
            if not post_id:
                raise FacebookPublishError("Facebook không trả về post id")
            status = await _verify_new_post(
                client,
                base=base,
                page_id=page_id,
                token=token,
                post_id=str(post_id),
            )
            if status.is_published is False:
                raise FacebookPublishError(
                    "Facebook đã tạo post nhưng is_published=false; bot không đánh dấu là đã đăng."
                )
            return FacebookPublishedPost(
                post_id=str(post_id),
                permalink_url=status.permalink_url,
                visibility_confirmed=status.public_visibility_confirmed,
                status=status,
            )

        photo_ids: list[str] = []
        for index, (mime_type, body) in enumerate(media):
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(
                mime_type, "jpg"
            )
            data = await _graph_post(
                client,
                f"{base}/{page_id}/photos",
                data={"published": "false", "access_token": token},
                files={"source": (f"zalo-{index}.{ext}", body, mime_type)},
            )
            photo_id = data.get("id")
            if not photo_id:
                raise FacebookPublishError("Facebook không trả về photo id")
            photo_ids.append(str(photo_id))

        payload = {
            "message": content,
            "published": "true",
            "access_token": token,
        }
        for index, photo_id in enumerate(photo_ids):
            payload[f"attached_media[{index}]"] = json.dumps({"media_fbid": photo_id})
        data = await _graph_post(client, f"{base}/{page_id}/feed", data=payload)
        post_id = data.get("id")
        if not post_id:
            raise FacebookPublishError("Facebook không trả về post id")

        status = await _verify_new_post(
            client,
            base=base,
            page_id=page_id,
            token=token,
            post_id=str(post_id),
        )
        if status.is_published is False:
            raise FacebookPublishError(
                "Facebook đã tạo post nhưng is_published=false; bot không đánh dấu là đã đăng."
            )
        return FacebookPublishedPost(
            post_id=str(post_id),
            permalink_url=status.permalink_url,
            visibility_confirmed=status.public_visibility_confirmed,
            status=status,
        )
