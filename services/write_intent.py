"""Ý định GHI dữ liệu của 1 tin nhắn (nhắc việc, ghi chú, chi tiêu), nhận ra bằng
regex - module nhỏ, không kéo theo router tool (services/tools.py) để chỗ khác
(bắt khoản chi từ chat, services/expense_service.py) dùng chung được.

So trên chữ thường CÒN DẤU; chỉ khi cả câu gõ không dấu mới so bản không dấu:
fold("nhạc") == fold("nhắc") == "nhac".
"""

from __future__ import annotations

import re

from core.text_normalize import fold, nfc

_PRONOUNS = r"anh|em|tôi|mình|giúp|giùm|chị|cô|chú|bác|con|cháu|tui|tớ|bố|mẹ|với|ba|má"
_PRONOUNS_PLAIN = r"anh|em|toi|minh|giup|gium|chi|co|chu|bac|con|chau|tui|to"
# (loại, mẫu trên chữ thường CÒN DẤU, mẫu khi cả câu gõ không dấu).
SIDE_EFFECT_INTENTS = (
    (
        "reminder",
        re.compile(
            rf"\bnhắc (?:nhở )?(?:{_PRONOUNS})\b|\bhẹn giờ\b|\bđặt (?:lịch|nhắc|báo thức)\b"
            r"|\bremind\b|\b(?:huỷ|hủy|xoá|xóa|bỏ) (?:cái )?nhắc\b"
        ),
        re.compile(
            rf"\bnhac (?:nho )?(?:{_PRONOUNS_PLAIN})\b|\bhen gio\b|\bdat (?:lich|nhac|bao thuc)\b"
            r"|\bremind\b|\b(?:huy|xoa) (?:cai )?nhac\b"
        ),
    ),
    (
        "note",
        re.compile(
            r"\bghi chú (?:giúp|giùm|lại|cho|vào|là)\b|\bghi chú\s*:|\bghi lại\b"
            r"|\bnote (?:lại|giúp|giùm)\b|\blưu lại\b"
        ),
        re.compile(r"\bghi chu (?:giup|gium|lai|cho|vao|la)\b|\bghi lai\b|\bnote (?:lai|giup)\b"),
    ),
    (
        "expense",
        re.compile(r"\b(?:ghi|thêm|lưu) (?:khoản |lại )?chi(?: tiêu)?\b"),
        re.compile(r"\b(?:ghi|them|luu) (?:khoan |lai )?chi(?: tieu)?\b"),
    ),
)
# Câu nhờ NHẮC / NHỚ (rộng hơn ý định nhắc việc ở trên): "mai nhắc anh...", "nhắc
# em mai...", "nhớ giúp anh ...". Dùng để KHÔNG bắt nhầm thành khoản chi.
_REMIND_OR_REMEMBER_RE = re.compile(
    r"\bnhắc\b|\bhẹn\b|\bnhớ (?:giúp|giùm|hộ|là|nhé|nha)\b|\bghi nhớ\b"
)
_REMIND_OR_REMEMBER_PLAIN_RE = re.compile(
    rf"\bnhac (?:nho |lai )?(?:{_PRONOUNS_PLAIN}|me|bo|viec)\b|\bhen (?:gio|lich)\b"
    r"|\bnho (?:giup|gium|ho|la|nhe|nha)\b|\bghi nho\b"
)


def _lower(text: str) -> tuple[str, bool]:
    low = nfc(text or "").lower()
    return low, fold(low) == low


def side_effect_intent(user_text: str) -> str:
    """ "reminder" | "note" | "expense" | "" - người dùng muốn bot GHI gì đó."""
    low, plain = _lower(user_text)
    for kind, accented, unaccented in SIDE_EFFECT_INTENTS:
        if (unaccented if plain else accented).search(low):
            return kind
    return ""


def asks_remind_or_remember(user_text: str) -> bool:
    """Câu nhờ nhắc việc / ghi chú / nhớ giúp (kể cả khi có kèm số tiền)."""
    if side_effect_intent(user_text) in ("reminder", "note"):
        return True
    low, plain = _lower(user_text)
    return bool((_REMIND_OR_REMEMBER_PLAIN_RE if plain else _REMIND_OR_REMEMBER_RE).search(low))
