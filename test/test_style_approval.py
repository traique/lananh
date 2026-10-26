"""Bài học "cách trả lời" phải được duyệt (services/style_approval.py): đề xuất của
bộ trích nhớ chỉ lưu chờ, hỏi 1 lần/ngày sau câu trả lời, [Ghi nhớ]/[Bỏ] trên
Telegram, "ghi nhớ"/"bỏ" hoặc /hoc ok|bo trên Zalo/Zoom; lời tự dặn rõ áp ngay."""

import json
import time
from types import SimpleNamespace

import pytest

from _memory_fakes import FakeMemoryDB
from core import config
from core import database as db
from handlers import callbacks
from services import action_buttons, memory_commands, memory_service, personal_settings
from services import reply_audience, style_approval

UID = 1


@pytest.fixture
def mem(monkeypatch):
    fake = FakeMemoryDB()
    fake.install(monkeypatch, db)
    fake.prompts = []
    fake.responses = []

    async def fake_utility(prompt):
        fake.prompts.append(prompt)
        return fake.responses.pop(0) if fake.responses else {"facts": []}

    async def cas(key, expected, value):
        if (fake.settings.get(key) or "") != expected:
            return False
        fake.settings[key] = value
        return True

    monkeypatch.setattr(memory_service, "_utility_json", fake_utility)
    monkeypatch.setattr(db, "compare_and_set_setting", cas)
    monkeypatch.delenv("STYLE_RULES_NEED_APPROVAL", raising=False)
    style_approval._awaiting.clear()
    style_approval._nothing_to_ask.clear()
    yield fake
    style_approval._awaiting.clear()
    style_approval._nothing_to_ask.clear()


def _style(fake, uid=UID):
    row = next((f for f in fake.current(uid) if f["key"] == memory_service.STYLE_KEY), None)
    return row["value"] if row else None


def _state(fake, uid=UID) -> dict:
    raw = fake.settings.get(personal_settings.style_pending_key(uid)) or ""
    return json.loads(raw) if raw else {}


async def _extract(fake, user_text, rules, uid=UID):
    fake.add_turn(uid, user_text, "dạ")
    fake.responses.append({"style_rules": rules})
    await memory_service.maybe_flush(uid, explicit=True)


@pytest.mark.parametrize(
    "text",
    [
        "từ giờ trả lời ngắn thôi",
        "lần sau em đừng chèn emoji nhé",
        "đừng chèn emoji nữa",
        "từ nay nói số liệu trước nha em",
        "cách trả lời: gạch đầu dòng",
    ],
)
def test_loi_tu_dan_ro_la_explicit(text):
    assert style_approval.explicit_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "trả lời ngắn thôi em",  # có thể chỉ cho câu này
        "anh thích đọc tin chứng khoán buổi sáng",
        "từ giờ anh tập thể dục mỗi sáng",
        "đừng lo nữa em",
    ],
)
def test_cau_thuong_khong_phai_explicit(text):
    assert not style_approval.explicit_request(text)


@pytest.mark.asyncio
async def test_de_xuat_cho_duyet_hoi_1_lan_roi_ghi_nho(mem):
    await db.save_fact(
        UID, memory_service.STYLE_KEY, "- Xưng hô thân mật", category=memory_service.STYLE_KEY
    )
    await _extract(mem, "trả lời ngắn thôi em", "- Xưng hô thân mật\n- Trả lời ngắn gọn")

    # Chưa áp: quy tắc cũ giữ nguyên, có đề xuất chờ.
    assert _style(mem) == "- Xưng hô thân mật"
    assert _state(mem)["add"] == ["Trả lời ngắn gọn"]
    turn = await memory_service.build_turn_memory(UID, "chào em")
    assert "ngắn gọn" not in turn.style_rules

    question = await style_approval.take_question(UID, buttons=True)
    assert question is not None and "+ Trả lời ngắn gọn" in question.text
    assert "Anh muốn em ghi nhớ không ạ?" in question.text
    # Mỗi đề xuất chỉ hỏi 1 lần.
    assert await style_approval.take_question(UID) is None

    reply = await style_approval.resolve(UID, True, question.proposal_id)
    assert reply.startswith("✅ Em ghi nhớ rồi ạ")
    assert _style(mem) == "- Xưng hô thân mật\n- Trả lời ngắn gọn"
    assert not _state(mem).get("add")
    assert await style_approval.resolve(UID, True) == style_approval.NONE_PENDING


