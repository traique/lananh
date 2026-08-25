"""Authenticated HTTP bridge used by the local zca-js process."""

import hmac
import os
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel

import messages as messages_module
from channels import zalo_repository, zalo_session, zalo_users
from channels.contracts import (
    ZaloGroupConfig,
    ZaloGroupMessageRequest,
    ZaloMessageRequest,
    ZaloMessageResponse,
    ZaloOutboxItem,
)
from channels.group_commands import maybe_handle_group_command
from channels.zalo_text import to_plain_text
from core import idempotency
from services.channel_chat_service import handle_channel_text, split_for_zalo
from services.channel_image_service import MAX_ZALO_IMAGE_BYTES, handle_channel_image
from services.concurrency import assistant_turn

router = APIRouter(prefix="/internal/zalo", tags=["zalo-internal"])


class SessionPayload(BaseModel):
    cookie: list | dict
    imei: str
    userAgent: str
    accountId: str


class ControllerPayload(BaseModel):
    controllerId: str


def _secret():
    return os.getenv("ZALO_BRIDGE_SECRET", "").strip()


def _auth(value):
    expected = _secret()
    if not expected:
        raise HTTPException(503, "Zalo bridge is not configured")
    if not value or not hmac.compare_digest(value, expected):
        raise HTTPException(403, "Forbidden")


def _controller_env():
    return os.getenv("ZALO_CONTROLLER_ID", "").strip()


async def _controller():
    return _controller_env() or await zalo_session.load_controller()


async def _auth_sender(secret, sender):
    """Tương thích ngược: chỉ check bridge secret (KHÔNG còn yêu cầu
    sender == controller). Kể từ khi hỗ trợ nhiều Zalo user (channels/zalo_users.py),
    việc "sender này có được phép nói chuyện với bot không" chuyển hẳn sang
    zalo_users.resolve() bên trong từng endpoint - vì mỗi endpoint cần xử lý
    khác nhau khi chưa pair (im lặng + báo owner) so với khi bị khoá."""
    _auth(secret)


def _zalo_chunks(outputs: list[str]) -> list[str]:
    """Zalo không render markdown, lọc sạch trước khi cắt tin."""
    return [chunk for message in outputs for chunk in split_for_zalo(to_plain_text(message))]


@router.get("/session")
async def get_session(x_zalo_bridge_secret: str | None = Header(default=None)):
    _auth(x_zalo_bridge_secret)
    return await zalo_session.load_session() or {}


