"""services/conversation_log.py: lưu MỌI câu trả lời vào lịch sử (kèm kind),
bản lưu báo cáo cổ phiếu theo quyền xem, chặn câu chat nói ngược policy."""

from datetime import datetime, timedelta, timezone

import pytest

from core import database as db
from services import conversation_log as clog
from services import memory_service
from stock import report_format as rfmt

OWNER_REPORT = (
    "📊 **FPT** — **120.500 VND** (10:00 26/10)\n"
    "Tín hiệu hệ thống: **🟡 GIỮ** (confidence 0.6, setup pullback, regime bull)\n"
    "Đang giữ, tín hiệu vẫn thuận lợi | Giá 120.500 | Tham khảo chốt lời 130.000 | "
    "Cân nhắc cắt lỗ dưới 110.000 | R:R ~2\n"
    "RSI14 55,0 | Trend ~3M 4,2% | Risk: medium\n" + "Lý do: xu hướng tăng; " * 120
)
MEMBER_REPORT = (
    "📊 **FPT** — **120.500 VND** (10:00 26/10)\n"
    "Thông tin thị trường tham khảo (không phải tín hiệu giao dịch):\n"
    "RSI14 55,0 | Trend ~3M 4,2%\n"
    "Nên mua quanh vùng mua 118.000, stop 110.000\n"
    "Bối cảnh VNINDEX: bull"
)


def test_ban_luu_bao_cao_chu_bot_giu_dong_tin_hieu_policy_nguyen_van():
    stored = clog.stock_record([OWNER_REPORT, "Diễn giải AI ..."], full_access=True)
    assert stored.startswith("[Báo cáo hệ thống - hành động do policy quyết định: HOLD]")
    assert "Tín hiệu hệ thống: **🟡 GIỮ**" in stored
    assert "Cân nhắc cắt lỗ dưới 110.000" in stored
    assert len(stored) <= clog.REPLY_MAX_CHARS
    assert clog.actions_in_report("Tín hiệu hệ thống: **⚪ ĐỨNG NGOÀI (NO_TRADE)** (x)") == [
        "NO_TRADE"
    ]


def test_ban_luu_bao_cao_thanh_vien_khong_co_dong_hanh_dong_hay_ke_hoach():
    stored = clog.stock_record([MEMBER_REPORT], full_access=False)
    assert stored.startswith(clog.MEMBER_STOCK_TAG)
    body = stored.split("\n", 1)[1]
    assert rfmt.find_plan_language(body) == []
    assert rfmt.find_action_conflicts(body, frozenset()) == []
    assert "RSI14 55,0" in body and "Bối cảnh VNINDEX" in body


def test_rut_gon_theo_loai():
    article = clog.compact_reply("Tiêu đề\n" + "nội dung " * 500, clog.KIND_ARTICLE)
    assert article.startswith("[Đã gửi nguyên văn bài báo") and len(article) < 700
    translated = clog.compact_reply("お世話になります" * 100, clog.KIND_TRANSLATE, "chào anh")
    assert "chào anh" in translated and "お世話" not in translated
    assert "tiếng Anh không lưu" in clog.compact_reply("long prompt", clog.KIND_IMAGE)
    assert len(clog.compact_reply("x" * 5000, clog.KIND_WEATHER)) == clog.REPLY_MAX_CHARS


@pytest.mark.asyncio
async def test_record_exchange_luu_kind_va_chi_bao_tri_nho_cho_luot_can_trich(monkeypatch):
    saved, flushed = [], []

    async def fake_add(uid, user_text, model_text, *, kind="chat"):
        saved.append((uid, user_text, model_text, kind))

    monkeypatch.setattr(db, "add_chat_turn", fake_add)
    monkeypatch.setattr(memory_service, "after_exchange", lambda uid, **kw: flushed.append(uid))
    assert await clog.record_exchange(1, "mai Đà Lạt mưa không", "🌤 ...", clog.KIND_WEATHER)
    assert await clog.record_exchange(1, "anh tên Trai", "dạ", clog.KIND_CHAT)
    assert await clog.record_exchange(2, "/rag dca", "Theo tài liệu ...", clog.KIND_RAG)
    assert not await clog.record_exchange(1, "   ", "x", clog.KIND_CHAT)
    assert [s[3] for s in saved] == ["weather", "chat", "rag"]
    assert flushed == [1, 2]


