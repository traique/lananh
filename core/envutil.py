"""Đọc biến môi trường dạng cờ/số nguyên, dùng chung giữa các module."""

import os


def env_on(name: str) -> bool:
    """Cờ mặc định BẬT; chỉ tắt khi đặt biến = 0 (vd bộ lọc Zalo → Facebook)."""
    return os.getenv(name, "1").strip() != "0"


def env_int(name: str, default: int, low: int, high: int) -> int:
    """Số nguyên kẹp trong [low, high]; giá trị không parse được -> default."""
    try:
        return max(low, min(high, int(os.getenv(name, str(default)))))
    except ValueError:
        return default
