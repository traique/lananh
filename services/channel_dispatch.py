"""Dispatcher chung cho tin nhắn văn bản tới từ Zalo và Zoom.

Trước đây web.py (Zoom) và channels/router.py (Zalo) mỗi nơi tự viết cùng
một chuỗi: đọc cache idempotency -> lệnh /fb_* ngoài khoá lượt -> khoá lượt
-> kiểm cache lại -> lệnh nhóm -> chat -> lưu cache. Hai bản đã lệch nhau dần.
Giờ cả hai gọi ``dispatch()``; mỗi kênh chỉ còn lo xác thực, resolve người
dùng và GỬI kết quả.

Handlers được TIÊM (DispatchHandlers) thay vì import cứng để kênh gọi truyền
đúng module-level name của mình (test monkeypatch ``router.handle_channel_text``
hay ``web.handle_channel_text`` vẫn có hiệu lực).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from services.channel_result import ChannelResult
from services.concurrency import assistant_turn


@dataclass(frozen=True)
class DispatchRequest:
    account_id: str  # khoá cache idempotency (account Zalo, hoặc "zoom-bot")
    event_id: str  # message_id Zalo / event_id Zoom
    cache_kind: str  # "text" (Zalo) | "zoom-text"
    user_id: int  # internal user id (Zalo: âm theo từng người; Zoom: chủ bot)
    is_admin: bool
    channel: str  # "zalo" | "zoom"
    text: str
    turn_key: str  # khoá assistant_turn ("zalo:<id>" / OWNER_TURN_KEY)
    # account_id Zalo dùng cho lệnh nhóm và /fb_* (None = không có tài khoản
    # Zalo nào đăng nhập -> bỏ qua 2 nhóm lệnh đó).
    command_account_id: str | None = None


@dataclass(frozen=True)
class DispatchHandlers:
    chat: Callable[..., Awaitable[ChannelResult]]
    get_cached: Callable[[str, str, str], Awaitable[dict | None]]
    save_cached: Callable[[str, str, str, dict], Awaitable[None]]
    facebook: Callable[[str, str], Awaitable[ChannelResult | None]] | None = None
    group: Callable[[str, str], Awaitable[ChannelResult | None]] | None = None
    chunk: Callable[[list[str]], list[str]] = field(default=lambda messages: list(messages))
    # Khoá lượt theo người dùng (services.concurrency.assistant_turn); tiêm để
    # kênh/test thay thế được.
    turn: Callable[[str], Any] = field(default=assistant_turn)


def payload_from_result(result: ChannelResult, chunk) -> dict[str, Any]:
    return {
        "messages": chunk(list(result.messages)),
        "provider": result.provider,
        "image_b64": result.image_b64,
        "image_url": result.image_url,
    }


async def dispatch(req: DispatchRequest, h: DispatchHandlers) -> dict[str, Any]:
    """Trả payload đã cache được: {messages, provider, image_b64, image_url}."""
    cached = await h.get_cached(req.account_id, req.event_id, req.cache_kind)
    if cached is not None:
        return cached

    text = req.text.strip()
    result: ChannelResult | None = None
    # /fb_* chạy NGOÀI assistant_turn() cố ý: /fb_ok gọi Facebook Graph API có
    # thể chậm và đã có atomic riêng (claim_post). Giữ khoá lượt của người này
    # suốt thời gian đó sẽ chặn chat của chính họ và chiếm 1 slot
    # MAX_CONCURRENT_TURNS.
    if req.is_admin and req.command_account_id and h.facebook is not None:
        result = await h.facebook(req.command_account_id, text)

    if result is None:
        async with h.turn(req.turn_key):
            # Re-check: một lượt gửi trùng của cùng event có thể đã được xử lý
            # xong trong lúc ta chờ khoá ở trên.
            cached = await h.get_cached(req.account_id, req.event_id, req.cache_kind)
            if cached is not None:
                return cached
            if req.is_admin and req.command_account_id and h.group is not None:
                result = await h.group(req.command_account_id, text)
            if result is None:
                # Chữ ký như handle_channel_text(user_id, text, is_admin) cũ của Zalo;
                # kênh khác bọc lambda để truyền channel=.
                result = await h.chat(req.user_id, text, req.is_admin)
            payload = payload_from_result(result, h.chunk)
            await h.save_cached(req.account_id, req.event_id, req.cache_kind, payload)
            return payload

    payload = payload_from_result(result, h.chunk)
    await h.save_cached(req.account_id, req.event_id, req.cache_kind, payload)
    return payload
