"""Tạo ảnh xem trước khi chia sẻ link (static/og-image.png, 1200x630 - cỡ Facebook,
Zalo, X khuyên dùng) từ logo nền tối static/logo-dark.webp và một dòng giới thiệu, cùng
biểu tượng vuông static/icon.png (512x512: apple-touch-icon, logo trong JSON-LD - favicon
64px quá nhỏ cho hai chỗ này).

Chạy tay một lần khi đổi logo hoặc đổi câu giới thiệu (cần Pillow + phông có dấu
tiếng Việt, mặc định DejaVu Sans):
    python scripts/make_og_image.py static [đường-dẫn-phông.ttf]
"""

import sys

from PIL import Image, ImageDraw, ImageFont

out = sys.argv[1]
font_path = sys.argv[2] if len(sys.argv) > 2 else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
W, H = 1200, 630
# Nền than + khối xanh đêm bo góc như favicon; chữ dùng màu chữ/chữ phụ của giao diện tối.
img = Image.new("RGB", (W, H), (14, 16, 15))
draw = ImageDraw.Draw(img)
draw.rounded_rectangle((48, 48, W - 48, H - 48), radius=40, fill=(10, 61, 42))
logo = Image.open(f"{out}/logo-dark.webp").convert("RGBA")
scale = 520 / logo.width
logo = logo.resize((520, round(logo.height * scale)), Image.LANCZOS)
img.paste(logo, ((W - logo.width) // 2, 170), logo)
lines = (
    ("Lịch âm · Thời tiết · Xổ số · Giá vàng", 44, (238, 241, 238)),
    ("Tử vi · Thần số học · Kỳ Môn - tính bằng thuật toán", 30, (185, 243, 214)),
)
y = 170 + logo.height + 56
for text, size, color in lines:
    font = ImageFont.truetype(font_path, size)
    width = draw.textlength(text, font=font)
    draw.text(((W - width) / 2, y), text, font=font, fill=color)
    y += size + 22
img.save(f"{out}/og-image.png", "PNG", optimize=True)
print("og-image.png", img.size)

# Biểu tượng vuông: nền xanh đêm kín (iOS tự bo góc), logo giữa, chừa lề an toàn.
icon = Image.new("RGB", (512, 512), (10, 61, 42))
mark = logo.resize((400, round(logo.height * 400 / logo.width)), Image.LANCZOS)
icon.paste(mark, ((512 - mark.width) // 2, (512 - mark.height) // 2), mark)
icon.save(f"{out}/icon.png", "PNG", optimize=True)
print("icon.png", icon.size)
