"""POST /internal/zalo/facebook-group-post-multipart: bài Facebook từ gateway gửi ảnh
dạng nhị phân (không base64) - channels/router.py."""

import json

import httpx
import pytest
from fastapi import FastAPI

from channels import facebook_intake
from channels import router as zalo_router

SECRET = "s3cr3t"
META = {
    "account_id": "acc",
    "group_id": "g1",
    "sender_id": "u1",
    "sender_name": "Lan",
    "message_ids": ["m1"],
    "text": "Nồi chiên 6 lít giá 899k https://s.shopee.vn/AbC",
}


@pytest.fixture
def ingested(monkeypatch):
    calls = []

    async def ingest(post, media):
        calls.append((post, list(media)))
        return facebook_intake.QUEUED

    monkeypatch.setattr(zalo_router, "_secret", lambda: SECRET)
    monkeypatch.delenv("ZALO_BRIDGE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(facebook_intake, "ingest", ingest)
    return calls


async def _post(files, *, data=None, secret=SECRET):
    app = FastAPI()
    app.include_router(zalo_router.router)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        return await client.post(
            "/internal/zalo/facebook-group-post-multipart",
            headers={"x-zalo-bridge-secret": secret},
            data=data if data is not None else {"meta": json.dumps(META)},
            files=files,
        )


@pytest.mark.asyncio
async def test_multipart_post_reaches_intake_with_raw_image_bytes(ingested):
    jpeg = b"\xff\xd8\xff" + b"1" * 100
    png = b"\x89PNG" + b"2" * 50

    response = await _post(
        [("media", ("a.jpg", jpeg, "image/jpeg")), ("media", ("b.png", png, "image/png"))]
    )

    assert response.status_code == 204
    [(post, media)] = ingested
    assert (post.account_id, post.group_id, post.sender_id) == ("acc", "g1", "u1")
    assert post.text == META["text"]
    assert media == [("image/jpeg", jpeg), ("image/png", png)]


@pytest.mark.asyncio
async def test_wrong_secret_is_rejected(ingested):
    response = await _post([], secret="sai")

    assert response.status_code == 403
    assert ingested == []


@pytest.mark.asyncio
async def test_unsupported_image_type_is_a_client_error(ingested):
    response = await _post([("media", ("a.gif", b"GIF89a", "image/gif"))])

    assert response.status_code == 400
    assert ingested == []


@pytest.mark.asyncio
async def test_too_large_image_is_rejected(ingested, monkeypatch):
    monkeypatch.setenv("ZALO_IMAGE_MAX_BYTES", "10")

    response = await _post([("media", ("a.jpg", b"\xff\xd8" + b"x" * 20, "image/jpeg"))])

    assert response.status_code == 413
    assert ingested == []


@pytest.mark.asyncio
async def test_missing_or_invalid_meta_is_rejected(ingested):
    missing = await _post([("media", ("a.jpg", b"\xff\xd8x", "image/jpeg"))], data={})
    invalid = await _post([], data={"meta": json.dumps({"account_id": "acc"})})

    assert missing.status_code == 400
    assert invalid.status_code == 422
    assert ingested == []


@pytest.mark.asyncio
async def test_empty_post_is_rejected(ingested):
    response = await _post([], data={"meta": json.dumps({**META, "text": " "})})

    assert response.status_code == 400
    assert ingested == []


def test_request_limits_allow_large_multipart_posts():
    from services import request_limits

    assert request_limits._limits("/internal/zalo/facebook-group-post-multipart", 0) == (
        22 * request_limits.MIB,
        True,
    )
