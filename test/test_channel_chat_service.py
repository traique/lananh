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

    async def fake_last_row(user_id):
        return calls.get("last_row")

    async def fake_kb(text):
        calls.setdefault("kb_calls", []).append(text)
        return calls.get("kb", "")

    monkeypatch.setattr(ccs.telemetry, "start", fake_start)
    monkeypatch.setattr(ccs.telemetry, "success", fake_success)
    monkeypatch.setattr(ccs.telemetry, "failure", fake_failure)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", fake_tool)
    monkeypatch.setattr(ccs.web_search, "plan_search", fake_plan)
    monkeypatch.setattr(ccs.memory_service, "build_turn_memory", fake_memory)
    monkeypatch.setattr(ccs.memory_service, "after_exchange", fake_after_exchange)
    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.conversation_log.db, "add_chat_turn", fake_add_turn)
    monkeypatch.setattr(ccs.conversation_log, "last_model_row", fake_last_row)
    # Kho rag/ thật của repo không được lọt vào test pipeline chung.
    monkeypatch.setattr(ccs.chat_context, "kb_grounding", fake_kb)
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
async def test_run_chat_turn_tra_id_cap_lich_su_vua_luu(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs

    async def add_turn(user_id, user_text, model_text, kind="chat"):
        return [21, 22]

    monkeypatch.setattr(ccs.conversation_log.db, "add_chat_turn", add_turn)
    turn = await ccs.run_chat_turn(1, "hỏi")
    await ccs.stop_background_tasks()
    assert turn.text == "trả lời" and turn.history_ids == (21, 22)


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
    # Câu lịch dương cũng trả bằng code (bộ tra lịch /lich), không sang router/chat AI.
    weekday = await ccs.handle_channel_text(1, "20/11 là thứ mấy", channel="zalo")
    assert "20/11" in weekday.messages[0] and "dương lịch" in weekday.messages[0]
    assert chat_fakes["chat"] is None  # không gọi AI chat
    assert chat_fakes["telemetry"] == []  # không ghi lượt chat AI


@pytest.mark.asyncio
async def test_tin_duoc_trich_dan_vao_grounding_co_rao_khong_gop_loi_nguoi_dung(chat_fakes):
    from services import channel_chat_service as ccs

    turn = await ccs.run_chat_turn(1, "ý 2 là sao em?", quote_text="1. Mua\n2. Giữ tiền mặt")
    await ccs.stop_background_tasks()
    grounding, _ = chat_fakes["chat"]
    assert "đang trả lời/trích dẫn" in grounding and "2. Giữ tiền mặt" in grounding
    assert "DỮ_LIỆU_NGOÀI" in grounding  # rào như dữ liệu ngoài
    assert chat_fakes["saved"][0] == ("user", "ý 2 là sao em?")
    # Lượt có dữ liệu ngoài: trích nhớ chỉ đọc lời người dùng.
    assert chat_fakes["kinds"] == ["chat_ext"] and turn.text == "trả lời"


@pytest.mark.asyncio
async def test_chat_ngay_sau_bao_cao_co_phieu_khong_duoc_noi_nguoc_policy(chat_fakes):
    from services import channel_chat_service as ccs
    from services import conversation_log

    report = (
        "📊 **FPT** — **120.500 VND**\nTín hiệu hệ thống: **🟡 GIỮ** (confidence 0.6)\n"
        "Đang giữ | Cân nhắc cắt lỗ dưới 110.000"
    )
    chat_fakes["last_row"] = {
        "kind": "stock",
        "content": conversation_log.stock_record([report], full_access=True),
    }
    chat_fakes["response"] = _FakeResponse("Anh nên bán hết FPT ngay đi.")
    turn = await ccs.run_chat_turn(1, "sao em bảo giữ?")
    await ccs.stop_background_tasks()
    assert turn.text.startswith("⚠️ Bản diễn giải AI lần này")
    assert "GIỮ" in turn.text and "nên bán" not in turn.text


@pytest.mark.asyncio
async def test_kenh_zalo_luu_tra_loi_nhanh_va_bao_cao_co_phieu_theo_quyen(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs

    async def no_command(*args, **kwargs):
        return None

    async def stock(user_id, text, is_admin=True):
        report = "📊 FPT\nThông tin tham khảo\nNên mua quanh vùng mua 118.000"
        return ccs.ChannelResult([report]), ""

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", no_command)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", no_command)
    monkeypatch.setattr(ccs.article_reader, "maybe_handle", no_command)
    monkeypatch.setattr(ccs.vn_daily_chat, "maybe_handle", no_command)
    monkeypatch.setattr(ccs.weather_chat, "maybe_handle", no_command)
    monkeypatch.setattr(ccs.lunar_chat, "maybe_handle", no_command)
    monkeypatch.setattr(ccs, "_handle_stock", stock)
    await ccs.handle_channel_text(-5, "phân tích FPT", is_admin=False, channel="zalo")
    assert chat_fakes["kinds"] == ["stock"]
    stored = chat_fakes["saved"][1][1]
    assert stored.startswith("[Báo cáo tham khảo") and "vùng mua" not in stored.lower()


@pytest.mark.asyncio
async def test_kenh_zalo_truyen_tin_trich_dan_xuong_chat(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs

    async def no_command(*args, **kwargs):
        return None

    async def no_stock(user_id, text, is_admin=True):
        return None, ""

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", no_command)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", no_command)
    monkeypatch.setattr(ccs, "_handle_stock", no_stock)
    await ccs.handle_channel_text(-5, "ý này là sao", is_admin=False, quote_text="Tin cũ ABC")
    await ccs.stop_background_tasks()
    assert "Tin cũ ABC" in chat_fakes["chat"][0]


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

    async def fake_reply_long(message, text, **kwargs):
        # Câu trả lời chat giờ kèm nút 👍/👎 (reply_markup) ở đoạn cuối.
        sent.append(text)
        seen["reply_markup"] = kwargs.get("reply_markup")

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

    async def fake_turn(user_id, text, *, grounding, channel, quote_text=None):
        seen.update(user_id=user_id, text=text, grounding=grounding, channel=channel)
        seen["quote"] = quote_text
        return ccs.ChatTurn("dạ em đây", used_fallback=True, prompt_id=5)

    monkeypatch.setattr(chat_router.channel_chat_service, "run_chat_turn", fake_turn)
    update = _TgUpdate("chào em")
    await chat_router.chat_msg(update, _TgContext())

    keyboard = seen.pop("reply_markup")
    assert seen == {
        "user_id": 42,
        "text": "chào em",
        "grounding": "GIÁ FPT",
        "channel": "telegram",
        "quote": None,
    }
    assert sent == ["dạ em đây\n\n⚙️ API"]
    # Có số liệu cổ phiếu kèm theo: chỉ 👍/👎, không có nút làm lại.
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert labels == ["👍", "👎"]


def test_telegram_lay_tin_duoc_tra_loi_hoac_doan_trich():
    from handlers import chat_router

    msg = _TgMessage("ý 2 là sao em?")
    assert chat_router.quoted_text(msg) is None
    msg.reply_to_message = type("R", (), {"text": None, "caption": "Chú thích ảnh"})()
    assert chat_router.quoted_text(msg) == "Chú thích ảnh"
    msg.reply_to_message = type("R", (), {"text": "x" * 900, "caption": None})()
    assert chat_router.quoted_text(msg) == "x" * 600
    # Đoạn trích chọn riêng (Message.quote) được ưu tiên.
    msg.quote = type("Q", (), {"text": "đoạn được chọn"})()
    assert chat_router.quoted_text(msg) == "đoạn được chọn"


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
async def test_nhac_xa_ngay_cau_tra_loi_phai_co_ca_ngay(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    confirmation = "Đã đặt nhắc 1 lần lúc 08:00 Chủ Nhật 15/11 (giờ VN): đóng tiền nhà"

    async def saved(user_id, text):
        return _outcome(
            status=reg.STATUS_OK,
            name="set_reminder",
            side_effect=True,
            text=confirmation,
            confirmation=confirmation,
            confirmation_key="08:00|8h|8 giờ&15/11|ngày 15",
        )

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", saved)
    # Đúng giờ nhưng sai ngày -> code nối câu xác nhận chuẩn.
    chat_fakes["response"] = _FakeResponse("Dạ 8h sáng mai em nhắc anh đóng tiền nhà nha")
    turn = await ccs.run_chat_turn(1, "nhắc anh ngày 15 8h sáng đóng tiền nhà")
    assert turn.text.endswith(f"✅ {confirmation}")

    chat_fakes["response"] = _FakeResponse("Dạ 8h sáng ngày 15 em nhắc anh đóng tiền nhà nha")
    turn = await ccs.run_chat_turn(1, "nhắc anh ngày 15 8h sáng đóng tiền nhà")
    assert "✅" not in turn.text


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

    async def daily(text, user_id=None):
        seen["daily_user_id"] = user_id  # None = không lưu thành phố thời tiết mặc định
        return None

    monkeypatch.setattr(ccs.vn_daily_chat, "maybe_handle", daily)
    monkeypatch.setattr(ccs.weather_chat, "maybe_handle", none)
    monkeypatch.setattr(ccs.lunar_chat, "maybe_handle", none)
    monkeypatch.setattr(ccs.article_reader, "maybe_handle", none)
    monkeypatch.setattr(ccs, "_handle_stock", no_stock)
    monkeypatch.setattr(ccs, "run_chat_turn", fake_turn)

    result = await ccs.handle_channel_text(
        -5, "cafe 45k", is_admin=False, channel="zalo", allow_side_effects=False
    )

    assert result.messages == ["dạ"] and seen["allow_side_effects"] is False
    assert seen["daily_user_id"] is None


# ─── Tích hợp: trí nhớ/lịch sử (conversation-memory) + tool trung thực ─────────


@pytest.mark.asyncio
async def test_luot_chi_doc_co_trich_dan_va_link_giu_ca_hai_goi(chat_fakes, monkeypatch):
    """1 lượt có cả tin trích dẫn + link (gói trí nhớ) lẫn tool bị chặn vì lượt chỉ
    đọc (gói tool): grounding giữ đủ khối, router không được đọc link lần 2, câu
    cảnh báo do code nối vào và lịch sử lưu đúng câu đã gửi."""
    from core import config
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 1)
    seen = {}

    async def blocked(user_id, text, **kwargs):
        seen["kwargs"] = kwargs
        seen["excluded"] = ccs.tools._turn_excluded.get()
        seen["admin"] = ccs.tools._turn_admin.get()
        return _outcome(
            status=reg.STATUS_BLOCKED,
            name="save_note",
            side_effect=True,
            effect_label="ghi chú",
            text="lượt này chỉ đọc",
        )

    async def page(user_id, text, *, allowed):
        seen["page_allowed"] = allowed
        return ccs.chat_context.PageContext(
            grounding="[Nội dung trang người dùng gửi: https://vnexpress.net/a]\nTRANG",
            url="https://vnexpress.net/a",
            read_ok=True,
            attempted=True,
        )

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", blocked)
    monkeypatch.setattr(ccs.chat_context, "read_link", page)
    chat_fakes["response"] = _FakeResponse("Dạ bài này nói về giá vàng ạ")

    turn = await ccs.run_chat_turn(
        1,
        "ghi chú giúp anh ý chính https://vnexpress.net/a",
        quote_text="Tin cũ về vàng",
        allow_side_effects=False,
    )
    await ccs.stop_background_tasks()

    assert seen["kwargs"] == {"allow_side_effects": False}
    assert "read_link" in seen["excluded"] and seen["page_allowed"] is True
    grounding, _ = chat_fakes["chat"]
    assert "Tin cũ về vàng" in grounding  # quote_grounding
    assert "TRANG" in grounding  # chat_context.read_link
    assert "CHƯA lưu được ghi chú" in grounding  # ToolOutcome.grounding()
    assert turn.text.endswith("⚠️ Em chưa lưu ghi chú này: lượt này chỉ đọc.")
    assert chat_fakes["saved"][-1] == ("model", turn.text)
    # Tool ghi bị chặn ở lượt chỉ đọc: câu trả lời nói chưa lưu -> không trích nhớ lượt này.
    assert chat_fakes["kinds"] == ["chat_readonly"]


@pytest.mark.asyncio
async def test_thanh_vien_khong_duoc_doc_link_thi_router_cung_khong_co_read_link(
    chat_fakes, monkeypatch
):
    from services import channel_chat_service as ccs

    monkeypatch.delenv("CHAT_READ_LINKS_MEMBERS", raising=False)
    seen = {}

    async def tool(user_id, text, **kwargs):
        seen["excluded"] = ccs.tools._turn_excluded.get()
        return _outcome()

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", tool)
    await ccs.run_chat_turn(-5, "tóm tắt https://example.com/a", channel="zalo")
    await ccs.stop_background_tasks()
    assert "read_link" in seen["excluded"]
    # Ngoài lượt chat thì không ẩn tool nào.
    assert ccs.tools._turn_excluded.get() == frozenset()


@pytest.mark.asyncio
async def test_router_da_tra_rag_thi_bo_doan_kho_tu_tra_trung(chat_fakes, monkeypatch):
    from core import config
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 1)

    async def rag(user_id, text, **kwargs):
        return _outcome(status=reg.STATUS_OK, name="rag_search", text="ĐOẠN RAG ROUTER")

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", rag)
    monkeypatch.setattr(ccs.chat_context, "kb_allowed", lambda *a, **k: True)
    chat_fakes["kb"] = "ĐOẠN RAG TỰ TRA"
    await ccs.run_chat_turn(1, "trong tài liệu của anh nói gì về DCA")
    await ccs.stop_background_tasks()
    grounding, _ = chat_fakes["chat"]
    assert "ĐOẠN RAG ROUTER" in grounding and "ĐOẠN RAG TỰ TRA" not in grounding


