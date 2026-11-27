"""services/quick_reply.py: MỘT chuỗi cổng trả lời nhanh cho Telegram (handlers/
chat_router) và Zalo/Zoom (channel_chat_service.handle_channel_text) - cùng câu thì
cùng cổng trả lời, cùng ghi lịch sử; hai chỗ từng lệch (/chuy theo người nhận,
is_admin của tác vụ định kỳ) giờ đi chung một đường."""

from __future__ import annotations

import pytest

from core import config
from services import (
    article_reader,
    attention,
    channel_chat_service,
    channel_command_service,
    chat_context,
    conversation_log,
    expense_service,
    lunar_chat,
    nl_commands,
    nutrition_service,
    portfolio_service,
    quick_reply,
    reminder_service,
    reply_audience,
    routines,
    style_approval,
    vn_daily_chat,
    weather_chat,
)

OWNER = 42
MEMBER = -5

# Câu mồi của từng cổng (cổng giả chỉ trả lời đúng câu của mình).
TRIGGERS = {
    "style_approval": "ghi nhớ",
    "portfolio_command": "/danhmuc",
    "channel_command": "/vang",
    "reminder_shortcut": "xong",
    "routine": "mỗi sáng thứ 2 8h tóm tắt tin VNM cho anh",
    "portfolio_natural": "đã mua FPT 100 giá 120",
    "expense": "cafe 45k",
    "meal": "trưa nay anh ăn bún chả",
    "article": "https://vnexpress.net/a.html",
    "attention": "hôm nay có gì cần anh chú ý",
    "nl_command": "em làm được gì",
    "vn_daily": "giá vàng hôm nay",
    "weather": "mai Đà Lạt mưa không",
    "lunar": "còn mấy ngày nữa tết",
}
SLASH = {"portfolio_command", "channel_command"}
WRITES = {
    "style_approval",
    "portfolio_command",
    "channel_command",
    "reminder_shortcut",
    "routine",
    "portfolio_natural",
    "expense",
    "meal",
}
KINDS = {
    "reminder_shortcut": "reminder",
    "routine": "reminder",
    "portfolio_natural": "portfolio",
    "expense": "expense",
    "meal": "meal",
    "article": "article",
    "attention": "daily",
    "nl_command": "command",
    "vn_daily": "daily",
    "weather": "weather",
    "lunar": "lunar",
}


def _answer(gate: str) -> str:
    return f"[{gate}] trả lời"


@pytest.fixture
def gates(monkeypatch):
    """Thay dịch vụ sau mỗi cổng bằng bản giả: ghi lại cổng nào được gọi, với tham số gì."""
    monkeypatch.setattr(config, "ALLOWED_USER_ID", OWNER)
    seen: dict = {"fired": [], "calls": {}, "recorded": [], "articles": []}

    def fake(gate: str, *, wrap=lambda text: text):
        async def run(*args, **kwargs):
            seen["calls"].setdefault(gate, []).append((args, kwargs))
            text = next((a for a in args if isinstance(a, str) and a in TRIGGERS.values()), "")
            if text != TRIGGERS[gate]:
                return None
            seen["fired"].append(gate)
            return wrap(_answer(gate))

        return run

    monkeypatch.setattr(style_approval, "maybe_handle_reply", fake("style_approval"))
    monkeypatch.setattr(portfolio_service, "handle_command", fake("portfolio_command"))
    monkeypatch.setattr(
        channel_command_service,
        "maybe_handle_command",
        fake("channel_command", wrap=lambda text: ([text], None)),
    )
    monkeypatch.setattr(reminder_service, "maybe_handle_shortcut", fake("reminder_shortcut"))
    monkeypatch.setattr(routines, "maybe_handle_chat", fake("routine"))
    monkeypatch.setattr(
        portfolio_service, "maybe_handle_natural_language", fake("portfolio_natural")
    )
    monkeypatch.setattr(expense_service, "maybe_handle_chat", fake("expense"))
    monkeypatch.setattr(nutrition_service, "maybe_handle_chat", fake("meal"))
    monkeypatch.setattr(article_reader, "maybe_handle", fake("article"))
    monkeypatch.setattr(
        nl_commands,
        "maybe_handle",
        fake("nl_command", wrap=lambda text: nl_commands.CommandRun((text,))),
    )
    monkeypatch.setattr(vn_daily_chat, "maybe_handle", fake("vn_daily"))
    monkeypatch.setattr(weather_chat, "maybe_handle", fake("weather"))
    monkeypatch.setattr(lunar_chat, "maybe_handle", fake("lunar"))

    async def build(user_id, channel, **kwargs):
        # Cổng "có gì cần chú ý" dùng is_attention_question THẬT; chỉ thay phần dựng tin.
        seen["fired"].append("attention")
        seen["calls"].setdefault("attention", []).append(((user_id, channel), kwargs))
        return _answer("attention")

    monkeypatch.setattr(attention, "build", build)

    async def record(user_id, user_text, reply, kind="chat", **kwargs):
        seen["recorded"].append((user_text, reply, kind))
        return True

    monkeypatch.setattr(conversation_log, "record_exchange", record)
    monkeypatch.setattr(
        chat_context,
        "remember_article",
        lambda user_id, text, article: seen["articles"].append((user_id, text, article)),
    )

    async def no_stock(user_id, text, is_admin=True):
        return None, ""

    async def ai_turn(user_id, text, **kwargs):
        seen["fired"].append("ai")
        seen["ai_kwargs"] = kwargs
        return channel_chat_service.ChatTurn("trả lời AI")

    monkeypatch.setattr(channel_chat_service, "_handle_stock", no_stock)
    monkeypatch.setattr(channel_chat_service, "run_chat_turn", ai_turn)

    async def no_question(user_id, buttons=False):
        return None

    monkeypatch.setattr(style_approval, "take_question", no_question)
    return seen


