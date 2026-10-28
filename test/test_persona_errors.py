"""Câu báo lỗi giọng Lan Anh: thành viên không bao giờ thấy tên provider, loại
exception hay thân response; chủ bot (Telegram/Zoom, Zalo controller) giữ chi
tiết kỹ thuật + gợi ý /status."""

import re

import pytest

import messages
from services import reply_audience

# Những thứ không được lọt tới thành viên.
_LEAKS = re.compile(r"Gemini|9Router|Groq|OpenRouter|Error\b|Exception|Traceback|/status|🔎")


@pytest.mark.parametrize("kind", sorted(messages._ERROR_TEMPLATES) + ["khong-ro"])
def test_thanh_vien_khong_thay_chi_tiet_ky_thuat(kind):
    for form in (None, "cô", "bạn", "em"):
        text = messages.friendly_error(
            kind, address_form=form, detail="RuntimeError: Groq 500 body={secret}"
        )
        assert not _LEAKS.search(text), text
        assert "secret" not in text


def test_chu_bot_giu_chi_tiet_va_goi_y_status():
    text = messages.friendly_error("chat", owner=True, detail="RuntimeError: provider chết")
    assert text.endswith("nha anh.\n🔎 RuntimeError: provider chết\n" + messages.OWNER_STATUS_HINT)
    assert "/status" not in messages.friendly_error("empty", owner=True)


def test_xung_ho_theo_cach_goi_rieng_hoac_trung_tinh():
    assert messages.friendly_error("chat", address_form="cô").endswith(
        "giúp cháu sau khoảng 1 phút nha cô."
    )
    assert messages.friendly_error("chat", address_form="em").endswith(
        "giúp chị sau khoảng 1 phút nha em."
    )
    neutral = messages.friendly_error("chat")
    assert neutral.endswith("giúp em sau khoảng 1 phút nha.") and " anh" not in neutral


def test_exception_detail_rut_gon():
    assert messages.exception_detail(ValueError("x")) == "ValueError: x"
    long = messages.exception_detail(RuntimeError("y" * 500), limit=50)
    assert len(long) == 51 and long.endswith("…")


@pytest.mark.asyncio
async def test_kiem_chu_bot_loi_thi_an_chi_tiet(monkeypatch):
    from core import config

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)

    async def boom():
        raise RuntimeError("DB lỗi")

    async def yes():
        return True

    assert await reply_audience.is_owner(42)  # Telegram/Zoom
    assert not await reply_audience.is_owner(-1)  # Zalo, không gắn người nhận
    with reply_audience.use(reply_audience.ReplyAudience(owner_check=boom)):
        assert not await reply_audience.is_owner(-1)
    with reply_audience.use(reply_audience.ReplyAudience(owner_check=yes, address_form="chị")):
        assert await reply_audience.is_owner(-1)
        text = await reply_audience.error_text("timeout", -1, "Hết 200s")
    # Chủ bot luôn được gọi "anh" dù có cách gọi riêng.
    assert "🔎 Hết 200s" in text and "nha anh." in text


# ─── Chat Zalo: lỗi lượt chat, hết giờ, nhánh cổ phiếu ──────────────────────


@pytest.fixture
def chat_stubs(monkeypatch):
    from services import channel_chat_service as ccs

    async def nothing(*args, **kwargs):
        return None

    async def no_stock(user_id, text, is_admin=True):
        return None, ""

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", nothing)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", nothing)
    monkeypatch.setattr(ccs, "_handle_stock", no_stock)
    return ccs


@pytest.mark.asyncio
async def test_loi_chat_cua_thanh_vien_khong_lo_exception(chat_stubs, monkeypatch):
    ccs = chat_stubs
    turns = [
        ccs.ChatTurn(failed=True, error_detail="TypeError: Groq trả 500 {body}"),
        ccs.ChatTurn(failed=True, timed_out=True, error_detail="Hết 200s chờ AI trả lời"),
        ccs.ChatTurn(""),
    ]

    async def fake_turn(*args, **kwargs):
        return turns.pop(0)

    monkeypatch.setattr(ccs, "run_chat_turn", fake_turn)
    replies = [
        (await ccs.handle_channel_text(-3, "hỏi", is_admin=True)).messages[0] for _ in range(3)
    ]

    assert replies == [
        messages.friendly_error("chat"),
        messages.friendly_error("timeout"),
        messages.friendly_error("empty"),
    ]
    assert not any(_LEAKS.search(reply) for reply in replies)


