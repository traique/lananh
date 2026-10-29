"""Ảnh chân dung cho trang /admin/avatar (web_avatar.py): kiểm, chuẩn hoá, lưu DB.

- Chỉ nhận PNG/JPEG/WebP (nhận dạng bằng Pillow, không tin Content-Type), tối đa
  MAX_UPLOAD_BYTES (5 MB) và MAX_SOURCE_PIXELS điểm ảnh.
- Chuẩn hoá: xoay đúng chiều theo EXIF rồi bỏ EXIF/ICC, kênh trong suốt đặt lên nền
  trắng, cạnh dài tối đa MAX_SIDE (1280 px), lưu WebP (~200-350 KB).
- Điểm mặt (``validate_landmarks``): đúng LANDMARK_COUNT (478) điểm MediaPipe Face
  Landmarker, toạ độ chuẩn hoá 0..1. Trình duyệt dò 1 lần lúc upload trên ảnh đã xoay
  đúng chiều - toạ độ chuẩn hoá không đổi khi thu nhỏ nên khớp ảnh đã chuẩn hoá.
- Ảnh mắt nhắm ('blink', tuỳ chọn) phải cùng kích thước ảnh chính sau chuẩn hoá (vẽ
  chồng lên nhau bằng chung lưới điểm mặt). Đổi ảnh chính khác kích thước thì xoá ảnh
  mắt nhắm cũ; xoá ảnh chính thì xoá luôn ảnh mắt nhắm.
- RAM (worker 512 MB): JPEG giải mã thẳng ở độ phân giải gần MAX_SIDE (``draft``), mỗi
  lúc chỉ xử lý 1 ảnh (semaphore) trong thread, xong thì trả heap (core/memtrim.py).
"""

from __future__ import annotations

import asyncio
import io
import json
import math
from dataclasses import dataclass
from datetime import datetime

from PIL import Image, ImageOps

from core import database as db
from core import memtrim

_MB = 1024 * 1024
MAX_UPLOAD_BYTES = 5 * _MB
# JPEG giải mã thu nhỏ (draft) nên nhận ảnh lớn hơn; PNG/WebP giải mã đủ cỡ (RGBA 4 byte
# mỗi điểm) nên trần thấp hơn để 1 ảnh không chiếm quá ~64 MB RAM.
MAX_SOURCE_PIXELS = 40_000_000
MAX_SOURCE_PIXELS_FULL_DECODE = 16_000_000
MAX_SIDE = 1280
MIN_SIDE = 200
WEBP_QUALITY = 82
LANDMARK_COUNT = 478
KINDS = ("main", "blink")
MIME = "image/webp"
_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
# Chuỗi JSON điểm mặt hợp lệ chỉ ~10-25 KB; trần để không giải mã chuỗi khổng lồ.
_LANDMARKS_MAX_CHARS = 200_000

_gate = asyncio.Semaphore(1)