@pytest.mark.asyncio
async def test_sau_bao_cao_co_phieu_cau_noi_nguoc_bi_thay_nhung_van_noi_xac_nhan_nhac(
    chat_fakes, monkeypatch
):
    from services import channel_chat_service as ccs
    from services import conversation_log
    from services import tool_registry as reg

    report = "📊 **FPT** — **120.500 VND**\nTín hiệu hệ thống: **🟡 GIỮ** (confidence 0.6)"
    chat_fakes["last_row"] = {
        "kind": "stock",
        "content": conversation_log.stock_record([report], full_access=True),
    }

    async def saved(user_id, text, **kwargs):
        return _outcome(
            status=reg.STATUS_OK,
            name="set_reminder",
            side_effect=True,
            confirmation="Đã đặt nhắc lúc 14:00: xem FPT",
            confirmation_key="14:00|14h|2h chiều",
        )

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", saved)
    chat_fakes["response"] = _FakeResponse("Anh nên bán hết FPT ngay đi.")
    turn = await ccs.run_chat_turn(1, "2h chiều nhắc anh xem FPT")
    await ccs.stop_background_tasks()
    assert turn.text.startswith("⚠️ Bản diễn giải AI lần này") and "nên bán" not in turn.text
    assert turn.text.endswith("✅ Đã đặt nhắc lúc 14:00: xem FPT")