@pytest.mark.asyncio
async def test_record_exchange_tra_id_cap_vua_luu(monkeypatch):
    """saved_ids nhận id 2 dòng chat_messages vừa lưu (🔄/✂️ xoá đúng cặp sau này)."""

    async def fake_add(uid, user_text, model_text, *, kind="chat"):
        return [7, 8]

    monkeypatch.setattr(db, "add_chat_turn", fake_add)
    monkeypatch.setattr(memory_service, "after_exchange", lambda uid, **kw: None)
    ids: list[int] = []
    assert await clog.record_exchange(1, "hỏi", "đáp", saved_ids=ids)
    assert ids == [7, 8]
    untouched: list[int] = []
    assert not await clog.record_exchange(1, "  ", "đáp", saved_ids=untouched)
    assert untouched == []


@pytest.mark.asyncio
async def test_record_exchange_db_loi_khong_raise(monkeypatch):
    async def broken(*args, **kwargs):
        raise ConnectionError("db down")

    monkeypatch.setattr(db, "add_chat_turn", broken)
    assert await clog.record_exchange(1, "hỏi", "đáp") is False


@pytest.mark.asyncio
async def test_run_and_record_luu_mot_lan_va_tra_ket_qua(monkeypatch):
    saved = []

    async def fake_add(uid, user_text, model_text, *, kind="chat"):
        saved.append((user_text, model_text, kind))

    async def call():
        return ("câu trả lời", "provider")

    monkeypatch.setattr(db, "add_chat_turn", fake_add)
    monkeypatch.setattr(memory_service, "after_exchange", lambda uid, **kw: None)
    result = await clog.run_and_record(1, "/agent hỏi", clog.KIND_AGENT, call)
    assert result == ("câu trả lời", "provider")
    assert saved == [("/agent hỏi", "câu trả lời", "agent")]


def _row(kind, content):
    return {"kind": kind, "content": content, "created_at": datetime.now(timezone.utc)}


def test_chat_sau_bao_cao_chu_bot_khong_duoc_noi_nguoc_action():
    stored = clog.stock_record([OWNER_REPORT], full_access=True)
    row = _row(clog.KIND_STOCK, stored)
    explain = "Hệ thống báo GIỮ vì xu hướng vẫn tăng, anh cứ theo dõi thêm."
    assert clog.guard_stock_reply(explain, row) == explain
    swapped = clog.guard_stock_reply("Em nghĩ anh nên mua thêm FPT ngay.", row)
    assert swapped.startswith("⚠️ Bản diễn giải AI lần này")
    assert "Tín hiệu hệ thống: **🟡 GIỮ**" in swapped
    # Lượt trước không phải báo cáo cổ phiếu: không chặn.
    advice = "Em nghĩ anh nên mua thêm."
    assert clog.guard_stock_reply(advice, _row(clog.KIND_CHAT, "x")) == advice
    assert clog.guard_stock_reply(advice, None) == advice


def test_chat_sau_bao_cao_tham_khao_thanh_vien_khong_co_ke_hoach():
    row = _row(clog.KIND_STOCK, clog.stock_record([MEMBER_REPORT], full_access=False))
    ok = "RSI đang ở vùng trung tính, xu hướng 3 tháng tăng nhẹ."
    assert clog.guard_stock_reply(ok, row) == ok
    swapped = clog.guard_stock_reply("Có thể đặt stop dưới 110.000 nhé.", row)
    assert swapped.startswith("⚠️ Bản diễn giải tự động")


@pytest.mark.asyncio
async def test_last_model_row_ngoai_phien_bo_qua(monkeypatch):
    old = {
        "kind": "stock",
        "content": "x",
        "created_at": datetime.now(timezone.utc) - timedelta(days=2),
    }

    async def fake_last(uid, max_chars=4000):
        return old

    monkeypatch.setattr(db, "get_last_model_row", fake_last)
    assert await clog.last_model_row(1) is None
    old["created_at"] = datetime.now(timezone.utc)
    assert (await clog.last_model_row(1))["kind"] == "stock"


