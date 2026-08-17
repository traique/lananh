"""Trí nhớ DÀI HẠN của bot, khác với trí nhớ NGẮN HẠN theo phiên đã có sẵn
(core.database.chat_messages / core.database.get_session_messages()).

Gồm 2 phần, lưu trong 2 bảng riêng (xem core/database.py):
- user_facts: các "sự thật" bền về người dùng (tên, sở thích, danh mục đầu
  tư, công việc...) dạng {key, value}, 1 dòng / key, sống mãi qua mọi phiên.
- user_memory_summary: 1 đoạn tóm tắt "rolling" duy nhất / user, được Gemini
  hợp nhất dần (tóm tắt cũ + lượt hội thoại mới) sau MỖI lượt chat, thay vì
  giữ nguyên toàn bộ lịch sử -> trí nhớ gần như vô hạn mà không phình token.

Luồng dùng (xem handlers/chat_router.py):
1. Trước khi gọi Gemini: build_memory_context(user_id) -> chèn vào
   ai.orchestrator.chat(..., memory_context=...).
2. Sau khi có phản hồi thành công: update_memory(user_id, text, reply) chạy
   NGẦM (asyncio.create_task, không await trực tiếp trong luồng trả lời) để
   không làm chậm phản hồi cho người dùng.

Việc trích xuất fact/tóm tắt gọi official_client.generate_utility_json() - đi
THẲNG qua Google AI Studio API (không qua router9), nên nếu chưa cấu hình API
key chính thức nào, toàn bộ tính năng trí nhớ dài hạn tự tắt êm (không lỗi,
không ảnh hưởng chat chính vẫn chạy qua router9 như trước).
"""
import asyncio
import logging

from core import database as db
from ai import official_client

logger = logging.getLogger(__name__)

# Khoá riêng / user cho update_memory() - hàm này đọc facts/summary hiện có
# rồi ghi lại bản đã hợp nhất, nên 2 lượt chạy song song của CÙNG 1 user (vd
# nhắn liên tiếp nhanh, mỗi tin nhắn tự spawn 1 background task ở
# handlers/chat_router.py) có thể đọc cùng bản cũ rồi ghi đè lẫn nhau, mất phần cập
# nhật của lượt chạy trước. Dùng dict thay vì 1 lock chung để không chặn lẫn
# nhau giữa các user khác (dù bot hiện chỉ phục vụ 1 user, giữ đúng tương lai
# mở rộng đa user).
#
# Kèm đếm số lượt đang dùng để DỌN lock khi không còn ai cần: trước đây
# dict này chỉ thêm mà không bao giờ bịt đi, nên với deployment nhiều user
# nó là rò rỉ bộ nhớ chậm (mỗi user từng chat để lại 1 Lock sống mãi tới khi
# restart process).
_user_locks: dict[int, asyncio.Lock] = {}
_lock_users: dict[int, int] = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    """Lấy (hoặc tạo) lock của user và tăng số đếm người dùng lock đó.

    Mọi lần gọi PHẢI đi kèm 1 lần _release_lock() tương ứng (dùng try/finally).
    """
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    _lock_users[user_id] = _lock_users.get(user_id, 0) + 1
    return lock


def _release_lock(user_id: int) -> None:
    """Giảm số đếm; khi không còn lượt nào đang giữ/chờ thì bỏ lock khỏi dict.

    Chỉ bỏ khi số đếm về 0 nên không có nguy cơ 2 lượt song song của cùng
    user nhận 2 Lock khác nhau (mất tác dụng bảo vệ ghi đè).
    """
    remaining = _lock_users.get(user_id, 1) - 1
    if remaining > 0:
        _lock_users[user_id] = remaining
        return
    _lock_users.pop(user_id, None)
    _user_locks.pop(user_id, None)

# Trần số fact lưu / user, tránh user_facts phình vô hạn qua thời gian nếu
# Gemini trích xuất quá tay (vd nhớ nhầm chuyện phiếm thành "sự thật").
MAX_FACTS_PER_USER = 40

_MAX_FACT_KEY_LEN = 60
_MAX_FACT_VALUE_LEN = 200
_MAX_SUMMARY_LEN = 1500