class PhotoError(ValueError):
    """Ảnh/điểm mặt không hợp lệ: ``message`` hiện cho người dùng, ``status`` HTTP."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Processed:
    data: bytes
    width: int
    height: int
    mime: str = MIME


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise PhotoError("kind", "kind phải là main hoặc blink.")
    return kind


def validate_landmarks(raw: object) -> list[list[float]]:
    """[[x, y], ...] đúng 478 điểm, x/y là số hữu hạn trong 0..1 (nhận thêm z, bỏ đi).
    ``raw`` là list hoặc chuỗi JSON. Sai cấu trúc -> PhotoError."""
    error = PhotoError(
        "landmarks", f"Dữ liệu điểm mặt không hợp lệ (cần {LANDMARK_COUNT} điểm, toạ độ 0..1)."
    )
    if isinstance(raw, (bytes, str)):
        if len(raw) > _LANDMARKS_MAX_CHARS:
            raise error
        try:
            raw = json.loads(raw)
        except ValueError:
            raise error from None
    if not isinstance(raw, list) or len(raw) != LANDMARK_COUNT:
        raise error
    points: list[list[float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or not 2 <= len(item) <= 3:
            raise error
        coords = []
        for value in item[:2]:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise error
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise error
            coords.append(round(float(value), 5))
        points.append(coords)
    return points


def _flatten(image: Image.Image) -> Image.Image:
    """RGB; kênh trong suốt (PNG/WebP) đặt lên nền trắng."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def process(data: bytes) -> Processed:
    """Ảnh upload -> WebP đã chuẩn hoá (đồng bộ - gọi qua ``normalize``)."""
    if not data:
        raise PhotoError("empty", "Chưa có file ảnh.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PhotoError("too_big", f"Ảnh tối đa {MAX_UPLOAD_BYTES // _MB} MB.", 413)
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in _FORMATS:
                raise PhotoError("format", "Chỉ nhận ảnh PNG, JPEG hoặc WebP.")
            limit = MAX_SOURCE_PIXELS if source.format == "JPEG" else MAX_SOURCE_PIXELS_FULL_DECODE
            if source.width * source.height > limit:
                raise PhotoError("pixels", "Ảnh quá nhiều điểm ảnh, chọn ảnh nhỏ hơn.")
            if source.format == "JPEG":
                # Giải mã ở độ phân giải gần MAX_SIDE: ảnh điện thoại 12 MP chỉ tốn vài MB.
                source.draft("RGB", (MAX_SIDE, MAX_SIDE))
            image = _flatten(ImageOps.exif_transpose(source))
    except PhotoError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError):
        raise PhotoError("format", "Chỉ nhận ảnh PNG, JPEG hoặc WebP.") from None
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    if min(image.size) < MIN_SIDE:
        raise PhotoError("small", f"Ảnh nhỏ quá, cạnh ngắn cần ít nhất {MIN_SIDE} px.")
    out = io.BytesIO()
    # Không truyền exif/icc_profile: bản lưu không còn metadata của ảnh gốc.
    image.save(out, "WEBP", quality=WEBP_QUALITY, method=4)
    return Processed(out.getvalue(), image.width, image.height)


async def normalize(data: bytes) -> Processed:
    async with _gate:
        try:
            return await asyncio.to_thread(process, data)
        finally:
            memtrim.request("ảnh avatar")


async def save(kind: str, data: bytes, landmarks: object = None) -> dict:
    """Kiểm + chuẩn hoá + lưu ảnh ``kind``. Raise PhotoError nếu không hợp lệ."""
    check_kind(kind)
    points = validate_landmarks(landmarks) if kind == "main" else None
    current = await db.avatar_photo_meta()
    if kind == "blink" and "main" not in current:
        raise PhotoError("no_main", "Tải ảnh chính trước rồi mới tới ảnh mắt nhắm.")
    photo = await normalize(data)
    if kind == "blink":
        main = current["main"]
        if (photo.width, photo.height) != (main["width"], main["height"]):
            raise PhotoError(
                "size_mismatch",
                f"Ảnh mắt nhắm phải cùng kích thước ảnh chính ({main['width']}x{main['height']}"
                ", chụp cùng khung).",
            )
    await db.avatar_photo_put(
        kind,
        mime=photo.mime,
        data=photo.data,
        width=photo.width,
        height=photo.height,
        landmarks=points,
    )
    blink_removed = False
    blink = current.get("blink")
    if (
        kind == "main"
        and blink
        and (blink["width"], blink["height"])
        != (
            photo.width,
            photo.height,
        )
    ):
        blink_removed = await db.avatar_photo_delete("blink") > 0
    return {
        "kind": kind,
        "width": photo.width,
        "height": photo.height,
        "bytes": len(photo.data),
        "blinkRemoved": blink_removed,
    }


async def remove(kind: str) -> int:
    return await db.avatar_photo_delete(check_kind(kind))


def version(updated_at: object) -> str:
    """Mã phiên bản ảnh cho URL (?v=) - đổi mỗi lần thay ảnh."""
    if isinstance(updated_at, datetime):
        return str(int(updated_at.timestamp() * 1000))
    return str(updated_at or "")
