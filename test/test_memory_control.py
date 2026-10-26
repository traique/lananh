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
    # Danh sách /memory vừa xem giữ trong RAM theo user_id - không lẫn giữa các test.
    memory_control._listings.clear()
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
    await _cmd("/memory")  # xem lại danh sách: giờ số 3 là highlight HPG
    done = (await _cmd("/quen", "3")).messages[0]
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
    from services import tool_registry as reg

    result = await tools._HANDLERS["remember_fact"](UID, content="nhớ giúp anh là sinh nhật mẹ 2/9")
    # ToolSpec của danh bạ (services/tool_registry): đã lưu -> status ok + câu xác nhận.
    assert result.status == reg.STATUS_OK
    assert result.text.startswith("📌 Em nhớ rồi") and "2/9" in result.text
    assert result.confirmation == result.text
    assert mem.current(UID)[0]["pinned"] is True
    # Bí mật bị từ chối -> CHƯA lưu (status invalid), lượt chat không được nói "đã nhớ".
    refused = await tools._HANDLERS["remember_fact"](UID, content="mật khẩu wifi là abc12345")
    assert refused.status == reg.STATUS_INVALID and "không lưu" in refused.text
    assert len(mem.current(UID)) == 1


@pytest.mark.asyncio
async def test_nho_bi_mat_khong_co_dau_tach_van_bi_tu_choi(mem):
    """find_secret cần "là/:/=" - remember() phải xét cả key/value đã tách."""
    for text in (
        "mật khẩu wifi nhà anh abc12345",
        "nhớ giúp anh mật khẩu wifi abc12345",
        "cách trả lời: luôn kèm mật khẩu wifi abc12345",
    ):
        reply = await memory_control.remember(UID, text)
        assert "không lưu" in reply, text
    assert mem.current(UID) == []
    refused = await tools._HANDLERS["remember_fact"](UID, content="pass gmail anh Abc@2024")
    assert "không lưu" in refused.text and mem.current(UID) == []
    # Quy tắc trả lời chỉ NHẮC tới mật khẩu (không có giá trị) vẫn lưu được.
    rule = await memory_control.remember(UID, "cách trả lời: đừng bao giờ hỏi mật khẩu của anh")
    assert rule.startswith("✅")


@pytest.mark.asyncio
async def test_nho_ve_ban_than_khong_ghi_de_nhau_qua_key_dai_tu(mem):
    """'anh là ...' không được thành key "anh" - lần /nho sau thay mất lần trước."""
    first = await memory_control.remember(UID, "anh là người Hà Nội")
    second = await memory_control.remember(UID, "anh là fan MU")
    third = await memory_control.remember(UID, "nhớ giúp anh là anh là con út")
    fourth = await memory_control.remember(UID, "mình là dân IT")
    assert all(r.startswith("📌 Em nhớ rồi") for r in (first, second, third, fourth))
    values = {r["value"] for r in mem.current(UID)}
    assert {"anh là người Hà Nội", "anh là fan MU", "anh là con út", "mình là dân IT"} <= values
    assert not {"anh", "minh"} & {r["key"] for r in mem.current(UID)}
    # Cùng key người dùng tự nhập, giá trị khác: báo rõ giá trị cũ thay vì im lặng.
    await memory_control.remember(UID, "size giày của vợ là 38")
    updated = await memory_control.remember(UID, "size giày của vợ là 39")
    assert "39" in updated and "trước đây: 38" in updated


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
    for cmd in ("quen", "nho", "ghim", "boghim", "xoaghichu", "xuatdulieu", "xoadulieu", "hoithan"):
        assert f"/{cmd}" in commands.HELP_TEXT
        assert f"/{cmd}" in channel_command_service.HELP_MEMBER
        assert f"/{cmd}" in channel_command_service.HELP_ADMIN
        assert cmd in menu


@pytest.mark.asyncio
async def test_lenh_tri_nho_khong_cho_vo_han_khi_trich_nho_nen_giu_khoa(mem, monkeypatch):
    """Trích nhớ nền giữ user_lock suốt lượt gọi AI (tới 90s): /nho, /quen, /ghim,
    /forget và tool remember_fact trong lượt chat chỉ chờ có trần rồi báo bận."""
    import asyncio

    monkeypatch.setattr(memory_service, "INTERACTIVE_LOCK_WAIT_SEC", 0.05)
    await _seed(mem)
    held = asyncio.Event()
    release = asyncio.Event()

    async def background_flush():
        async with memory_service.user_lock(UID):
            held.set()
            await release.wait()

    task = asyncio.create_task(background_flush())
    await held.wait()
    replies = [
        await memory_control.remember(UID, "size giày của vợ là 38"),
        (await _cmd("/memory")).messages[0],
        (await _cmd("/quen", "so thich")).messages[0],
        (await _cmd("/ghim", "1")).messages[0],
        (await _cmd("/forget")).messages[0],
        await memory_control.delete_own_data(UID, "xacnhan"),
    ]
    assert all(r == memory_service.MEMORY_BUSY_TEXT for r in replies[2:]), replies
    assert replies[0] == memory_service.MEMORY_BUSY_TEXT
    assert len(mem.current(UID)) == 3  # chưa đổi gì
    release.set()
    await task
    # Đếm khoá đúng: không rò lock sau các lần chờ hết giờ.
    assert UID not in memory_service._user_locks and UID not in memory_service._lock_users
    assert (await memory_control.remember(UID, "size giày của vợ là 38")).startswith("📌")


