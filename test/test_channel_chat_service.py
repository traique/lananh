import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.channel_chat_service import split_for_zalo


def test_split_for_zalo_keeps_short_message():
    assert split_for_zalo("xin chào", limit=20) == ["xin chào"]


def test_split_for_zalo_prefers_paragraph_boundaries():
    text = "Đoạn thứ nhất.\n\nĐoạn thứ hai dài hơn."
    chunks = split_for_zalo(text, limit=20)
    assert chunks == ["Đoạn thứ nhất.", "Đoạn thứ hai dài", "hơn."]
    assert " ".join(chunks).replace(". ", ". ")


def test_split_for_zalo_empty_message():
    assert split_for_zalo("   ") == []


# ─── run_chat_turn: pipeline chat chung Telegram/Zalo/Zoom ──────────────────


class _FakeResponse:
    def __init__(self, text, used_fallback=False):
        self.text = text
        self.used_fallback = used_fallback


@pytest.fixture
def chat_fakes(monkeypatch):
    from services import channel_chat_service as ccs

    calls = {"telemetry": [], "saved": [], "chat": None}

    async def fake_start(user_id, action, prompt, channel="telegram"):
        calls["telemetry"].append(("start", channel))
        return 11

    async def fake_success(prompt_id, action, content):
        calls["telemetry"].append(("success", prompt_id))

    async def fake_failure(prompt_id, action, exc):
        calls["telemetry"].append(("failure", prompt_id))

    async def fake_tool(user_id, text):
        return "TOOL"

    async def fake_plan(text, user_id=None):
        from services.web_search import SearchPlan

        return SearchPlan()

    async def fake_memory(user_id, query=""):
        return ccs.memory_service.TurnMemory(context="MEM")

    async def fake_chat(user_id, text, *, grounding, memory_context, require_real_search, **kw):
        calls["chat"] = (grounding, memory_context)
        return calls.get("response", _FakeResponse("trả lời", used_fallback=True))

    async def fake_add_turn(user_id, user_text, model_text, kind="chat"):
        # 1 transaction cho cả lượt (core.database.add_chat_turn).
        calls["saved"].extend([("user", user_text), ("model", model_text)])
        calls.setdefault("kinds", []).append(kind)

    def fake_after_exchange(user_id, **kwargs):
        calls.setdefault("flush", []).append(user_id)

    monkeypatch.setattr(ccs.telemetry, "start", fake_start)
    monkeypatch.setattr(ccs.telemetry, "success", fake_success)
    monkeypatch.setattr(ccs.telemetry, "failure", fake_failure)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", fake_tool)
    monkeypatch.setattr(ccs.web_search, "plan_search", fake_plan)
    monkeypatch.setattr(ccs.memory_service, "build_turn_memory", fake_memory)
    monkeypatch.setattr(ccs.memory_service, "after_exchange", fake_after_exchange)
    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.conversation_log.db, "add_chat_turn", fake_add_turn)
    return calls


@pytest.mark.asyncio
async def test_run_chat_turn_gop_ngu_canh_va_luu_lich_su(chat_fakes):
    from services import channel_chat_service as ccs

    turn = await ccs.run_chat_turn(1, "hỏi", grounding="GIÁ", channel="zalo")
    await ccs.stop_background_tasks()

    assert turn == ccs.ChatTurn("trả lời", True, prompt_id=11)
    assert chat_fakes["chat"] == ("GIÁ\n\nTOOL", "MEM")
    assert chat_fakes["saved"] == [("user", "hỏi"), ("model", "trả lời")]
    assert chat_fakes["telemetry"] == [("start", "zalo"), ("success", 11)]