@pytest.mark.asyncio
async def test_zalo_controller_van_thay_chi_tiet_loi(chat_stubs, monkeypatch):
    ccs = chat_stubs

    async def fake_turn(*args, **kwargs):
        return ccs.ChatTurn(failed=True, error_detail="TypeError: hỏng")

    async def is_controller():
        return True

    monkeypatch.setattr(ccs, "run_chat_turn", fake_turn)
    with reply_audience.use(reply_audience.ReplyAudience(owner_check=is_controller)):
        result = await ccs.handle_channel_text(-3, "hỏi", is_admin=True)
    assert "🔎 TypeError: hỏng" in result.messages[0] and "/status" in result.messages[0]


@pytest.mark.asyncio
async def test_nhanh_co_phieu_het_gio_cau_than_thien(chat_stubs, monkeypatch):
    ccs = chat_stubs
    from ai.timeouts import TurnTimeoutError

    async def slow_stock(user_id, text, is_admin=True):
        raise TurnTimeoutError("Hết 200s chờ AI trả lời (mọi provider đều chậm/lỗi)")

    monkeypatch.setattr(ccs, "_handle_stock", slow_stock)
    result = await ccs.handle_channel_text(-3, "phân tích FPT", is_admin=False)
    assert result.messages == [messages.friendly_error("timeout")]


@pytest.mark.asyncio
async def test_khong_tra_ra_ma_co_phieu_khong_goi_thanh_vien_la_anh(monkeypatch):
    """Câu "chưa tra ra mã" trên Zalo theo cách gọi riêng; chưa đặt thì trung tính."""
    from services import channel_chat_service as ccs

    async def no_symbols(text):
        return []

    monkeypatch.setattr(ccs.stock_analysis, "find_valid_symbols", no_symbols)
    monkeypatch.setattr(ccs.stock_analysis, "looks_like_price_question", lambda text: True)
    neutral, _ = await ccs._handle_stock(-3, "giá mã đó bao nhiêu", is_admin=False)
    assert "anh" not in neutral.messages[0].lower().replace("lan anh", "")
    with reply_audience.use(reply_audience.ReplyAudience(address_form="cô")):
        aunt, _ = await ccs._handle_stock(-3, "giá mã đó bao nhiêu", is_admin=False)
    assert aunt.messages[0].startswith("Cô ơi cháu chưa tra ra mã")
    assert messages.stock_symbol_unresolved(owner=True) == messages.STOCK_SYMBOL_UNRESOLVED


def test_loi_lay_gia_co_phieu_khong_xung_ho():
    text = messages.STOCK_FETCH_ERROR.format(symbol="FPT")
    assert "FPT" in text and " anh" not in text and not text.startswith("Em")


# ─── Ảnh Zalo -> prompt ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loi_anh_cua_thanh_vien_khong_lo_exception(monkeypatch, tmp_path):
    from core import config
    from services import channel_image_service as cis

    async def broken(instruction, path):
        raise TypeError("Groq vision 500: {upstream body}")

    async def noop(*args, **kwargs):
        return 1

    async def is_controller():
        return True

    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(cis.image_prompt.orchestrator, "analyze_image", broken)
    monkeypatch.setattr(cis.telemetry, "start", noop)
    monkeypatch.setattr(cis.telemetry, "failure", noop)

    member, _ = await cis.handle_channel_image(-4, b"\x89PNG", "image/png", "", "m1")
    assert member == [messages.friendly_error("image")]
    assert not _LEAKS.search(member[0]) and "upstream" not in member[0]

    with reply_audience.use(reply_audience.ReplyAudience(owner_check=is_controller)):
        owner, _ = await cis.handle_channel_image(-4, b"\x89PNG", "image/png", "", "m2")
    assert "🔎 TypeError: Groq vision 500" in owner[0]


