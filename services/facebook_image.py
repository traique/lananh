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
