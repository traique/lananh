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
        return "MEM"

    async def fake_chat(user_id, text, *, grounding, memory_context, require_real_search, **kw):
        calls["chat"] = (grounding, memory_context)
        return calls.get("response", _FakeResponse("trả lời", used_fallback=True))

    async def fake_add_turn(user_id, user_text, model_text):
        # 1 transaction cho cả lượt (core.database.add_chat_turn).
        calls["saved"].extend([("user", user_text), ("model", model_text)])

    async def fake_update_memory(user_id, text, reply):
        return None

    monkeypatch.setattr(ccs.telemetry, "start", fake_start)
    monkeypatch.setattr(ccs.telemetry, "success", fake_success)
    monkeypatch.setattr(ccs.telemetry, "failure", fake_failure)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", fake_tool)
    monkeypatch.setattr(ccs.web_search, "plan_search", fake_plan)
    monkeypatch.setattr(ccs.memory_service, "build_memory_context", fake_memory)
    monkeypatch.setattr(ccs.memory_service, "update_memory", fake_update_memory)
    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.db, "add_chat_turn", fake_add_turn)
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
        return ""

    async def slow_chat(user_id, text, **kwargs):
        async def call():
            await _asyncio.sleep(0.2)
            return type("R", (), {"text": "ok", "used_fallback": False})()

        return await timeouts.bounded(call)

    async def telemetry_noop(*args, **kwargs):
        return 1

    monkeypatch.setattr(svc.web_search, "plan_search", slow_search)
    monkeypatch.setattr(svc.tools, "maybe_run_tool", no_tool)
    monkeypatch.setattr(svc.memory_service, "build_memory_context", no_memory)
    monkeypatch.setattr(svc.orchestrator, "chat", slow_chat)
    monkeypatch.setattr(svc.telemetry, "start", telemetry_noop)
    monkeypatch.setattr(svc.telemetry, "failure", telemetry_noop)
    monkeypatch.setattr(svc.telemetry, "success", telemetry_noop)

    turn = await svc.run_chat_turn(1, "xin chào", channel="zalo")
    assert turn.failed and turn.timed_out
    assert turn.error_detail.startswith("Hết")


# ─── Trung thực về nhắc việc/ghi chú (ToolOutcome -> câu do code nối) ──────


def _outcome(**kw):
    from services import tool_registry as reg

    return reg.ToolOutcome(**kw)