@pytest.mark.asyncio
async def test_loi_tu_dan_ro_ap_ngay_va_tat_duyet_bang_env(mem, monkeypatch):
    await _extract(mem, "từ giờ trả lời ngắn thôi", "- Trả lời ngắn gọn")
    assert _style(mem) == "- Trả lời ngắn gọn" and not _state(mem)

    monkeypatch.setenv("STYLE_RULES_NEED_APPROVAL", "0")
    await _extract(mem, "anh thích số liệu", "- Trả lời ngắn gọn\n- Nêu số liệu trước")
    assert _style(mem) == "- Trả lời ngắn gọn\n- Nêu số liệu trước"
    assert await style_approval.take_question(UID) is None


@pytest.mark.asyncio
async def test_gop_nhieu_de_xuat_va_ap_len_quy_tac_moi_nhat(mem):
    await _extract(mem, "ngắn thôi", "- Trả lời ngắn gọn")
    await _extract(mem, "không cần emoji đâu", "- Không dùng emoji")
    assert _state(mem)["add"] == ["Trả lời ngắn gọn", "Không dùng emoji"]
    # Trong lúc chờ, người dùng tự dặn rõ 1 quy tắc khác -> áp ngay.
    await _extract(mem, "từ giờ nói số liệu trước", "- Nêu số liệu trước")
    assert _style(mem) == "- Nêu số liệu trước"

    question = await style_approval.take_question(UID)
    assert "+ Trả lời ngắn gọn" in question.text and "+ Không dùng emoji" in question.text
    await style_approval.resolve(UID, True)
    assert _style(mem) == "- Nêu số liệu trước\n- Trả lời ngắn gọn\n- Không dùng emoji"


@pytest.mark.asyncio
async def test_de_xuat_bo_dong_cu_va_bo_thi_khong_de_xuat_lai(mem):
    await db.save_fact(UID, memory_service.STYLE_KEY, "- Trả lời thật dài\n- Có ví dụ")
    await _extract(mem, "dài quá", "- Có ví dụ\n- Trả lời ngắn gọn")
    state = _state(mem)
    assert state["add"] == ["Trả lời ngắn gọn"] and state["del"] == ["Trả lời thật dài"]
    question = await style_approval.take_question(UID)
    assert "− thôi: Trả lời thật dài" in question.text

    assert "bỏ qua" in await style_approval.resolve(UID, False)
    assert _style(mem) == "- Trả lời thật dài\n- Có ví dụ"
    # Cùng đề xuất lần sau: dòng đã bị bỏ không thêm lại (chỉ còn phần "thôi").
    await _extract(mem, "dài quá", "- Có ví dụ\n- Trả lời ngắn gọn")
    assert _state(mem)["add"] == []


@pytest.mark.asyncio
async def test_toi_da_1_lan_moi_ngay_va_het_han(mem, monkeypatch):
    await _extract(mem, "ngắn thôi", "- Trả lời ngắn gọn")
    assert await style_approval.take_question(UID) is not None
    await style_approval.resolve(UID, False)
    await _extract(mem, "đừng dùng emoji", "- Không dùng emoji")
    assert await style_approval.take_question(UID) is None  # hôm nay đã hỏi rồi
    monkeypatch.setattr(style_approval, "_today", lambda: "2099-01-01")
    assert await style_approval.take_question(UID) is not None

    # Hết hạn sau PENDING_TTL_SEC: không hỏi, /hoc báo không còn gì.
    await _extract(mem, "viết gạch đầu dòng", "- Dùng gạch đầu dòng")
    key = personal_settings.style_pending_key(UID)
    state = _state(mem)
    state["created"] = time.time() - style_approval.PENDING_TTL_SEC - 1
    mem.settings[key] = json.dumps(state)
    style_approval._nothing_to_ask.clear()
    monkeypatch.setattr(style_approval, "_today", lambda: "2099-01-02")
    assert await style_approval.take_question(UID) is None
    assert (await style_approval.handle_command("", UID)).startswith(style_approval.NONE_PENDING)


@pytest.mark.asyncio
async def test_tra_loi_bang_chu_ngay_sau_cau_hoi_va_lenh_hoc(mem):
    await _extract(mem, "ngắn thôi", "- Trả lời ngắn gọn", uid=-5)
    with reply_audience.use(reply_audience.ReplyAudience(address_form="cô")):
        question = await style_approval.take_question(-5)
        assert 'Nhắn "ghi nhớ"' in question.text and "Cô muốn cháu ghi nhớ" in question.text
        # Tin khác chen vào -> "ghi nhớ" sau đó không còn tính.
        assert await style_approval.maybe_handle_reply(-5, "mai trời sao em") is None
        assert await style_approval.maybe_handle_reply(-5, "ghi nhớ") is None
        assert _style(mem, -5) is None
        # /hoc vẫn dùng được bất cứ lúc nào; dữ liệu tách riêng từng người.
        reply = await memory_commands.handle("/hoc", "", -5, channel="zalo")
        assert "+ Trả lời ngắn gọn" in reply.messages[0]
        reply = await memory_commands.handle("/hoc", "ok", -5, channel="zalo")
        assert reply.messages[0].startswith("✅ Cháu ghi nhớ rồi ạ")
    assert _style(mem, -5) == "- Trả lời ngắn gọn" and _style(mem, UID) is None

    await _extract(mem, "không cần emoji", "- Trả lời ngắn gọn\n- Không dùng emoji", uid=-6)
    question = await style_approval.take_question(-6)
    assert question is not None
    assert "bỏ qua" in await style_approval.maybe_handle_reply(-6, "Bỏ nhé")
    assert _style(mem, -6) is None


