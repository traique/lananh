"""Facebook Page publisher using the Graph API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import logging
from collections.abc import Awaitable, Callable

import httpx

from services import http_client


class FacebookPublishError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class FacebookPublicationUncertain(FacebookPublishError):
    def __init__(self, message: str, *, post_id: str | None = None):
        super().__init__(message)
        self.post_id = post_id


logger = logging.getLogger(__name__)


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


def _env_suffix(page_key: str) -> str:
    """"default" keeps the original FACEBOOK_PAGE_ID/... env names so existing
    single-page deployments need no changes. Any other key (e.g. "2") reads
    FACEBOOK_PAGE_ID_2 / FACEBOOK_PAGE_ACCESS_TOKEN_2 instead."""
    return "" if page_key == "default" else f"_{page_key}"


def _settings(page_key: str = "default") -> tuple[str, str, str]:
    suffix = _env_suffix(page_key)
    page_id = os.getenv(f"FACEBOOK_PAGE_ID{suffix}", "").strip()
    token = os.getenv(f"FACEBOOK_PAGE_ACCESS_TOKEN{suffix}", "").strip()
    version = (
        os.getenv(f"FACEBOOK_GRAPH_VERSION{suffix}", "").strip()
        or os.getenv("FACEBOOK_GRAPH_VERSION", "v26.0").strip()
        or "v26.0"
    )
    if not page_id or not token:
        raise FacebookPublishError(
            f"Chưa cấu hình FACEBOOK_PAGE_ID{suffix} và FACEBOOK_PAGE_ACCESS_TOKEN{suffix} "
            f"cho page '{page_key}'."
        )
    return page_id, token, version


# Page chỉ dành cho luồng tự động (services/market_page.py). Phải loại khỏi
# configured_page_keys() mặc định, nếu không /fb_ok sẽ đăng cả bài Shopee lên đó.
MARKET_PAGE_KEY = "MARKET"
DEDICATED_PAGE_KEYS = frozenset({MARKET_PAGE_KEY})


def configured_page_keys(include_dedicated: bool = False) -> list[str]:
    """Every page_key with both env vars set, "default" first."""
    keys = []
    if os.getenv("FACEBOOK_PAGE_ID", "").strip() and os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip():
        keys.append("default")
    for name in os.environ:
        prefix = "FACEBOOK_PAGE_ID_"
        if not name.startswith(prefix):
            continue
        key = name[len(prefix):]
        if key in DEDICATED_PAGE_KEYS and not include_dedicated:
            continue
        if (
            os.getenv(name, "").strip()
            and os.getenv(f"FACEBOOK_PAGE_ACCESS_TOKEN_{key}", "").strip()
        ):
            keys.append(key)
    return keys


async def _graph_post(client: httpx.AsyncClient, url: str, **kwargs) -> dict:
    response = await client.post(url, **kwargs)
    if response.is_error:
        try:
            detail = response.json().get("error", {}).get("message")
        except Exception:
            detail = None
        raise FacebookPublishError(
            detail or f"Facebook Graph API HTTP {response.status_code}",
            status_code=response.status_code,
        )
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
        except (FacebookPublishError, httpx.HTTPError):
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


async def inspect_page_post(post_id: str, page_key: str = "default") -> FacebookPostStatus:
    """Inspect a previously-created Page post for public/timeline visibility."""
    page_id, token, version = _settings(page_key)
    base = f"https://graph.facebook.com/{version}"
    timeout = httpx.Timeout(30.0, connect=15.0)
    async with http_client.scoped(timeout=timeout) as client:
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
    content: str, media: list[tuple[str, bytes]], page_key: str = "default", *,
    before_create: Callable[[], Awaitable[None]] | None = None,
    on_created: Callable[[str], Awaitable[None]] | None = None,
) -> FacebookPublishedPost:
    page_id, token, version = _settings(page_key)
    base = f"https://graph.facebook.com/{version}"
    timeout = httpx.Timeout(60.0, connect=15.0)
    async with http_client.scoped(timeout=timeout) as client:
        if not media:
            return await _create_and_verify(
                client, base=base, page_id=page_id, token=token,
                payload={
                    "message": content,
                    "published": "true",
                    "access_token": token,
                },
                before_create=before_create, on_created=on_created,
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
        return await _create_and_verify(
            client, base=base, page_id=page_id, token=token, payload=payload,
            before_create=before_create, on_created=on_created,
        )


async def _create_and_verify(
    client, *, base, page_id, token, payload, before_create, on_created,
) -> FacebookPublishedPost:
    if before_create is not None:
        await before_create()
    try:
        data = await _graph_post(client, f"{base}/{page_id}/feed", data=payload)
    except FacebookPublishError as exc:
        if exc.status_code is not None and exc.status_code < 500:
            raise
        raise FacebookPublicationUncertain("Chưa biết Facebook đã tạo bài hay chưa; cần đối soát.") from exc
    except Exception as exc:
        raise FacebookPublicationUncertain("Chưa biết Facebook đã tạo bài hay chưa; cần đối soát.") from exc
    post_id = str(data.get("id") or "")
    if not post_id:
        raise FacebookPublicationUncertain("Facebook không trả post ID; không tự gửi lại thao tác tạo bài.")
    if on_created is not None:
        try:
            await on_created(post_id)
        except Exception as exc:
            raise FacebookPublicationUncertain(
                f"Đã tạo Facebook Post ID {post_id} nhưng chưa lưu được trạng thái.", post_id=post_id,
            ) from exc
    try:
        status = await _verify_new_post(
            client,
            base=base,
            page_id=page_id,
            token=token,
            post_id=post_id,
        )
    except Exception as exc:
        logger.warning("Facebook Post ID %s đã tạo; đọc trạng thái lỗi (%s).", post_id, type(exc).__name__)
        status = FacebookPostStatus(post_id, None, None, None, None, None)
    return FacebookPublishedPost(post_id, status.permalink_url, status.public_visibility_confirmed, status)


async def post_comment(post_id: str, message: str, page_key: str = "default") -> str:
    _, token, version = _settings(page_key)
    timeout = httpx.Timeout(30.0, connect=15.0)
    async with http_client.scoped(timeout=timeout) as client:
        data = await _graph_post(
            client,
            f"https://graph.facebook.com/{version}/{post_id}/comments",
            data={"message": message, "access_token": token},
        )
    comment_id = str(data.get("id") or "")
    if not comment_id:
        raise FacebookPublishError("Facebook không trả comment ID")
    return comment_id