def _aged(kind, content, minutes):
    row = _row(kind, content)
    row["created_at"] = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return row


MEMBER_QUOTE = "📊 **HPG**: **27.500 VND** (10:00 26/10)\n🟢 +300 (+1,10%) so với phiên trước"


def test_chat_doi_chu_de_lau_sau_bao_gia_khong_bi_chan():
    """2 tiếng sau 1 câu báo giá, câu chat chuyện khác không nhắc mã không bị thay."""
    row = _aged(clog.KIND_STOCK, clog.stock_record([MEMBER_QUOTE], full_access=False), 120)
    throat = "Đau họng thì chị nên giữ ấm cổ họng, uống nước ấm nhé."
    assert clog.guard_stock_reply(throat, row, user_text="đau họng làm sao em") == throat
    run = "Chị có thể đặt target chạy bộ 5km mỗi sáng."
    assert clog.guard_stock_reply(run, row, user_text="tập thể dục sao cho đều") == run
    owner_row = _aged(clog.KIND_STOCK, clog.stock_record([OWNER_REPORT], full_access=True), 120)
    car = "Theo em anh nên mua VF8 nếu hay đi đường dài."
    assert clog.guard_stock_reply(car, owner_row, user_text="xe điện nào ổn em") == car


def test_chat_nhac_dung_ma_da_bao_cao_van_bi_chan_du_da_lau():
    row = _aged(clog.KIND_STOCK, clog.stock_record([MEMBER_QUOTE], full_access=False), 120)
    reply = "HPG đang tăng nhẹ.\nChị nên mua quanh 27.000, stop 25.000."
    guarded = clog.guard_stock_reply(reply, row, user_text="hpg thế nào em", address_form="chị")
    assert guarded.startswith("HPG đang tăng nhẹ.")
    assert "nên mua" not in guarded and "stop" not in guarded
    assert guarded.endswith("nha chị.")
    # Báo cáo vừa gửi + câu hỏi không nêu mã khác: vẫn chặn.
    recent = _aged(clog.KIND_STOCK, clog.stock_record([MEMBER_QUOTE], full_access=False), 3)
    assert "stop" not in clog.guard_stock_reply("Đặt stop 25.000 nhé.", recent, user_text="vậy sao")
    # Vừa gửi nhưng câu hỏi nói về mã KHÁC: không áp tín hiệu của HPG.
    owner_row = _aged(clog.KIND_STOCK, clog.stock_record([OWNER_REPORT], full_access=True), 3)
    other = "VNM thì em thấy anh nên mua dần."
    assert clog.guard_stock_reply(other, owner_row, user_text="còn VNM thì sao") == other


def test_chat_sau_bao_cao_chi_bo_dong_vi_pham_khong_thay_ca_cau():
    owner_row = _row(clog.KIND_STOCK, clog.stock_record([OWNER_REPORT], full_access=True))
    reply = "FPT vẫn trong xu hướng tăng.\nEm nghĩ anh nên mua thêm FPT ngay."
    guarded = clog.guard_stock_reply(reply, owner_row, user_text="sao em bảo giữ?")
    assert guarded.startswith("FPT vẫn trong xu hướng tăng.")
    assert "nên mua" not in guarded
    assert "Tín hiệu hệ thống: **🟡 GIỮ**" in guarded
    # Không còn dòng nào: câu báo + dòng số liệu đã lưu, không bao giờ chỉ câu báo.
    member_row = _row(clog.KIND_STOCK, clog.stock_record([MEMBER_QUOTE], full_access=False))
    only = clog.guard_stock_reply("Chị nên mua HPG quanh 27.000.", member_row, user_text="x")
    assert "27.500 VND" in only and "nên mua" not in only


def test_cau_bao_ban_tham_khao_khong_bi_chinh_bo_loc_xoa():
    note = clog.REFERENCE_NOTE.format(ai="em", Ai="Em", tail=" chị")
    assert rfmt.find_plan_language(note) == []
    assert rfmt.find_action_conflicts(note, frozenset()) == []
    once = clog.reference_only_reply("FPT tăng.\nNên mua vùng 120.", [], address_form="chị")
    assert clog.reference_only_reply(once, [], address_form="chị") == once
