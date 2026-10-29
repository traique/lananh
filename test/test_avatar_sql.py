"""SQL ảnh avatar (migrations/029, core/repositories/avatar.py) trên PostgreSQL thật.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

import pytest

import test_delivery_sql as _delivery_sql
from core import database as db

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db

_POINTS = [[0.1, 0.2]] * 478


def _bytes(value) -> bytes:
    # asyncpg trả bytes; cầu PGlite của test trả chuỗi hex.
    return bytes.fromhex(value) if isinstance(value, str) else bytes(value)


async def _put(kind, width=800, landmarks=_POINTS, data=b"\x00\x01webp"):
    await db.avatar_photo_put(
        kind, mime="image/webp", data=data, width=width, height=600, landmarks=landmarks
    )


@pytest.mark.asyncio
async def test_luu_doc_thay_xoa_anh_avatar(delivery_db):
    await _put("main")
    meta = await db.avatar_photo_meta()
    assert set(meta) == {"main"} and "data" not in meta["main"]  # meta không kéo bytea
    assert meta["main"]["width"] == 800 and meta["main"]["landmarks"][0] == [0.1, 0.2]
    row = await db.avatar_photo_get("main")
    assert _bytes(row["data"]) == b"\x00\x01webp" and row["mime"] == "image/webp"
    await _put("main", width=900, data=b"new")  # UPSERT
    assert (await db.avatar_photo_meta())["main"]["width"] == 900
    await _put("blink", width=900, landmarks=None)
    assert (await db.avatar_photo_meta())["blink"]["landmarks"] is None
    assert await db.avatar_photo_get("khong-co") is None
    # Xoá ảnh chính kéo theo ảnh mắt nhắm.
    assert await db.avatar_photo_delete("main") == 2 and await db.avatar_photo_meta() == {}


@pytest.mark.asyncio
async def test_rang_buoc_bang_avatar(delivery_db):
    with pytest.raises(Exception, match="avatar_photo_main_landmarks"):
        await _put("main", landmarks=None)
    with pytest.raises(Exception, match="avatar_photo_kind_check"):
        await _put("other")
    with pytest.raises(Exception, match="avatar_photo_size_check"):
        await _put("blink", width=0, landmarks=None)
