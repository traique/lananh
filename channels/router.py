"""Authenticated HTTP bridge used by the local zca-js process."""

import asyncio
import base64
import binascii
import functools
import hmac
import ipaddress
import logging
import os
import time
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

import messages as messages_module
from channels import facebook_repository, zalo_repository, zalo_session, zalo_users
from channels.contracts import (
    ZaloGroupConfig,
    ZaloFacebookPostRequest,
    ZaloGroupMessageRequest,
    ZaloMessageRequest,
    ZaloMessageResponse,
    ZaloOutboxItem,
)
from channels.facebook_commands import maybe_handle_facebook_command, prepare_post
from channels.group_commands import maybe_handle_group_command
from channels.zalo_text import to_plain_text
from core import alerts, idempotency
from core.envutil import env_on
from services import channel_dispatch, reply_audience
from services.channel_chat_service import handle_channel_text, split_for_zalo
from services.channel_image_service import MAX_ZALO_IMAGE_BYTES, handle_channel_image
from services import facebook_dedup, facebook_intake_stats
from services.facebook_caption import skip_reason
from services.facebook_image import compact_image, image_dhash
from services.concurrency import assistant_turn, channel_message_turn
from services.reminder_delivery import NotificationTarget, notification_target

logger = logging.getLogger(__name__)

# Bridge chỉ dành cho zalo-gateway chạy CÙNG container (gọi qua loopback). Trên
# Render cổng web là public nên ngoài bridge secret còn chặn theo địa chỉ gọi tới.
_DEFAULT_ALLOWED_HOSTS = "127.0.0.1,::1"
# Request đi qua proxy (Render, Cloudflare...) luôn mang 1 trong các header này;
# gateway gọi thẳng loopback thì không. Chặn cả khi uvicorn tin proxy
# (FORWARDED_ALLOW_IPS) và đã đổi client.host thành IP do header khai báo.
_FORWARDED_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")
_REJECT_LOG_INTERVAL_SEC = 60.0
_last_reject_log = 0.0


@functools.lru_cache(maxsize=8)
def _parse_allowed_hosts(raw: str) -> tuple[tuple, frozenset[str]]:
    """Tách danh sách (vd "127.0.0.1,::1,10.0.0.0/8,testclient") thành mạng IP + tên host."""
    networks, names = [], set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            names.add(item.lower())
    return tuple(networks), frozenset(names)


def _host_allowed(host: str | None) -> bool:
    if not host:
        return False
    raw = os.getenv("ZALO_BRIDGE_ALLOWED_HOSTS", "").strip() or _DEFAULT_ALLOWED_HOSTS
    networks, names = _parse_allowed_hosts(raw)
    if host.lower() in names:
        return True
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    # Socket dual-stack báo IPv4 dạng "::ffff:127.0.0.1".
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in network for network in networks)