class _Message:
    def __init__(self):
        self.photos = []

    async def reply_photo(self, photo=None, **kwargs):
        self.photos.append(photo)


class _Update:
    def __init__(self, user_id=OWNER):
        self.message = _Message()
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type("C", (), {"id": user_id})()


@pytest.fixture
def telegram(gates, monkeypatch):
    """Chạy 1 câu qua handlers/chat_router._quick_reply; trả (câu đọc /noi, tin đã gửi)."""
    from handlers import chat_router

    sent: list[str] = []

    async def reply_long(message, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(chat_router.common, "reply_long_text", reply_long)

    async def run(text, *, allow_side_effects=True):
        sent.clear()
        spoken = await chat_router._quick_reply(
            _Update(), OWNER, text, allow_side_effects=allow_side_effects
        )
        return spoken, list(sent)

    return run


def _fired(gates) -> list[str]:
    fired = list(gates["fired"])
    gates["fired"].clear()
    return fired


def test_thu_tu_cong_khoa_lai():
    assert [g.name for g in quick_reply.GATES] == list(TRIGGERS)
    assert {g.name for g in quick_reply.GATES if g.stage == quick_reply.WRITE} == WRITES
    assert {g.name for g in quick_reply.GATES if g.slash} == SLASH
    assert {g.name: g.kind for g in quick_reply.GATES if g.kind} == KINDS
    # Nhóm GHI đứng liền trước nhóm đọc: Zalo/Zoom chạy 2 nhóm bằng 2 lần resolve.
    stages = [g.stage for g in quick_reply.GATES]
    assert stages == sorted(stages, key=lambda s: s != quick_reply.WRITE)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", [g for g in TRIGGERS if g not in SLASH])
async def test_cung_cau_hai_kenh_cung_mot_cong(gate, gates, telegram):
    text = TRIGGERS[gate]
    spoken, sent = await telegram(text)
    tg_fired, tg_recorded = _fired(gates), list(gates["recorded"])
    gates["recorded"].clear()

    result = await channel_chat_service.handle_channel_text(OWNER, text, True, "zalo")
    zalo_fired, zalo_recorded = _fired(gates), list(gates["recorded"])

    assert tg_fired == zalo_fired == [gate]
    assert sent == result.messages == [_answer(gate)]
    assert tg_recorded == zalo_recorded
    kind = KINDS.get(gate)
    assert zalo_recorded == ([(text, _answer(gate), kind)] if kind else [])
    assert result.market == (gate == "portfolio_natural")
    # /noi đọc kèm câu trả lời nhanh; lệnh từ câu tự nhiên thì không.
    assert spoken == ("" if gate == "nl_command" else _answer(gate))


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", sorted(SLASH))
async def test_lenh_gach_cheo_chi_qua_cong_tren_zalo_zoom(gate, gates, telegram):
    """Telegram có CommandHandler riêng cho lệnh / (bot_app.py) - chuỗi trả lời nhanh
    của Telegram bỏ qua 2 cổng lệnh, Zalo/Zoom thì qua đúng cổng đó."""
    spoken, sent = await telegram(TRIGGERS[gate])
    assert spoken is None and sent == [] and _fired(gates) == []
    assert gate not in gates["calls"]
    result = await channel_chat_service.handle_channel_text(OWNER, TRIGGERS[gate], True, "zoom")
    assert _fired(gates) == [gate] and result.messages == [_answer(gate)]
    assert result.market == (gate == "portfolio_command")


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", sorted(WRITES - SLASH))
async def test_luot_chi_doc_bo_qua_cong_ghi_ca_hai_kenh(gate, gates, telegram):
    spoken, sent = await telegram(TRIGGERS[gate], allow_side_effects=False)
    assert spoken is None and sent == [] and _fired(gates) == []
    result = await channel_chat_service.handle_channel_text(
        OWNER, TRIGGERS[gate], True, "zalo", allow_side_effects=False
    )
    assert _fired(gates) == ["ai"] and result.messages == ["trả lời AI"]
    assert gates["ai_kwargs"]["allow_side_effects"] is False
    assert gate not in gates["calls"]


@pytest.mark.asyncio
async def test_luot_chi_doc_truyen_xuong_cong_doc_giong_nhau(gates, telegram):
    await telegram("câu thường", allow_side_effects=False)
    await channel_chat_service.handle_channel_text(
        OWNER, "câu thường", True, "zalo", allow_side_effects=False
    )
    nl_calls = [kwargs for _, kwargs in gates["calls"]["nl_command"]]
    assert [c["allow_side_effects"] for c in nl_calls] == [False, False]
    assert [c["channel"] for c in nl_calls] == ["telegram", "zalo"]
    # Không lưu thành phố thời tiết mặc định từ chữ chép tin thoại.
    assert [args[1] for args, _ in gates["calls"]["vn_daily"]] == [None, None]


@pytest.mark.asyncio
async def test_bai_bao_duoc_giu_cho_cau_hoi_noi_tiep(gates, telegram):
    await telegram(TRIGGERS["article"])
    await channel_chat_service.handle_channel_text(OWNER, TRIGGERS["article"], True, "zalo")
    assert gates["articles"] == [(OWNER, TRIGGERS["article"], _answer("article"))] * 2


# ─── Hai chỗ từng lệch giữa Telegram và Zalo/Zoom ────────────────────────────


@pytest.mark.asyncio
async def test_co_gi_can_chu_y_theo_nguoi_nhan_moi_kenh(gates, telegram):
    """Telegram trước đây gọi attention.build(user_id, "telegram") trần; giờ như /chuy
    (channel_command_service.attention_text): quyền + cách xưng hô của người nhận."""
    await telegram(TRIGGERS["attention"])
    with reply_audience.use(reply_audience.ReplyAudience(address_form="chị")):
        member = await channel_chat_service.handle_channel_text(
            MEMBER, TRIGGERS["attention"], False, "zalo"
        )
    assert member.messages == [_answer("attention")]
    telegram_call, member_call = gates["calls"]["attention"]
    assert telegram_call == (
        (OWNER, "telegram"),
        {"is_owner": True, "is_admin": True, "address_form": None},
    )
    assert member_call == (
        (MEMBER, "zalo"),
        {"is_owner": False, "is_admin": False, "address_form": "chị"},
    )


@pytest.mark.asyncio
async def test_tac_vu_dinh_ky_nhan_quyen_admin_cua_kenh(gates, telegram):
    """Telegram trước đây không truyền is_admin cho routines (mặc định False)."""
    await telegram(TRIGGERS["routine"])
    await channel_chat_service.handle_channel_text(MEMBER, TRIGGERS["routine"], False, "zalo")
    calls = gates["calls"]["routine"]
    assert [(args[2], kwargs) for args, kwargs in calls] == [
        ("telegram", {"is_admin": True}),
        ("zalo", {"is_admin": False}),
    ]


@pytest.mark.asyncio
async def test_telegram_doc_kem_cau_xac_nhan_tac_vu_dinh_ky(gates, monkeypatch):
    """Nhánh tác vụ định kỳ trước đây trả True (không phải câu đã gửi) nên /noi đọc
    hỏng; giờ đọc đúng câu xác nhận như mọi câu trả lời nhanh khác."""
    from handlers import chat_router

    spoken = []

    async def reply_long(message, text, **kwargs):
        return None

    async def speak(update, context, user_id, text, *, voice_in):
        spoken.append(text)

    async def send_chat_action(**kwargs):
        return None

    monkeypatch.setattr(chat_router.common, "reply_long_text", reply_long)
    monkeypatch.setattr(chat_router, "_speak", speak)
    bot = type("B", (), {"send_chat_action": staticmethod(send_chat_action)})()
    context = type("Ctx", (), {"bot": bot})()
    await chat_router.route_text(_Update(), context, TRIGGERS["routine"])
    assert spoken == [_answer("routine")]


# ─── resolve() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_tra_ten_cong_va_loc_theo_nhom(gates):
    reply = await quick_reply.resolve(OWNER, TRIGGERS["expense"], "zalo", is_admin=True)
    assert reply.gate == "expense" and reply.text == _answer("expense")
    assert (
        await quick_reply.resolve(OWNER, TRIGGERS["expense"], "zalo", stage=quick_reply.READ)
        is None
    )
    weather = await quick_reply.resolve(OWNER, TRIGGERS["weather"], "zalo", stage=quick_reply.READ)
    assert weather.gate == "weather"
    assert (
        await quick_reply.resolve(OWNER, TRIGGERS["weather"], "zalo", stage=quick_reply.WRITE)
        is None
    )
    assert await quick_reply.resolve(OWNER, "câu thường", "telegram", is_admin=True) is None


@pytest.mark.asyncio
async def test_lenh_tu_cau_tu_nhien_kem_anh(gates, monkeypatch, telegram):
    from handlers import chat_router

    async def with_image(user_id, text, **kwargs):
        return nl_commands.CommandRun(("Ảnh nè",), image_url="https://img/1.png")

    monkeypatch.setattr(nl_commands, "maybe_handle", with_image)
    update = _Update()
    assert await chat_router._quick_reply(update, OWNER, "tạo ảnh con mèo") == ""
    assert update.message.photos == ["https://img/1.png"]
    result = await channel_chat_service.handle_channel_text(OWNER, "tạo ảnh con mèo", True, "zoom")
    assert result.messages == ["Ảnh nè"] and result.image_url == "https://img/1.png"
    assert result.provider is None
