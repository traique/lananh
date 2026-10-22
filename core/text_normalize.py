"""Chuẩn hoá text tiếng Việt ở BOUNDARY nhận tin nhắn (Telegram/Zalo/Zoom).

Vì sao cần: chữ Việt có dấu tồn tại ở 2 dạng Unicode tương đương:
- NFC (dựng sẵn): "ế" = 1 codepoint (U+1EBF)
- NFD (phân rã):   "ế" = 3 codepoint (U+0065 U+0302 U+0301)

Hai dạng này hiển thị giống hệt nhau nhưng so sánh chuỗi (==, so khớp lệnh,
tìm kiếm trong memory_service, match ticker cổ phiếu) sẽ SAI nếu một bên NFC
một bên NFD. Nguồn phổ biến sinh NFD: bàn phím/macOS, copy-paste từ PDF, một
số input method Zalo/iOS.

Chuẩn hoá 1 lần duy nhất, ngay khi nhận text từ người dùng - KHÔNG chuẩn hoá
lại ở các bước xử lý sau, để tránh rải logic này khắp nơi.

Không bao giờ strip dấu hay ASCII-hoá tên người dùng ở đây - đó là hành vi
khác hoàn toàn (làm slug), không phải chuẩn hoá encoding.
"""

import re
import unicodedata


def nfc(text: str | None) -> str:
    """Chuẩn hoá text về NFC. An toàn với None/chuỗi rỗng, không đổi nghĩa
    hay xoá ký tự nào, chỉ gộp lại đúng 1 cách biểu diễn Unicode.
    """
    if not text:
        return ""
    return unicodedata.normalize("NFC", text)


def fold(text: str) -> str:
    """Chữ thường, BỎ DẤU tiếng Việt, gộp khoảng trắng - chỉ dùng để SO KHỚP
    (nhận diện câu hỏi lịch/thời tiết, tên địa danh), không dùng để hiển thị
    hay lưu. Khác nfc(): hàm này cố ý làm mất thông tin dấu."""
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text.lower()).strip()
