"""core/settings_cache.py + hook ghi của core/database.py: khoá đọc nhiều (pairing Zoom,
session Zalo, con trỏ quét, mốc job dọn DB) không chạm DB mỗi lượt mà không bao giờ cũ
hơn những gì process đã ghi qua db.set_setting / compare_and_set_setting."""

import asyncio

import asyncpg
import pytest

from core import database as db, settings_cache


class _SettingsPool:
    """Bảng settings trong RAM, đếm số câu SELECT."""

    def __init__(self):
        self.rows: dict[str, str] = {}
        self.reads = 0
        self.fail_writes = 0

    async def fetchrow(self, sql, key):
        self.reads += 1
        return {"value": self.rows[key]} if key in self.rows else None

    async def execute(self, sql, *args):
        if self.fail_writes:
            self.fail_writes -= 1
            raise RuntimeError("lỗi ghi giả lập")
        if sql.lstrip().startswith("UPDATE"):
            key, expected, value = args
            if self.rows.get(key) != expected:
                return "UPDATE 0"
            self.rows[key] = value
            return "UPDATE 1"
        key, value = args
        self.rows[key] = value
        return "INSERT 0 1"


@pytest.fixture
def settings_pool(monkeypatch):
    pool = _SettingsPool()

    async def get_pool():
        return pool

    monkeypatch.setattr(db, "get_pool", get_pool)
    return pool


@pytest.mark.asyncio
async def test_ghi_qua_db_set_setting_cap_nhat_cache_khoa_dang_doc(settings_pool):
    assert await settings_cache.get("k") is None
    assert await settings_cache.get("k") is None
    assert settings_pool.reads == 1  # lần 2 lấy từ RAM
    await db.set_setting("k", "v1")  # ghi thẳng qua db (không qua settings_cache.set)
    assert await settings_cache.get("k") == "v1" and settings_pool.reads == 1
    # Xoay khoá mã hoá (compare-and-set) cũng cập nhật cache khi thắng.
    assert await db.compare_and_set_setting("k", "v1", "v2")
    assert not await db.compare_and_set_setting("k", "v1", "v3")
    assert await settings_cache.get("k") == "v2" and settings_pool.reads == 1
    # Khoá chỉ ghi, chưa từng đọc qua cache: không chiếm chỗ trong cache.
    await db.set_setting("chi-ghi", "x")
    assert "chi-ghi" not in settings_cache._cache


@pytest.mark.asyncio
async def test_ghi_loi_bo_khoa_khoi_cache(settings_pool):
    settings_pool.rows["k"] = "cu"
    assert await settings_cache.get("k") == "cu"
    settings_pool.fail_writes = 1
    with pytest.raises(RuntimeError):
        await db.set_setting("k", "moi")
    # Không rõ DB đang giữ gì -> đọc lại DB ở lần sau.
    assert "k" not in settings_cache._cache
    assert await settings_cache.get("k") == "cu" and settings_pool.reads == 2


@pytest.mark.asyncio
async def test_doc_cham_khong_nhet_gia_tri_cu_vao_cache_sau_khi_ghi(monkeypatch):
    gate = asyncio.Event()

    async def slow_get_setting(key):
        await gate.wait()
        return "gia-tri-cu"

    async def set_setting(key, value):
        db._setting_written(key, value)

    monkeypatch.setattr(db, "get_setting", slow_get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    reader = asyncio.create_task(settings_cache.get("k"))
    await asyncio.sleep(0)
    await settings_cache.set("k", "moi")  # ghi xong trong lúc lượt đọc DB còn treo
    gate.set()
    assert await reader == "gia-tri-cu"
    assert await settings_cache.get("k") == "moi"


@pytest.mark.asyncio
async def test_pairing_zoom_doc_db_1_lan(settings_pool):
    assert await db.zoom_get_pairing() is None
    await db.zoom_set_pairing("jid@zoom", "Chủ")
    reads = settings_pool.reads
    for _ in range(3):
        assert await db.zoom_get_pairing() == ("jid@zoom", "Chủ")
    assert settings_pool.reads == reads  # mỗi webhook Zoom không còn 2 lượt đọc DB
    await db.zoom_clear_pairing()
    assert await db.zoom_get_pairing() is None


@pytest.mark.asyncio
async def test_session_zalo_doc_tu_cache_va_tu_noi_lai(settings_pool, monkeypatch):
    from cryptography.fernet import Fernet

    from channels import zalo_session
    from core import crypto

    monkeypatch.setattr(crypto, "_fernet", Fernet(Fernet.generate_key()))

    await zalo_session.save_controller("  ctl-1 ")
    reads = settings_pool.reads
    assert await zalo_session.load_controller() == "ctl-1"
    await zalo_session.save_session({"accountId": "84901"})
    assert await zalo_session.load_account_id() == "84901"
    assert await zalo_session.load_account_id() == "84901"
    assert settings_pool.reads == reads
    assert settings_pool.rows["zalo:controller:v1"] != "ctl-1"  # DB chỉ giữ bản mã hoá

    # Kết nối rảnh bị cắt: lệnh đọc đầu tiên lỗi kết nối, tự thử lại.
    settings_cache.clear()
    real_fetchrow = settings_pool.fetchrow
    calls = []

    async def flaky_fetchrow(sql, key):
        calls.append(key)
        if len(calls) == 1:
            raise asyncpg.ConnectionDoesNotExistError("connection was closed")
        return await real_fetchrow(sql, key)

    async def no_reset(failed_pool):
        return None

    monkeypatch.setattr(settings_pool, "fetchrow", flaky_fetchrow)
    monkeypatch.setattr(db, "_reset_pool", no_reset)
    assert await zalo_session.load_controller() == "ctl-1" and len(calls) == 2