def _env_flag(name: str) -> bool:
    """Cờ mặc định TẮT; đặt biến = 1/true để bật."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _reject_caller(host: str | None, reason: str) -> None:
    global _last_reject_log
    now = time.monotonic()
    # Bot dò quét có thể gọi liên tục: log tối đa 1 dòng/phút.
    if now - _last_reject_log >= _REJECT_LOG_INTERVAL_SEC:
        _last_reject_log = now
        logger.warning("Chặn request /internal/zalo từ %s (%s).", host or "?", reason)
    raise HTTPException(403, "Forbidden")


async def _require_local_caller(request: Request) -> None:
    host = request.client.host if request.client else None
    if not _host_allowed(host):
        _reject_caller(host, "địa chỉ ngoài ZALO_BRIDGE_ALLOWED_HOSTS")
    if not _env_flag("ZALO_BRIDGE_ALLOW_FORWARDED") and any(
        name in request.headers for name in _FORWARDED_HEADERS
    ):
        _reject_caller(host, "request đi qua proxy")


router = APIRouter(
    prefix="/internal/zalo",
    tags=["zalo-internal"],
    dependencies=[Depends(_require_local_caller)],
)


class SessionPayload(BaseModel):
    cookie: list | dict
    imei: str
    userAgent: str
    accountId: str


class ControllerPayload(BaseModel):
    controllerId: str


class OutboxFailurePayload(BaseModel):
    error: str = ""
    chunks_sent: int = Field(default=0, ge=0)


class GatewayStatePayload(BaseModel):
    account_id: str = ""
    state: str = Field(pattern=r"^(connected|reconnecting|closed|login_required|send_failed)$")
    detail: str = Field(default="", max_length=300)


# Trạng thái gateway lần cuối đã báo chủ bot - chỉ báo khi ĐỔI trạng thái (hoặc
# send_failed mới) để một đợt mất kết nối dài không thành hàng loạt tin.
_last_reported_gateway_state: dict[str, str] = {}
_GATEWAY_STATE_TEXT = {
    "closed": "⚠️ Zalo gateway mất kết nối ({detail}). Đang tự kết nối lại từ session đã lưu.",
    "connected": "✅ Zalo gateway đã kết nối lại ({detail}).",
    "login_required": "🚨 Zalo gateway không kết nối lại được ({detail}). Dùng /zalo để đăng nhập lại.",
    "send_failed": "⚠️ Zalo gateway không gửi được trả lời ({detail}).",
}


def gateway_state_alert_text(account_id: str, state: str, detail: str) -> str | None:
    """Text cảnh báo cho chủ bot, hoặc None nếu không cần báo (trạng thái lặp,
    reconnecting, hoặc 'connected' khi trước đó chưa từng báo mất kết nối)."""
    previous = _last_reported_gateway_state.get(account_id)
    if state == "reconnecting":
        return None
    if state == "connected" and previous not in ("closed", "login_required"):
        _last_reported_gateway_state[account_id] = state
        return None
    if state == previous and state != "send_failed":
        return None
    _last_reported_gateway_state[account_id] = state
    template = _GATEWAY_STATE_TEXT.get(state)
    if template is None:
        return None
    return template.format(detail=detail or "không rõ")


def _secret():
    return os.getenv("ZALO_BRIDGE_SECRET", "").strip()


def _auth(value):
    expected = _secret()
    if not expected:
        raise HTTPException(503, "Zalo bridge is not configured")
    # So sánh bytes: header non-ASCII không làm compare_digest TypeError (500 thay vì 403).
    if not value or not hmac.compare_digest(str(value).encode(), expected.encode()):
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


@router.post("/gateway-state", status_code=204)
async def gateway_state(
    payload: GatewayStatePayload,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    """Gateway Node báo mất kết nối/kết nối lại/không gửi được tin - trước đây
    listener đóng chỉ set api=null, outbox/reminder/digest ứ đọng im lặng tới
    khi chủ bot gõ /zalo."""
    _auth(x_zalo_bridge_secret)
    logger.info(
        "Zalo gateway state=%s account=%s detail=%s",
        payload.state,
        payload.account_id,
        payload.detail,
    )
    text = gateway_state_alert_text(payload.account_id, payload.state, payload.detail)
    if text:
        alerts.send(text)
    return Response(status_code=204)


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
    async with channel_message_turn(payload.account_id, payload.message_id, "text"):
        with notification_target(
            NotificationTarget(
                "zalo",
                payload.conversation_id,
                payload.account_id,
                event_key=f"zalo:{payload.account_id}:{payload.message_id}",
            )
        ):
            return await _receive_text(payload)


async def _is_controller(external_id: str) -> bool:
    """Người gửi là tài khoản Zalo của chủ bot (controller ghép đôi qua /pair) -
    KHÁC role admin: /zaloadmin cấp được admin cho người khác."""
    controller = await _controller()
    return bool(controller) and external_id == controller


def _audience(zalo_user: zalo_users.ZaloUser, sender_id: str) -> reply_audience.ReplyAudience:
    """Người nhận của lượt (services/reply_audience.py): cách gọi riêng chủ bot
    đặt cho người này (/zalopair <id> <tên> chị) và cách kiểm (lười) có phải
    chủ bot không - chỉ chủ bot thấy chi tiết lỗi kỹ thuật, nhãn ⚙️ API."""
    return reply_audience.ReplyAudience(
        address_form=getattr(zalo_user, "address_form", None),
        owner_check=functools.partial(_is_controller, sender_id),
    )


def _chat_for(zalo_user: zalo_users.ZaloUser, sender_id: str):
    """handle_channel_text chạy trong ngữ cảnh người nhận của zalo_user. Tra tên
    module-level lúc gọi để test monkeypatch ``router.handle_channel_text`` vẫn có
    hiệu lực."""

    async def chat(user_id: int, text: str, is_admin: bool = True):
        with reply_audience.use(_audience(zalo_user, sender_id)):
            return await handle_channel_text(user_id, text, is_admin)

    return chat


async def _receive_text(payload: ZaloMessageRequest):
    zalo_user = await zalo_users.resolve(payload.sender_id)
    if zalo_user is None:
        # Người lạ CHƯA pair: im lặng với sender (không lộ ra là bot chưa
        # cấu hình xong), chỉ âm thầm báo owner Telegram. KHÔNG cache
        # response rỗng - nếu owner pair xong rồi người này nhắn lại
        # (message_id mới) vẫn xử lý bình thường như mọi tin nhắn khác.
        cached = await idempotency.get_zalo_response(payload.account_id, payload.message_id, "text")
        if cached is not None:
            return ZaloMessageResponse.model_validate(cached)
        zalo_users.notify_unpaired(payload.sender_id, payload.sender_name)
        return ZaloMessageResponse(messages=[], provider=None)
    if not zalo_user.is_active:
        return ZaloMessageResponse(messages=[messages_module.ZALO_LOCKED_REPLY], provider=None)

    # user_id RIÊNG cho từng external_id (zalo_user.internal_user_id) - KHÔNG
    # dùng config.ALLOWED_USER_ID, để cách ly hoàn toàn ngữ cảnh chat/trí nhớ
    # giữa từng người nhắn Zalo và giữa họ với chủ bot Telegram. Xem docstring
    # channels/zalo_users.py. Chuỗi cache -> /fb_* -> khoá lượt -> lệnh nhóm ->
    # chat dùng chung với Zoom (services/channel_dispatch.py).
    payload_dict = await channel_dispatch.dispatch(
        channel_dispatch.DispatchRequest(
            account_id=payload.account_id,
            event_id=payload.message_id,
            cache_kind="text",
            user_id=zalo_user.internal_user_id,
            is_admin=zalo_user.is_admin,
            channel="zalo",
            text=payload.text,
            turn_key=f"zalo:{zalo_user.internal_user_id}",
            command_account_id=payload.account_id,
        ),
        channel_dispatch.DispatchHandlers(
            chat=_chat_for(zalo_user, payload.sender_id),
            get_cached=idempotency.get_zalo_response,
            save_cached=idempotency.save_zalo_response,
            facebook=maybe_handle_facebook_command,
            group=maybe_handle_group_command,
            chunk=_zalo_chunks,
            turn=assistant_turn,
        ),
    )
    return ZaloMessageResponse.model_validate(payload_dict)


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
    try:
        size = int(request.headers.get("content-length", "0") or 0)
    except ValueError as exc:
        raise HTTPException(400, "Invalid Content-Length") from exc
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

    async with assistant_turn(f"zalo:{zalo_user.internal_user_id}"):
        cached = await idempotency.get_zalo_response(
            x_zalo_account_id,
            x_zalo_message_id,
            "image-prompt",
        )
        if cached is not None:
            return ZaloMessageResponse.model_validate(cached)
        with reply_audience.use(_audience(zalo_user, x_zalo_sender_id)):
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


@router.get("/facebook-groups/{account_id}", response_model=list[ZaloGroupConfig])
async def facebook_groups(
    account_id: str,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    _auth(x_zalo_bridge_secret)
    return [
        ZaloGroupConfig(group_id=group_id, alias=alias)
        for group_id, alias in await facebook_repository.list_groups(account_id)
    ]


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


@router.post("/facebook-group-post", status_code=204)
async def facebook_group_post(
    payload: ZaloFacebookPostRequest,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    _auth(x_zalo_bridge_secret)
    media: list[tuple[str, bytes]] = []
    total_bytes = 0
    try:
        max_image_bytes = int(os.getenv("ZALO_IMAGE_MAX_BYTES", str(8 * 1024 * 1024)))
    except ValueError:
        max_image_bytes = 8 * 1024 * 1024
    for item in payload.media:
        try:
            body = base64.b64decode(item.data_b64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(400, "Invalid image base64") from exc
        if not body or len(body) > max_image_bytes:
            raise HTTPException(413, "Image too large")
        total_bytes += len(body)
        if total_bytes > 16 * 1024 * 1024:
            raise HTTPException(413, "Media payload too large")
        media.append((item.mime_type, body))
    if not payload.text.strip() and not media:
        raise HTTPException(400, "Empty Facebook post")
    # Lọc ở đây (sau khi gateway đã gộp ảnh + chữ cùng người gửi). Trả 204 chứ không báo lỗi:
    # gateway thử lại bài bị lỗi (4xx: 1 lần rồi bỏ, 5xx/mạng: backoff có giới hạn).
    reason = skip_reason(
        payload.text,
        bool(media),
        require_photo_and_caption=env_on("FACEBOOK_REQUIRE_PHOTO_AND_CAPTION"),
        skip_voucher=env_on("FACEBOOK_SKIP_VOUCHER_POSTS"),
    )
    if reason:
        logger.info(
            "Bỏ qua bài Zalo (%s) nhóm=%s người gửi=%s: %.60s",
            reason,
            payload.group_id,
            payload.sender_id,
            " ".join(payload.text.split()),
        )
        facebook_intake_stats.record(reason, payload.text)
        return Response(status_code=204)
    # Nén ảnh TRƯỚC khi lưu (Supabase free tier 500 MB) và lấy dHash để chống trùng.
    # Xử lý tuần tự từng ảnh để giữ đỉnh RAM thấp trên máy 512 MB.
    stored_media: list[tuple[str, bytes]] = []
    image_hashes: list[int] = []
    for mime_type, body in media:
        stored = await asyncio.to_thread(compact_image, mime_type, body)
        stored_media.append(stored)
        digest = await asyncio.to_thread(image_dhash, stored[1])
        if digest is not None:
            image_hashes.append(digest)
    media.clear()
    fingerprint = facebook_dedup.build_fingerprint(payload.text, image_hashes)
    try:
        post_id = await facebook_repository.create_post(
            account_id=payload.account_id,
            group_id=payload.group_id,
            sender_id=payload.sender_id,
            sender_name=payload.sender_name,
            source_message_ids=payload.message_ids,
            content=payload.text.strip(),
            media=stored_media,
            fingerprint=fingerprint,
        )
    except facebook_repository.DuplicatePostError as exc:
        logger.info(
            "Bỏ qua bài Zalo trùng (%s) nhóm=%s người gửi=%s: %.60s",
            exc,
            payload.group_id,
            payload.sender_id,
            " ".join(payload.text.split()),
        )
        facebook_intake_stats.record(f"trùng lặp: {exc.match.reason}", payload.text)
        return Response(status_code=204)
    if post_id is None:
        return Response(status_code=204)
    facebook_intake_stats.record(facebook_intake_stats.QUEUED)
    note = None
    pruned = await facebook_repository.enforce_pending_cap(payload.account_id)
    if pruned:
        facebook_intake_stats.record(facebook_intake_stats.PRUNED, amount=len(pruned))
        shown = ", ".join(f"#{i}" for i in pruned[:10]) + ("..." if len(pruned) > 10 else "")
        note = (
            f"🧹 Hàng chờ vượt {facebook_repository.max_pending()} bài: "
            f"đã tự xoá {len(pruned)} bài chờ cũ nhất ({shown})."
        )
    await prepare_post(payload.account_id, post_id, note=note)
    try:
        await facebook_repository.prune_history()
    except Exception:
        logger.warning("Dọn dữ liệu Facebook cũ lỗi; thử lại lần sau.", exc_info=True)
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


@router.post("/outbox/{item_id}/failed")
async def outbox_failed(
    item_id: int,
    payload: OutboxFailurePayload,
    x_zalo_bridge_secret: str | None = Header(default=None),
):
    """Gateway báo gửi 1 tin outbox lỗi (sau khi đã gửi được ``chunks_sent`` đoạn).

    Tin được hẹn thử lại theo backoff; quá ZALO_OUTBOX_MAX_ATTEMPTS lần thì bỏ
    cuộc và báo chủ bot 1 lần, các tin phía sau không bị chặn."""
    _auth(x_zalo_bridge_secret)
    error = " ".join(payload.error.split())[: zalo_repository.OUTBOX_ERROR_MAX_CHARS]
    error = error or "lỗi không rõ"
    row = await zalo_repository.mark_outbox_failed(item_id, error, payload.chunks_sent)
    if row is None:
        return {"status": "ignored"}
    if row["failed"]:
        logger.warning(
            "Bỏ cuộc tin Zalo outbox #%s tới %s sau %s lần lỗi: %s",
            item_id,
            row["recipient_id"],
            row["attempts"],
            error,
        )
        alerts.send(
            messages_module.ZALO_OUTBOX_FAILED_ALERT.format(
                item_id=item_id,
                recipient=row["recipient_id"],
                attempts=row["attempts"],
                error=error[:200],
            )
        )
        return {"status": "failed", "attempts": row["attempts"]}
    logger.info("Tin Zalo outbox #%s lỗi lần %s, sẽ thử lại: %s", item_id, row["attempts"], error)
    return {"status": "retry", "attempts": row["attempts"]}


@router.get("/outbox/{account_id}")
async def account_outbox(account_id: str, x_zalo_bridge_secret: str | None = Header(default=None)):
    _auth(x_zalo_bridge_secret)
    return [dict(row) for row in await zalo_repository.get_account_outbox(account_id)]
