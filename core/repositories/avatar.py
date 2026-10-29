"""SQL ảnh chân dung trang /admin/avatar (migrations/029, services/avatar_photo.py).

``meta()`` không đọc cột ảnh (bytea vài trăm KB) - trang avatar chỉ cần kích thước,
điểm mặt và lúc cập nhật; ảnh tải riêng qua ``photo()`` ở route có phiên admin.
"""

from __future__ import annotations

import json
from typing import Any


def _landmarks(raw: Any) -> list | None:
    # asyncpg trả jsonb dạng chuỗi (không đăng ký codec), PGlite trả đã giải mã.
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else list(raw)


async def meta(pool: Any) -> dict[str, dict]:
    rows = await pool.fetch(
        "SELECT kind, mime, width, height, landmarks, updated_at FROM avatar_photo"
    )
    return {
        row["kind"]: {
            "mime": row["mime"],
            "width": row["width"],
            "height": row["height"],
            "landmarks": _landmarks(row["landmarks"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    }


async def photo(pool: Any, kind: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT mime, data, width, height, updated_at FROM avatar_photo WHERE kind = $1", kind
    )
    return dict(row) if row else None


async def put(
    pool: Any,
    kind: str,
    *,
    mime: str,
    data: bytes,
    width: int,
    height: int,
    landmarks: list | None,
) -> None:
    """Thêm hoặc thay ảnh ``kind`` (UPSERT - chạy lại an toàn)."""
    await pool.execute(
        """
        INSERT INTO avatar_photo (kind, mime, data, width, height, landmarks, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, now())
        ON CONFLICT (kind) DO UPDATE SET
            mime = EXCLUDED.mime,
            data = EXCLUDED.data,
            width = EXCLUDED.width,
            height = EXCLUDED.height,
            landmarks = EXCLUDED.landmarks,
            updated_at = now()
        """,
        kind,
        mime,
        data,
        width,
        height,
        None if landmarks is None else json.dumps(landmarks, separators=(",", ":")),
    )


async def delete(pool: Any, kind: str) -> int:
    """Xoá ảnh ``kind``; xoá ảnh chính thì xoá luôn ảnh mắt nhắm (không còn ảnh nào để
    khớp kích thước). Trả số dòng đã xoá."""
    status = await pool.execute(
        "DELETE FROM avatar_photo WHERE kind = $1 OR ($1 = 'main' AND kind = 'blink')", kind
    )
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0
