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
