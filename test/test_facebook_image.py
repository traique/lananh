import io

from PIL import Image

from services.facebook_image import brand_image


def _png(color, size=(400, 300)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def test_brand_image_adds_white_border_and_gold_logo_in_corner():
    mime, body = brand_image("image/png", _png((20, 40, 200)))
    image = Image.open(io.BytesIO(body))
    assert mime == "image/jpeg" and image.format == "JPEG"
    assert image.width > 400 and image.height > 300
    assert min(image.getpixel((0, 0))) > 240  # border
    mark = image.crop((image.width - 110, image.height - 45, image.width - 4, image.height - 4))
    clean = image.crop((4, image.height - 45, 110, image.height - 4))
    assert max(r for r, _, _ in mark.getdata()) > 150  # gold
    assert max(r for r, _, _ in clean.getdata()) < 60


def test_brand_image_changes_bytes_and_keeps_undecodable_input():
    original = _png((10, 120, 200))
    assert brand_image("image/png", original)[1] != original
    assert brand_image("image/jpeg", b"not an image") == ("image/jpeg", b"not an image")
