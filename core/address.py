"""Cách xưng hô riêng cho từng thành viên Zalo.

Persona mặc định gọi người dùng là "anh" và xưng "em" (chat_skill.yaml) - đúng
với chủ bot nhưng sai với mẹ, vợ, đồng nghiệp nữ... được pair vào Zalo. Chủ
bot đặt cách gọi cho từng người qua ``/zalopair <id> [tên] [chị|cô|...]``; giá
trị lưu ở cột zalo_users.address_form (migrations/024, CHECK đúng danh sách
dưới đây) và được dùng cho persona, lời chào khi pair, câu báo lỗi.

Danh sách đóng (whitelist) để cache persona có trần và CHECK của DB khớp code.
"""

from __future__ import annotations

import unicodedata

# Cách gọi người dùng -> Lan Anh tự xưng tương ứng.
ADDRESS_FORMS: dict[str, str] = {
    "anh": "em",
    "chị": "em",
    "cô": "cháu",
    "chú": "cháu",
    "bạn": "mình",
    "em": "chị",
}


def normalize(value: str | None) -> str | None:
    """Chuẩn hoá về 1 khoá của ADDRESS_FORMS (NFC, chữ thường); lạ/rỗng -> None."""
    if not value:
        return None
    form = unicodedata.normalize("NFC", value).strip().lower()
    return form if form in ADDRESS_FORMS else None


def self_word(form: str) -> str:
    """Lan Anh tự xưng gì khi gọi người dùng là ``form`` (form đã chuẩn hoá)."""
    return ADDRESS_FORMS[form]


def split_trailing_form(text: str) -> tuple[str, str | None]:
    """Tách cách xưng hô ở CUỐI phần tên của lệnh pair: "Mẹ cô" -> ("Mẹ", "cô").

    Chỉ nhận token viết thường đúng danh sách để tên riêng không bị cắt nhầm:
    "Tuấn Anh", "Mai Chi" vẫn là tên. Không có token hợp lệ -> (text, None)."""
    text = unicodedata.normalize("NFC", text or "").strip()
    head, _, last = text.rpartition(" ")
    if last in ADDRESS_FORMS:
        return head.strip(), last
    return text, None
