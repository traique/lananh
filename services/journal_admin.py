"""Trang /admin "Nhật ký" (ý từ Memory Viewer của isair/jarvis): xem ghi chú phiên
(user_session_notes) xếp theo ngày và tóm tắt dài hạn (user_memory_summary) của chủ
bot hoặc 1 người Zalo, tìm theo từ khoá, sửa/xoá từng ghi chú.

- Phân trang có trần: PAGE_SIZE ghi chú/trang, tối đa MAX_PAGE trang (mỗi người chỉ
  giữ ~100 phiên - db.SESSION_NOTES_RETENTION). Từ khoá tối đa MAX_QUERY ký tự, tìm
  ILIKE trong tiêu đề, ý chính, tóm tắt (không phân biệt hoa thường).
- Sửa/xoá chạy trong ``memory_service.user_lock`` (cùng khoá với bộ trích nhớ đang
  ghi ghi chú phiên) - chờ quá LOCK_WAIT_SEC thì báo bận. Sửa ghi đè cả 3 trường;
  để trống cả 3 = xoá. Xoá chỉ xoá ghi chú phiên, không đụng lịch sử chat.
- Chỉ nhận user_id có trong danh sách người dùng của trang admin (chủ bot + Zalo đã
  ghép) - không đọc/sửa dữ liệu theo id tuỳ ý.
Mọi chữ trả về là dữ liệu thô: trang admin escape HTML khi hiển thị.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from core import database as db
from core.text_normalize import nfc
from services import memory_service

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
PAGE_SIZE = 20
MAX_PAGE = 50
MAX_QUERY = 100
TITLE_MAX = 200
BULLETS_MAX = 1500
SUMMARY_MAX = 1200
LOCK_WAIT_SEC = 5.0
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class JournalBusy(RuntimeError):
    """Bộ trích nhớ đang ghi trí nhớ của người này - thử lại sau."""


def clean_query(raw: str | None) -> str:
    return " ".join(nfc(raw or "").split())[:MAX_QUERY]


def clean_field(raw: object, limit: int) -> str:
    text = _CONTROL_RE.sub("", nfc(str(raw or ""))).replace("\r\n", "\n").strip()
    return text[:limit]


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _day(value) -> str:
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=VN_TZ)
        return stamp.astimezone(VN_TZ).date().isoformat()
    return str(value or "")[:10]


async def page(user_id: int, query: str = "", page_no: int = 1) -> dict:
    """1 trang nhật ký: ghi chú phiên mới bắt đầu trước, gom theo ngày (giờ VN)."""
    query = clean_query(query)
    page_no = max(1, min(MAX_PAGE, int(page_no or 1)))
    total, rows = await db.search_session_notes(
        user_id, query, limit=PAGE_SIZE, offset=(page_no - 1) * PAGE_SIZE
    )
    days: list[dict] = []
    for row in rows:
        day = _day(row.get("started_at"))
        if not days or days[-1]["day"] != day:
            days.append({"day": day, "notes": []})
        days[-1]["notes"].append(
            {
                "session_key": int(row["session_key"]),
                "title": str(row.get("title") or ""),
                "bullets": str(row.get("bullets") or ""),
                "summary": str(row.get("summary") or ""),
                "started_at": _iso(row.get("started_at")),
                "updated_at": _iso(row.get("updated_at")),
            }
        )
    summary = await db.get_summary_row(user_id) if page_no == 1 and not query else None
    pages = min(MAX_PAGE, max(1, -(-total // PAGE_SIZE)))
    return {
        "user_id": user_id,
        "query": query,
        "page": page_no,
        "pages": pages,
        "total": total,
        "page_size": PAGE_SIZE,
        "summary": (
            {
                "text": str(summary.get("summary") or ""),
                "updated_at": _iso(summary.get("updated_at")),
            }
            if summary and summary.get("summary")
            else None
        ),
        "days": days,
    }


async def _locked_rewrite(
    user_id: int, session_key: int, *, title: str, bullets: str, summary: str
) -> bool:
    try:
        async with memory_service.user_lock(user_id, wait_sec=LOCK_WAIT_SEC):
            if await db.get_session_note(user_id, session_key) is None:
                return False
            await db.rewrite_session_note(
                user_id, session_key, title=title, bullets=bullets, summary=summary
            )
            return True
    except memory_service.MemoryBusy as exc:
        raise JournalBusy(str(exc)) from exc


async def edit_note(
    user_id: int, session_key: int, *, title: object, bullets: object, summary: object
) -> bool:
    """Ghi đè 1 ghi chú phiên (cả 3 trường rỗng = xoá). False nếu không có ghi chú đó."""
    return await _locked_rewrite(
        user_id,
        session_key,
        title=clean_field(title, TITLE_MAX),
        bullets=clean_field(bullets, BULLETS_MAX),
        summary=clean_field(summary, SUMMARY_MAX),
    )


async def delete_note(user_id: int, session_key: int) -> bool:
    return await _locked_rewrite(user_id, session_key, title="", bullets="", summary="")