@pytest.mark.asyncio
async def test_kenh_zalo_luu_lich_su_loi_tat_nhac_va_chi_tieu(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs

    async def no_command(*args, **kwargs):
        return None

    async def shortcut(user_id, text):
        return "✅ Dạ xong: uống thuốc" if text == "xong" else None

    async def expense(user_id, text):
        return "✅ Đã ghi #3: cafe 45.000đ" if "45k" in text else None

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", no_command)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", no_command)
    monkeypatch.setattr(ccs.channel_command_service, "maybe_handle_command", no_command)
    monkeypatch.setattr(ccs.reminder_service, "maybe_handle_shortcut", shortcut)
    monkeypatch.setattr(ccs.expense_service, "maybe_handle_chat", expense)

    await ccs.handle_channel_text(-5, "xong", is_admin=False)
    await ccs.handle_channel_text(-5, "cafe 45k", is_admin=False)

    assert chat_fakes["kinds"] == ["reminder", "expense"]
    assert ("model", "✅ Đã ghi #3: cafe 45.000đ") in chat_fakes["saved"]
    assert chat_fakes.get("flush") is None  # lượt code không kích trích trí nhớ


@pytest.mark.asyncio
async def test_telegram_luu_lich_su_loi_tat_nhac_va_chi_tieu(telegram_chat, monkeypatch):
    from services import channel_chat_service as ccs
    from services import conversation_log

    chat_router, sent, _ = telegram_chat
    recorded = []

    async def shortcut(user_id, text):
        return "✅ Dạ xong: uống thuốc" if text == "xong" else None

    async def expense(user_id, text):
        return "✅ Đã ghi #3: cafe 45.000đ" if "45k" in text else None

    async def record(user_id, user_text, reply, kind="chat", **kwargs):
        recorded.append((user_text, reply, kind))
        return True

    async def turn_must_not_run(*args, **kwargs):
        raise AssertionError("lối tắt/chi tiêu không được gọi AI")

    monkeypatch.setattr(chat_router.reminder_service, "maybe_handle_shortcut", shortcut)
    monkeypatch.setattr(chat_router.expense_service, "maybe_handle_chat", expense)
    monkeypatch.setattr(conversation_log, "record_exchange", record)
    monkeypatch.setattr(ccs, "run_chat_turn", turn_must_not_run)

    await chat_router.chat_msg(_TgUpdate("xong"), _TgContext())
    await chat_router.chat_msg(_TgUpdate("cafe 45k"), _TgContext())

    assert sent == ["✅ Dạ xong: uống thuốc", "✅ Đã ghi #3: cafe 45.000đ"]
    assert recorded == [
        ("xong", "✅ Dạ xong: uống thuốc", "reminder"),
        ("cafe 45k", "✅ Đã ghi #3: cafe 45.000đ", "expense"),
    ]


