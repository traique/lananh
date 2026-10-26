"""Người dùng tự xem/sửa trí nhớ: /memory đánh số, /quen, /nho, /ghim, ghi chú,
/xuatdulieu, /xoadulieu (services/memory_control.py, memory_commands.py) trên
Telegram và Zalo/Zoom."""

import json

import pytest

from _memory_fakes import FakeMemoryDB
from core import database as db
from services import (
    channel_command_service,
    memory_commands,
    memory_control,
    memory_service,
    reply_audience,
    tools,
)

UID = 42


@pytest.fixture
def mem(monkeypatch):
    fake = FakeMemoryDB()
    fake.install(monkeypatch, db)
    return fake


async def _seed(fake):
    await db.save_fact(UID, "ten", "Trai", importance=3)
    await db.save_fact(UID, "cong_viec", "PM ở Nắng Decor")
    await db.save_fact(UID, "so_thich", "cà phê muối")
    await db.add_highlight(UID, "Ngày 15/8 bàn chốt lời HPG quanh 28", 25)
    await db.add_highlight(UID, "Anh hay uống cà phê muối buổi sáng", 25)


async def _cmd(command, argument="", *, channel="telegram", about="anh"):
    reply = await memory_commands.handle(command, argument, UID, channel=channel, about=about)
    assert reply is not None
    return reply


@pytest.mark.asyncio
async def test_memory_danh_so_ca_fact_va_highlight(mem):
    await _seed(mem)
    text = (await _cmd("/memory")).messages[0]
    assert "Trí nhớ dài hạn của em về anh (BẬT" in text
    assert "1. ten: Trai (từ " in text
    assert "Chi tiết đáng nhớ gần đây:" in text and "HPG quanh 28" in text
    # Số thứ tự liền mạch: 3 fact rồi 2 highlight.
    assert "4. Anh hay uống" in text and "5. Ngày 15/8" in text


@pytest.mark.asyncio
async def test_quen_theo_so_tu_khoa_va_tu_choi_khi_mo_ho(mem):
    await _seed(mem)
    ambiguous = (await _cmd("/quen", "cà phê")).messages[0]
    assert "Có 2 mục khớp" in ambiguous and "/quen 3" in ambiguous
    assert len(mem.current(UID)) == 3  # chưa xoá gì
    done = (await _cmd("/quen", "so thich")).messages[0]
    assert done.startswith("🗑️ Em quên rồi: so thich: cà phê muối")
    # Highlight nhắc tới giá trị đó cũng bị xoá; key vào danh sách "đừng học lại".
    assert [h["content"] for h in mem.highlights] == ["Ngày 15/8 bàn chốt lời HPG quanh 28"]
    forget = await memory_service.get_forget_list(UID)
    assert forget[-1]["k"] == "so_thich"
    done = (await _cmd("/quen", "3")).messages[0]  # giờ số 3 là highlight HPG
    assert "HPG" in done and mem.highlights == []
    assert "không thấy" in (await _cmd("/quen", "xe máy")).messages[0]


@pytest.mark.asyncio
async def test_nho_ghim_fact_nguoi_dung_va_chan_bi_mat(mem):
    reply = (await _cmd("/nho", "size giày của vợ là 38")).messages[0]
    assert reply == "📌 Em nhớ rồi: size giay cua vo: 38"
    row = mem.current(UID)[0]
    assert row["pinned"] and row["source"] == "user" and row["importance"] == 3
    # Bộ trích xuất tự động không ghi đè được.
    assert await db.save_fact(UID, "size_giay_cua_vo", "40") == "protected"
    secret = (await _cmd("/nho", "mật khẩu wifi là abc12345")).messages[0]
    assert "không lưu" in secret and len(mem.current(UID)) == 1
    style = (await _cmd("/nho", "cách trả lời: nói số trước")).messages[0]
    assert "nói số trước" in style
    turn = await memory_service.build_turn_memory(UID, "")
    assert "nói số trước" in turn.style_rules


@pytest.mark.asyncio
async def test_remember_fact_tool_bo_tien_to_nho_giup(mem):
    result = await tools._HANDLERS["remember_fact"](UID, content="nhớ giúp anh là sinh nhật mẹ 2/9")
    assert result.startswith("📌 Em nhớ rồi") and "2/9" in result
    assert mem.current(UID)[0]["pinned"] is True


@pytest.mark.asyncio
async def test_ghim_va_bo_ghim(mem):
    await _seed(mem)
    assert "Đã ghim" in (await _cmd("/ghim", "2")).messages[0]
    assert any(r["pinned"] for r in mem.current(UID))
    assert "Đã bỏ ghim" in (await _cmd("/boghim", "1")).messages[0]
    assert "không phải 1 thông tin" in (await _cmd("/ghim", "5")).messages[0]


@pytest.mark.asyncio
async def test_ghi_chu_tim_va_xoa_theo_so_cua_danh_sach_moi_nhat(mem):
    for text in ("size giày của vợ là 38", "mua sữa", "lịch họp thứ 6"):
        await db.add_note(UID, text)
    listing = (await _cmd("/notes")).messages[0]
    assert "1. lịch họp thứ 6" in listing and "3. size giày" in listing
    found = (await _cmd("/notes", "giày vợ")).messages[0]
    assert "3. size giày của vợ là 38" in found and "sữa" not in found
    assert "Đã xoá ghi chú: size giày" in (await _cmd("/xoaghichu", "3")).messages[0]
    assert "tatca xacnhan" in (await _cmd("/xoaghichu", "tatca")).messages[0]
    assert "Đã xoá 2" in (await _cmd("/xoaghichu", "tatca xacnhan")).messages[0]