@pytest.mark.asyncio
async def test_router_hong_co_y_dinh_nhac_thi_noi_canh_bao_chua_luu(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    async def unavailable(user_id, text):
        return _outcome(status=reg.STATUS_UNAVAILABLE, intent="reminder")

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", unavailable)
    chat_fakes["response"] = _FakeResponse("Dạ em đặt nhắc rồi ạ")

    turn = await ccs.run_chat_turn(1, "nhắc anh 8h sáng mai uống thuốc")
    await ccs.stop_background_tasks()

    assert "⚠️ Em chưa lưu được nhắc việc này" in turn.text
    assert "CHƯA lưu được nhắc việc" in chat_fakes["chat"][0]
    assert chat_fakes["saved"][-1] == ("model", turn.text)  # lịch sử lưu đúng câu đã gửi


@pytest.mark.asyncio
async def test_router_none_cho_cau_nhac_ve_nhac_khong_noi_canh_bao(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    async def none(user_id, text):
        return _outcome(status=reg.STATUS_NONE, router_ran=True)

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", none)
    chat_fakes["response"] = _FakeResponse("Hay thật anh ạ")

    turn = await ccs.run_chat_turn(1, "bài nhạc anh gửi hay ghê")

    assert turn.text == "Hay thật anh ạ"


@pytest.mark.asyncio
async def test_da_luu_ma_cau_tra_loi_thieu_gio_thi_noi_xac_nhan_cua_code(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    confirmation = "Đã đặt nhắc 1 lần lúc 08:00 Thứ Ba 27/10 (giờ VN): uống thuốc"

    async def saved(user_id, text):
        return _outcome(
            status=reg.STATUS_OK,
            name="set_reminder",
            side_effect=True,
            text=confirmation,
            confirmation=confirmation,
            confirmation_key="08:00|8h|8 giờ",
        )

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", saved)
    chat_fakes["response"] = _FakeResponse("Dạ em nhớ rồi nha anh")
    turn = await ccs.run_chat_turn(1, "nhắc anh 8h sáng mai uống thuốc")
    assert turn.text.endswith(f"✅ {confirmation}")
    assert "dùng ĐÚNG thông tin" in chat_fakes["chat"][0]

    chat_fakes["response"] = _FakeResponse("Dạ 8h sáng mai em nhắc anh uống thuốc nha")
    turn = await ccs.run_chat_turn(1, "nhắc anh 8h sáng mai uống thuốc")
    assert "✅" not in turn.text  # đã đúng giờ (1 trong các cách viết) -> không lặp xác nhận


@pytest.mark.asyncio
async def test_luot_chi_doc_truyen_allow_side_effects_va_bao_ly_do(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    seen = {}

    async def blocked(user_id, text, **kwargs):
        seen.update(kwargs)
        return _outcome(
            status=reg.STATUS_BLOCKED,
            name="set_reminder",
            side_effect=True,
            effect_label="nhắc việc",
            text="lượt này chỉ đọc",
        )

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", blocked)
    turn = await ccs.run_chat_turn(1, "nhắc anh 8h uống thuốc", allow_side_effects=False)

    assert seen == {"allow_side_effects": False}
    assert "⚠️ Em chưa lưu nhắc việc này: lượt này chỉ đọc." in turn.text


@pytest.mark.asyncio
async def test_canh_bao_chua_luu_dung_cach_xung_ho_thanh_vien(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    async def failed(user_id, text):
        return _outcome(status=reg.STATUS_FAILED, intent="note", name="save_note")

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", failed)
    turn = await ccs.run_chat_turn(
        -5, "ghi chú giúp chị mua sữa", channel="zalo", address_form="chị"
    )

    assert "⚠️ Em chưa lưu được ghi chú này - chị nhắn lại giúp em" in turn.text


@pytest.mark.asyncio
async def test_luot_hong_sau_khi_da_luu_nhac_van_bao_da_luu(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    async def saved(user_id, text):
        return _outcome(
            status=reg.STATUS_OK, side_effect=True, confirmation="Đã đặt nhắc lúc 08:00"
        )

    async def boom(*args, **kwargs):
        raise RuntimeError("provider chết")

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", saved)
    monkeypatch.setattr(ccs.orchestrator, "chat", boom)
    turn = await ccs.run_chat_turn(1, "nhắc anh 8h uống thuốc")

    assert turn.failed and turn.tool_note == "Đã đặt nhắc lúc 08:00"
    assert ccs.failure_message(turn, owner=True).endswith("✅ Đã đặt nhắc lúc 08:00")


@pytest.mark.asyncio
async def test_loi_tat_hoan_xong_chay_truoc_chat(monkeypatch):
    from services import channel_chat_service as ccs

    async def shortcut(user_id, text):
        return "✅ Dạ em ghi nhận xong: uống thuốc" if text == "xong" else None

    async def no_command(*args, **kwargs):
        return None

    async def chat_must_not_run(*args, **kwargs):
        raise AssertionError("lối tắt không được gọi AI")

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", no_command)
    monkeypatch.setattr(ccs.reminder_service, "maybe_handle_shortcut", shortcut)
    monkeypatch.setattr(ccs, "run_chat_turn", chat_must_not_run)

    result = await ccs.handle_channel_text(-5, "xong", is_admin=False, channel="zalo")

    assert result.messages == ["✅ Dạ em ghi nhận xong: uống thuốc"]


@pytest.mark.asyncio
async def test_luot_chi_doc_bo_qua_moi_nhanh_ghi_du_lieu(monkeypatch):
    """handle_channel_text(allow_side_effects=False): không lệnh /, không ghi danh mục/
    chi tiêu/lối tắt nhắc việc; lượt chat AI nhận allow_side_effects=False."""
    from services import channel_chat_service as ccs

    async def must_not_run(*args, **kwargs):
        raise AssertionError("lượt chỉ đọc không được chạy nhánh ghi dữ liệu")

    async def no_stock(user_id, text, is_admin=True):
        return None, ""

    async def none(*args, **kwargs):
        return None

    seen = {}

    async def fake_turn(user_id, text, **kwargs):
        seen.update(kwargs)
        return ccs.ChatTurn("dạ")

    for target, name in (
        (ccs.portfolio_service, "handle_command"),
        (ccs.portfolio_service, "maybe_handle_natural_language"),
        (ccs.channel_command_service, "maybe_handle_command"),
        (ccs.reminder_service, "maybe_handle_shortcut"),
        (ccs.expense_service, "maybe_handle_chat"),
    ):
        monkeypatch.setattr(target, name, must_not_run)
    monkeypatch.setattr(ccs.vn_daily_chat, "maybe_handle", none)
    monkeypatch.setattr(ccs.weather_chat, "maybe_handle", none)
    monkeypatch.setattr(ccs.lunar_chat, "maybe_handle", none)
    monkeypatch.setattr(ccs.article_reader, "maybe_handle", none)
    monkeypatch.setattr(ccs, "_handle_stock", no_stock)
    monkeypatch.setattr(ccs, "run_chat_turn", fake_turn)

    result = await ccs.handle_channel_text(
        -5, "cafe 45k", is_admin=False, channel="zalo", allow_side_effects=False
    )

    assert result.messages == ["dạ"] and seen["allow_side_effects"] is False