@pytest.mark.asyncio
async def test_anh_khong_ra_prompt_cau_than_thien(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from core import config
    from services import channel_image_service as cis

    async def empty(instruction, path):
        return SimpleNamespace(text="", used_fallback=False)

    async def noop(*args, **kwargs):
        return 1

    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(cis.image_prompt.orchestrator, "analyze_image", empty)
    monkeypatch.setattr(cis.telemetry, "start", noop)
    monkeypatch.setattr(cis.telemetry, "success", noop)
    with reply_audience.use(reply_audience.ReplyAudience(address_form="chị")):
        result, _ = await cis.handle_channel_image(-4, b"\x89PNG", "image/png", "", "m3")
    assert result == [messages.friendly_error("image_empty", address_form="chị")]
    assert "Gemini" not in result[0]


@pytest.mark.asyncio
async def test_prompt_tu_anh_chi_chu_bot_thay_model_doc_anh(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from core import config
    from services import channel_image_service as cis

    async def read(instruction, path):
        return SimpleNamespace(
            text="Photographic portrait of a woman on a white bicycle.",
            used_fallback=False,
            provider="router9",
            model="gpt-4o-mini",
        )

    async def noop(*args, **kwargs):
        return 1

    async def is_controller():
        return True

    async def no_record(*args, **kwargs):
        return None

    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(cis.image_prompt.orchestrator, "analyze_image", read)
    monkeypatch.setattr(cis.telemetry, "start", noop)
    monkeypatch.setattr(cis.telemetry, "success", noop)
    monkeypatch.setattr(cis.conversation_log, "record_exchange", no_record)

    member, _ = await cis.handle_channel_image(-4, b"\x89PNG", "image/png", "", "m4")
    assert "Đọc ảnh" not in member[0] and "router9" not in member[0]
    with reply_audience.use(reply_audience.ReplyAudience(owner_check=is_controller)):
        owner, _ = await cis.handle_channel_image(-4, b"\x89PNG", "image/png", "", "m5")
    assert "🤖 Đọc ảnh: router9 (gpt-4o-mini)" in owner[0]


# ─── Router Zalo: chủ bot = controller, KHÔNG phải role admin ────────────────


@pytest.mark.asyncio
async def test_router_chi_controller_moi_la_chu_bot(monkeypatch):
    from channels import router as zalo_router
    from channels import zalo_users

    async def controller():
        return "z-owner"

    monkeypatch.setattr(zalo_router, "_controller", controller)
    admin = zalo_users.ZaloUser("z-admin", -2, "Admin", "admin", "active")
    owner = zalo_users.ZaloUser("z-owner", -1, "Chủ", "admin", "active")

    with reply_audience.use(zalo_router._audience(admin, "z-admin")):
        assert not await reply_audience.is_owner(-2)
    with reply_audience.use(zalo_router._audience(owner, "z-owner")):
        assert await reply_audience.is_owner(-1)


@pytest.mark.asyncio
async def test_lenh_prompt_gia_cua_thanh_vien_khong_lo_exception(monkeypatch):
    from services import channel_command_service as cmd

    async def broken(*args, **kwargs):
        raise RuntimeError("OpenRouter 429: {quota body}")

    async def empty(*args, **kwargs):
        return type("R", (), {"text": "", "used_fallback": False})()

    async def noop(*args, **kwargs):
        return 1

    monkeypatch.setattr(cmd.telemetry, "start", noop)
    monkeypatch.setattr(cmd.telemetry, "success", noop)
    monkeypatch.setattr(cmd.telemetry, "failure", noop)
    monkeypatch.setattr(cmd.price_search, "get_cached_price", lambda product: None)
    monkeypatch.setattr(cmd.price_search, "search_price", broken)
    monkeypatch.setattr(cmd.orchestrator, "ask", broken)

    prompt_error, _ = await cmd._prompt(-8, "cô gái")
    price_error, _ = await cmd._price(-8, "iPhone")
    monkeypatch.setattr(cmd.orchestrator, "ask", empty)
    prompt_empty, _ = await cmd._prompt(-8, "cô gái")

    assert prompt_error == [messages.friendly_error("prompt")]
    assert price_error == [messages.friendly_error("price")]
    assert prompt_empty == [messages.friendly_error("prompt_empty")]
    assert not any(_LEAKS.search(text[0]) for text in (prompt_error, price_error, prompt_empty))
