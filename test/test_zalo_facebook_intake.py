"""Bộ lọc bài từ nhóm Zalo trước khi vào hàng chờ Facebook (channels/router.py)."""
import base64
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import facebook_repository  # noqa: E402
from channels import router as zalo_router  # noqa: E402
from channels.contracts import ZaloFacebookPostRequest  # noqa: E402

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
PRODUCT = "Tai nghe Bluetooth XYZ chính hãng giá 199k https://s.shopee.vn/abc"


def _post(text: str, *, image: bool):
    media = [{"mime_type": "image/png", "data_b64": PNG_B64}] if image else []
    return ZaloFacebookPostRequest(
        account_id="acc",
        group_id="g1",
        sender_id="u1",
        sender_name="Người gửi",
        message_ids=["m1"],
        text=text,
        media=media,
    )


@pytest.fixture
def intake(monkeypatch):
    calls = {"created": [], "prepared": []}

    async def fake_create_post(**kwargs):
        calls["created"].append(kwargs)
        return 7

    async def fake_prepare_post(account_id, post_id):
        calls["prepared"].append((account_id, post_id))

    monkeypatch.setattr(zalo_router, "_secret", lambda: "s3cr3t")
    monkeypatch.setattr(facebook_repository, "create_post", fake_create_post)
    monkeypatch.setattr(zalo_router, "prepare_post", fake_prepare_post)
    monkeypatch.delenv("FACEBOOK_REQUIRE_PHOTO_AND_CAPTION", raising=False)
    monkeypatch.delenv("FACEBOOK_SKIP_VOUCHER_POSTS", raising=False)
    return calls


async def _submit(payload):
    return await zalo_router.facebook_group_post(payload, "s3cr3t")


@pytest.mark.asyncio
async def test_post_with_photo_and_caption_is_queued(intake):
    response = await _submit(_post(PRODUCT, image=True))

    assert response.status_code == 204
    assert len(intake["created"]) == 1
    assert intake["created"][0]["content"] == PRODUCT
    assert intake["prepared"] == [("acc", 7)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "",
        "Link: https://s.shopee.vn/abc",
        "Link mua: s.shopee.vn/abcDEF123",
        "🔥🔥 https://s.shopee.vn/abc",
    ],
)
async def test_photo_without_caption_is_dropped_but_acknowledged(intake, text):
    response = await _submit(_post(text, image=True))

    assert response.status_code == 204  # 204 để gateway không thử lại bài này mãi
    assert intake["created"] == [] and intake["prepared"] == []


@pytest.mark.asyncio
async def test_caption_without_photo_is_dropped_but_acknowledged(intake):
    response = await _submit(_post(PRODUCT, image=False))

    assert response.status_code == 204
    assert intake["created"] == []


@pytest.mark.asyncio
async def test_voucher_only_caption_with_banner_photo_is_dropped(intake):
    voucher = "Săn mã 50% ShopeeVip 0H, lưu mã tại banner này https://s.shopee.vn/113YJ836rP"

    response = await _submit(_post(voucher, image=True))

    assert response.status_code == 204
    assert intake["created"] == []


@pytest.mark.asyncio
async def test_product_post_mentioning_voucher_but_with_price_is_kept(intake):
    text = "Tai nghe XYZ giá chỉ 199k, lưu mã giảm 20k https://s.shopee.vn/abc"

    await _submit(_post(text, image=True))

    assert len(intake["created"]) == 1


@pytest.mark.asyncio
async def test_completely_empty_post_is_still_rejected(intake):
    with pytest.raises(HTTPException) as exc:
        await _submit(_post("", image=False))

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_photo_and_caption_requirement_can_be_switched_off(intake, monkeypatch):
    monkeypatch.setenv("FACEBOOK_REQUIRE_PHOTO_AND_CAPTION", "0")

    await _submit(_post(PRODUCT, image=False))

    assert len(intake["created"]) == 1


@pytest.mark.asyncio
async def test_voucher_filter_can_be_switched_off(intake, monkeypatch):
    monkeypatch.setenv("FACEBOOK_SKIP_VOUCHER_POSTS", "0")

    await _submit(_post("Lưu mã giảm 50% lúc 0H https://s.shopee.vn/abc", image=True))

    assert len(intake["created"]) == 1


@pytest.mark.asyncio
async def test_wrong_bridge_secret_is_rejected_before_any_filtering(intake):
    with pytest.raises(HTTPException) as exc:
        await zalo_router.facebook_group_post(_post(PRODUCT, image=True), "sai")

    assert exc.value.status_code == 403
    assert intake["created"] == []