# ─── Lọc link lạ ở lượt provider tự tra web ─────────────────────────────────


class _SearchedResponse(_FakeResponse):
    def __init__(self, text, citations=()):
        super().__init__(text)
        self.searched = True
        self.citations = list(citations)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["untrusted", "all"])
async def test_luot_tu_tra_web_van_vo_hieu_link_mang_du_lieu(chat_fakes, monkeypatch, mode):
    """Provider tự tra web (native/forced, Gemini AUTO có grounding) đọc trang lạ
    cùng persona/trí nhớ: link mang dữ liệu (query string không khớp nguồn) vẫn bị
    vô hiệu hoá; link trích dẫn không query vẫn giữ."""
    from core import config
    from services import channel_chat_service as ccs

    monkeypatch.setattr(config, "REPLY_URL_FILTER", mode)
    chat_fakes["response"] = _SearchedResponse(
        "Giá vàng 80tr (https://vnexpress.net/gia-vang-1.html, https://sjc.com.vn/gia?ngay=26). "
        "Xem https://evil.example/?d=ho-so-cua-anh",
        citations=["https://sjc.com.vn/gia?ngay=26"],
    )
    turn = await ccs.run_chat_turn(1, "giá vàng hôm nay")
    await ccs.stop_background_tasks()

    assert "https://vnexpress.net/gia-vang-1.html" in turn.text
    assert "https://sjc.com.vn/gia?ngay=26" in turn.text
    assert "https://evil.example" not in turn.text
    assert "hxxps://evil[.]example/?d=ho-so-cua-anh" in turn.text


