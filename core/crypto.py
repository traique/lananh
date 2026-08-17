"""Mã hoá đối xứng (Fernet) cho các giá trị nhạy cảm lưu trong bảng settings.
Dùng core.config.SETTINGS_ENC_KEY. Mọi thao tác ghi đều fail closed: thiếu/sai
khoá hoặc mã hoá lỗi phải dừng thay vì âm thầm lưu plaintext.

Prefix "enc:" PHẢI giữ nguyên: đây là scheme đang chạy thật trong production
(trước refactor nằm trong gemini_client._enc/_dec). Đổi prefix sẽ khiến các
giá trị đã mã hoá sẵn trong DB không giải mã được nữa sau khi deploy.
"""
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from core import config

_PREFIX = "enc:"


def _build_fernet() -> Fernet | None:
    if not config.SETTINGS_ENC_KEY:
        return None
    try:
        return Fernet(config.SETTINGS_ENC_KEY.encode())
    except Exception as exc:
        raise RuntimeError("SETTINGS_ENC_KEY không hợp lệ; từ chối dùng plaintext.") from exc


_fernet = _build_fernet()


def _require_fernet() -> Fernet:
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
        # Giá trị cũ (từ trước khi bật SETTINGS_ENC_KEY) hoặc chưa cấu hình key.
        return value
    try:
        return _require_fernet().decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Không giải mã được settings: sai SETTINGS_ENC_KEY hoặc ciphertext hỏng."
        ) from exc