@pytest.mark.asyncio
async def test_forget_xoa_ca_de_xuat_cho(mem):
    await _extract(mem, "ngắn thôi", "- Trả lời ngắn gọn")
    await memory_service.clear_memory(UID)
    assert await style_approval.pending(UID) is None
    assert personal_settings.style_pending_key(UID) in personal_settings.memory_user_keys(UID)


def test_nut_hoc_trong_whitelist():
    rows = action_buttons.style_approval_rows(10**11)
    assert [label for label, _ in rows[0]] == ["✅ Ghi nhớ", "🗑 Bỏ"]
    for _label, data in rows[0]:
        assert len(data.encode()) <= 64 and action_buttons.parse(data) is not None
    assert action_buttons.parse("hoc:5:k") == action_buttons.Action("hoc", "5", "k")
    assert action_buttons.parse("hoc:5:x") is None
    assert not action_buttons.needs_turn_lock("hoc:5:k")


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs.get("reply_markup")))


class _Query:
    def __init__(self, data):
        self.id = f"style-{data}"
        self.data = data
        self.message = _Message()
        self.answers = []
        self.markups = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)


@pytest.mark.asyncio
async def test_bam_nut_ghi_nho_tren_telegram(mem, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", UID)
    await _extract(mem, "ngắn thôi", "- Trả lời ngắn gọn")
    question = await style_approval.take_question(UID, buttons=True)
    query = _Query(f"hoc:{question.proposal_id}:k")
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=UID, username="x"),
        effective_message=query.message,
    )
    await callbacks.on_callback(update, SimpleNamespace(bot=None))
    assert _style(mem) == "- Trả lời ngắn gọn"
    assert query.answers[0].startswith("✅") and query.markups == [None]
    # Nút cũ bấm lại: báo không còn đề xuất.
    query2 = _Query(f"hoc:{question.proposal_id}:b")
    update.callback_query = query2
    await callbacks.on_callback(update, SimpleNamespace(bot=None))
    assert query2.message.replies[0][0] == style_approval.NONE_PENDING


@pytest.mark.asyncio
async def test_zalo_hoi_bang_tin_rieng_sau_cau_tra_loi(mem, monkeypatch):
    from services import channel_chat_service as ccs

    async def fake_turn(user_id, text, **kwargs):
        return ccs.ChatTurn("Dạ cô ơi.")

    async def none(*args, **kwargs):
        return None

    async def no_stock(user_id, text, is_admin=True):
        return None, ""

    monkeypatch.setattr(ccs, "run_chat_turn", fake_turn)
    monkeypatch.setattr(ccs.portfolio_service, "handle_command", none)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", none)
    monkeypatch.setattr(ccs.reminder_service, "maybe_handle_shortcut", none)
    monkeypatch.setattr(ccs, "_handle_stock", no_stock)

    await _extract(mem, "ngắn thôi", "- Trả lời ngắn gọn", uid=-5)
    first = await ccs.handle_channel_text(-5, "xin chào", is_admin=False)
    assert first.messages[0] == "Dạ cô ơi." and "ghi nhớ" in first.messages[1]
    again = await ccs.handle_channel_text(-5, "ghi nhớ nhé", is_admin=False)
    assert again.messages[0].startswith("✅") and _style(mem, -5) == "- Trả lời ngắn gọn"
    later = await ccs.handle_channel_text(-5, "cảm ơn em", is_admin=False)
    assert later.messages == ["Dạ cô ơi."]


@pytest.mark.asyncio
async def test_telegram_gui_cau_hoi_rieng_kem_nut(mem):
    from handlers import chat_router

    message = _Message()
    await chat_router._ask_style_approval(message, UID)
    assert message.replies == []  # chưa có đề xuất: không gửi gì
    await _extract(mem, "ngắn thôi", "- Trả lời ngắn gọn")
    await chat_router._ask_style_approval(message, UID)
    text, markup = message.replies[0]
    assert "+ Trả lời ngắn gọn" in text and "Bấm nút" in text
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["✅ Ghi nhớ", "🗑 Bỏ"]
    await chat_router._ask_style_approval(message, UID)
    assert len(message.replies) == 1