@pytest.mark.asyncio
async def test_luot_tu_tra_web_co_du_lieu_ngoai_loc_day_du_kem_nguon_trich_dan(
    chat_fakes, monkeypatch
):
    """Lượt vừa có dữ liệu ngoài đã rào (Tavily mỏng) vừa tự tra web: lọc đầy đủ,
    nguồn trích dẫn của provider là nguồn được phép."""
    from core import config, untrusted
    from services import channel_chat_service as ccs

    monkeypatch.setattr(config, "REPLY_URL_FILTER", "untrusted")
    chat_fakes["response"] = _SearchedResponse(
        "Theo https://sjc.com.vn/bang-gia và https://la.example/bai-khac.",
        citations=["https://sjc.com.vn/bang-gia"],
    )
    fenced = untrusted.wrap_untrusted("kết quả mỏng https://cafef.vn/a", "Tavily")
    turn = await ccs.run_chat_turn(1, "giá vàng hôm nay", grounding=fenced)
    await ccs.stop_background_tasks()

    assert "https://sjc.com.vn/bang-gia" in turn.text
    assert "hxxps://la[.]example/bai-khac" in turn.text


@pytest.mark.asyncio
async def test_tat_loc_link_thi_luot_tu_tra_web_giu_nguyen(chat_fakes, monkeypatch):
    from core import config
    from services import channel_chat_service as ccs

    monkeypatch.setattr(config, "REPLY_URL_FILTER", "off")
    chat_fakes["response"] = _SearchedResponse("Xem https://evil.example/?d=1")
    turn = await ccs.run_chat_turn(1, "giá vàng hôm nay")
    await ccs.stop_background_tasks()
    assert turn.text == "Xem https://evil.example/?d=1"


