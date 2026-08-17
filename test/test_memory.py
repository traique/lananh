"""Unit test cho services/memory_service.py (trí nhớ dài hạn: user_facts + rolling summary)."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import database as db  # noqa: E402
from ai import official_client  # noqa: E402
from services import memory_service  # noqa: E402


@pytest.mark.asyncio
async def test_update_memory_upsert_delete_va_trim(monkeypatch):
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

    async def fake_generate_utility_json(prompt):
        return {
            "facts": [
                {"key": "ten", "value": "Trai", "delete": False},
                {"key": "so_thich", "value": "ca phe", "delete": False},
                {"key": "nghe_nghiep", "delete": True},
            ],
            "summary": "Tom tat moi hop nhat",
        }

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    monkeypatch.setattr(db, "get_summary", fake_get_summary)
    monkeypatch.setattr(db, "upsert_fact", fake_upsert_fact)
    monkeypatch.setattr(db, "delete_fact", fake_delete_fact)
    monkeypatch.setattr(db, "trim_facts", fake_trim_facts)
    monkeypatch.setattr(db, "set_summary", fake_set_summary)
    monkeypatch.setattr(official_client, "generate_utility_json", fake_generate_utility_json)

    await memory_service.update_memory(1, "anh la ky su, thich ca phe", "da anh")

    assert ("upsert", "ten", "Trai") in calls
    assert ("upsert", "so_thich", "ca phe") in calls
    assert ("delete", "nghe_nghiep") in calls
    assert ("trim", memory_service.MAX_FACTS_PER_USER) in calls
    assert ("summary", "Tom tat moi hop nhat") in calls


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

    await memory_service.update_memory(1, "xin chào", "chào anh")
    assert write_calls == []


@pytest.mark.asyncio
async def test_build_memory_context_dinh_dang_dung(monkeypatch):
    async def fake_get_facts(uid):
        return [("ten", "Trai"), ("danh_muc", "FPT, HPG")]

    async def fake_get_summary(uid):
        return "Đã trò chuyện vài lần về đầu tư chứng khoán."

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    monkeypatch.setattr(db, "get_summary", fake_get_summary)

    ctx = await memory_service.build_memory_context(1)
    assert "TRÍ NHỚ VỀ NGƯỜI DÙNG" in ctx
    assert "ten=Trai" in ctx
    assert "danh_muc=FPT, HPG" in ctx
    assert "Đã trò chuyện vài lần" in ctx


@pytest.mark.asyncio
async def test_build_memory_context_rong_khi_chua_co_gi(monkeypatch):
    async def fake_empty_facts(uid):
        return []

    async def fake_empty_summary(uid):
        return ""

    monkeypatch.setattr(db, "get_facts", fake_empty_facts)
    monkeypatch.setattr(db, "get_summary", fake_empty_summary)
    monkeypatch.setattr(db, "VECTOR_ENABLED", False)

    ctx = await memory_service.build_memory_context(1)
    assert ctx == ""


@pytest.mark.asyncio
async def test_semantic_recall_tat_khi_vector_disabled(monkeypatch):
    """db.VECTOR_ENABLED=False -> KHÔNG được gọi embed_text/semantic_search
    dù có query_text, và không xuất hiện khối 'nhớ lại theo ngữ nghĩa'."""

    async def fake_empty_facts(uid):
        return []

    async def fake_empty_summary(uid):
        return ""

    async def fail_if_called(*a, **kw):
        raise AssertionError("Không được gọi khi VECTOR_ENABLED=False")

    monkeypatch.setattr(db, "get_facts", fake_empty_facts)
    monkeypatch.setattr(db, "get_summary", fake_empty_summary)
    monkeypatch.setattr(db, "VECTOR_ENABLED", False)
    monkeypatch.setattr(official_client, "embed_text", fail_if_called)

    ctx = await memory_service.build_memory_context(1, query_text="hôm trước nói gì về FPT")
    assert ctx == ""


@pytest.mark.asyncio
async def test_semantic_recall_bat_chen_ket_qua_vao_context(monkeypatch):
    async def fake_empty_facts(uid):
        return []

    async def fake_empty_summary(uid):
        return ""

    async def fake_embed(text):
        return [0.1, 0.2, 0.3]

    async def fake_semantic_search(uid, emb, top_k=3):
        return ["User: FPT hôm qua tăng mạnh\nTrợ lý: Dạ anh, FPT +3%"]

    monkeypatch.setattr(db, "get_facts", fake_empty_facts)
    monkeypatch.setattr(db, "get_summary", fake_empty_summary)
    monkeypatch.setattr(db, "VECTOR_ENABLED", True)
    monkeypatch.setattr(official_client, "embed_text", fake_embed)
    monkeypatch.setattr(db, "semantic_search", fake_semantic_search)

    ctx = await memory_service.build_memory_context(1, query_text="hôm trước nói gì về FPT")
    assert "nhớ lại theo ngữ nghĩa" in ctx
    assert "FPT +3%" in ctx


# ─── clear_memory: phải xoá cả embeddings, không chỉ facts/summary (#4) ─────

@pytest.mark.asyncio
async def test_clear_memory_xoa_ca_embeddings(monkeypatch):
    calls = []

    async def fake_clear_facts(uid):
        calls.append(("clear_facts", uid))

    async def fake_set_summary(uid, s):
        calls.append(("set_summary", uid, s))

    async def fake_clear_chat_embeddings(uid):
        calls.append(("clear_chat_embeddings", uid))

    monkeypatch.setattr(db, "clear_facts", fake_clear_facts)
    monkeypatch.setattr(db, "set_summary", fake_set_summary)
    monkeypatch.setattr(db, "clear_chat_embeddings", fake_clear_chat_embeddings)

    await memory_service.clear_memory(7)

    assert ("clear_facts", 7) in calls
    assert ("set_summary", 7, "") in calls
    assert ("clear_chat_embeddings", 7) in calls


# ─── update_memory: khoá theo user_id, 2 lượt cùng user không chồng nhau (#8) ─

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

