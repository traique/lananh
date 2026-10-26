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
- ``prompt_secret_spans``: bản CHẶT hơn cho prompt gửi provider
  (services/prompt_redaction.py): cùng các từ khoá neo nhưng chỉ khoanh PHẦN GIÁ
  TRỊ (giữ "mật khẩu wifi là" cho model hiểu câu) và thêm luật chống báo nhầm
  (giá trị phải trông như mật khẩu, không phải số tiền/đơn vị; thẻ không có từ
  khoá phải viết theo nhóm số) - ẩn nhầm làm hỏng 1 câu hỏi bình thường.

Chỉ là lớp giảm thiểu cho trí nhớ dài hạn: chat_messages (200 dòng/người) và
bảng prompts (telemetry) vẫn giữ nguyên văn tin nhắn như trước.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from core import untrusted

_SEP = r"\s*(?:là|la|:|=|-)\s*"
# Từ khoá neo - dùng chung cho chặn LƯU trí nhớ (_PATTERNS) và ẩn trong prompt
# gửi provider (_PROMPT_RULES).
_OTP_KW = (
    r"otp|mã xác (?:thực|nhận|minh)|ma xac (?:thuc|nhan|minh)|"
    r"verification code|mã kích hoạt|ma kich hoat"
)
_PASSWORD_KW = r"mật khẩu|mat khau|password|passwd|pwd"
_ACCOUNT_KW = (
    r"stk|số tài khoản|so tai khoan|số tk|so tk|account number|"
    r"tài khoản ngân hàng|tai khoan ngan hang"
)
_ID_KW = (
    r"cccd|cmnd|căn cước|can cuoc|chứng minh(?: nhân dân| thư)?|"
    r"chung minh(?: nhan dan| thu)?|hộ chiếu|ho chieu|passport"
)
_KEY_KW = (
    r"seed phrase|private key|recovery phrase|khoá bí mật|khóa bí mật|"
    r"khoa bi mat|cụm từ khôi phục|cum tu khoi phuc"
)
_API_KEY_RE = re.compile(
    r"(?<![\w-])(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|"
    r"gsk_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_\w{30,}|"
    r"xox[baprs]-[\w-]{10,}|AKIA[0-9A-Z]{16}|\d{8,10}:[A-Za-z0-9_-]{35})"
)
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mã OTP", re.compile(rf"(?<!\w)(?:{_OTP_KW})\b\D{{0,20}}\d{{4,8}}", re.IGNORECASE)),
    (
        "mật khẩu",
        re.compile(
            rf"(?<!\w)(?:{_PASSWORD_KW}|pass)\b[^\n]{{0,30}}?" + _SEP + r"\S{3,}",
            re.IGNORECASE,
        ),
    ),
    # "mk"/"pin" chỉ khi có dấu tách rõ ("mk: abc123", "pin là 1234") - "mk" còn
    # là cách viết tắt của "mình".
    ("mật khẩu", re.compile(r"(?<!\w)(?:mk|pin)" + _SEP + r"\S{3,}", re.IGNORECASE)),
    (
        "số tài khoản",
        re.compile(rf"(?<!\w)(?:{_ACCOUNT_KW})\b\D{{0,25}}\d[\d .-]{{5,22}}\d", re.IGNORECASE),
    ),
    (
        "số giấy tờ tuỳ thân",
        re.compile(rf"(?<!\w)(?:{_ID_KW})\b\D{{0,25}}[A-Za-z]?\d{{8,12}}", re.IGNORECASE),
    ),
    ("khoá bí mật", re.compile(rf"(?<!\w)(?:{_KEY_KW})\b", re.IGNORECASE)),
    ("API key", _API_KEY_RE),
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


# ─── Bản dành cho prompt gửi provider (services/prompt_redaction.py) ─────────
# Khác find_secret/redact (chặn LƯU trí nhớ - bắt nhầm chỉ mất 1 fact): ở đây ẩn
# nhầm làm hỏng câu hỏi bình thường, nên mỗi luật khoanh đúng nhóm giá trị "v"
# (giữ từ khoá cho model hiểu câu) và có bộ kiểm giá trị riêng.