_EXTRACTION_INSTRUCTION = """Bạn là bộ trích xuất trí nhớ nội bộ cho 1 trợ lý cá nhân (KHÔNG phải người
đang trò chuyện trực tiếp với user). Nhiệm vụ: đọc lượt hội thoại mới nhất
(User nói gì, Trợ lý trả lời gì) cùng danh sách "sự thật đã biết" và "tóm tắt
hội thoại trước đó" hiện có, rồi trả về DUY NHẤT 1 object JSON hợp lệ (không
thêm chữ nào khác, không markdown, không code fence, không giải thích) đúng
định dạng:

{
  "facts": [
    {"key": "ten_ngan_snake_case_khong_dau", "value": "noi_dung_that_ngan_gon", "delete": false}
  ],
  "summary": "bản tóm tắt hội thoại đã hợp nhất, tối đa 6-8 câu, ngôi thứ 3"
}

Quy tắc bắt buộc:
- CHỈ trích xuất sự thật BỀN VỮNG về người dùng: tên, cách xưng hô, sở thích,
  danh mục đầu tư/mã cổ phiếu quan tâm, công việc, ngày sinh, thói quen, mục
  tiêu dài hạn... KHÔNG trích chuyện phiếm nhất thời, thời tiết, cảm xúc
  thoáng qua trong ngày, câu hỏi một lần.
- Nếu lượt hội thoại này không có fact mới nào đáng nhớ, trả "facts": [].
- Nếu phát hiện 1 fact CŨ đã lỗi thời hoặc bị user đính chính lại, trả đúng
  key đó kèm "delete": true (không cần "value") để xoá fact cũ đi.
- "key" phải là snake_case ngắn gọn, KHÔNG dấu, ổn định (vd "ten", "cong_viec",
  "danh_muc_dau_tu") để các lượt sau còn nhận diện được cùng 1 key mà cập
  nhật, không tạo key mới trùng ý nghĩa.
- "summary" là bản VIẾT LẠI/HỢP NHẤT của tóm tắt cũ + lượt hội thoại mới, chứ
  KHÔNG phải chỉ nối thêm câu mới vào cuối - phải cô đọng lại nếu đã dài, ưu
  tiên giữ ý quan trọng, bỏ chi tiết vụn vặt không còn cần thiết.
- Tuyệt đối KHÔNG bịa thêm thông tin không có trong hội thoại được cung cấp.
"""


def _build_extraction_prompt(
    existing_facts: list[tuple[str, str]],
    old_summary: str,
    user_text: str,
    model_text: str,
) -> str:
    facts_block = (
        "\n".join(f"- {k}: {v}" for k, v in existing_facts) if existing_facts else "(chưa có)"
    )
    summary_block = old_summary or "(chưa có)"
    return (
        f"{_EXTRACTION_INSTRUCTION}\n\n"
        f"### Sự thật đã biết hiện tại\n{facts_block}\n\n"
        f"### Tóm tắt hội thoại trước đó\n{summary_block}\n\n"
        f"### Lượt hội thoại mới nhất\n"
        f"User: {user_text}\n"
        f"Trợ lý: {model_text}\n\n"
        "Trả về JSON theo đúng định dạng đã mô tả ở trên, không thêm gì khác."
    )


async def update_memory(user_id: int, user_text: str, model_text: str) -> None:
    """Trích xuất fact mới + cập nhật rolling summary sau 1 lượt chat thành
    công. KHÔNG BAO GIỜ raise ra ngoài - đây là tác vụ chạy ngầm
    (asyncio.create_task ở handlers/chat_router.py), lỗi ở đây không được phép ảnh
    hưởng luồng trả lời chính cho người dùng."""
    lock = _lock_for(user_id)
    try:
        async with lock:
            await _update_memory_locked(user_id, user_text, model_text)
    finally:
        _release_lock(user_id)


