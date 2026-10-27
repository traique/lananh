"""Phục vụ tin thoại trả lời (services/voice_reply.py) cho Zalo.

- ``GET|HEAD /internal/zalo/tts/<tên>``: gateway tải clip qua loopback (cùng
  ``_require_local_caller`` + ``x-zalo-bridge-secret`` như mọi route bridge), tải lên
  CDN Zalo rồi sendVoice bằng link CDN (link bền, không phụ thuộc bot còn thức).
- ``GET|HEAD /media/tts/<tên>``: link CÔNG KHAI cho zca-js sendVoice khi tải lên CDN
  lỗi - app Zalo của người nhận tự tải file (zca-js gửi HEAD trước để lấy
  Content-Length). Không cần đăng nhập: tên file mang chữ ký HMAC có hạn
  (VOICE_REPLY_URL_TTL_MIN phút), clip nằm trong kho RAM có trần; hết hạn/sai chữ
  ký/đã bị đẩy khỏi kho -> 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response

from channels.router import _auth, _require_local_caller
from services import voice_reply

public_router = APIRouter(include_in_schema=False)
internal_router = APIRouter(
    prefix="/internal/zalo",
    include_in_schema=False,
    dependencies=[Depends(_require_local_caller)],
)


def _clip_response(name: str) -> Response:
    item = voice_reply.store.get(name)
    if item is None:
        return Response(status_code=404)
    return Response(
        content=item.data,
        media_type=item.mime,
        headers={
            "Cache-Control": "private, max-age=600",
            "Content-Disposition": f'inline; filename="lan-anh.{item.ext}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@public_router.api_route("/media/tts/{name}", methods=["GET", "HEAD"])
async def public_clip(name: str) -> Response:
    return _clip_response(name)


@internal_router.api_route("/tts/{name}", methods=["GET", "HEAD"])
async def internal_clip(
    name: str, x_zalo_bridge_secret: str | None = Header(default=None)
) -> Response:
    _auth(x_zalo_bridge_secret)
    return _clip_response(name)