_FPT_GROUNDING = (
    "[DỮ LIỆU GIÁ THỰC TẾ lúc 10:00 26/10/2026 giờ VN, lấy trực tiếp từ DNSE - ...:\n"
    "- FPT: 120.500 VND (10:00), +1.500 (+1,26%) so với phiên trước (119.000 VND)]"
)


@pytest.mark.asyncio
async def test_thanh_vien_hoi_ma_co_so_lieu_gia_khong_nhan_ke_hoach_giao_dich(chat_fakes):
    """Lượt chat có số liệu giá DNSE của thành viên Zalo: code bỏ dòng vùng mua/stop."""
    from services import channel_chat_service as ccs

    chat_fakes["response"] = _FakeResponse(
        "FPT là doanh nghiệp công nghệ lớn, tăng trưởng đều.\n"
        "Chị nên mua tích luỹ vùng 120-125, cắt lỗ dưới 110."
    )
    question = "em ơi FPT có đáng để tích luỹ dài hạn không"
    turn = await ccs.run_chat_turn(
        -5, question, grounding=_FPT_GROUNDING, channel="zalo", address_form="chị"
    )
    assert turn.text.startswith("FPT là doanh nghiệp công nghệ lớn")
    assert "cắt lỗ" not in turn.text and "nên mua" not in turn.text
    assert "thông tin tham khảo" in turn.text and turn.text.endswith("nha chị.")
    # Cả câu chỉ toàn kế hoạch: câu báo + dòng giá, không bao giờ rỗng.
    chat_fakes["response"] = _FakeResponse("Nên mua vùng 120, stop 110, target 140.")
    turn = await ccs.run_chat_turn(-5, question, grounding=_FPT_GROUNDING, channel="zalo")
    assert "FPT: 120.500 VND" in turn.text and "stop" not in turn.text
    # Admin Zalo giữ bản đầy đủ như trước.
    plan = "Nên mua vùng 120, stop 110."
    chat_fakes["response"] = _FakeResponse(plan)
    turn = await ccs.run_chat_turn(
        -6, question, grounding=_FPT_GROUNDING, channel="zalo", is_admin=True
    )
    assert turn.text == plan
    await ccs.stop_background_tasks()


@pytest.mark.asyncio
async def test_khong_phat_ban_nhap_ngay_sau_bao_cao_co_phieu(chat_fakes, monkeypatch):
    from ai import drafts
    from services import channel_chat_service as ccs
    from services import conversation_log

    seen = []

    async def fake_chat(user_id, text, **kw):
        seen.append(drafts.partial_sink() is not None)
        return _FakeResponse("Dạ anh.")

    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    with drafts.sink_scope(lambda text: None):
        await ccs.run_chat_turn(1, "chào em")
        chat_fakes["last_row"] = {
            "kind": conversation_log.KIND_STOCK,
            "content": conversation_log.stock_record(
                ["📊 **HPG** — **27.500 VND**\nTín hiệu hệ thống: **🟡 GIỮ** (x)\nĐang giữ"],
                full_access=True,
            ),
        }
        await ccs.run_chat_turn(1, "vậy có nên bán không?")
    await ccs.stop_background_tasks()
    assert seen == [True, False]


@pytest.mark.asyncio
async def test_luot_chi_doc_nho_giup_khong_de_bo_trich_nho_tu_luu(chat_fakes, monkeypatch):
    """Tin thoại "nhớ giúp anh..." (tool ghi bị chặn): câu trả lời nói chưa lưu thì
    bộ trích nhớ không được lưu thay - không flush ngay, kind ngoài MEMORY_KINDS."""
    from services import channel_chat_service as ccs
    from services import conversation_log
    from services import tool_registry as reg

    flushes = []
    monkeypatch.setattr(ccs.memory_service, "after_exchange", lambda uid, **kw: flushes.append(kw))

    async def blocked(user_id, text, **kwargs):
        return _outcome(
            status=reg.STATUS_BLOCKED,
            name="remember_fact",
            side_effect=True,
            effect_label="điều cần nhớ",
            text="lượt này chỉ đọc",
        )

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", blocked)
    turn = await ccs.run_chat_turn(1, "nhớ giúp anh là sinh nhật vợ 12/5", allow_side_effects=False)
    assert "chưa lưu" in turn.text
    assert chat_fakes["kinds"] == [conversation_log.KIND_CHAT_READONLY]
    assert conversation_log.KIND_CHAT_READONLY not in conversation_log.MEMORY_KINDS
    assert flushes == []

    async def no_tool(user_id, text, **kwargs):
        return None

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", no_tool)
    await ccs.run_chat_turn(1, "hôm nay anh mệt quá", allow_side_effects=False)
    assert chat_fakes["kinds"][-1] == conversation_log.KIND_CHAT
    assert flushes == [{"explicit": False}]


