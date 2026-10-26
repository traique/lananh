"""Chặn bí mật trước MỌI lần ghi trí nhớ (fact, highlight, ghi chú phiên, tóm tắt).

Người dùng hay nhắn kiểu "mật khẩu wifi là ...", "OTP 123456", "stk 0123...".
Trước đây bộ trích xuất không có luật nào ngăn chúng thành fact lưu plaintext
trong user_facts và được chèn lại vào prompt ở các phiên sau. Module này:

- ``find_secret``: nhận diện bí mật bằng regex NEO THEO TỪ KHOÁ (otp, mật khẩu/
  mk/pass, stk/số tài khoản, cccd/cmnd, thẻ + kiểm Luhn, tiền tố API key). Cố ý
  KHÔNG bắt dãy số trần: bot chứng khoán đầy giá, khối lượng, số điện thoại.
- ``redact``: thay đoạn bí mật bằng nhãn "[đã ẩn ...]" (cửa sổ hội thoại gửi
  bộ trích xuất, tóm tắt/ghi chú phiên).
- ``clean``: bỏ ký tự vô hình/homoglyph (core.untrusted.sanitize) trước khi lưu.

Chỉ là lớp giảm thiểu cho trí nhớ dài hạn: chat_messages (200 dòng/người) và
bảng prompts (telemetry) vẫn giữ nguyên văn tin nhắn như trước.
"""

from __future__ import annotations

import re

from core import untrusted

_SEP = r"\s*(?:là|la|:|=|-)\s*"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "mã OTP",
        re.compile(
            r"(?<!\w)(?:otp|mã xác (?:thực|nhận|minh)|ma xac (?:thuc|nhan|minh)|"
            r"verification code|mã kích hoạt|ma kich hoat)\b\D{0,20}\d{4,8}",
            re.IGNORECASE,
        ),
    ),
    (
        "mật khẩu",
        re.compile(
            r"(?<!\w)(?:mật khẩu|mat khau|password|passwd|pass|pwd)\b"
            r"[^\n]{0,30}?" + _SEP + r"\S{3,}",
            re.IGNORECASE,
        ),
    ),
    # "mk"/"pin" chỉ khi có dấu tách rõ ("mk: abc123", "pin là 1234") - "mk" còn
    # là cách viết tắt của "mình".
    ("mật khẩu", re.compile(r"(?<!\w)(?:mk|pin)" + _SEP + r"\S{3,}", re.IGNORECASE)),
    (
        "số tài khoản",
        re.compile(
            r"(?<!\w)(?:stk|số tài khoản|so tai khoan|số tk|so tk|account number|"
            r"tài khoản ngân hàng|tai khoan ngan hang)\b\D{0,25}\d[\d .-]{5,22}\d",
            re.IGNORECASE,
        ),
    ),
    (
        "số giấy tờ tuỳ thân",
        re.compile(
            r"(?<!\w)(?:cccd|cmnd|căn cước|can cuoc|chứng minh(?: nhân dân| thư)?|"
            r"chung minh(?: nhan dan| thu)?|hộ chiếu|ho chieu|passport)\b\D{0,25}[A-Za-z]?\d{8,12}",
            re.IGNORECASE,
        ),
    ),
    (
        "khoá bí mật",
        re.compile(
            r"(?<!\w)(?:seed phrase|private key|recovery phrase|khoá bí mật|khóa bí mật|"
            r"khoa bi mat|cụm từ khôi phục|cum tu khoi phuc)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "API key",
        re.compile(
            r"(?<![\w-])(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|"
            r"gsk_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_\w{30,}|"
            r"xox[baprs]-[\w-]{10,}|AKIA[0-9A-Z]{16}|\d{8,10}:[A-Za-z0-9_-]{35})"
        ),
    ),
)
_CARD_RE = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")
_CARD_KEYWORD_RE = re.compile(
    r"(?<!\w)(?:thẻ|the tin dung|card|visa|master ?card|jcb|atm|napas)\b", re.IGNORECASE
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    has_keyword = bool(_CARD_KEYWORD_RE.search(text))
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if not 13 <= len(digits) <= 19 or not _luhn_ok(digits):
            continue
        # 15-19 chữ số qua được Luhn gần như chắc chắn là số thẻ; 13-14 thì cần từ khoá.
        if len(digits) >= 15 or has_keyword:
            spans.append(match.span())
    return spans


def find_secret(text: str) -> str | None:
    """Loại bí mật đầu tiên tìm thấy (để báo người dùng), hoặc None."""
    text = text or ""
    for label, pattern in _PATTERNS:
        if pattern.search(text):
            return label
    if _card_spans(text):
        return "số thẻ"
    return None


def redact(text: str) -> str:
    """Thay mọi đoạn bí mật bằng "[đã ẩn <loại>]"."""
    text = text or ""
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[đã ẩn {label}]", text)
    for start, end in reversed(_card_spans(text)):
        text = text[:start] + "[đã ẩn số thẻ]" + text[end:]
    return text


def clean(text: str) -> str:
    """Bỏ ký tự vô hình, gấp homoglyph, gộp khoảng trắng."""
    return re.sub(r"\s+", " ", untrusted.sanitize(text or "")).strip()


# Key fact nói thẳng là bí mật ("mat_khau_wifi", "so_tai_khoan", "otp"...): bỏ
# luôn dù giá trị đứng riêng ("abc12345") trông vô hại.
_SECRET_KEY_RE = re.compile(
    r"\b(?:mat khau|password|passwd|pwd|otp|ma xac thuc|so the|the tin dung|"
    r"stk|so tai khoan|so tk|cccd|cmnd|can cuoc|ho chieu|passport|api key|"
    r"private key|seed phrase|khoa bi mat)\b"
)


def names_secret(text: str) -> bool:
    """Văn bản nêu thẳng loại bí mật ("mật khẩu wifi ...", "stk ...") dù không có
    dấu tách để find_secret bắt được giá trị."""
    from core.text_normalize import fold

    return bool(_SECRET_KEY_RE.search(fold(text or "")))


def is_secret_fact(key: str, value: str) -> bool:
    """Fact {key, value} có phải bí mật không (xét cả key lẫn giá trị)."""
    from core.text_normalize import fold

    key_text = fold((key or "").replace("_", " "))
    if _SECRET_KEY_RE.search(key_text):
        return True
    return find_secret(f"{key_text}: {value}") is not None or find_secret(value) is not None


def safe_memory_text(text: str) -> str | None:
    """Văn bản đã làm sạch nếu được phép lưu vào trí nhớ; None nếu chứa bí mật."""
    cleaned = clean(text)
    if not cleaned or find_secret(cleaned):
        return None
    return cleaned
