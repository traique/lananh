"""Mã hoá đối xứng (Fernet) cho các giá trị nhạy cảm lưu trong bảng settings.
Dùng core.config.SETTINGS_ENC_KEY. Mọi thao tác ghi đều fail closed: thiếu/sai
khoá hoặc mã hoá lỗi phải dừng thay vì âm thầm lưu plaintext.

Prefix "enc:" PHẢI giữ nguyên: đây là scheme đang chạy thật trong production
(trước refactor nằm trong gemini_client._enc/_dec). Đổi prefix sẽ khiến các
giá trị đã mã hoá sẵn trong DB không giải mã được nữa sau khi deploy.

Xoay khoá: đặt khoá MỚI vào SETTINGS_ENC_KEY và khoá CŨ vào SETTINGS_ENC_KEY_OLD
(nhiều khoá cũ cách nhau dấu phẩy). MultiFernet mã hoá bằng khoá đầu, giải mã
thử lần lượt mọi khoá -> giá trị cũ vẫn đọc được, giá trị ghi mới dùng khoá mới.
Khi mọi giá trị đã được ghi lại (hoặc chạy ``rotate_value`` cho từng key), bỏ
SETTINGS_ENC_KEY_OLD đi.

SETTINGS_REJECT_PLAINTEXT=1: giá trị trong DB KHÔNG có prefix "enc:" bị từ chối
khi đọc (thay vì chấp nhận như dữ liệu cũ chưa mã hoá) - bật sau khi chắc chắn
mọi secret đã được mã hoá để chặn đường hạ cấp "ai ghi được plaintext vào
settings thì app vẫn dùng".
"""

from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from core import config

_PREFIX = "enc:"


def _build_fernet() -> MultiFernet | Fernet | None:
    if not config.SETTINGS_ENC_KEY:
        return None
    keys = [config.SETTINGS_ENC_KEY] + [
        k.strip() for k in (config.SETTINGS_ENC_KEY_OLD or "").split(",") if k.strip()
    ]
    try:
        fernets = [Fernet(k.encode()) for k in keys]
    except Exception as exc:
        raise RuntimeError(
            "SETTINGS_ENC_KEY/SETTINGS_ENC_KEY_OLD không hợp lệ; từ chối dùng plaintext."
        ) from exc
    return MultiFernet(fernets) if len(fernets) > 1 else fernets[0]


_fernet = _build_fernet()


def _require_fernet():
    if _fernet is None:
        raise RuntimeError("Thiếu SETTINGS_ENC_KEY; từ chối lưu settings dạng plaintext.")
    return _fernet


def encrypt(value: str) -> str:
    try:
        return _PREFIX + _require_fernet().encrypt(value.encode()).decode()
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("Mã hoá settings thất bại; không ghi plaintext.") from exc


def decrypt(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not value.startswith(_PREFIX):
        if value and config.SETTINGS_REJECT_PLAINTEXT:
            raise RuntimeError(
                "Settings chứa giá trị chưa mã hoá trong khi SETTINGS_REJECT_PLAINTEXT=1 - từ chối dùng."
            )
        # Giá trị cũ (từ trước khi bật SETTINGS_ENC_KEY) hoặc chưa cấu hình key.
        return value
    try:
        return _require_fernet().decrypt(value[len(_PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Không giải mã được settings: sai SETTINGS_ENC_KEY (kể cả SETTINGS_ENC_KEY_OLD) "
            "hoặc ciphertext hỏng."
        ) from exc


def _primary_fernet(fernet) -> Fernet | None:
    """Khoá đầu tiên (mới nhất) của MultiFernet; chính Fernet nếu chỉ có 1 khoá."""
    if isinstance(fernet, MultiFernet):
        inner = getattr(fernet, "_fernets", None)
        return inner[0] if inner else None
    return fernet


def needs_rotation(value: Optional[str]) -> bool:
    """True nếu ciphertext KHÔNG giải mã được bằng khoá mới nhất (tức đang dùng
    khoá cũ trong SETTINGS_ENC_KEY_OLD) - gọi rotate_value() để ghi lại."""
    if not value or not value.startswith(_PREFIX) or not isinstance(_fernet, MultiFernet):
        return False
    primary = _primary_fernet(_fernet)
    if primary is None:
        return False
    token = value[len(_PREFIX) :].encode()
    try:
        primary.decrypt(token)
        return False
    except InvalidToken:
        pass
    try:
        _fernet.decrypt(token)
        return True
    except InvalidToken:
        return False


def rotate_value(value: str) -> str:
    """Mã hoá lại ciphertext bằng khoá đầu tiên (mới nhất) mà không cần plaintext
    rời khỏi hàm này."""
    fernet = _require_fernet()
    if not value.startswith(_PREFIX):
        return encrypt(value)
    if isinstance(fernet, MultiFernet):
        return _PREFIX + fernet.rotate(value[len(_PREFIX) :].encode()).decode()
    return value