@pytest.mark.asyncio
async def test_quen_theo_so_dung_danh_sach_vua_xem_du_trich_nho_nen_chen_them(mem):
    """/memory rồi flush nền thêm highlight (dời số) trước khi /quen: số vẫn trỏ đúng
    mục người dùng đã thấy."""
    await _seed(mem)
    listing = (await _cmd("/memory")).messages[0]
    assert "4. Anh hay uống cà phê muối buổi sáng" in listing
    await db.add_highlight(UID, "Vợ anh sinh nhật 12/5", 25)  # flush nền chen vào
    done = (await _cmd("/quen", "4")).messages[0]
    assert "Anh hay uống cà phê muối" in done
    assert [h["content"] for h in mem.highlights] == [
        "Ngày 15/8 bàn chốt lời HPG quanh 28",
        "Vợ anh sinh nhật 12/5",
    ]
    # Mục đã đổi (fact được cập nhật -> dòng mới) từ lúc xem: không xoá nhầm.
    await db.save_fact(UID, "cong_viec", "PM ở công ty mới")  # số 3 trong danh sách đã xem
    changed = (await _cmd("/quen", "3")).messages[0]
    assert "đã thay đổi" in changed and "/memory" in changed
    assert any(r["key"] == "cong_viec" for r in mem.current(UID))
    # /ghim theo số cũng theo danh sách đã xem.
    assert "ten: Trai" in (await _cmd("/ghim", "1")).messages[0]


@pytest.mark.asyncio
async def test_quen_theo_so_khi_chua_xem_danh_sach_thi_hoi_xac_nhan(mem, monkeypatch):
    await _seed(mem)
    asked = (await _cmd("/quen", "3")).messages[0]
    assert "cong viec: PM ở Nắng Decor" in asked and "/quen 3 xacnhan" in asked
    assert len(mem.current(UID)) == 3  # chưa xoá gì
    done = (await _cmd("/quen", "3 xacnhan")).messages[0]
    assert done.startswith("🗑️ Em quên rồi: cong viec")
    # Danh sách đã xem quá hạn: hỏi lại thay vì xoá ngay.
    await _cmd("/memory")
    monkeypatch.setattr(memory_control, "LISTING_TTL_SEC", -1.0)
    again = (await _cmd("/quen", "1")).messages[0]
    assert "/quen 1 xacnhan" in again and len(mem.current(UID)) == 2


@pytest.mark.asyncio
async def test_quen_xoa_ca_ghi_chu_phien_tom_tat_cu_va_su_kien_nhac_toi(mem):
    """/quen 1 fact: ghi chú phiên, tóm tắt cũ và sự kiện hỏi thăm nhắc tới giá
    trị đó (đều được chèn vào prompt) cũng không còn giữ nó."""
    from datetime import datetime, timezone

    await db.save_fact(UID, "suc_khoe", "tiểu đường type 2", importance=3)
    started = datetime.now(timezone.utc)
    await db.upsert_session_note(
        UID,
        10,
        title="Chuyện sức khoẻ",
        bullets="Anh kể bị tiểu đường type 2\nAnh định đi bơi",
        summary="Anh kể bị tiểu đường type 2. Anh định đi bơi buổi sáng.",
        started_at=started,
    )
    await db.upsert_session_note(UID, 20, title="Tiểu đường type 2", started_at=started)
    await db.set_summary(UID, "Anh làm PM. Anh bị tiểu đường type 2 từ năm ngoái.")
    mem.add_event(UID, "tái khám tiểu đường type 2")
    mem.add_event(UID, "phỏng vấn ở công ty X")
    done = (await _cmd("/quen", "suc khoe")).messages[0]
    assert done.startswith("🗑️ Em quên rồi")
    note = mem.session_notes[(UID, 10)]
    assert note["bullets"] == "Anh định đi bơi"
    assert note["summary"] == "Anh định đi bơi buổi sáng."
    assert (UID, 20) not in mem.session_notes  # chỉ còn tiêu đề nhắc tới -> bỏ cả ghi chú
    assert mem.summary[UID] == "Anh làm PM."
    statuses = {e["title"]: e["status"] for e in mem.events}
    assert statuses == {"tái khám tiểu đường type 2": "done", "phỏng vấn ở công ty X": "pending"}


@pytest.mark.asyncio
async def test_forget_dung_ca_su_kien_hoi_tham(mem):
    mem.add_event(UID, "phỏng vấn ở công ty X")
    assert "Đã xoá toàn bộ" in (await _cmd("/forget")).messages[0]
    assert mem.events == []
