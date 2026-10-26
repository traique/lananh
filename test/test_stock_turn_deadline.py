"""Một hạn chót chung cho nhánh cổ phiếu Telegram/Zalo/Zoom và /gia.

Các hàm phân tích nuốt mọi exception (kể cả TurnTimeoutError) nên kiểm tra
bằng thời gian chạy thật: hết hạn chót thì bounded() từ chối ngay, mã sau vẫn
gửi bản số liệu, cuối lượt có 1 câu báo phần diễn giải AI quá giờ.
"""

import asyncio
import time

import pytest

import messages
from ai import timeouts


class _Status:
    async def delete(self):
        return None


class _Message:
    def __init__(self, text):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return _Status()


class _Update:
    def __init__(self, text, user_id=42):
        self.message = _Message(text)
        self.effective_message = self.message
        self.effective_user = type("U", (), {"id": user_id, "username": "owner"})()
        self.effective_chat = type("C", (), {"id": user_id})()


class _Context:
    def __init__(self):
        async def send_chat_action(**kwargs):
            return None

        self.bot = type("B", (), {"send_chat_action": staticmethod(send_chat_action)})()


async def _slow_ai_call():
    """Một lượt gọi AI giả chậm 1s đi qua bounded() như provider chain thật."""

    async def call():
        await asyncio.sleep(1.0)
        return "không bao giờ tới"

    return await timeouts.bounded(call)


async def _fake_analyze_symbol(symbol, *, user_text="", user_id=None, on_rule_report=None, **kw):
    """Giống stock.analysis.analyze_symbol: gửi bản số liệu trước, LLM lỗi/hết
    giờ thì nuốt exception và trả "" (bản rule-based đã gửi)."""
    await on_rule_report(f"SỐ LIỆU {symbol}")
    try:
        await _slow_ai_call()
    except Exception:
        return ""
    return "DIỄN GIẢI"


def _patch_stock(monkeypatch, analysis_module, *, personas=None):
    async def symbols(text, limit=3):
        return ["FPT", "HPG"]

    monkeypatch.setattr(analysis_module, "find_valid_symbols", symbols)
    monkeypatch.setattr(analysis_module, "wants_portfolio_analysis", lambda t, s: False)
    monkeypatch.setattr(
        analysis_module.stock_personas, "detect_personas", lambda t: list(personas or [])
    )
    monkeypatch.setattr(analysis_module, "wants_full_analysis", lambda t, s: True)
    monkeypatch.setattr(analysis_module, "analyze_symbol", _fake_analyze_symbol)


@pytest.fixture
def short_deadline(monkeypatch):
    monkeypatch.setattr(timeouts, "_default_turn_seconds", lambda: 0.3)


@pytest.fixture
def no_telemetry(monkeypatch):
    from services.telemetry import telemetry

    async def noop(*args, **kwargs):
        return 1

    for name in ("start", "success", "failure"):
        monkeypatch.setattr(telemetry, name, noop)


@pytest.mark.asyncio
async def test_telegram_nhanh_co_phieu_mot_han_chot_va_bao_qua_gio(
    monkeypatch, short_deadline, no_telemetry
):
    from core import config
    from handlers import chat_router, stock_handler

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)

    async def none(*args, **kwargs):
        return None

    monkeypatch.setattr(chat_router.portfolio_service, "maybe_handle_natural_language", none)
    monkeypatch.setattr(chat_router.article_reader, "maybe_handle", none)
    monkeypatch.setattr(chat_router.weather_chat, "maybe_handle", none)
    monkeypatch.setattr(chat_router.lunar_chat, "maybe_handle", none)
    _patch_stock(monkeypatch, stock_handler.stock_analysis)
    sent: list[str] = []

    async def reply_long(message, text):
        sent.append(text)

    monkeypatch.setattr(stock_handler.common, "reply_long_text", reply_long)
    recorded = []

    async def record(user_id, text, reply, kind="chat", **kwargs):
        recorded.append((user_id, text, reply, kind, kwargs.get("stock_outputs")))
        return True

    monkeypatch.setattr(chat_router.conversation_log, "record_exchange", record)

    update = _Update("phân tích FPT HPG")
    started = time.monotonic()
    await chat_router.chat_msg(update, _Context())
    elapsed = time.monotonic() - started

    # Không hạn chót ngoài: mỗi mã chờ đủ 1s (2s). Có hạn chót 0,3s cho CẢ lượt.
    assert elapsed < 0.9
    assert sent == ["SỐ LIỆU FPT", "SỐ LIỆU HPG"]  # mã sau vẫn có bản số liệu
    # Bản số liệu đã gửi cũng vào lịch sử (kind stock) cho câu hỏi nối tiếp.
    assert recorded and recorded[0][3] == "stock"
    assert recorded[0][4] == ["SỐ LIỆU FPT", "SỐ LIỆU HPG"]
    note = update.message.replies[-1]
    assert "FPT, HPG" in note and "quá giờ" in note
    assert messages.STOCK_ANALYZE_FAILED.format(symbol="HPG") not in update.message.replies