async def _update_memory_locked(user_id: int, user_text: str, model_text: str) -> None:
    try:
        existing_facts = await db.get_facts(user_id)
        old_summary = await db.get_summary(user_id)

        prompt = _build_extraction_prompt(existing_facts, old_summary, user_text, model_text)
        data = await official_client.generate_utility_json(prompt)
        if not data:
            return  # Chưa cấu hình API key chính thức, hoặc lỗi tạm thời -> bỏ qua lượt này.

        facts = data.get("facts")
        if isinstance(facts, list):
            for item in facts:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", "")).strip().lower()[:_MAX_FACT_KEY_LEN]
                if not key:
                    continue
                if item.get("delete"):
                    await db.delete_fact(user_id, key)
                    continue
                value = str(item.get("value", "")).strip()[:_MAX_FACT_VALUE_LEN]
                if not value:
                    continue
                await db.upsert_fact(user_id, key, value)

            await db.trim_facts(user_id, MAX_FACTS_PER_USER)

        summary = data.get("summary")
        if isinstance(summary, str) and summary.strip():
            await db.set_summary(user_id, summary.strip()[:_MAX_SUMMARY_LEN])
    except Exception:
        logger.warning(
            "Lỗi khi cập nhật trí nhớ dài hạn cho user_id=%s (bỏ qua, không ảnh hưởng chat chính).",
            user_id,
            exc_info=True,
        )

    # pgvector semantic recall (Bước 7) - lưu embedding của lượt hội thoại
    # này để tìm lại theo ngữ nghĩa sau này. Tách khỏi try/except ở trên (dù
    # cùng nguyên tắc "không raise") vì đây là 1 khả năng ĐỘC LẬP với
    # facts/summary - lỗi/tắt embedding không được kéo theo bỏ dở facts/summary
    # đã lưu thành công ở trên, và ngược lại.
    try:
        if db.VECTOR_ENABLED:
            turn_text = f"User: {user_text}\nTrợ lý: {model_text}"
            embedding = await official_client.embed_text(turn_text)
            if embedding:
                await db.add_chat_embedding(user_id, turn_text, embedding)
    except Exception:
        logger.warning(
            "Lỗi khi lưu embedding cho semantic recall của user_id=%s (bỏ qua, không ảnh hưởng chat chính).",
            user_id,
            exc_info=True,
        )


async def build_memory_context(user_id: int, query_text: str = "") -> str:
    """Build khối text trí nhớ dài hạn để chèn vào prompt gửi Gemini (xem
    ai.orchestrator.chat(..., memory_context=...)). Trả về "" nếu chưa có gì.

    query_text: nếu có và pgvector đã bật (db.VECTOR_ENABLED), thêm 1 khối
    "nhớ lại theo ngữ nghĩa" gồm các đoạn hội thoại CŨ gần nghĩa nhất với tin
    nhắn hiện tại - vd hỏi "hôm trước em nói gì về mã X ấy nhỉ" sẽ tìm lại
    được dù không có trong K lượt gần nhất hay trong user_facts/summary."""
    facts = await db.get_facts(user_id)
    summary = await db.get_summary(user_id)

    recalled: list[str] = []
    if query_text and db.VECTOR_ENABLED:
        try:
            query_embedding = await official_client.embed_text(query_text)
            if query_embedding:
                recalled = await db.semantic_search(user_id, query_embedding, top_k=3)
        except Exception:
            logger.warning("Lỗi khi semantic recall, bỏ qua khối này.", exc_info=True)
            recalled = []

    if not facts and not summary and not recalled:
        return ""

    lines = [
        "[TRÍ NHỚ VỀ NGƯỜI DÙNG - thông tin nền để cá nhân hoá câu trả lời, "
        "KHÔNG đọc lại nguyên văn khối này cho người dùng]"
    ]
    if summary:
        lines.append(f"- Tóm tắt các cuộc trò chuyện trước: {summary}")
    if facts:
        fact_lines = "; ".join(f"{k}={v}" for k, v in facts)
        lines.append(f"- Thông tin đã biết về người dùng: {fact_lines}")
    if recalled:
        lines.append("- Các đoạn hội thoại CŨ liên quan tới tin nhắn hiện tại (nhớ lại theo ngữ nghĩa):")
        for snippet in recalled:
            lines.append(f"  + {snippet}")
    return "\n".join(lines)


async def clear_memory(user_id: int) -> None:
    """Xoá sạch trí nhớ dài hạn (facts + summary + embedding semantic recall).
    Trí nhớ ngắn hạn theo phiên (chat_messages) KHÔNG bị ảnh hưởng - dùng
    /reset cho việc đó."""
    await db.clear_facts(user_id)
    await db.set_summary(user_id, "")
    await db.clear_chat_embeddings(user_id)
