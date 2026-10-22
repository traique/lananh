"""Tạo logo web (static/logo-light.webp, logo-dark.webp, favicon.png) từ ảnh
logo LCTA nền đen: tách nền theo độ sáng, bản nền sáng tô lại vàng kim đậm.

Chạy tay một lần khi đổi logo (cần numpy + Pillow):
    python scripts/make_site_logo.py <logo-nen-den.png> static
"""

import sys

import numpy as np
from PIL import Image, ImageDraw

src = Image.open(sys.argv[1]).convert("RGB")
out = sys.argv[2]
rgb = np.asarray(src).astype(np.float32) / 255.0
lum = rgb.max(axis=2)


def build(alpha_floor, alpha_gamma, darken, sat):
    a = np.clip((lum - alpha_floor) / (1 - alpha_floor), 0, 1) ** alpha_gamma
    safe = np.maximum(lum, 1e-4)[..., None]
    color = np.clip(rgb / safe, 0, 1)  # bỏ nhân trước alpha: màu thuần của từng điểm
    gray = color.mean(axis=2, keepdims=True)
    color = np.clip(gray + (color - gray) * sat, 0, 1) * darken
    img = np.dstack([color, a[..., None]])
    return Image.fromarray((img * 255).round().astype(np.uint8), "RGBA")


def build_light():
    """Nền sáng: giữ vân sáng/tối của ảnh gốc, tô lại theo dải vàng kim ấm
    (nâu vàng #553705 -> vàng #b88622) để đủ tương phản trên nền trắng."""
    a = np.clip((lum - 0.22) / 0.78, 0, 1) ** 1.3
    safe = np.maximum(lum, 1e-4)[..., None]
    shade = np.clip(rgb / safe, 0, 1).mean(axis=2)  # 0..1 trong vùng chữ
    shade = (shade - shade.min()) / max(shade.max() - shade.min(), 1e-4)
    lo, hi = np.array([0x55, 0x37, 0x05]) / 255, np.array([0xB8, 0x86, 0x22]) / 255
    color = lo + (hi - lo) * (shade[..., None] ** 0.8)
    img = np.dstack([color, a[..., None]])
    return Image.fromarray((img * 255).round().astype(np.uint8), "RGBA")


dark = build(alpha_floor=0.03, alpha_gamma=1.0, darken=1.0, sat=1.0)  # nền tối: giữ hào quang
light = build_light()
bbox = dark.getchannel("A").point(lambda v: 255 if v > 40 else 0).getbbox()
pad = 12
bbox = (
    max(bbox[0] - pad, 0),
    max(bbox[1] - pad, 0),
    min(bbox[2] + pad, src.width),
    min(bbox[3] + pad, src.height),
)
print("bbox", bbox)
for name, im in (("dark", dark), ("light", light)):
    im = im.crop(bbox)
    h = 96  # hiển thị cao 48px, 2x cho màn retina
    im = im.resize((round(im.width * h / im.height), h), Image.LANCZOS)
    im.save(f"{out}/logo-{name}.webp", "WEBP", quality=90, method=6)
    print(name, im.size)
# favicon vuông 64px (bản nền tối đặt trên nền đen bo góc để rõ ở mọi tab)
mark = dark.crop(bbox)
side = max(mark.size)
sq = Image.new("RGBA", (side, side), (17, 17, 17, 255))
sq.alpha_composite(mark, ((side - mark.width) // 2, (side - mark.height) // 2))
mask = Image.new("L", (side, side), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=side // 5, fill=255)
sq.putalpha(mask)
sq.resize((64, 64), Image.LANCZOS).save(f"{out}/favicon.png", optimize=True)
