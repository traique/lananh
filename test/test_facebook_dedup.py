import io

from PIL import Image, ImageDraw

from services import facebook_dedup as dedup
from services.facebook_image import image_dhash

BASE = (
    "Áo khoác dù chống nắng siêu nhẹ, có 4 màu, size M đến XL, "
    "giá chỉ 159k https://s.shopee.vn/AbC123"
)


def _row(post_id, text, images=()):
    fp = dedup.build_fingerprint(text, list(images))
    return {
        "post_id": post_id,
        "text_hash": fp.text_hash,
        "product_keys": fp.product_keys,
        "image_hashes": fp.image_hashes,
        "folded_text": fp.folded_text,
    }


def _png(color, shape="box"):
    img = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(img)
    if shape == "box":
        draw.rectangle((60, 60, 300, 240), fill=color)
    else:
        draw.ellipse((150, 20, 390, 290), fill=color)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def test_same_text_with_different_spacing_case_and_accents_is_duplicate():
    candidate = dedup.build_fingerprint(
        "  AO KHOAC du chong nang sieu nhe, co 4 mau, size M den XL, gia chi 159k!!"
    )
    match = dedup.find_duplicate(candidate, [_row(9, BASE)])
    assert match and match.post_id == 9


def test_same_shopee_link_is_duplicate_even_with_new_caption():
    candidate = dedup.build_fingerprint("Đang sale nè mọi người https://s.shopee.vn/AbC123?ref=x")
    match = dedup.find_duplicate(candidate, [_row(4, BASE)])
    assert match and match.reason == "trùng sản phẩm/link Shopee"


def test_same_product_id_from_long_urls_is_duplicate():
    a = "Váy hoa https://shopee.vn/Vay-hoa-nhi-i.111.222?sp_atk=1"
    b = "Váy đẹp lắm https://shopee.vn/product/111/222"
    assert dedup.product_keys(a) == dedup.product_keys(b) == ["item:111:222"]
    assert dedup.find_duplicate(dedup.build_fingerprint(b), [_row(1, a)])


def test_short_link_paths_are_case_sensitive():
    assert dedup.product_keys("https://s.shopee.vn/AbC") != dedup.product_keys(
        "https://s.shopee.vn/abc"
    )


def test_near_identical_text_is_duplicate():
    edited = BASE.replace("có 4 màu", "có tới 4 màu") + " freeship"
    assert dedup.find_duplicate(dedup.build_fingerprint(edited), [_row(2, BASE.split("https")[0])])


def test_different_products_are_not_duplicates():
    other = "Quần jean nữ ống rộng cạp cao, 3 màu, giá 189k https://s.shopee.vn/Xyz789"
    assert dedup.find_duplicate(dedup.build_fingerprint(other), [_row(1, BASE)]) is None


def test_shared_banner_image_alone_is_not_duplicate():
    banner = image_dhash(_png("red"))
    other = "Quần jean nữ ống rộng cạp cao, 3 màu, giá 189k https://s.shopee.vn/Xyz789"
    candidate = dedup.build_fingerprint(other, [banner])
    assert dedup.find_duplicate(candidate, [_row(1, BASE, [banner])]) is None


def test_all_images_matching_is_duplicate():
    hashes = [image_dhash(_png("red")), image_dhash(_png("blue", "circle"))]
    candidate = dedup.build_fingerprint("Mẫu mới về nha", hashes)
    match = dedup.find_duplicate(candidate, [_row(6, "Bài khác hẳn", hashes)])
    assert match and match.reason == "toàn bộ ảnh trùng"


def test_dhash_is_stable_under_recompression():
    from services.facebook_image import compact_image

    original = _png("green")
    _, recompressed = compact_image("image/png", original)
    a, b = image_dhash(original), image_dhash(recompressed)
    assert a is not None and b is not None
    assert dedup._hamming(a, b) <= dedup.IMAGE_HAMMING_MAX


def test_short_text_has_no_text_hash():
    assert dedup.build_fingerprint("Hot nè").text_hash is None


def test_same_template_with_different_price_is_a_new_deal():
    repriced = BASE.replace("159k", "129k").split("https")[0]
    assert (
        dedup.find_duplicate(dedup.build_fingerprint(repriced), [_row(2, BASE.split("https")[0])])
        is None
    )


def test_same_seller_template_for_different_products_is_not_duplicate():
    a = "Hàng mới về nha mọi người, áo thun nam cotton co giãn, giá 99k, đủ size M L XL"
    b = "Hàng mới về nha mọi người, quần short kaki nam túi hộp, giá 99k, đủ size M L XL"
    assert dedup.find_duplicate(dedup.build_fingerprint(b), [_row(1, a)]) is None


# ─── Lọc trùng giữa các nhóm (bản 2) ─────────────────────────────────────────

DEAL = (
    "Nồi chiên không dầu 6 lít, lòng chống dính, hẹn giờ 60 phút, "
    "bảo hành 12 tháng, giá chỉ 899k, freeship toàn quốc"
)


def _group_row(post_id, text, group_id, *, images=(), sender_id="seller"):
    return {**_row(post_id, text, images), "group_id": group_id, "sender_id": sender_id}