_PROMPT_SEP = r"[ \t]*(?:là|la|:|=|-)[ \t]*"
# Đuôi số tiền/đơn vị ngay sau giá trị: "pass - 15tr", "stk có 1500000 đồng".
_UNIT_TAIL_RE = re.compile(
    r"[ \t]*(?:k|tr|triệu|trieu|tỷ|ty|nghìn|nghin|ngàn|ngan|đ|đồng|dong|vnd|vnđ|usd|"
    r"củ|cu|lít|lit|mah|wh|kg|km|gb|mb|tb|giây|giay|phút|phut|giờ|gio|cp|cổ phiếu|co phieu)"
    r"(?!\w)",
    re.IGNORECASE,
)
_MONEY_VALUE_RE = re.compile(r"^\d+(?:[.,]\d+)*(?:k|tr|m|b|đ|d|%|mah|gb|tb|kg|km|h)?$", re.I)
# Chữ không dấu hay gặp ngay sau "mật khẩu ... là" trong câu hỏi thường.
_PLAIN_WORDS = frozenset(
    {
        "khong",
        "thuong",
        "nhung",
        "khoang",
        "chuong",
        "truong",
        "nguyen",
        "phuong",
        "required",
        "optional",
        "incorrect",
        "invalid",
    }
)
_TRAILING_PUNCT = ".,;:!?)]}\"'»…"


def _password_like(value: str) -> bool:
    """Giá trị sau "mật khẩu ... là" trông như mật khẩu: ASCII liền (không dấu
    tiếng Việt), >= 4 ký tự, có chữ số/ký hiệu/hoa xen giữa, hoặc >= 6 chữ không
    phải từ thường gặp; không phải số tiền/đơn vị ("15tr", "5000mAh")."""
    if len(value) < 4 or not value.isascii() or not value.isprintable():
        return False
    if _MONEY_VALUE_RE.match(value):
        return value.isdigit() and len(value) >= 6  # mật khẩu toàn số "12345678"
    has_digit = any(c.isdigit() for c in value)
    has_symbol = any(not c.isalnum() for c in value)
    mixed_case = any(c.isupper() for c in value[1:]) and any(c.islower() for c in value)
    if has_digit or has_symbol or mixed_case:
        return True
    return len(value) >= 6 and value.lower() not in _PLAIN_WORDS


def _pin_like(value: str) -> bool:
    return value.isdigit() and 4 <= len(value) <= 8


def _seed_like(value: str) -> bool:
    return len(value.strip()) >= 16