@router.put("/session", status_code=204)
async def put_session(
    payload: SessionPayload,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    _auth(x_zalo_bridge_secret)
    await zalo_session.save_session(payload.model_dump())
    return Response(status_code=204)


@router.delete("/session", status_code=204)
async def delete_session(x_zalo_bridge_secret: str | None = Header(default=None)):
    _auth(x_zalo_bridge_secret)
    await zalo_session.clear_session()
    return Response(status_code=204)


@router.get("/controller")
async def get_controller(x_zalo_bridge_secret: str | None = Header(default=None)):
    _auth(x_zalo_bridge_secret)
    return {"controllerId": await _controller()}


@router.put("/controller", status_code=204)
async def put_controller(
    payload: ControllerPayload,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    _auth(x_zalo_bridge_secret)
    if not payload.controllerId.strip():
        raise HTTPException(400, "Missing controllerId")
    await zalo_session.save_controller(payload.controllerId)
    # Người ghép đôi qua flow "/pair <mã>" (gateway) luôn trở thành ADMIN ĐẦU
    # TIÊN trong bảng zalo_users - giữ nguyên trải nghiệm ghép đôi cũ (owner
    # lấy mã từ Telegram /zalo, gửi /pair <mã> cho Zalo B), chỉ khác là giờ
    # định danh này còn được lưu vào zalo_users để dùng chung cơ chế phân
    # quyền multi-user với những người pair sau (/zalopair, /zaloadmin).
    await zalo_users.pair_as_admin(payload.controllerId.strip())
    return Response(status_code=204)


@router.delete("/controller", status_code=204)
async def delete_controller(x_zalo_bridge_secret: str | None = Header(default=None)):
    _auth(x_zalo_bridge_secret)
    await zalo_session.clear_controller()
    return Response(status_code=204)


@router.post("/message", response_model=ZaloMessageResponse)
async def receive(
    payload: ZaloMessageRequest,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    await _auth_sender(x_zalo_bridge_secret, payload.sender_id)
    async with assistant_turn():
        cached = await idempotency.get_zalo_response(
            payload.account_id,
            payload.message_id,
            "text",
        )
        if cached is not None:
            return ZaloMessageResponse.model_validate(cached)

        zalo_user = await zalo_users.resolve(payload.sender_id)
        if zalo_user is None:
            # Người lạ CHƯA pair: im lặng với sender (không lộ ra là bot chưa
            # cấu hình xong), chỉ âm thầm báo owner Telegram. KHÔNG cache
            # response rỗng - nếu owner pair xong rồi người này nhắn lại
            # (message_id mới) vẫn xử lý bình thường như mọi tin nhắn khác.
            zalo_users.notify_unpaired(payload.sender_id, payload.sender_name)
            return ZaloMessageResponse(messages=[], provider=None)
        if not zalo_user.is_active:
            return ZaloMessageResponse(messages=[messages_module.ZALO_LOCKED_REPLY], provider=None)

        result = None
        if zalo_user.is_admin:
            result = await maybe_handle_group_command(payload.account_id, payload.text)
        if result is None:
            # user_id RIÊNG cho từng external_id (zalo_user.internal_user_id) -
            # KHÔNG dùng _shared_user_id()/config.ALLOWED_USER_ID nữa, để cách
            # ly hoàn toàn ngữ cảnh chat/trí nhớ giữa từng người nhắn Zalo và
            # giữa họ với chủ bot Telegram. Xem docstring channels/zalo_users.py.
            result = await handle_channel_text(
                zalo_user.internal_user_id, payload.text.strip(), zalo_user.is_admin
            )
        response = ZaloMessageResponse(
            messages=_zalo_chunks(result.messages),
            provider=result.provider,
            image_b64=result.image_b64,
        )
        await idempotency.save_zalo_response(
            payload.account_id,
            payload.message_id,
            "text",
            response.model_dump(),
        )
        return response


@router.post("/image-prompt", response_model=ZaloMessageResponse)
async def image_prompt(
    request: Request,
    x_zalo_bridge_secret: str | None = Header(default=None),
    x_zalo_sender_id: str = Header(),
    x_zalo_message_id: str = Header(),
    x_zalo_caption: str = Header(default=""),
    x_zalo_account_id: str = Header(default="zalo-bot"),
):
    await _auth_sender(x_zalo_bridge_secret, x_zalo_sender_id)
    size = int(request.headers.get("content-length", "0") or 0)
    if size > MAX_ZALO_IMAGE_BYTES:
        raise HTTPException(413, "Image too large")
    body = await request.body()
    if len(body) > MAX_ZALO_IMAGE_BYTES:
        raise HTTPException(413, "Image too large")

    zalo_user = await zalo_users.resolve(x_zalo_sender_id)
    if zalo_user is None:
        zalo_users.notify_unpaired(x_zalo_sender_id)
        return ZaloMessageResponse(messages=[], provider=None)
    if not zalo_user.is_active:
        return ZaloMessageResponse(messages=[messages_module.ZALO_LOCKED_REPLY], provider=None)

    async with assistant_turn():
        cached = await idempotency.get_zalo_response(
            x_zalo_account_id,
            x_zalo_message_id,
            "image-prompt",
        )
        if cached is not None:
            return ZaloMessageResponse.model_validate(cached)
        messages, provider = await handle_channel_image(
            zalo_user.internal_user_id,
            body,
            request.headers.get("content-type", ""),
            unquote(x_zalo_caption)[:500],
            x_zalo_message_id,
        )
        response = ZaloMessageResponse(
            messages=_zalo_chunks(messages),
            provider=provider,
        )
        await idempotency.save_zalo_response(
            x_zalo_account_id,
            x_zalo_message_id,
            "image-prompt",
            response.model_dump(),
        )
        return response


@router.get("/groups/{account_id}", response_model=list[ZaloGroupConfig])
async def groups(
    account_id: str,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    _auth(x_zalo_bridge_secret)
    return [
        ZaloGroupConfig(group_id=group_id, alias=alias)
        for group_id, alias in await zalo_repository.list_groups(account_id)
    ]


@router.post("/group-message", status_code=204)
async def group_message(
    payload: ZaloGroupMessageRequest,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    _auth(x_zalo_bridge_secret)
    await zalo_repository.save_group_message(
        account_id=payload.account_id,
        group_id=payload.group_id,
        message_id=payload.message_id,
        sender_id=payload.sender_id,
        sender_name=payload.sender_name,
        text=payload.text,
        sent_at_ms=payload.sent_at_ms,
    )
    return Response(status_code=204)


@router.get("/outbox/{account_id}/{recipient_id}", response_model=list[ZaloOutboxItem])
async def outbox(
    account_id: str,
    recipient_id: str,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    _auth(x_zalo_bridge_secret)
    return [
        ZaloOutboxItem(id=row["id"], content=row["content"])
        for row in await zalo_repository.get_pending_outbox(account_id, recipient_id)
    ]


@router.post("/outbox/{item_id}/ack", status_code=204)
async def ack(
    item_id: int,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    _auth(x_zalo_bridge_secret)
    await zalo_repository.mark_outbox_sent(item_id)
    return Response(status_code=204)
