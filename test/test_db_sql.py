"""SQL tầng DB lõi trên PostgreSQL thật: dọn dữ liệu cũ (services/db_maintenance.py),
lịch sử phiên chat (get_session_messages).

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng
một lần). Dùng chung fixture ``delivery_db`` của file đó (đã chạy mọi migration).
"""

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db
from services import db_maintenance as dm

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db


async def _chat(pool, uid, rows):
    """rows: [(role, content, số giây trước "bây giờ")], cũ -> mới (id tăng dần)."""
    for role, content, ago in rows:
        await pool.execute(
            "INSERT INTO chat_messages (telegram_user_id, role, content, created_at) "
            "VALUES ($1, $2, $3, now() - $4::integer * interval '1 second')",
            uid,
            role,
            content,
            ago,
        )


@pytest.mark.asyncio
async def test_lich_su_phien_cat_o_khoang_nghi_dau_tien(delivery_db):
    await _chat(
        delivery_db,
        1,
        [
            ("user", "tin càng cũ hơn", 6 * 3600),
            ("model", "tin phiên trước", 5 * 3600),
            # Khoảng nghỉ > 1 giờ ở đây -> phiên hiện tại bắt đầu từ tin kế.
            ("user", "tin trước đó 30s", 40),
            ("model", "tin mới nhất", 10),
        ],
    )
    await _chat(delivery_db, 2, [("user", "người khác", 5)])
    assert await db.get_session_messages(1, 10, 3600) == [
        ("user", "tin trước đó 30s"),
        ("model", "tin mới nhất"),
    ]
    # Không có khoảng nghỉ nào vượt ngưỡng: lấy đủ k dòng gần nhất, cũ -> mới.
    assert await db.get_session_messages(1, 3, 10 * 3600) == [
        ("model", "tin phiên trước"),
        ("user", "tin trước đó 30s"),
        ("model", "tin mới nhất"),
    ]


@pytest.mark.asyncio
async def test_lich_su_phien_tin_moi_nhat_qua_cu_thi_rong_va_gioi_han_k(delivery_db):
    await _chat(delivery_db, 3, [("model", "tin cũ", 2 * 3600)])
    assert await db.get_session_messages(3, 10, 3600) == []
    # Cặp lưu cùng transaction (cùng created_at) vẫn thuộc 1 phiên; k cắt phần cũ.
    turns = [(role, f"tin {i}", 50 - 20 * (i // 2)) for i, role in enumerate(["user", "model"] * 3)]
    await _chat(delivery_db, 4, turns)
    assert await db.get_session_messages(4, 2, 3600) == [("user", "tin 4"), ("model", "tin 5")]
    assert len(await db.get_session_messages(4, 10, 3600)) == 6
    assert await db.get_session_messages(5, 10, 3600) == []  # chưa có tin nào


@pytest.mark.asyncio
async def test_moi_luat_don_du_lieu_chay_duoc_va_giu_dong_can_thiet(delivery_db, caplog):
    pool = delivery_db
    await pool.execute(
        """INSERT INTO backup_runs (run_key, kind, finished_at) VALUES
        ('b-old', 'backup', now() - interval '500 days'),
        ('b-new', 'backup', now() - interval '1 day'),
        ('d-older', 'drill', now() - interval '600 days'),
        ('d-latest-but-old', 'drill', now() - interval '500 days')"""
    )
    await pool.execute(
        """INSERT INTO app_devices (telegram_user_id, name, token_hash, revoked_at) VALUES
        (1, 'revoked-old', NULL, now() - interval '100 days'),
        (1, 'revoked-new', NULL, now() - interval '10 days')"""
    )
    await pool.execute(
        "INSERT INTO app_devices (telegram_user_id, name, token_hash) VALUES (1, 'active', 'tok')"
    )
    await pool.execute(
        """INSERT INTO app_devices (telegram_user_id, name, pair_code_hash, pair_expires_at) VALUES
        (1, 'pending-old', 'c1', now() - interval '100 days'),
        (1, 'pending-new', 'c2', now() - interval '1 day')"""
    )

    deleted = await dm.prune_retention()

    # Câu DELETE của MỌI luật chạy được trên schema thật (luật lỗi SQL bị nuốt + log cảnh báo).
    assert set(deleted) == {rule.label for rule in dm.RETENTION_RULES}
    assert not [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert deleted["backup_runs"] == 2 and deleted["app_devices"] == 2
    keys = {r["run_key"] for r in await pool.fetch("SELECT run_key FROM backup_runs")}
    # Dòng mới nhất mỗi loại luôn còn (watchdog/admin đọc nó), kể cả khi đã quá hạn giữ.
    assert keys == {"b-new", "d-latest-but-old"}
    names = {r["name"] for r in await pool.fetch("SELECT name FROM app_devices")}
    assert names == {"revoked-new", "active", "pending-new"}