@pytest.mark.asyncio
async def test_xuat_du_lieu_telegram_file_json_zalo_ban_chu(mem, monkeypatch):
    await _seed(mem)
    await db.add_note(UID, "mua sữa")

    async def no_reminders(*args, **kwargs):
        return []

    async def no_events(*args, **kwargs):
        return []

    monkeypatch.setattr(db, "list_reminders_between", no_reminders)
    monkeypatch.setattr(db, "list_user_events", no_events)
    reply = await _cmd("/xuatdulieu")
    raw, name = reply.file
    data = json.loads(raw)
    assert name.endswith(".json") and len(raw) <= memory_control.EXPORT_MAX_BYTES
    assert {f["key"] for f in data["tri_nho"]["thong_tin"]} == {"ten", "cong_viec", "so_thich"}
    assert data["ghi_chu"][0]["content"] == "mua sữa"
    text = (await _cmd("/xuatdulieu", channel="zalo")).messages[0]
    assert "cong viec: PM ở Nắng Decor" in text
    assert len(text) <= memory_control.EXPORT_TEXT_MAX_CHARS


@pytest.mark.asyncio
async def test_xoa_du_lieu_can_xac_nhan_chu_bot_giu_danh_muc(mem, monkeypatch):
    await _seed(mem)
    await db.add_note(UID, "x")
    mem.add_turn(UID, "chào", "dạ")
    cleared = []

    async def clear_events(uid):
        cleared.append(uid)

    monkeypatch.setattr(db, "clear_user_events", clear_events)
    warn = (await _cmd("/xoadulieu")).messages[0]
    assert "xacnhan" in warn and "danh mục cổ phiếu" in warn and mem.facts
    done = (await _cmd("/xoadulieu", "xacnhan")).messages[0]
    assert done.startswith("🗑️ Đã xoá")
    assert mem.facts == [] and mem.notes == [] and mem.chat == [] and cleared == [UID]


@pytest.mark.asyncio
async def test_xoa_du_lieu_thanh_vien_zalo_goi_xoa_theo_internal_id(mem, monkeypatch):
    from channels import zalo_users

    deleted = []

    async def delete_own(uid):
        deleted.append(uid)

    monkeypatch.setattr(zalo_users, "delete_own_data", delete_own)
    reply = await memory_commands.handle("/xoadulieu", "xacnhan", -9, channel="zalo")
    assert reply.messages[0].startswith("🗑️ Đã xoá") and deleted == [-9]


@pytest.mark.asyncio
async def test_zalo_lenh_tri_nho_goi_dung_cach_xung_ho(mem):
    await db.save_fact(-9, "ten", "Lan")
    with reply_audience.use(reply_audience.ReplyAudience(address_form="chị")):
        messages, _ = await channel_command_service.maybe_handle_command(
            -9, "/memory", is_admin=False, channel="zalo"
        )
    assert "về chị" in messages[0] and "ten: Lan" in messages[0]
    messages, _ = await channel_command_service.maybe_handle_command(
        -9, "/memory", is_admin=False, channel="zalo"
    )
    assert messages[0].startswith("🧠 Trí nhớ dài hạn (BẬT")  # chưa đặt xưng hô: trung tính


@pytest.mark.asyncio
async def test_memory_on_off_va_forget(mem):
    assert "Đã tắt" in (await _cmd("/memory", "off")).messages[0]
    assert mem.settings[f"memory_enabled_{UID}"] == "0"
    assert "Đã bật" in (await _cmd("/memory", "on")).messages[0]
    await _seed(mem)
    assert "Đã xoá toàn bộ" in (await _cmd("/forget")).messages[0]
    assert mem.facts == [] and mem.highlights == []


@pytest.mark.asyncio
async def test_admin_sua_tri_nho_theo_id(mem):
    await _seed(mem)
    data = await memory_control.admin_items(UID)
    fact = data["facts"][0]
    assert await memory_control.admin_edit(UID, "pin", "fact", fact["id"]) is True
    assert mem.current(UID)[0]["pinned"]
    assert await memory_control.admin_edit(UID, "forget", "fact", fact["id"]) is True
    assert fact["key"] not in {r["key"] for r in mem.current(UID)}
    hl = data["highlights"][0]
    assert await memory_control.admin_edit(UID, "forget", "highlight", hl["id"]) is True
    assert await memory_control.admin_edit(UID, "forget", "highlight", 99999) is False


def test_lenh_tri_nho_co_trong_help_va_menu():
    import bot_app
    from handlers import commands

    menu = {c.command for c in bot_app.COMMANDS}
    for cmd in ("quen", "nho", "ghim", "boghim", "xoaghichu", "xuatdulieu", "xoadulieu"):
        assert f"/{cmd}" in commands.HELP_TEXT
        assert f"/{cmd}" in channel_command_service.HELP_MEMBER
        assert f"/{cmd}" in channel_command_service.HELP_ADMIN
        assert cmd in menu
