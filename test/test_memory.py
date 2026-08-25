"""Unit test cho services/memory_service.py (trí nhớ dài hạn: user_facts +
rolling summary + highlights - xem docstring đầu services/memory_service.py)."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import database as db  # noqa: E402
from ai import official_client  # noqa: E402
from services import memory_service  # noqa: E402


@pytest.fixture(autouse=True)
def _memory_defaults(monkeypatch):
    """Mặc định cho mọi test trong file này: trí nhớ dài hạn đang BẬT, và
    đang ở ĐẦU 1 phiên chat mới (session rỗng) - override riêng ở từng test
    khi cần kiểm tra ngược lại."""
    async def fake_get_setting(key):
        return None  # is_enabled() coi None là "bật" (mặc định)

    async def fake_get_session_messages(uid, k, timeout_sec):
        return []  # _is_new_session() coi [] là "phiên mới"

    monkeypatch.setattr(db, "get_setting", fake_get_setting)
    monkeypatch.setattr(db, "get_session_messages", fake_get_session_messages)


@pytest.mark.asyncio
async def test_update_memory_upsert_delete_trim_va_highlight(monkeypatch):
    calls: list[tuple] = []

    async def fake_get_facts(uid):
        return [("ten", "Trai_cu"), ("nghe_nghiep", "ky_su")]

    async def fake_get_summary(uid):
        return "Tom tat cu"

    async def fake_upsert_fact(uid, k, v):
        calls.append(("upsert", k, v))

    async def fake_delete_fact(uid, k):
        calls.append(("delete", k))

    async def fake_trim_facts(uid, n):
        calls.append(("trim", n))

    async def fake_set_summary(uid, s):
        calls.append(("summary", s))

    async def fake_add_highlight(uid, content, keep_n):
        calls.append(("highlight", content, keep_n))

    async def fake_generate_utility_json(prompt):
        return {
            "facts": [
                {"key": "ten", "value": "Trai", "delete": False},
                {"key": "so_thich", "value": "ca phe", "delete": False},
                {"key": "nghe_nghiep", "delete": True},
            ],
            "summary": "Tom tat moi hop nhat",
            "highlight": "Ngay 15/8 ban chot loi HPG quanh gia 28",
        }

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    monkeypatch.setattr(db, "get_summary", fake_get_summary)
    monkeypatch.setattr(db, "upsert_fact", fake_upsert_fact)
    monkeypatch.setattr(db, "delete_fact", fake_delete_fact)
    monkeypatch.setattr(db, "trim_facts", fake_trim_facts)
    monkeypatch.setattr(db, "set_summary", fake_set_summary)
    monkeypatch.setattr(db, "add_highlight", fake_add_highlight)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate_utility_json)

    await memory_service.update_memory(1, "anh la ky su, thich ca phe", "da anh")

    assert ("upsert", "ten", "Trai") in calls
    assert ("upsert", "so_thich", "ca phe") in calls
    assert ("delete", "nghe_nghiep") in calls
    assert ("trim", memory_service.MAX_FACTS_PER_USER) in calls
    assert ("summary", "Tom tat moi hop nhat") in calls
    assert ("highlight", "Ngay 15/8 ban chot loi HPG quanh gia 28", memory_service.MAX_HIGHLIGHTS_PER_USER) in calls


@pytest.mark.asyncio
async def test_update_memory_khong_luu_highlight_khi_null(monkeypatch):
    """Phần lớn lượt hội thoại không có gì đáng highlight -> Gemini trả
    highlight=null -> KHÔNG được gọi db.add_highlight."""
    called = []

    async def fake_get_facts(uid):
        return []

    async def fake_get_summary(uid):
        return ""

    async def fake_add_highlight(*a, **kw):
        called.append((a, kw))

    async def fake_generate_utility_json(prompt):
        return {"facts": [], "summary": "", "highlight": None}

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    monkeypatch.setattr(db, "get_summary", fake_get_summary)
    monkeypatch.setattr(db, "add_highlight", fake_add_highlight)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate_utility_json)

    await memory_service.update_memory(1, "chào em", "chào anh")
    assert called == []


@pytest.mark.asyncio
async def test_update_memory_khong_raise_khi_gemini_loi(monkeypatch):
    """Lỗi ở tác vụ nền KHÔNG được raise ra ngoài - chat chính phải luôn
    tiếp tục bình thường dù trí nhớ dài hạn cập nhật thất bại."""

    async def fake_get_facts(uid):
        return []

    async def fake_get_summary(uid):
        return ""

    async def fake_raise(prompt):
        raise RuntimeError("mô phỏng lỗi API")

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    monkeypatch.setattr(db, "get_summary", fake_get_summary)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_raise)

    await memory_service.update_memory(1, "xin chào", "chào anh")  # không được raise


@pytest.mark.asyncio
async def test_update_memory_khong_lam_gi_khi_chua_co_api_key(monkeypatch):
    """generate_utility_json trả None (chưa cấu hình API key) -> không gọi
    bất kỳ hàm ghi DB nào."""
    write_calls = []

    async def fake_get_facts(uid):
        return []

    async def fake_get_summary(uid):
        return ""

    async def fake_none(prompt):
        return None

    async def fail_if_called(*a, **kw):
        write_calls.append((a, kw))

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    monkeypatch.setattr(db, "get_summary", fake_get_summary)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_none)
    monkeypatch.setattr(db, "upsert_fact", fail_if_called)
    monkeypatch.setattr(db, "set_summary", fail_if_called)
    monkeypatch.setattr(db, "add_highlight", fail_if_called)

    await memory_service.update_memory(1, "xin chào", "chào anh")
    assert write_calls == []


@pytest.mark.asyncio
async def test_build_memory_context_dinh_dang_dung(monkeypatch):
    async def fake_get_facts(uid):
        return [("ten", "Trai"), ("danh_muc", "FPT, HPG")]

    async def fake_get_summary(uid):
        return "Đã trò chuyện vài lần về đầu tư chứng khoán."

    async def fake_get_highlights(uid, limit):
        return ["Ngày 15/8 đã bàn chốt lời HPG quanh giá 28"]

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    monkeypatch.setattr(db, "get_summary", fake_get_summary)
    monkeypatch.setattr(db, "get_highlights", fake_get_highlights)

    ctx = await memory_service.build_memory_context(1)
    assert "TRÍ NHỚ VỀ NGƯỜI DÙNG" in ctx
    assert "ten=Trai" in ctx
    assert "danh_muc=FPT, HPG" in ctx
    assert "Đã trò chuyện vài lần" in ctx
    assert "chốt lời HPG quanh giá 28" in ctx


@pytest.mark.asyncio
async def test_build_memory_context_rong_khi_chua_co_gi(monkeypatch):
    async def fake_empty(uid):
        return []

    async def fake_empty_summary(uid):
        return ""

    async def fake_empty_highlights(uid, limit):
        return []

    monkeypatch.setattr(db, "get_facts", fake_empty)
    monkeypatch.setattr(db, "get_summary", fake_empty_summary)
    monkeypatch.setattr(db, "get_highlights", fake_empty_highlights)

    ctx = await memory_service.build_memory_context(1)
    assert ctx == ""


@pytest.mark.asyncio
async def test_build_memory_context_rong_khi_giua_phien_chat(monkeypatch):
    """Đang ở giữa 1 phiên chat (session đã có tin nhắn trước đó) -> KHÔNG
    được chèn lại trí nhớ dài hạn, kể cả khi đã có facts/summary/highlights -
    tránh nhồi lại mỗi tin nhắn làm lệch ý đang hỏi (xem docstring module)."""
    async def fake_get_session_messages(uid, k, timeout_sec):
        return [("user", "câu trước đó trong cùng phiên")]

    async def fail_if_called(*a, **kw):
        raise AssertionError("không được đọc facts/summary/highlights giữa phiên chat")

    monkeypatch.setattr(db, "get_session_messages", fake_get_session_messages)
    monkeypatch.setattr(db, "get_facts", fail_if_called)
    monkeypatch.setattr(db, "get_summary", fail_if_called)
    monkeypatch.setattr(db, "get_highlights", fail_if_called)

    assert await memory_service.build_memory_context(1) == ""


# ─── clear_memory: phải xoá cả highlights, không chỉ facts/summary ─────────

@pytest.mark.asyncio
async def test_clear_memory_xoa_ca_highlights(monkeypatch):
    calls = []

    async def fake_clear_facts(uid):
        calls.append(("clear_facts", uid))

    async def fake_set_summary(uid, s):
        calls.append(("set_summary", uid, s))

    async def fake_clear_highlights(uid):
        calls.append(("clear_highlights", uid))

    monkeypatch.setattr(db, "clear_facts", fake_clear_facts)
    monkeypatch.setattr(db, "set_summary", fake_set_summary)
    monkeypatch.setattr(db, "clear_highlights", fake_clear_highlights)

    await memory_service.clear_memory(7)

    assert ("clear_facts", 7) in calls
    assert ("set_summary", 7, "") in calls
    assert ("clear_highlights", 7) in calls


# ─── update_memory: khoá theo user_id, 2 lượt cùng user không chồng nhau ───

@pytest.mark.asyncio
async def test_update_memory_khoa_theo_user_khong_chay_chong_nhau(monkeypatch):
    order: list[str] = []
    release_first = asyncio.Event()

    async def fake_get_facts(uid):
        return []

    async def fake_get_summary(uid):
        return ""

    async def fake_generate_utility_json(prompt):
        # Lượt đầu tiên cố ý "chạy chậm" (đợi tín hiệu) để lượt thứ 2 (cùng
        # user) có cơ hội chen vào NẾU không có lock - nếu lock hoạt động
        # đúng, lượt 2 phải đợi lượt 1 xong hoàn toàn mới được bắt đầu.
        if "cham" in prompt:
            order.append("bat_dau_cham")
            await release_first.wait()
            order.append("ket_thuc_cham")
        else:
            order.append("bat_dau_nhanh")
            order.append("ket_thuc_nhanh")
        return {"facts": [], "summary": ""}

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    monkeypatch.setattr(db, "get_summary", fake_get_summary)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate_utility_json)

    task_cham = asyncio.create_task(memory_service.update_memory(1, "cham", "phan hoi"))
    await asyncio.sleep(0.01)  # đảm bảo lượt "chậm" đã vào lock trước
    task_nhanh = asyncio.create_task(memory_service.update_memory(1, "nhanh", "phan hoi"))
    await asyncio.sleep(0.01)

    release_first.set()
    await asyncio.gather(task_cham, task_nhanh)

    # Lượt "nhanh" phải đợi "chậm" kết thúc hoàn toàn mới được bắt đầu.
    assert order == ["bat_dau_cham", "ket_thuc_cham", "bat_dau_nhanh", "ket_thuc_nhanh"]


@pytest.mark.asyncio
async def test_update_memory_khac_user_khong_bi_chan_nhau(monkeypatch):
    order: list[str] = []
    release_user_1 = asyncio.Event()

    async def fake_get_facts(uid):
        return []

    async def fake_get_summary(uid):
        return ""

    async def fake_generate_utility_json(prompt):
        if "user1" in prompt:
            order.append("bat_dau_user1")
            await release_user_1.wait()
            order.append("ket_thuc_user1")
        else:
            order.append("bat_dau_user2")
            order.append("ket_thuc_user2")
        return {"facts": [], "summary": ""}

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    monkeypatch.setattr(db, "get_summary", fake_get_summary)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate_utility_json)

    task_user1 = asyncio.create_task(memory_service.update_memory(1, "user1", "phan hoi"))
    await asyncio.sleep(0.01)
    task_user2 = asyncio.create_task(memory_service.update_memory(2, "user2", "phan hoi"))
    # user2 không dùng chung lock với user1 nên phải chạy xong NGAY, không
    # cần đợi release_user_1.set().
    await asyncio.sleep(0.01)
    assert "ket_thuc_user2" in order
    assert "ket_thuc_user1" not in order  # user1 vẫn đang bị chặn ở await

    release_user_1.set()
    await asyncio.gather(task_user1, task_user2)


@pytest.mark.asyncio
async def test_update_memory_khong_goi_gi_khi_user_da_tat(monkeypatch):
    """Tắt trí nhớ dài hạn cho user -> update_memory() return sớm, KHÔNG được
    gọi generate_utility_json (tiết kiệm lượt gọi official_client)."""
    called = []

    async def fake_get_setting(key):
        return "0" if key == "memory_enabled_1" else None

    async def fake_generate_utility_json(prompt):
        called.append(prompt)
        return {"facts": [], "summary": ""}

    monkeypatch.setattr(db, "get_setting", fake_get_setting)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate_utility_json)

    await memory_service.update_memory(1, "xin chao", "da anh")

    assert called == []


@pytest.mark.asyncio
async def test_build_memory_context_rong_khi_user_da_tat(monkeypatch):
    async def fake_get_setting(key):
        return "0" if key == "memory_enabled_1" else None

    async def fail_if_called(*a, **kw):
        raise AssertionError("không được đọc facts/summary/highlights khi trí nhớ đang tắt")

    monkeypatch.setattr(db, "get_setting", fake_get_setting)
    monkeypatch.setattr(db, "get_facts", fail_if_called)
    monkeypatch.setattr(db, "get_summary", fail_if_called)
    monkeypatch.setattr(db, "get_highlights", fail_if_called)

    assert await memory_service.build_memory_context(1) == ""