# "pass" chỉ khi đi liền dấu tách hoặc 1 từ chỉ tài khoản ("pass wifi: ..."):
# "pass lại xe - 15tr" (bán lại đồ) không phải mật khẩu.
_PASS_TARGETS = (
    r"wifi|wf|word|mail|email|gmail|fb|facebook|zalo|bank|app|máy|may|laptop|"
    r"tk|tài khoản|tai khoan|admin|router|modem"
)
_PROMPT_RULES: tuple[tuple[str, re.Pattern[str], Callable[[str], bool] | None], ...] = (
    (
        "mã OTP",
        re.compile(rf"(?<!\w)(?:{_OTP_KW})\b[^\d\n]{{0,20}}?(?P<v>(?<!\d)\d{{4,8}})(?!\d)", re.I),
        None,
    ),
    (
        "mật khẩu",
        re.compile(
            rf"(?<!\w)(?:{_PASSWORD_KW})\b[^\n.?!]{{0,30}}?{_PROMPT_SEP}(?P<v>\S{{3,64}})", re.I
        ),
        _password_like,
    ),
    (
        "mật khẩu",
        re.compile(
            rf"(?<!\w)pass(?:[ \t]+(?:{_PASS_TARGETS}))?{_PROMPT_SEP}(?P<v>\S{{3,64}})", re.I
        ),
        _password_like,
    ),
    ("mật khẩu", re.compile(rf"(?<!\w)mk{_PROMPT_SEP}(?P<v>\S{{3,64}})", re.I), _password_like),
    ("mã PIN", re.compile(rf"(?<!\w)pin{_PROMPT_SEP}(?P<v>\S{{3,16}})", re.I), _pin_like),
    (
        "số tài khoản",
        re.compile(
            rf"(?<!\w)(?:{_ACCOUNT_KW})\b[^\d\n]{{0,25}}?"
            r"(?P<v>(?<![\d.,])\d[\d -]{4,22}\d)(?![\d.,]*\d)",
            re.I,
        ),
        None,
    ),
    (
        "số giấy tờ tuỳ thân",
        re.compile(
            rf"(?<!\w)(?:{_ID_KW})\b[^\d\n]{{0,25}}?"
            r"(?P<v>(?<![\w.,])[A-Za-z]?\d{8,12})(?![\d.,]*\d)",
            re.I,
        ),
        None,
    ),
    (
        "khoá bí mật",
        re.compile(
            rf"(?<!\w)(?:{_KEY_KW})\b[^\n]{{0,20}}?{_PROMPT_SEP}(?P<v>[A-Za-z0-9+/=_ -]{{16,600}})",
            re.I,
        ),
        _seed_like,
    ),
)
# Thẻ trong prompt: không có từ khoá thẻ ngay trước thì phải viết theo nhóm
# (4-4-4-4, 4-6-5) mới tính; dãy 13-19 số liền chỉ tính khi có từ khoá (vốn hoá,
# doanh thu 15 chữ số trong số liệu cổ phiếu không bị ẩn nhầm).
_GROUPED_CARD_RE = re.compile(
    r"(?<![\d.,])(?:\d{4}(?:[ -]\d{4}){2}[ -]\d{1,7}|\d{4}[ -]\d{6}[ -]\d{4,5})(?![\d.,]*\d)"
)
_PLAIN_CARD_RE = re.compile(r"(?<![\d.,])\d{13,19}(?![\d.,]*\d)")
_CARD_KEYWORD_BEFORE_RE = re.compile(
    r"(?<!\w)(?:thẻ|the tin dung|the atm|card|visa|master ?card|jcb|atm|napas)\b[^\d\n]{0,25}$",
    re.IGNORECASE,
)


def _prompt_card_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for regex, grouped in ((_GROUPED_CARD_RE, True), (_PLAIN_CARD_RE, False)):
        for match in regex.finditer(text):
            digits = re.sub(r"\D", "", match.group(0))
            if not 13 <= len(digits) <= 19 or not _luhn_ok(digits):
                continue
            before = text[max(0, match.start() - 40) : match.start()]
            if _CARD_KEYWORD_BEFORE_RE.search(before) or (grouped and len(digits) >= 15):
                spans.append(match.span())
    return spans


def prompt_secret_spans(text: str) -> list[tuple[int, int, str]]:
    """Các đoạn GIÁ TRỊ bí mật trong văn bản sắp gửi provider: (start, end, loại),
    đã sắp theo vị trí, không chồng nhau (đoạn bắt đầu sớm hơn/dài hơn thắng)."""
    text = text or ""
    found: list[tuple[int, int, str]] = []
    for label, pattern, check in _PROMPT_RULES:
        for match in pattern.finditer(text):
            value = match.group("v").rstrip(_TRAILING_PUNCT)
            start = match.start("v")
            end = start + len(value)
            if not value or (check is not None and not check(value)):
                continue
            if _UNIT_TAIL_RE.match(text, end):
                continue
            found.append((start, end, label))
    found.extend((m.start(), m.end(), "API key") for m in _API_KEY_RE.finditer(text))
    found.extend((start, end, "số thẻ") for start, end in _prompt_card_spans(text))
    found.sort(key=lambda span: (span[0], -span[1]))
    spans: list[tuple[int, int, str]] = []
    for span in found:
        if spans and span[0] < spans[-1][1]:
            continue
        spans.append(span)
    return spans
