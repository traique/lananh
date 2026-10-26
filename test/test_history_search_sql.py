"""SQL tìm lịch sử chat (core/repositories/chat_history.search) trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db


@pytest.mark.asyncio
async def test_tim_khong_phan_biet_dau_kem_cau_cung_luot_va_rieng_nguoi(delivery_db):
    await db.add_chat_turn(42, "QUỸ ETF nào phí thấp?", "Quỹ ETF VN30 phí 0,6%/năm.")
    await db.add_chat_turn(42, "giá vàng hôm nay", "Vàng SJC 120 triệu/lượng.")
    await db.add_chat_turn(-5, "quỹ etf của thành viên", "riêng thành viên")

    rows = await db.search_chat_history(42, ["quy", "etf"])
    assert [(r["role"], r["content"]) for r in rows] == [
        ("model", "Quỹ ETF VN30 phí 0,6%/năm."),
        ("user", "QUỸ ETF nào phí thấp?"),
    ]
    assert rows[0]["pair_content"] == "QUỸ ETF nào phí thấp?"
    assert rows[1]["pair_content"] == "Quỹ ETF VN30 phí 0,6%/năm."
    assert await db.search_chat_history(42, ["dau", "tu"]) == []
    # Từ lạ (ký tự đại diện LIKE) bị bỏ, không quét cả bảng.
    assert await db.search_chat_history(42, ["%", "_"]) == []
    member = await db.search_chat_history(-5, ["etf"])
    assert [r["content"] for r in member] == ["quỹ etf của thành viên"]


@pytest.mark.asyncio
async def test_tran_quet_va_cat_noi_dung(delivery_db):
    for i in range(5):
        await db.add_chat_turn(7, f"hỏi thuế lần {i}", "x" * 900)
    rows = await db.search_chat_history(7, ["thue"], scan_limit=3, max_chars=50)
    assert len(rows) == 3
    assert all(len(r["pair_content"]) == 50 for r in rows)