def test_repost_in_another_group_with_extra_lines_is_duplicate():
    repost = (
        "Chào cả nhà nhóm Săn Sale, hôm nay có deal ngon nè!\n"
        + DEAL
        + "\nVào nhóm để nhận thêm mã nha"
    )
    candidate = dedup.build_fingerprint(repost, group_id="g2", sender_id="khac")

    match = dedup.find_duplicate(candidate, [_group_row(3, DEAL, "g1")])

    assert match is not None
    assert match.post_id == 3
    assert match.reason == dedup.REASON_CONTAINED
    assert match.group_id == "g1"
    assert match.cross_group is True


def test_same_group_duplicate_is_not_marked_cross_group():
    candidate = dedup.build_fingerprint(DEAL + "!!", group_id="g1")

    match = dedup.find_duplicate(candidate, [_group_row(3, DEAL, "g1")])

    assert match is not None and match.cross_group is False


def test_repost_with_extra_lines_but_new_price_is_a_new_deal():
    repost = "Chào cả nhà, deal mới nè!\n" + DEAL.replace("899k", "799k") + "\nVào nhóm nha"
    candidate = dedup.build_fingerprint(repost, group_id="g2")

    assert dedup.find_duplicate(candidate, [_group_row(3, DEAL, "g1")]) is None


def test_short_generic_greeting_contained_in_old_post_is_not_duplicate():
    greeting = "Hàng mới về nha cả nhà ơi, giá 899k"
    candidate = dedup.build_fingerprint(greeting, group_id="g2")

    assert dedup.find_duplicate(candidate, [_group_row(3, DEAL, "g1")]) is None


def test_same_seller_reposting_photo_with_new_intro_in_another_group_is_duplicate():
    photo = image_dhash(_png("purple"))
    first = "Nồi chiên 6 lít lòng chống dính, bảo hành 12 tháng, giá 899k, ship nhanh"
    again = "Mọi người ơi nồi chiên 6 lít lòng chống dính đang rẻ lắm, chỉ 899k thôi nha"
    candidate = dedup.build_fingerprint(again, [photo], group_id="g2", sender_id="seller")

    match = dedup.find_duplicate(candidate, [_group_row(5, first, "g1", images=[photo])])

    assert match is not None and match.reason == dedup.REASON_SAME_SENDER
    assert match.cross_group is True


def test_other_member_reusing_seller_photo_with_different_text_is_not_duplicate():
    photo = image_dhash(_png("purple"))
    first = "Nồi chiên 6 lít lòng chống dính, bảo hành 12 tháng, giá 899k, ship nhanh"
    again = "Mọi người ơi nồi chiên 6 lít lòng chống dính đang rẻ lắm, chỉ 899k thôi nha"
    candidate = dedup.build_fingerprint(again, [photo], group_id="g2", sender_id="nguoi-khac")

    assert dedup.find_duplicate(candidate, [_group_row(5, first, "g1", images=[photo])]) is None


def test_same_seller_photo_with_new_price_is_a_new_deal():
    photo = image_dhash(_png("purple"))
    first = "Nồi chiên 6 lít lòng chống dính, bảo hành 12 tháng, giá 899k, ship nhanh"
    again = "Mọi người ơi nồi chiên 6 lít lòng chống dính đang rẻ lắm, còn 699k thôi nha"
    candidate = dedup.build_fingerprint(again, [photo], group_id="g2", sender_id="seller")

    assert dedup.find_duplicate(candidate, [_group_row(5, first, "g1", images=[photo])]) is None


def test_slightly_different_recompressed_photo_with_similar_text_is_duplicate():
    photo = image_dhash(_png("orange"))
    near = photo ^ 0b1111111  # lệch 7 bit: ảnh bị nén/thu nhỏ lại khi chuyển tiếp
    text = DEAL.replace("freeship toàn quốc", "")
    candidate = dedup.build_fingerprint(text + " ship hoả tốc", [near], group_id="g2")

    match = dedup.find_duplicate(candidate, [_group_row(8, DEAL, "g1", images=[photo])])

    assert match is not None
    assert match.post_id == 8


def test_price_keys_reads_vietnamese_money_formats():
    text = dedup.normalize_text(
        "Áo 159k, giá gốc 1.299.000đ, còn 1tr2 hoặc 2tr500, 199000 vnd, đơn 0đ, 1 triệu 2 màu"
    )

    assert dedup.price_keys(text) == {159_000, 1_299_000, 1_200_000, 2_500_000, 199_000, 1_000_000}


def test_index_compares_queue_posts_only_with_earlier_posts():
    rows = [_group_row(9, DEAL, "g2"), _group_row(4, DEAL, "g1")]
    index = dedup.DedupIndex(rows)
    candidate = dedup.build_fingerprint(DEAL, group_id="g2")

    assert index.find(candidate, before_post_id=9).post_id == 4
    assert index.find(candidate, before_post_id=4) is None
    assert index.find(candidate, exclude={9}).post_id == 4


def test_old_rows_without_group_columns_still_match():
    candidate = dedup.build_fingerprint(DEAL, group_id="g2")

    match = dedup.find_duplicate(candidate, [_row(2, DEAL)])

    assert match is not None and match.group_id is None and match.cross_group is False


def test_scan_of_many_rows_is_fast():
    import time

    rows = [
        _group_row(i, f"Sản phẩm số {i} " + "mô tả dài " * 60 + f" giá {i}k", f"g{i % 5}")
        for i in range(800)
    ]
    candidate = dedup.build_fingerprint(DEAL, group_id="g9")
    started = time.perf_counter()

    assert dedup.find_duplicate(candidate, rows) is None
    assert time.perf_counter() - started < 1.0