@pytest.mark.asyncio
async def test_zalo_nhanh_co_phieu_bao_qua_gio_mot_cau(monkeypatch, short_deadline):
    from services import channel_chat_service as ccs

    async def none(*args, **kwargs):
        return None

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", none)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", none)
    monkeypatch.setattr(ccs.channel_command_service, "maybe_handle_command", none)
    monkeypatch.setattr(ccs.article_reader, "maybe_handle", none)
    monkeypatch.setattr(ccs.weather_chat, "maybe_handle", none)
    monkeypatch.setattr(ccs.lunar_chat, "maybe_handle", none)
    _patch_stock(monkeypatch, ccs.stock_analysis)

    started = time.monotonic()
    result = await ccs.handle_channel_text(-5, "phân tích FPT HPG", is_admin=False)
    assert time.monotonic() - started < 0.9
    assert result.messages[:2] == ["SỐ LIỆU FPT", "SỐ LIỆU HPG"]
    assert len(result.messages) == 3
    assert "FPT, HPG" in result.messages[2] and "quá giờ" in result.messages[2]


@pytest.mark.asyncio
async def test_llm_loi_khong_het_gio_van_bao_thay_loi_hua(monkeypatch):
    """Không hết hạn chót nhưng LLM lỗi: vẫn báo 1 câu thay cho "⏳ ... gửi tiếp"."""
    from services import channel_chat_service as ccs

    async def failed_llm(symbol, *, on_rule_report=None, **kw):
        await on_rule_report(f"SỐ LIỆU {symbol}")
        return ""

    _patch_stock(monkeypatch, ccs.stock_analysis)
    monkeypatch.setattr(ccs.stock_analysis, "analyze_symbol", failed_llm)
    result, _ = await ccs._handle_stock(1, "phân tích FPT HPG")
    assert result.messages[-1].startswith("ℹ️ Phần diễn giải AI cho FPT, HPG chưa tạo được")


@pytest.mark.asyncio
async def test_persona_gom_cac_ma_loi_thanh_mot_dong(monkeypatch):
    from services import channel_chat_service as ccs

    async def persona(symbol, persona_ids, text, **kw):
        if symbol == "FPT":
            return "Góc nhìn FPT"
        return messages.STOCK_ANALYZE_FAILED.format(symbol=symbol)

    _patch_stock(monkeypatch, ccs.stock_analysis, personas=["buffett"])
    monkeypatch.setattr(ccs.stock_analysis, "analyze_persona", persona)
    result, _ = await ccs._handle_stock(1, "buffett nghĩ gì về FPT HPG")
    assert result.messages == ["Góc nhìn FPT", messages.STOCK_ANALYZE_FAILED.format(symbol="HPG")]


@pytest.mark.asyncio
async def test_telegram_persona_het_gio_gom_mot_dong(monkeypatch, short_deadline, no_telemetry):
    from handlers import stock_handler

    async def persona(symbol, persona_ids, text, **kw):
        try:
            await _slow_ai_call()
        except Exception:
            return messages.STOCK_ANALYZE_FAILED.format(symbol=symbol)
        return "không tới"

    monkeypatch.setattr(stock_handler.stock_analysis, "analyze_persona", persona)
    update = _Update("buffett nghĩ gì về FPT HPG")
    with timeouts.turn_deadline():
        await stock_handler._handle_persona_view(update, 42, ["FPT", "HPG"], ["buffett"], "")
    assert len(update.message.replies) == 1
    assert "FPT, HPG" in update.message.replies[0] and "quá giờ" in update.message.replies[0]


@pytest.mark.asyncio
async def test_search_price_co_han_chot_rieng_cho_ca_luot(monkeypatch, short_deadline):
    from ai import tavily_client
    from services import price_search

    async def tavily_down(*args, **kwargs):
        raise tavily_client.TavilyError("tắt")

    seen = []

    async def slow_ask(*args, **kwargs):
        seen.append(timeouts.remaining_turn_seconds())
        return await _slow_ai_call()

    monkeypatch.setattr(price_search.tavily_client, "search_results", tavily_down)
    monkeypatch.setattr(price_search.orchestrator, "ask", slow_ask)
    started = time.monotonic()
    with pytest.raises(timeouts.TurnTimeoutError):
        await price_search.search_price("iPhone 16")
    assert time.monotonic() - started < 0.9
    assert seen and seen[0] is not None and seen[0] <= 0.3


@pytest.mark.asyncio
async def test_lenh_gia_kenh_chat_bao_het_gio(monkeypatch, no_telemetry):
    from services import channel_command_service as ccs_cmd

    async def timeout(product):
        raise timeouts.TurnTimeoutError("Hết 200s chờ AI trả lời")

    monkeypatch.setattr(ccs_cmd.price_search, "get_cached_price", lambda p: None)
    monkeypatch.setattr(ccs_cmd.price_search, "search_price", timeout)
    outputs, provider = await ccs_cmd._price(-1, "iPhone 16")
    # Thành viên: câu giọng Lan Anh, không lộ chi tiết kỹ thuật.
    assert outputs == [messages.friendly_error("timeout")] and provider is None
    assert "Hết 200s" not in outputs[0]
    monkeypatch.setattr(ccs_cmd.config, "ALLOWED_USER_ID", 7)
    outputs, _ = await ccs_cmd._price(7, "iPhone 16")
    assert outputs[0].startswith("⏱️ AI trả lời chậm quá") and "🔎 Hết 200s" in outputs[0]