@pytest.mark.asyncio
async def test_run_chat_turn_loi_tra_failed_khong_raise(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs

    async def boom(*args, **kwargs):
        raise RuntimeError("provider chết")

    monkeypatch.setattr(ccs.orchestrator, "chat", boom)
    turn = await ccs.run_chat_turn(1, "hỏi")

    assert turn.failed and turn.prompt_id == 11
    assert chat_fakes["saved"] == []
    assert chat_fakes["telemetry"] == [("start", "telegram"), ("failure", 11)]


@pytest.mark.asyncio
async def test_zalo_chat_truyen_kenh_va_quyen_admin_xuong_persona(chat_fakes, monkeypatch):
    """Kênh và quyền admin đi tới orchestrator.chat để chọn gợi ý trình bày
    (Zalo chữ thuần) và biến thể persona (admin Zalo khác thành viên)."""
    from services import channel_chat_service as ccs

    seen = []

    async def fake_chat(user_id, text, **kwargs):
        seen.append((kwargs["channel"], kwargs["is_admin"]))
        return _FakeResponse("dạ")

    async def no_command(*args, **kwargs):
        return None

    async def no_stock(user_id, text, is_admin=True):
        return None, ""

    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.portfolio_service, "handle_command", no_command)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", no_command)
    monkeypatch.setattr(ccs, "_handle_stock", no_stock)

    await ccs.handle_channel_text(-5, "xin chào", is_admin=False, channel="zalo")
    await ccs.handle_channel_text(-6, "xin chào", is_admin=True, channel="zalo")
    await ccs.run_chat_turn(42, "xin chào")
    await ccs.stop_background_tasks()

    assert seen == [("zalo", False), ("zalo", True), ("telegram", False)]


@pytest.mark.asyncio
async def test_nhan_api_du_phong_chi_hien_cho_chu_bot(chat_fakes, monkeypatch):
    from core import config
    from services import channel_chat_service as ccs

    async def no_command(*args, **kwargs):
        return None

    async def no_stock(user_id, text, is_admin=True):
        return None, ""

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", no_command)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", no_command)
    monkeypatch.setattr(ccs, "_handle_stock", no_stock)

    # Thành viên Zalo: chỉ câu trả lời, nhãn dự phòng là chẩn đoán của chủ bot.
    member = await ccs.handle_channel_text(-1, "xin chào", channel="zalo")
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 1)
    owner = await ccs.handle_channel_text(1, "xin chào", channel="zoom")
    await ccs.stop_background_tasks()

    assert member.provider == owner.provider == "api"
    assert member.messages == ["trả lời"]
    assert owner.messages == [
        "trả lời\n\n⚙️ _Câu trả lời do API dự phòng trả về, ngôn ngữ có thể khác thường ngày._"
    ]


@pytest.mark.asyncio
async def test_link_bao_tra_nguyen_bai_khong_qua_chat(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs

    async def no_command(*args, **kwargs):
        return None

    async def fake_article(text):
        return "📰 NGUYÊN BÀI" if "vnexpress.net" in text else None

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", no_command)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", no_command)
    monkeypatch.setattr(ccs.article_reader, "maybe_handle", fake_article)

    result = await ccs.handle_channel_text(1, "https://vnexpress.net/a.html", channel="zalo")

    assert result.messages == ["📰 NGUYÊN BÀI"]
    assert chat_fakes["chat"] is None  # không gọi AI


@pytest.mark.asyncio
async def test_cau_hoi_am_lich_tra_loi_thang_khong_goi_ai(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs

    async def no_command(*args, **kwargs):
        return None

    async def router_must_not_run(user_id, text):
        raise AssertionError("không được gọi router AI cho câu âm lịch")

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", no_command)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", no_command)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", router_must_not_run)

    result = await ccs.handle_channel_text(1, "còn mấy ngày nữa tết", channel="zalo")

    assert result.messages[0].startswith("Còn ")
    assert "Tết Nguyên đán" in result.messages[0]
    assert chat_fakes["chat"] is None  # không gọi AI chat
    assert chat_fakes["telemetry"] == []  # không ghi lượt chat AI


# ─── Telegram chat_msg dùng lại run_chat_turn ───────────────────────────────


class _TgMessage:
    def __init__(self, text):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _TgUpdate:
    def __init__(self, text, user_id=42):
        self.message = _TgMessage(text)
        self.effective_message = self.message
        self.effective_user = type("U", (), {"id": user_id, "username": "owner"})()
        self.effective_chat = type("C", (), {"id": user_id})()


class _TgContext:
    def __init__(self):
        async def send_chat_action(**kwargs):
            return None

        self.bot = type("B", (), {"send_chat_action": staticmethod(send_chat_action)})()


