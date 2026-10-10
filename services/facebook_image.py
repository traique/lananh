"""Brand Page photos (thin border + LCTA logo) so re-posted images don't match the source file."""

import io
import logging
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "lcta_logo.png"
LOGO_WIDTH_RATIO = 0.22
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _logo() -> Image.Image:
    return Image.open(LOGO_PATH).convert("RGBA")


def brand_image(mime_type: str, body: bytes) -> tuple[str, bytes]:
    try:
        with Image.open(io.BytesIO(body)) as source:
            rgba = ImageOps.exif_transpose(source).convert("RGBA")
        logo = _logo()
    except (OSError, ValueError):
        logger.warning("Không gắn được logo; đăng ảnh gốc.", exc_info=True)
        return mime_type, body

    flat = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba).convert("RGB")
    short_side = min(flat.size)
    framed = ImageOps.expand(flat, border=max(2, round(short_side * 0.012)), fill="white")

    width = max(60, round(framed.width * LOGO_WIDTH_RATIO))
    mark = logo.resize((width, round(logo.height * width / logo.width)), Image.LANCZOS)
    margin = max(6, round(framed.width * 0.025))
    canvas = framed.convert("RGBA")
    canvas.alpha_composite(mark, (framed.width - margin - mark.width, framed.height - margin - mark.height))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, "JPEG", quality=92)
    return "image/jpeg", out.getvalue()


# ─── Lưu trữ gọn cho Supabase free tier (500 MB) ─────────────────────────────

MAX_STORED_SIDE = 2048  # Facebook không hiển thị lớn hơn mức này
STORED_JPEG_QUALITY = 85


def compact_image(mime_type: str, body: bytes) -> tuple[str, bytes]:
    """Thu nhỏ (cạnh dài <= 2048px), bỏ EXIF, nén JPEG trước khi lưu DB.

    Ảnh điện thoại 2-4 MB thường còn 200-500 KB; chất lượng vẫn đủ cho Facebook
    (ảnh còn được gắn khung/logo bằng brand_image lúc đăng). Nếu bản nén không
    nhỏ hơn bản gốc hoặc ảnh lỗi thì giữ nguyên bản gốc.
    """
    try:
        with Image.open(io.BytesIO(body)) as source:
            # JPEG: giải mã thẳng ở độ phân giải gần 2048px, tiết kiệm nhiều RAM.
            source.draft("RGB", (MAX_STORED_SIDE, MAX_STORED_SIDE))
            image = ImageOps.exif_transpose(source)
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                image = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
            image = image.convert("RGB")
            image.thumbnail((MAX_STORED_SIDE, MAX_STORED_SIDE), Image.LANCZOS)
            out = io.BytesIO()
            image.save(out, "JPEG", quality=STORED_JPEG_QUALITY, optimize=True, progressive=True)
    except (OSError, ValueError, Image.DecompressionBombError):
        logger.warning("Không nén được ảnh; lưu bản gốc.", exc_info=True)
        return mime_type, body
    compressed = out.getvalue()
    if len(compressed) >= len(body):
        return mime_type, body
    return "image/jpeg", compressed


def image_dhash(body: bytes) -> int | None:
    """dHash 64-bit (signed để vừa cột BIGINT): ảnh giống nhau -> hash lệch ít bit."""
    try:
        with Image.open(io.BytesIO(body)) as source:
            source.draft("L", (64, 64))
            gray = ImageOps.exif_transpose(source).convert("L").resize((9, 8), Image.LANCZOS)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    pixels = gray.tobytes()  # 72 byte xám, theo hàng
    value = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            value = (value << 1) | (1 if left > right else 0)
    return value - (1 << 64) if value >= (1 << 63) else value
