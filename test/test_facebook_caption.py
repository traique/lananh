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


@pytest.mark.parametrize(
    "text",
    [
        "Áo thun nam 159K, nhập mã giảm 20K https://s.shopee.vn/a",
        "Son kem lì 89k săn voucher 0h nha https://s.shopee.vn/a",
        "Nồi chiên 1tr2 lưu mã giảm thêm 100k https://s.shopee.vn/a",
        "Quạt mini 1.299.000 nhập mã giảm https://s.shopee.vn/a",
        "Váy hoa nhí, lưu mã giảm 15% https://shopee.vn/vay-hoa-i.123.456",
        "Tai nghe XYZ voucher 30k https://shopee.vn/product/11/22",
    ],
)
def test_product_posts_previously_dropped_are_now_kept(text):
    """Trước đây giá dạng "159K", "1tr2", "1.299.000" không được nhận là giá nên
    bài sản phẩm có nhắc mã giảm bị loại nhầm là "chỉ báo mã giảm giá"."""
    assert not caption.is_voucher_only(text)


@pytest.mark.parametrize(
    "text",
    [
        "Lưu mã freeship max 30k đơn 0đ https://s.shopee.vn/a",
        "Voucher hoàn xu 15% tối đa 100k https://s.shopee.vn/a",
        "Săn mã giảm 100k đơn 500k lúc 12h https://s.shopee.vn/a",
        "Mã giảm 10% giảm tối đa 50.000đ https://s.shopee.vn/a",
    ],
)
def test_pure_voucher_posts_are_still_dropped(text):
    assert caption.is_voucher_only(text)


def test_cta_varies_by_post_but_is_stable_per_post():
    ctas = {caption.build_caption(f"Deal {SOURCE}", True, seed=i) for i in range(6)}
    assert len(ctas) == len(caption.COMMENT_CTAS)
    assert caption.build_caption(f"Deal {SOURCE}", True, seed=3) == caption.build_caption(
        f"Deal {SOURCE}", True, seed=3
    )
    once = caption.build_caption(f"Deal {SOURCE}", True, seed=3)
    assert caption.build_caption(once, True, seed=4) == once  # không thêm câu dẫn lần 2


def test_build_comment_puts_each_link_on_its_own_line():
    text = caption.build_comment(["https://s.shopee.vn/a", "https://s.shopee.vn/b"], seed=1)
    assert text.splitlines()[1:] == ["https://s.shopee.vn/a", "https://s.shopee.vn/b"]
    assert caption.build_comment([], seed=1) == ""


def test_missing_numbers_ignores_separators():
    assert caption.missing_numbers("Giá 199.000đ giảm 15%", "chỉ 199,000 đ, giảm 15 %") == []
    assert caption.missing_numbers("Giá 199k, 2 màu", "giá 189k, 2 màu") == ["199"]


@pytest.mark.asyncio
async def test_rewrite_retries_once_when_a_price_is_changed(monkeypatch):
    ask = AsyncMock(
        side_effect=[
            SimpleNamespace(text="Áo đẹp giá 189k"),
            SimpleNamespace(text="Dưới đây là bài viết lại:\n\n**Áo đẹp** chỉ 199k"),
        ]
    )
    monkeypatch.setattr(caption.orchestrator, "ask", ask)
    assert await caption.rewrite_caption(f"Áo đẹp 199k {SOURCE}") == ("Áo đẹp chỉ 199k", True)
    assert "199" in ask.await_args_list[1].args[0].split("LƯU Ý")[1]


@pytest.mark.asyncio
async def test_rewrite_keeps_original_when_ai_keeps_inventing_numbers(monkeypatch):
    ask = AsyncMock(return_value=SimpleNamespace(text="Áo đẹp giá 189k"))
    monkeypatch.setattr(caption.orchestrator, "ask", ask)
    assert await caption.rewrite_caption(f"Áo đẹp 199k {SOURCE}") == ("Áo đẹp 199k", False)
    assert ask.await_count == 2


def test_rewrite_prompt_asks_for_human_voice_without_cliches():
    prompt = caption._REWRITE_PROMPT
    assert "người thật" in prompt and "Siêu phẩm" in prompt and "Markdown" in prompt


# Bài thật từ nhóm Zalo (10/2026) lọt bộ lọc cũ: "canh mã/back" theo khung giờ.
REAL_VOUCHER_POSTS = [
    "💸15H BACK SVIP 25% MAX 999K/300K https://s.shopee.vn/5fpPUonmi1",
    "💸15H BACK MXH 30% 50% ÁP TOÀN SÀN (không cần đổi link)\n► Mã FB 30% tối đa 300k đơn 50K\n"
    "► Mã Instagram 50% tối đa 50K/50K https://s.shopee.vn/5fpPUbs8sj",
    "💸15H CANH BACK Mã Trendy\n► Áp list: https://s.shopee.vn/8KqAfYyzqI https://s.shopee.vn/2VsNiqJlAc",
    "💸15H BACK MÃ BÁCH HÓA https://s.shopee.vn/7AeDHTeF5m",
    "15H CANH BACK LOẠT MÃ EXTRA CŨNG NGON\n🔥 https://s.shopee.vn/9peySmH6Wp",
]


@pytest.mark.parametrize("text", REAL_VOUCHER_POSTS)
def test_real_back_voucher_posts_are_skipped(text):
    assert caption.skip_reason(text, True) == "chỉ báo mã giảm giá"


@pytest.mark.parametrize(
    "text",
    [
        "Balo back to school chống nước 199k https://s.shopee.vn/a",
        "Son kem lì màu đỏ gạch, mã màu 02, giá 89k https://s.shopee.vn/a",
        "Kem chống nắng mà giá chỉ 99k thôi https://s.shopee.vn/a",
        "Nồi chiên không dầu 5L 1tr2, áp mã giảm thêm https://s.shopee.vn/a",
        "Váy hoa nhí 15h sale https://shopee.vn/vay-hoa-i.123.456",
        "Tai nghe bluetooth chống ồn, pin 30 giờ https://s.shopee.vn/a",
    ],
)
def test_product_posts_with_similar_words_are_kept(text):
    assert caption.skip_reason(text, True) is None