@pytest.fixture
def telegram_chat(monkeypatch):
    from core import config
    from handlers import chat_router, stock_handler

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    sent = []
    seen = {}

    async def no_portfolio(user_id, text):
        return None

    async def route(update, user_id, text):
        return stock_handler.StockRouteResult(handled=False, grounding="GIÁ FPT")

    async def fake_reply_long(message, text):
        sent.append(text)

    monkeypatch.setattr(
        chat_router.portfolio_service, "maybe_handle_natural_language", no_portfolio
    )
    monkeypatch.setattr(chat_router.stock_handler, "maybe_handle", route)
    monkeypatch.setattr(chat_router.common, "reply_long_text", fake_reply_long)
    return chat_router, sent, seen


@pytest.mark.asyncio
async def test_telegram_chat_dung_pipeline_chung_va_nhan_api_ngan(telegram_chat, monkeypatch):
    from services import channel_chat_service as ccs

    chat_router, sent, seen = telegram_chat

    async def fake_turn(user_id, text, *, grounding, channel):
        seen.update(user_id=user_id, text=text, grounding=grounding, channel=channel)
        return ccs.ChatTurn("dạ em đây", used_fallback=True, prompt_id=5)

    monkeypatch.setattr(chat_router.channel_chat_service, "run_chat_turn", fake_turn)
    update = _TgUpdate("chào em")
    await chat_router.chat_msg(update, _TgContext())

    assert seen == {"user_id": 42, "text": "chào em", "grounding": "GIÁ FPT", "channel": "telegram"}
    assert sent == ["dạ em đây\n\n⚙️ API"]


@pytest.mark.asyncio
async def test_telegram_chat_loi_va_rong_giu_thong_bao_cu(telegram_chat, monkeypatch):
    import messages
    from services import channel_chat_service as ccs

    chat_router, sent, _ = telegram_chat
    turns = [ccs.ChatTurn(failed=True), ccs.ChatTurn("", used_fallback=True)]

    async def fake_turn(*args, **kwargs):
        return turns.pop(0)

    monkeypatch.setattr(chat_router.channel_chat_service, "run_chat_turn", fake_turn)
    update = _TgUpdate("chào em")
    await chat_router.chat_msg(update, _TgContext())
    await chat_router.chat_msg(update, _TgContext())

    assert update.message.replies == [messages.friendly_error("chat", owner=True)]
    assert "Gemini" not in update.message.replies[0]
    assert sent == [messages.CHAT_GENERIC_ERROR]


@pytest.mark.asyncio
async def test_run_chat_turn_shares_one_deadline_across_search_and_chat(monkeypatch):
    """Hạn chót CHAT_TURN_TIMEOUT_SEC áp cho CẢ lượt: web search + chat cộng dồn
    vượt hạn thì lượt dừng với thông báo hết giờ, không mỗi lượt gọi 1 hạn riêng."""
    import asyncio as _asyncio

    from ai import timeouts
    from services import channel_chat_service as svc

    monkeypatch.setattr(timeouts, "_default_turn_seconds", lambda: 0.3)

    async def slow_search(text, user_id=None):
        async def call():
            await _asyncio.sleep(0.2)
            return svc.web_search.SearchPlan("kết quả", "off", True)

        return await timeouts.bounded(call)

    async def no_tool(user_id, text):
        return ""

    async def no_memory(user_id, query=""):
        from services.memory_service import TurnMemory

        return TurnMemory()

    async def slow_chat(user_id, text, **kwargs):
        async def call():
            await _asyncio.sleep(0.2)
            return type("R", (), {"text": "ok", "used_fallback": False})()

        return await timeouts.bounded(call)

    async def telemetry_noop(*args, **kwargs):
        return 1

    monkeypatch.setattr(svc.web_search, "plan_search", slow_search)
    monkeypatch.setattr(svc.tools, "maybe_run_tool", no_tool)
    monkeypatch.setattr(svc.memory_service, "build_turn_memory", no_memory)
    monkeypatch.setattr(svc.orchestrator, "chat", slow_chat)
    monkeypatch.setattr(svc.telemetry, "start", telemetry_noop)
    monkeypatch.setattr(svc.telemetry, "failure", telemetry_noop)
    monkeypatch.setattr(svc.telemetry, "success", telemetry_noop)

    turn = await svc.run_chat_turn(1, "xin chào", channel="zalo")
    assert turn.failed and turn.timed_out
    assert turn.error_detail.startswith("Hết")
