"""Kênh Zoom Team Chat: xác thực webhook, gửi tin, parse sự kiện.

Port gần như nguyên vẹn từ vietassist/channels/zoom.py, chỉ khác ở việc dùng
core.config (module phẳng, os.getenv) thay vì core.config.settings (Pydantic
object) - xem core/config.py::ZOOM_* của repo này.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass

import httpx

from core import config
from core.text_normalize import nfc

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.zoom.us/oauth/token"
_MESSAGE_URL = "https://api.zoom.us/v2/im/chat/messages"
_HTTP_TIMEOUT = httpx.Timeout(15.0)

_token_lock = asyncio.Lock()
_cached_token: str | None = None
_cached_token_expiry: float = 0.0


@dataclass(frozen=True)
class ZoomEvent:
    event_id: str
    sender_jid: str
    text: str
    to_jid: str
    channel_name: str = ""
    account_id: str = ""

    @property
    def reply_jid(self) -> str:
        """JID dùng để TRẢ LỜI (khác với to_jid của sự kiện nhận vào).

        Theo docs Zoom, "toJid" trong webhook nghĩa là JID của channel/user MÀ TIN
        NHẮN ĐƯỢC GỬI ĐẾN - trong chat 1:1, đó chính là JID của BOT (người nhận tin),
        không phải của người dùng. Nếu dùng nguyên toJid để trả lời trong trường hợp
        1:1, Zoom sẽ trả 7004 "No channel or user can be found with the given to_jid"
        vì bạn đang cố gửi tin nhắn CHO CHÍNH BOT. Chỉ khi là kênh nhóm (channel_name
        khác rỗng) thì toJid mới là đích hợp lệ (JID của channel) để trả lời về."""
        return self.to_jid if self.channel_name else self.sender_jid


def verify_webhook_token(authorization_header: str) -> bool:
    """Xác thực webhook Zoom gửi tới bằng Verification Token CŨ (header Authorization ==
    ZOOM_VERIFICATION_TOKEN nguyên văn, KHÔNG có tiền tố "Bearer "). Chỉ dùng cho app kiểu
    'General App + Chatbot' đời cũ. App tạo mới trên Marketplace (mục Access > Token >
    Secret Token, đi cùng Event Subscriptions) PHẢI dùng verify_webhook_signature() bên
    dưới thay vì hàm này."""
    if not config.ZOOM_VERIFICATION_TOKEN:
        return False
    return hmac.compare_digest(authorization_header, config.ZOOM_VERIFICATION_TOKEN)


def verify_webhook_signature(
    signature_header: str, timestamp_header: str, raw_body: bytes
) -> bool:
    """Xác thực webhook Zoom bằng chữ ký HMAC-SHA256 (cơ chế Secret Token + Event
    Subscriptions hiện hành trên Marketplace). Zoom ký message dạng
    "v0:{timestamp}:{raw_body}" bằng Secret Token, gửi kèm header:
      - x-zm-request-timestamp: timestamp dùng để ký
      - x-zm-signature: "v0=" + hex digest
    Xem: https://developers.zoom.us/docs/api/webhooks/#verify-webhook-events"""
    if not config.ZOOM_SECRET_TOKEN or not signature_header or not timestamp_header:
        return False
    message = f"v0:{timestamp_header}:{raw_body.decode('utf-8')}"
    computed_hash = hmac.new(
        config.ZOOM_SECRET_TOKEN.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    expected_signature = f"v0={computed_hash}"
    return hmac.compare_digest(signature_header, expected_signature)


def build_url_validation_response(plain_token: str) -> dict:
    """Xây phản hồi cho bước xác thực challenge-response khi bấm Validate trên
    Marketplace (event 'endpoint.url_validation'). Zoom POST payload chứa plainToken,
    app phải trả lại {"plainToken": ..., "encryptedToken": HMAC-SHA256(plainToken)}
    ký bằng Secret Token - KHÔNG cần verify chữ ký ở bước này vì đây là bước thiết lập.
    Xem: https://developers.zoom.us/docs/api/webhooks/#validate-your-webhook-endpoint"""
    encrypted_token = hmac.new(
        config.ZOOM_SECRET_TOKEN.encode("utf-8"),
        plain_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": encrypted_token}


async def _fetch_access_token() -> tuple[str, int]:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(
            _TOKEN_URL,
            params={"grant_type": "client_credentials"},
            auth=(config.ZOOM_CLIENT_ID, config.ZOOM_CLIENT_SECRET),
        )
    if response.status_code >= 400:
        # Log rõ lý do Zoom từ chối (invalid_client, invalid_request...) thay vì chỉ
        # raise HTTPStatusError chung chung — giúp chẩn đoán nhanh sai client_id/secret/
        # scope hay app chưa activate.
        logger.error("Zoom OAuth token request failed (%s): %s", response.status_code, response.text)
    response.raise_for_status()
    payload = response.json()
    return payload["access_token"], int(payload.get("expires_in", 3600))


async def _access_token() -> str:
    """Chatbot token (grant_type=client_credentials, scope imchat:bot) — cache theo TTL
    trừ hao 60s để tránh dùng token vừa hết hạn giữa lúc gọi API gửi tin nhắn."""
    global _cached_token, _cached_token_expiry
    async with _token_lock:
        if _cached_token and time.monotonic() < _cached_token_expiry:
            return _cached_token
        token, expires_in = await _fetch_access_token()
        _cached_token = token
        _cached_token_expiry = time.monotonic() + max(60, expires_in - 60)
        return token


class ZoomSendError(RuntimeError):
    """Một hoặc nhiều đoạn của tin nhắn đã cắt không gửi được tới Zoom sau khi retry."""


_MAX_MESSAGE_CHARS = 4096  # Giới hạn của Zoom Team Chat cho 1 tin nhắn (docs Zoom).

# Zoom Team Chat dùng phương ngữ Markdown RIÊNG (giống Slack), khác hẳn GFM mà các
# model AI hay sinh ra:
#   - Bold:      *text*   (GFM dùng **text**)
#   - Italic:    _text_   (giống nhau)
#   - Gạch ngang: ~text~   (GFM dùng ~~text~~)
#   - KHÔNG có header (#, ##, ###) và KHÔNG có bảng (| a | b |) - Zoom hiện nguyên
#     văn các ký tự đó như text thường, rất xấu.
_MD_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_MD_TABLE_SEP_CELL = re.compile(r"^:?-{2,}:?$")
_MD_HEADER = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_STRIKE = re.compile(r"~~(.+?)~~")


def _to_zoom_markdown(text: str) -> str:
    """Chuyển markdown GFM (## header, **bold**, bảng |a|b|) mà model AI hay sinh ra
    sang phương ngữ Markdown mà Zoom Team Chat thực sự hỗ trợ."""
    lines_out: list[str] = []
    for line in text.split("\n"):
        header_match = _MD_HEADER.match(line)
        table_match = _MD_TABLE_ROW.match(line)
        if header_match:
            lines_out.append(f"*{header_match.group(2).strip()}*")
            continue
        if table_match:
            cells = [c.strip() for c in table_match.group(1).split("|")]
            if all(_MD_TABLE_SEP_CELL.match(c) for c in cells if c):
                continue  # dòng phân cách "|---|---|" của bảng markdown - bỏ qua
            lines_out.append("• " + " — ".join(c for c in cells if c))
            continue
        lines_out.append(line)
    converted = "\n".join(lines_out)
    converted = _MD_BOLD.sub(r"*\1*", converted)
    converted = _MD_STRIKE.sub(r"~\1~", converted)
    return converted


async def _post_message_once(to_jid: str, text: str, user_jid: str, account_id: str) -> None:
    token = await _access_token()
    body = {
        "robot_jid": config.ZOOM_BOT_JID,
        "to_jid": to_jid,
        "user_jid": user_jid,
        "account_id": account_id,
        "is_markdown_support": True,
        "content": {"body": [{"type": "message", "text": text}]},
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(
            _MESSAGE_URL, json=body, headers={"Authorization": f"Bearer {token}"}
        )
    if response.status_code >= 400:
        logger.error(
            "Zoom send message failed (%s) to_jid=%s user_jid=%s account_id=%s: %s",
            response.status_code,
            to_jid,
            user_jid,
            account_id,
            response.text,
        )
    response.raise_for_status()


_CHUNK_RETRY_ATTEMPTS = 3
_CHUNK_RETRY_BACKOFF_SEC = 1.5


async def _post_message(to_jid: str, text: str, user_jid: str, account_id: str) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, _CHUNK_RETRY_ATTEMPTS + 1):
        try:
            await _post_message_once(to_jid, text, user_jid, account_id)
            return
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            last_exc = exc
            if attempt < _CHUNK_RETRY_ATTEMPTS:
                logger.warning(
                    "Zoom send message retry %s/%s to_jid=%s: %s",
                    attempt,
                    _CHUNK_RETRY_ATTEMPTS,
                    to_jid,
                    exc,
                )
                await asyncio.sleep(_CHUNK_RETRY_BACKOFF_SEC * attempt)
    assert last_exc is not None
    raise last_exc


def _split_message(text: str, limit: int) -> list[str]:
    """Cắt text dài thành nhiều đoạn <= limit ký tự, ưu tiên cắt tại ranh giới dòng
    trống/xuống dòng thay vì cắt cứng giữa từ hoặc giữa cặp dấu markdown."""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = window.rfind("\n\n")
        if split_at < limit // 2:
            split_at = window.rfind("\n")
        if split_at < limit // 2:
            split_at = window.rfind(" ")
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def send_message(
    to_jid: str, text: str, user_jid: str | None = None, account_id: str | None = None
) -> None:
    effective_user_jid = user_jid or to_jid
    effective_account_id = account_id or config.ZOOM_ACCOUNT_ID
    text = _to_zoom_markdown(text)
    chunks = _split_message(text, _MAX_MESSAGE_CHARS)
    errors: list[str] = []
    for chunk in chunks:
        try:
            await _post_message(to_jid, chunk, effective_user_jid, effective_account_id)
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            errors.append(str(exc))
            logger.error(
                "Zoom send message: bỏ qua 1 đoạn sau khi hết lượt retry (vẫn gửi tiếp "
                "các đoạn còn lại) to_jid=%s: %s",
                to_jid,
                exc,
            )
    if errors:
        raise ZoomSendError(
            f"Gửi {len(errors)}/{len(chunks)} đoạn tin nhắn Zoom thất bại: " + "; ".join(errors)
        )


async def _post_image_message_once(
    to_jid: str, image_url: str, caption: str, user_jid: str, account_id: str
) -> None:
    """Chatbot API hỗ trợ 1 block content kiểu "attachments" nhận thẳng
    img_url - ZOOM TỰ TẢI ảnh về từ URL đó, bot không cần upload/host lại
    (khác endpoint /chat/users/{userId}/messages/files - endpoint đó thuộc
    Zoom Chat API user-based, KHÔNG dùng robot_jid như chatbot app này).

    QUAN TRỌNG: theo đúng schema chính thức của Zoom (xem ví dụ mẫu chính chủ
    github.com/zoom/unsplash-chatbot), block "attachments" BẮT BUỘC phải nằm
    LỒNG bên trong 1 item kiểu "section" (field "sections"), KHÔNG được đặt
    trực tiếp ở top-level "body" - nếu đặt sai chỗ (như code cũ ở đây từng
    làm), Zoom không nhận diện được đây là ảnh và chỉ hiện link thô cho
    người dùng dù request vẫn trả 200 OK. Xem thêm:
    https://developers.zoom.us/docs/chat/customizing-messages/"""
    token = await _access_token()
    body = {
        "robot_jid": config.ZOOM_BOT_JID,
        "to_jid": to_jid,
        "user_jid": user_jid,
        "account_id": account_id,
        "content": {
            "body": [
                {
                    "type": "section",
                    "sections": [
                        {
                            "type": "attachments",
                            "resource_url": image_url,
                            "img_url": image_url,
                            "information": {
                                "title": {"text": (caption.strip()[:200] or "Ảnh do Agnes AI tạo")}
                            },
                        }
                    ],
                }
            ]
        },
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(
            _MESSAGE_URL, json=body, headers={"Authorization": f"Bearer {token}"}
        )
    if response.status_code >= 400:
        logger.error(
            "Zoom send image failed (%s) to_jid=%s user_jid=%s account_id=%s: %s",
            response.status_code,
            to_jid,
            user_jid,
            account_id,
            response.text,
        )
    response.raise_for_status()


async def send_image_message(
    to_jid: str,
    image_url: str,
    caption: str = "",
    user_jid: str | None = None,
    account_id: str | None = None,
) -> None:
    """Gửi 1 ảnh qua URL công khai (xem lệnh /anh - services/channel_command_service.py).
    Cùng retry-3-lần như send_message(), nhưng KHÔNG cắt đoạn (1 ảnh = 1 lần
    gọi API, không có khái niệm "quá dài" như text)."""
    effective_user_jid = user_jid or to_jid
    effective_account_id = account_id or config.ZOOM_ACCOUNT_ID
    last_exc: Exception | None = None
    for attempt in range(1, _CHUNK_RETRY_ATTEMPTS + 1):
        try:
            await _post_image_message_once(
                to_jid, image_url, caption, effective_user_jid, effective_account_id
            )
            return
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            last_exc = exc
            if attempt < _CHUNK_RETRY_ATTEMPTS:
                logger.warning(
                    "Zoom send image retry %s/%s to_jid=%s: %s",
                    attempt,
                    _CHUNK_RETRY_ATTEMPTS,
                    to_jid,
                    exc,
                )
                await asyncio.sleep(_CHUNK_RETRY_BACKOFF_SEC * attempt)
    assert last_exc is not None
    raise ZoomSendError(f"Gửi ảnh qua Zoom thất bại: {last_exc}")


def parse_event(payload: dict[str, object]) -> ZoomEvent | None:
    """Rút sự kiện tin nhắn/slash command từ webhook payload Zoom.

    LƯU Ý QUAN TRỌNG: tên field chính xác trong payload thật (userJid/user_jid,
    toJid/to_jid, cmd/message...) phụ thuộc loại app + phiên bản event Zoom gửi cho app cụ
    thể của bạn — có thể khác so với những gì hàm này giả định. Dùng tính năng gửi sự kiện
    thử trên Marketplace (mục Feature > Chatbot > Bot Endpoint URL) để xem đúng payload thật
    app nhận được, rồi chỉnh lại danh sách field bên dưới nếu cần trước khi deploy thật."""
    event_payload = payload.get("payload")
    if not isinstance(event_payload, dict):
        return None
    # nfc(): boundary nhận tin nhắn Zoom - xem core/text_normalize.py.
    text = nfc(
        str(
            event_payload.get("cmd") or event_payload.get("message") or event_payload.get("content") or ""
        ).strip()
    )
    sender_jid = str(
        event_payload.get("userJid") or event_payload.get("user_jid") or ""
    ).strip()
    to_jid = str(
        event_payload.get("toJid") or event_payload.get("to_jid") or sender_jid
    ).strip()
    channel_name = str(
        event_payload.get("channelName") or event_payload.get("channel_name") or ""
    ).strip()
    account_id = str(
        event_payload.get("accountId") or event_payload.get("account_id") or ""
    ).strip()
    event_id = str(
        event_payload.get("messageId")
        or event_payload.get("message_id")
        or f"{sender_jid}:{payload.get('event_ts', '')}"
    ).strip()
    if not sender_jid or not text or not event_id:
        return None
    return ZoomEvent(event_id, sender_jid, text, to_jid, channel_name, account_id)
