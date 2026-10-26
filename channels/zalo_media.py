"""Bridge loopback cho tin nhắn thoại và file tài liệu từ zalo-gateway.

Cùng khuôn với POST /internal/zalo/image-prompt (channels/router.py): chỉ nhận
gọi từ loopback (``_require_local_caller``), header ``x-zalo-bridge-secret``,
chặn kích thước body TRƯỚC khi đọc (services/request_limits.py cũng chặn ở tầng
ASGI), người lạ chưa pair -> im lặng + báo chủ bot, tài khoản bị khoá -> câu báo
khoá, xử lý trong ``assistant_turn("zalo:<internal_user_id>")`` và cache câu trả
lời theo (account_id, message_id, kind) để gateway gửi lại không chạy lượt 2 lần.
Lượt chạy trong task con huỷ được: "/huy" của chính người đó (gửi ngoài hàng đợi,
channels/router.py) dừng lượt nghe/đọc file đang chạy, trả câu "em dừng rồi".

Hợp đồng (gateway gửi raw bytes, KHÔNG base64):

POST /internal/zalo/voice
    headers: x-zalo-bridge-secret, x-zalo-sender-id, x-zalo-message-id,
             x-zalo-account-id (mặc định "zalo-bot"), content-type: audio/*,
             x-zalo-duration-ms (tuỳ chọn - thời lượng từ payload Zalo)
    body:    file ghi âm (m4a/aac/ogg/mp3/wav..., nhận theo magic bytes),
             tối đa VOICE_MAX_BYTES (mặc định 5 MB)
    trả:     ZaloMessageResponse {"messages": [...], "provider": null|"api",
             "image_b64": null}; messages[0] là "🎙️ Em nghe là: «...»" (hoặc câu
             báo lỗi giọng Lan Anh), các tin sau là câu trả lời. Cache kind "voice".

POST /internal/zalo/file
    headers: x-zalo-bridge-secret, x-zalo-sender-id, x-zalo-message-id,
             x-zalo-account-id (mặc định "zalo-bot"),
             x-zalo-file-name (encodeURIComponent, vd "hop-dong.pdf"),
             x-zalo-caption (encodeURIComponent, tuỳ chọn - câu hỏi kèm file),
             content-type: application/pdf | text/plain | text/markdown |
             application/vnd.openxmlformats-officedocument.wordprocessingml.document
    body:    file (nhận theo magic bytes + đuôi), tối đa DOCUMENT_MAX_BYTES (5 MB)
    trả:     ZaloMessageResponse; messages là câu trả lời về tài liệu (không
             caption = tóm tắt) hoặc câu báo lỗi/định dạng chưa hỗ trợ. Cache kind
             "file". "lưu vào kho" từ Zalo luôn bị từ chối (chỉ chủ bot trên
             Telegram) nhưng vẫn trả lời về tài liệu.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Request

import messages as messages_module
from channels import zalo_users
from channels.contracts import ZaloMessageResponse
from channels.router import (
    _audience,
    _auth_sender,
    _require_local_caller,
    _zalo_chunks,
    media_quota_gate,
    media_turn,
)
from core import idempotency
from services import document_service, reply_audience, turn_control, voice_service
from services.concurrency import TurnCancelled, assistant_turn, run_cancellable

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/zalo",
    tags=["zalo-internal"],
    dependencies=[Depends(_require_local_caller)],
)


async def _read_capped_body(request: Request, limit: int, label: str) -> bytes:
    """Đọc body sau khi kiểm Content-Length; vượt ``limit`` -> 413."""
    try:
        size = int(request.headers.get("content-length", "0") or 0)
    except ValueError as exc:
        raise HTTPException(400, "Invalid Content-Length") from exc
    if size > limit:
        raise HTTPException(413, f"{label} too large")
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(413, f"{label} too large")
    return body


def _duration_ms(raw: str) -> int | None:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def _resolve_sender(
    sender_id: str,
) -> tuple[zalo_users.ZaloUser | None, ZaloMessageResponse | None]:
    """(người dùng đã pair, None) hoặc (None, câu trả lời dừng sớm)."""
    zalo_user = await zalo_users.resolve(sender_id)
    if zalo_user is None:
        zalo_users.notify_unpaired(sender_id)
        return None, ZaloMessageResponse(messages=[], provider=None)
    if not zalo_user.is_active:
        return None, ZaloMessageResponse(
            messages=[messages_module.ZALO_LOCKED_REPLY], provider=None
        )
    return zalo_user, None


async def _cancellable(
    internal_user_id: int, factory: Callable[[], Awaitable[tuple[list[str], str | None]]]
) -> tuple[list[str], str | None]:
    """Chạy lượt trong task con khoá "zalo:<id>" như /image-prompt: "/huy" gửi
    ngoài hàng đợi (channels/router.py) huỷ được. Bị huỷ -> câu "em dừng rồi" là
    câu trả lời bình thường (được cache: gateway gửi lại không chạy lại lượt)."""
    try:
        return await run_cancellable(f"zalo:{internal_user_id}", factory)
    except TurnCancelled:
        return [await turn_control.cancelled_text_for(internal_user_id)], None


@router.post("/voice", response_model=ZaloMessageResponse)
async def voice(
    request: Request,
    x_zalo_bridge_secret: str | None = Header(default=None),
    x_zalo_sender_id: str = Header(),
    x_zalo_message_id: str = Header(),
    x_zalo_account_id: str = Header(default="zalo-bot"),
    x_zalo_duration_ms: str = Header(default=""),
):
    await _auth_sender(x_zalo_bridge_secret, x_zalo_sender_id)
    body = await _read_capped_body(request, voice_service.max_bytes(), "Voice")
    zalo_user, early = await _resolve_sender(x_zalo_sender_id)
    if zalo_user is None:
        return early
    blocked = await media_quota_gate(zalo_user, x_zalo_account_id, x_zalo_message_id, "voice")
    if blocked is not None:
        return blocked
    with media_turn(x_zalo_account_id, x_zalo_message_id, zalo_user):
        return await _voice_turn(
            body,
            zalo_user,
            x_zalo_sender_id,
            x_zalo_message_id,
            x_zalo_account_id,
            x_zalo_duration_ms,
        )


async def _voice_turn(
    body: bytes,
    zalo_user: zalo_users.ZaloUser,
    x_zalo_sender_id: str,
    x_zalo_message_id: str,
    x_zalo_account_id: str,
    x_zalo_duration_ms: str,
) -> ZaloMessageResponse:
    async with assistant_turn(f"zalo:{zalo_user.internal_user_id}"):
        cached = await idempotency.get_zalo_response(x_zalo_account_id, x_zalo_message_id, "voice")
        if cached is not None:
            return ZaloMessageResponse.model_validate(cached)
        with reply_audience.use(_audience(zalo_user, x_zalo_sender_id)):
            replies, provider = await _cancellable(
                zalo_user.internal_user_id,
                functools.partial(
                    voice_service.handle_channel_voice,
                    zalo_user.internal_user_id,
                    body,
                    is_admin=zalo_user.is_admin,
                    duration_ms=_duration_ms(x_zalo_duration_ms),
                ),
            )
        del body
        response = ZaloMessageResponse(messages=_zalo_chunks(replies), provider=provider)
        await idempotency.save_zalo_response(
            x_zalo_account_id, x_zalo_message_id, "voice", response.model_dump()
        )
        return response


@router.post("/file", response_model=ZaloMessageResponse)
async def document_file(
    request: Request,
    x_zalo_bridge_secret: str | None = Header(default=None),
    x_zalo_sender_id: str = Header(),
    x_zalo_message_id: str = Header(),
    x_zalo_account_id: str = Header(default="zalo-bot"),
    x_zalo_file_name: str = Header(default=""),
    x_zalo_caption: str = Header(default=""),
):
    await _auth_sender(x_zalo_bridge_secret, x_zalo_sender_id)
    body = await _read_capped_body(request, document_service.max_bytes(), "File")
    zalo_user, early = await _resolve_sender(x_zalo_sender_id)
    if zalo_user is None:
        return early
    blocked = await media_quota_gate(zalo_user, x_zalo_account_id, x_zalo_message_id, "file")
    if blocked is not None:
        return blocked
    with media_turn(x_zalo_account_id, x_zalo_message_id, zalo_user):
        return await _file_turn(
            request,
            body,
            zalo_user,
            x_zalo_sender_id,
            x_zalo_message_id,
            x_zalo_account_id,
            x_zalo_file_name,
            x_zalo_caption,
        )


async def _file_turn(
    request: Request,
    body: bytes,
    zalo_user: zalo_users.ZaloUser,
    x_zalo_sender_id: str,
    x_zalo_message_id: str,
    x_zalo_account_id: str,
    x_zalo_file_name: str,
    x_zalo_caption: str,
) -> ZaloMessageResponse:
    async with assistant_turn(f"zalo:{zalo_user.internal_user_id}"):
        cached = await idempotency.get_zalo_response(x_zalo_account_id, x_zalo_message_id, "file")
        if cached is not None:
            return ZaloMessageResponse.model_validate(cached)
        with reply_audience.use(_audience(zalo_user, x_zalo_sender_id)):
            replies, provider = await _cancellable(
                zalo_user.internal_user_id,
                functools.partial(
                    document_service.handle_channel_file,
                    zalo_user.internal_user_id,
                    body,
                    filename=unquote(x_zalo_file_name)[:200],
                    content_type=request.headers.get("content-type", ""),
                    caption=unquote(x_zalo_caption)[:500],
                    message_id=x_zalo_message_id,
                ),
            )
        del body
        response = ZaloMessageResponse(messages=_zalo_chunks(replies), provider=provider)
        await idempotency.save_zalo_response(
            x_zalo_account_id, x_zalo_message_id, "file", response.model_dump()
        )
        return response
