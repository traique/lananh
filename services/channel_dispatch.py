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

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core import config, trace
from services import concurrency, notify_hub, turn_control, usage_quota
from services.channel_result import ChannelResult
from services.concurrency import assistant_turn

logger = logging.getLogger(__name__)


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
    # Tin người gửi đang trả lời (Zalo quote_text) - chỉ truyền cho chat khi có.
    quote_text: str | None = None


@dataclass(frozen=True)
class DispatchHandlers:
    chat: Callable[..., Awaitable[ChannelResult]]
    get_cached: Callable[[str, str, str], Awaitable[dict | None]]
    save_cached: Callable[[str, str, str, dict], Awaitable[None]]
    facebook: Callable[[str, str], Awaitable[ChannelResult | None]] | None = None
    group: Callable[[str, str], Awaitable[ChannelResult | None]] | None = None
    chunk: Callable[[list[str]], list[str]] = field(default=lambda messages: list(messages))
    # Zalo: cắt tin KÈM styles zca-js (channels/zalo_text.zalo_rich_chunks) - có thì
    # dùng thay ``chunk``, payload có thêm "styles" (cùng chỉ số với messages).
    rich_chunk: Callable[..., tuple[list[str], list[list[dict]]]] | None = None
    # Khoá lượt theo người dùng (services.concurrency.assistant_turn); tiêm để
    # kênh/test thay thế được.
    turn: Callable[[str], Any] = field(default=assistant_turn)


async def _save_cached_best_effort(h: DispatchHandlers, req: DispatchRequest, payload) -> None:
    """Lưu cache idempotency kiểu best-effort: câu trả lời ĐÃ sinh xong không được
    mất chỉ vì DB lỗi lúc ghi cache (trước đây lỗi này làm gateway Zalo gửi
    BRIDGE_ERROR_REPLY thay cho câu trả lời). Mất cache chỉ có nghĩa 1 lượt gửi
    trùng từ gateway (hiếm) có thể chạy lại."""
    try:
        await h.save_cached(req.account_id, req.event_id, req.cache_kind, payload)
    except Exception:
        logger.warning(
            "Không lưu được cache idempotency %s/%s (DB lỗi) - vẫn trả câu trả lời.",
            req.cache_kind,
            req.event_id,
            exc_info=True,
        )


def payload_from_result(result: ChannelResult, chunk, rich_chunk=None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider": result.provider,
        "image_b64": result.image_b64,
        "image_url": result.image_url,
    }
    if rich_chunk is None:
        return {"messages": chunk(list(result.messages)), **payload}
    messages, styles = rich_chunk(list(result.messages), colorize=result.market)
    return {"messages": messages, **payload, "styles": styles if any(styles) else None}


async def _chat(h: DispatchHandlers, req: DispatchRequest, text: str) -> ChannelResult:
    # Chữ ký như handle_channel_text(user_id, text, is_admin) cũ của Zalo; kênh
    # khác bọc lambda để truyền channel=. quote_text chỉ thêm khi có (handler
    # cũ/giả lập không nhận tham số này vẫn chạy).
    if req.quote_text:
        return await h.chat(req.user_id, text, req.is_admin, quote_text=req.quote_text)
    return await h.chat(req.user_id, text, req.is_admin)


def _user_kind(req: DispatchRequest) -> str:
    if config.is_owner_user_id(req.user_id):
        return "owner"
    return "admin" if req.is_admin else "member"


async def dispatch(req: DispatchRequest, h: DispatchHandlers) -> dict[str, Any]:
    """Trả payload đã cache được: {messages, provider, image_b64, image_url}.

    Mã lượt (core/trace.py) = "<kênh>:<account_id>:<event_id>" - trùng event_key
    của kênh, gom log + provider_calls + turn_metrics của lượt."""
    with trace.turn(
        f"{req.channel}:{req.account_id}:{req.event_id}",
        channel=req.channel,
        user_kind=_user_kind(req),
    ):
        payload = await _dispatch(req, h)
    # Người dùng vừa nhắn: xả tin đang chờ gom của CHÍNH họ sau vài giây (sau câu
    # trả lời) - services/notify_hub.py.
    notify_hub.note_activity(req.user_id)
    return payload


async def _dispatch(req: DispatchRequest, h: DispatchHandlers) -> dict[str, Any]:
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
    # Lệnh chỉ đọc, không AI (/help, /notes, /lich...): cũng chạy ngoài khoá lượt
    # để dùng được trong lúc lượt dài của chính người đó đang chạy
    # (services/turn_control.py).
    if result is None and turn_control.is_readonly(text):
        result = await _chat(h, req, text)
    # Quota ngày của thành viên Zalo (services/usage_quota.py): SAU kiểm cache (lượt
    # gửi lại của gateway không bị chặn/đếm 2 lần), TRƯỚC khoá lượt (người bị chặn
    # không giữ chỗ). Câu báo hết lượt cũng được cache như mọi câu trả lời.
    decision = usage_quota.ALLOW
    if result is None:
        decision = await usage_quota.check(
            req.user_id, text, channel=req.channel, is_admin=req.is_admin
        )
        if not decision.allowed:
            result = ChannelResult([decision.notice] if decision.notice else [])

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
                # Task con huỷ được bằng /huy (services/concurrency.py). Bị huỷ thì
                # trả câu "em dừng rồi" như 1 kết quả bình thường: cache vẫn ghi để
                # lượt gửi lại của gateway/inbox không chạy lại lượt đã huỷ.
                try:
                    with usage_quota.scope(decision, req.user_id):
                        result = await concurrency.run_cancellable(
                            req.turn_key, lambda: _chat(h, req, text)
                        )
                except concurrency.TurnCancelled:
                    result = ChannelResult([await turn_control.cancelled_text_for(req.user_id)])
            payload = payload_from_result(result, h.chunk, h.rich_chunk)
            await _save_cached_best_effort(h, req, payload)
            return payload

    payload = payload_from_result(result, h.chunk, h.rich_chunk)
    await _save_cached_best_effort(h, req, payload)
    return payload
