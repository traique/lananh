"""Trả lời nhanh bằng code trước lượt AI - MỘT chuỗi cổng dùng chung mọi kênh.

Trước đây Telegram (handlers/chat_router._quick_reply) viết tay lại chuỗi tiền-AI của
Zalo/Zoom (channel_chat_service._side_effect_paths + handle_channel_text) và hai bản
đã lệch nhau. Giờ cả hai gọi ``resolve()`` trên cùng danh sách ``GATES``; kênh chỉ
còn lo GỬI kết quả (Telegram: tin chữ/ảnh, /noi đọc kèm; Zalo/Zoom: ChannelResult).

Cổng chạy đúng thứ tự, cổng đầu tiên trả lời thì dừng (None = xét cổng sau):

- Cổng GHI (``stage=WRITE``, bỏ qua ở lượt chỉ đọc như chữ chép từ tin thoại):
  "ghi nhớ"/"bỏ" ngay sau câu hỏi duyệt cách trả lời, lệnh / (danh mục + lệnh kênh -
  chỉ Zalo/Zoom, Telegram có CommandHandler riêng), "hoãn 10p"/"xong" sau tin nhắc,
  tác vụ định kỳ, danh mục bằng lời, chi tiêu, bữa ăn.
- Cổng ĐỌC: bài báo trong link, "có gì cần anh chú ý", lệnh từ câu tự nhiên
  (services/nl_commands.py), vàng/tỷ giá/xổ số, thời tiết, âm lịch.

Câu trả lời nhanh vào lịch sử (services/conversation_log) theo ``kind`` của cổng để
câu hỏi nối tiếp ("thế còn ngày mai?") có ngữ cảnh; ``kind=None`` không ghi.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from services import (
    article_reader,
    attention,
    channel_command_service,
    chat_context,
    conversation_log,
    expense_service,
    lunar_chat,
    nl_commands,
    nutrition_service,
    portfolio_service,
    reminder_service,
    routines,
    style_approval,
    vn_daily_chat,
    weather_chat,
)

WRITE = "write"
READ = "read"
# Kênh có bộ lệnh "/" riêng (PTB CommandHandler, bot_app.py): bỏ qua cổng lệnh /.
NATIVE_COMMAND_CHANNELS = frozenset({"telegram"})


@dataclass(frozen=True)
class QuickTurn:
    """Tin đang xét. is_admin: Telegram (chỉ chủ bot) luôn True như lệnh /."""

    user_id: int
    text: str
    channel: str
    is_admin: bool
    allow_side_effects: bool


@dataclass(frozen=True)
class QuickReply:
    """Câu trả lời nhanh; kênh tự định dạng và gửi."""

    gate: str = ""  # tên cổng đã trả lời
    messages: tuple[str, ...] = ()
    provider: str | None = None  # "api": lệnh / dùng provider dự phòng
    image_b64: str | None = None
    image_url: str | None = None
    market: bool = False  # tin danh mục: Zalo tô màu % tăng/giảm
    # Lệnh chạy từ câu tự nhiên: gửi nguyên văn như gõ lệnh, không đọc kèm /noi.
    command: bool = False

    @property
    def text(self) -> str:
        return "\n\n".join(m for m in self.messages if m)


GateFn = Callable[[QuickTurn], Awaitable["QuickReply | str | None"]]


@dataclass(frozen=True)
class Gate:
    name: str
    kind: str | None  # conversation_log.KIND_* để ghi lịch sử; None = không ghi
    run: GateFn
    stage: str = READ
    # Lệnh "/" của kênh chat; kênh trong NATIVE_COMMAND_CHANNELS bỏ qua cổng này.
    slash: bool = False
    # Việc làm thêm ngay sau khi ghi lịch sử.
    after: Callable[[QuickTurn, QuickReply], None] | None = None


# ─── Cổng ghi dữ liệu ───────────────────────────────────────────────────────


async def _style_approval(turn: QuickTurn) -> str | None:
    return await style_approval.maybe_handle_reply(turn.user_id, turn.text)


async def _portfolio_command(turn: QuickTurn) -> QuickReply | None:
    reply = await portfolio_service.handle_command(turn.user_id, turn.text)
    return None if reply is None else QuickReply(messages=(reply,), market=True)


async def _channel_command(turn: QuickTurn) -> QuickReply | None:
    result = await channel_command_service.maybe_handle_command(
        turn.user_id, turn.text, turn.is_admin, turn.channel
    )
    if result is None:
        return None
    outputs, provider = result
    return QuickReply(
        messages=tuple(outputs),
        provider=provider,
        # Ảnh /anh đi kèm qua ContextVar - lấy (và xoá) ngay sau lệnh.
        image_b64=channel_command_service.take_pending_image(),
        image_url=channel_command_service.take_pending_image_url(),
    )


async def _reminder_shortcut(turn: QuickTurn) -> str | None:
    return await reminder_service.maybe_handle_shortcut(turn.user_id, turn.text)


async def _routine(turn: QuickTurn) -> str | None:
    # TRƯỚC router AI để "mỗi sáng thứ 2 8h tóm tắt tin VNM cho anh" không thành nhắc
    # việc gửi câu cố định.
    return await routines.maybe_handle_chat(
        turn.user_id, turn.text, turn.channel, is_admin=turn.is_admin
    )


async def _portfolio_natural(turn: QuickTurn) -> QuickReply | None:
    reply = await portfolio_service.maybe_handle_natural_language(turn.user_id, turn.text)
    return None if reply is None else QuickReply(messages=(reply,), market=True)


async def _expense(turn: QuickTurn) -> str | None:
    return await expense_service.maybe_handle_chat(turn.user_id, turn.text)


async def _meal(turn: QuickTurn) -> str | None:
    # Chỉ khi NUTRITION_NL=1 (services/nutrition_service.py).
    return await nutrition_service.maybe_handle_chat(turn.user_id, turn.text)


# ─── Cổng chỉ đọc ───────────────────────────────────────────────────────────


async def _article(turn: QuickTurn) -> str | None:
    return await article_reader.maybe_handle(turn.text)


def _remember_article(turn: QuickTurn, reply: QuickReply) -> None:
    # Giữ bài vừa đọc (đã rào) cho câu hỏi nối tiếp "bài này nói gì về...".
    chat_context.remember_article(turn.user_id, turn.text, reply.text)


async def _attention(turn: QuickTurn) -> str | None:
    # Như lệnh /chuy của người nhận này: chủ bot thấy cả mục hệ thống, admin Zalo thấy
    # bài Facebook chờ duyệt, thành viên chỉ dữ liệu của mình.
    if not attention.is_attention_question(turn.text):
        return None
    return await channel_command_service.attention_text(turn.user_id, turn.channel, turn.is_admin)


async def _nl_command(turn: QuickTurn) -> QuickReply | None:
    run = await nl_commands.maybe_handle(
        turn.user_id,
        turn.text,
        channel=turn.channel,
        is_admin=turn.is_admin,
        allow_side_effects=turn.allow_side_effects,
    )
    if run is None:
        return None
    return QuickReply(
        messages=tuple(run.messages),
        image_b64=run.image_b64,
        image_url=run.image_url,
        command=True,
    )


async def _vn_daily(turn: QuickTurn) -> str | None:
    # Lượt chỉ đọc: không lưu thành phố thời tiết mặc định ("anh ở Đà Nẵng").
    return await vn_daily_chat.maybe_handle(
        turn.text, turn.user_id if turn.allow_side_effects else None
    )


async def _weather(turn: QuickTurn) -> str | None:
    return await weather_chat.maybe_handle(turn.text, user_id=turn.user_id)


async def _lunar(turn: QuickTurn) -> str | None:
    return await lunar_chat.maybe_handle(turn.text)


GATES: tuple[Gate, ...] = (
    Gate("style_approval", None, _style_approval, WRITE),
    Gate("portfolio_command", None, _portfolio_command, WRITE, slash=True),
    Gate("channel_command", None, _channel_command, WRITE, slash=True),
    Gate("reminder_shortcut", conversation_log.KIND_REMINDER, _reminder_shortcut, WRITE),
    Gate("routine", conversation_log.KIND_REMINDER, _routine, WRITE),
    Gate("portfolio_natural", conversation_log.KIND_PORTFOLIO, _portfolio_natural, WRITE),
    Gate("expense", conversation_log.KIND_EXPENSE, _expense, WRITE),
    Gate("meal", conversation_log.KIND_MEAL, _meal, WRITE),
    Gate("article", conversation_log.KIND_ARTICLE, _article, after=_remember_article),
    Gate("attention", conversation_log.KIND_DAILY, _attention),
    Gate("nl_command", conversation_log.KIND_COMMAND, _nl_command),
    Gate("vn_daily", conversation_log.KIND_DAILY, _vn_daily),
    Gate("weather", conversation_log.KIND_WEATHER, _weather),
    Gate("lunar", conversation_log.KIND_LUNAR, _lunar),
)


def _applies(gate: Gate, turn: QuickTurn, stage: str | None) -> bool:
    if stage is not None and gate.stage != stage:
        return False
    if gate.stage == WRITE and not turn.allow_side_effects:
        return False
    return not (gate.slash and turn.channel in NATIVE_COMMAND_CHANNELS)


async def resolve(
    user_id: int,
    text: str,
    channel: str,
    *,
    is_admin: bool = False,
    allow_side_effects: bool = True,
    stage: str | None = None,
) -> QuickReply | None:
    """Câu trả lời của cổng đầu tiên nhận ``text`` (đã ghi lịch sử theo ``kind`` của
    cổng); None = không cổng nào nhận, đi tiếp xuống nhánh cổ phiếu/lượt AI.

    allow_side_effects=False: bỏ mọi cổng GHI, lệnh từ câu tự nhiên chỉ chạy lệnh đọc.
    stage: chỉ xét nhóm cổng WRITE hoặc READ (None = cả hai, theo đúng thứ tự)."""
    turn = QuickTurn(user_id, text, channel, is_admin, allow_side_effects)
    for gate in GATES:
        if not _applies(gate, turn, stage):
            continue
        value = await gate.run(turn)
        if value is None:
            continue
        if isinstance(value, QuickReply):
            reply = replace(value, gate=gate.name)
        else:
            reply = QuickReply(gate.name, (value,))
        if gate.kind is not None:
            await conversation_log.record_exchange(user_id, text, reply.text, gate.kind)
        if gate.after is not None:
            gate.after(turn, reply)
        return reply
    return None
