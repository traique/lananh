"""Facebook Page publisher using the Graph API."""

import json
import os

import httpx


class FacebookPublishError(RuntimeError):
    pass


def _settings() -> tuple[str, str, str]:
    page_id = os.getenv("FACEBOOK_PAGE_ID", "").strip()
    token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
    version = os.getenv("FACEBOOK_GRAPH_VERSION", "v23.0").strip() or "v23.0"
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


async def publish_page_post(content: str, media: list[tuple[str, bytes]]) -> str:
    page_id, token, version = _settings()
    base = f"https://graph.facebook.com/{version}"
    timeout = httpx.Timeout(60.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if not media:
            data = await _graph_post(
                client,
                f"{base}/{page_id}/feed",
                data={"message": content, "access_token": token},
            )
            post_id = data.get("id")
            if not post_id:
                raise FacebookPublishError("Facebook không trả về post id")
            return str(post_id)

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
            "access_token": token,
        }
        for index, photo_id in enumerate(photo_ids):
            payload[f"attached_media[{index}]"] = json.dumps({"media_fbid": photo_id})
        data = await _graph_post(client, f"{base}/{page_id}/feed", data=payload)
        post_id = data.get("id")
        if not post_id:
            raise FacebookPublishError("Facebook không trả về post id")
        return str(post_id)
