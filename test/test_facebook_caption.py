from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import facebook_caption as caption

SOURCE = "https://shopee.vn/product/1/2"
AFFILIATE = "https://s.shopee.vn/abc"


def test_strip_shopee_urls_keeps_other_links_and_trailing_punctuation():
    text = f"Giảm sâu: {SOURCE}. Xem thêm https://example.com/x và {AFFILIATE}, mua ngay"
    assert caption.strip_shopee_urls(text) == (
        "Giảm sâu: . Xem thêm https://example.com/x và , mua ngay"
    )


def test_build_caption_appends_cta_once_and_only_for_posts_with_links():
    body = f"Deal sốc\n\n\n{SOURCE}"
    assert caption.build_caption(body, True) == f"Deal sốc\n\n{caption.COMMENT_CTA}"
    assert caption.build_caption(f"Deal sốc\n\n{caption.COMMENT_CTA}", True).count(
        caption.COMMENT_CTA
    ) == 1
    assert caption.build_caption(body, False) == "Deal sốc"
    assert caption.build_caption(SOURCE, True) == caption.COMMENT_CTA


@pytest.mark.asyncio
async def test_rewrite_caption_never_leaves_a_shopee_link(monkeypatch):
    ask = AsyncMock(return_value=SimpleNamespace(text=f"Hời quá nè {AFFILIATE}"))
    monkeypatch.setattr(caption.orchestrator, "ask", ask)
    assert await caption.rewrite_caption(f"Deal {SOURCE}") == ("Hời quá nè", True)
    assert SOURCE not in ask.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [SimpleNamespace(text=""), RuntimeError("quota")])
async def test_rewrite_caption_falls_back_to_original_without_links(monkeypatch, answer):
    ask = AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock(return_value=answer)
    monkeypatch.setattr(caption.orchestrator, "ask", ask)
    assert await caption.rewrite_caption(f"Deal {SOURCE}") == ("Deal", False)


@pytest.mark.parametrize(
    "text",
    [
        "VÔ LỰA TRƯỚC DEAL VIP 15H LÁT SĂN NÈ MN\nhttps://s.shopee.vn/LnrWhdCWK",
        "‼ Chưa gì mà thấy team săn mã 50% ShopeeVip đêm nay hơi bị đông rồi đó nghen\n"
        "0H lưu mã tại banner này:\nhttps://s.shopee.vn/113YJ836rP\n"
        "Lưu ý: Mã giảm 50% tối đa 500K áp thời trang, sức khỏe và làm đẹp",
        "luu ma giam 30k truoc 0h nhe https://s.shopee.vn/abc",  # không dấu
        "Nhập mã FREESHIP giảm 20K cho đơn từ 0Đ https://shope.ee/x",
        "Voucher giảm 20.000đ cho đơn từ 99.000đ, tối đa 30.000đ https://s.shopee.vn/a",
    ],
)
def test_voucher_only_posts_are_detected(text):
    assert caption.is_voucher_only(text)


@pytest.mark.parametrize(
    "text",
    [
        "Tai nghe Bluetooth XYZ giá chỉ 199k, áp mã giảm 20k https://s.shopee.vn/a",
        "Chỉ còn 129.000đ cho áo thun nam, lưu mã giảm thêm 10% https://s.shopee.vn/a",
        "Combo sữa tắm đồng giá 99k, nhập mã SALE giảm thêm https://s.shopee.vn/a",
        "Giá 250.000₫, voucher giảm 20K https://s.shopee.vn/a",
        "Áo thun nam 199.000đ, lưu mã giảm 10% https://s.shopee.vn/a",
        "Chỉ từ 99k, săn mã giảm thêm https://s.shopee.vn/a",
        "Áo khoác dù chống nắng siêu nhẹ, nhiều màu https://s.shopee.vn/a",  # không nhắc mã
        "Mã giảm 50K cho đơn từ 0Đ. " + "Mô tả sản phẩm rất dài. " * 30,  # quá dài
        "",
        "https://s.shopee.vn/onlylink",
    ],
)
def test_product_posts_and_unrelated_text_are_not_voucher_only(text):
    assert not caption.is_voucher_only(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tai nghe Bluetooth XYZ chính hãng https://s.shopee.vn/a", True),
        ("Tai nghe XYZ chính hãng", True),
        ("Hot nè mn ơi", False),  # quá ngắn để là caption
        ("Link: https://s.shopee.vn/a", False),
        ("Link mua: https://s.shopee.vn/a", False),
        ("Link mua: s.shopee.vn/abcDEF123", False),  # link không có https://
        ("shope.ee/abcdef", False),
        ("www.shopee.vn/product/1/2", False),
        ("https://s.shopee.vn/a", False),
        ("🔥🔥🔥 !!! https://s.shopee.vn/a", False),
        ("", False),
    ],
)
def test_has_caption_ignores_links_emoji_and_punctuation(text, expected):
    assert caption.has_caption(text) is expected


def test_skip_reason_requires_both_photo_and_caption():
    product = "Tai nghe Bluetooth XYZ giá 199k https://s.shopee.vn/a"

    assert caption.skip_reason(product, True) is None
    assert caption.skip_reason(product, False) == "có caption nhưng không có ảnh"
    assert caption.skip_reason("", True) == "có ảnh nhưng không có caption"
    assert caption.skip_reason("Link: https://s.shopee.vn/a", True) == "có ảnh nhưng không có caption"


def test_skip_reason_voucher_filter_applies_to_posts_with_photo_too():
    voucher = "Lưu mã giảm 50% lúc 0H tại banner này https://s.shopee.vn/a"

    assert caption.skip_reason(voucher, True) == "chỉ báo mã giảm giá"
    assert caption.skip_reason(voucher, True, skip_voucher=False) is None


def test_skip_reason_switches_can_be_turned_off_independently():
    text_only = "Áo khoác dù chống nắng siêu nhẹ https://s.shopee.vn/a"

    assert caption.skip_reason(text_only, False, require_photo_and_caption=False) is None
    assert caption.skip_reason("", True, require_photo_and_caption=False) is None
