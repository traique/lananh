"""Thao tác file tạm dùng chung (ảnh tải về để phân tích...)."""

import asyncio
import os


async def safe_delete(path) -> None:
    """Xoá file trong thread riêng; file không tồn tại/lỗi OS thì bỏ qua."""
    try:
        await asyncio.to_thread(os.remove, path)
    except OSError:
        pass