# ─── Sửa 27/10: hỏi giá sản phẩm không bị nhánh cổ phiếu chặn ───────────────


@pytest.mark.asyncio
async def test_hoi_gia_san_pham_di_xuong_chat_co_tra_web_that(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs

    seen = {}

    async def fake_chat(user_id, text, *, grounding, memory_context, require_real_search, **kw):
        seen["forced"] = require_real_search
        return _FakeResponse("Giá tham khảo...")

    async def no_symbols(text):
        return []

    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.stock_analysis, "find_valid_symbols", no_symbols)
    stock, grounding = await ccs._handle_stock(1, "giá samsung s24 plus cũ")
    assert stock is None and grounding == ""
    turn = await ccs.run_chat_turn(1, "giá samsung s24 plus cũ", channel="zoom")
    await ccs.stop_background_tasks()
    assert turn.text == "Giá tham khảo..." and seen["forced"] is True
    # Hỏi cổ phiếu rõ ràng mà không ra mã: vẫn báo "chưa tra ra mã".
    blocked, _ = await ccs._handle_stock(1, "giá cổ phiếu ABCXYZ")
    assert blocked is not None and "chưa tra ra mã" in blocked.messages[0]


@pytest.mark.asyncio
async def test_telegram_hoi_gia_san_pham_khong_bao_loi_ma(monkeypatch):
    from handlers import stock_handler

    async def no_symbols(text):
        return []

    sent = []

    class Msg:
        async def reply_text(self, text, **kwargs):
            sent.append(text)

    update = type("U", (), {"message": Msg()})()
    monkeypatch.setattr(stock_handler.stock_analysis, "find_valid_symbols", no_symbols)
    route = await stock_handler.maybe_handle(update, 1, "giá samsung s24 plus cũ")
    assert route.handled is False and sent == []
    route = await stock_handler.maybe_handle(update, 1, "giá cổ phiếu ABCXYZ")
    assert route.handled is True and "chưa tra ra mã" in sent[0]


# ─── Persona: phần phân tích cổ phiếu chỉ kèm lượt cổ phiếu/đầu tư ──────────


@pytest.mark.asyncio
async def test_phan_co_phieu_cua_persona_chi_kem_luot_co_phieu(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs
    from services import conversation_log

    seen = []

    async def fake_chat(user_id, text, **kw):
        seen.append(kw["stock_rules"])
        return _FakeResponse("Dạ anh.")

    async def no_tool(user_id, text, **kwargs):
        return _outcome()

    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", no_tool)
    await ccs.run_chat_turn(1, "tối nay ăn gì giờ")
    await ccs.run_chat_turn(1, "có nên mua vàng lúc này không")
    # Nhánh cổ phiếu đã gắn số liệu giá: luôn kèm, dù câu không có chữ cổ phiếu.
    await ccs.run_chat_turn(1, "vậy sao", grounding="[DỮ LIỆU GIÁ THỰC TẾ FPT]")
    # Ngay sau báo cáo cổ phiếu: câu nối tiếp không nhắc mã vẫn kèm.
    chat_fakes["last_row"] = {"kind": conversation_log.KIND_STOCK, "content": "📊 **HPG**"}
    await ccs.run_chat_turn(1, "sao em bảo giữ?")
    await ccs.stop_background_tasks()
    assert seen == [False, True, True, True]


@pytest.mark.asyncio
async def test_router_doc_danh_muc_thi_kem_phan_co_phieu(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    seen = []

    async def fake_chat(user_id, text, **kw):
        seen.append(kw["stock_rules"])
        return _FakeResponse("Dạ anh.")

    async def portfolio(user_id, text, **kwargs):
        return _outcome(status=reg.STATUS_OK, name="get_portfolio", text="FPT 100 cp")

    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.tools, "maybe_run_tool", portfolio)
    await ccs.run_chat_turn(1, "anh đang cầm những gì")
    await ccs.stop_background_tasks()
    assert seen == [True]
