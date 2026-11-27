"""Nhận bài nhóm Zalo cho luồng Facebook: lọc, nén ảnh, chống trùng giữa các nhóm rồi
đưa vào hàng chờ duyệt (trước đây nằm hết trong handler ``/facebook-group-post`` của
channels/router.py).

Phần nhanh chạy ngay trong request: bộ lọc (services/facebook_caption.skip_reason), nén
ảnh + dHash (thread), so trùng với bài của MỌI nhóm nguồn và ghi hàng chờ
(channels/facebook_repository.create_post). Phần chậm - giữ trần hàng chờ, AI viết lại
caption, gửi bản xem trước cho admin, dọn dữ liệu cũ - chạy nền ``_FOLLOWUP_SLOTS`` việc
một lúc: gateway được trả 204 ngay sau khi bài đã nằm an toàn trong DB, hàng đợi tin
nhóm của gateway không bị chặn tới vài phút vì AI, và ngân sách request "nặng" của
services/request_limits.py được nhả sớm. Quá ``_MAX_BACKLOG`` việc nền đang chờ thì chạy
luôn trong request (gateway tự chậm lại thay vì RAM lớn dần).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from channels import facebook_commands, facebook_group_names, facebook_repository
from core.envutil import env_on
from services import facebook_dedup, facebook_intake_stats
from services.background_tasks import stop_tracked_tasks
from services.facebook_caption import skip_reason
from services.facebook_image import compact_image, image_dhash

logger = logging.getLogger(__name__)

_FOLLOWUP_SLOTS = 1
_MAX_BACKLOG = 50
_slots: asyncio.Semaphore | None = None
_tasks: set[asyncio.Task] = set()

SKIPPED = "skipped"
DUPLICATE = "duplicate"
NOT_SOURCE = "not_source"
QUEUED = "queued"


@dataclass(frozen=True)
class IncomingPost:
    account_id: str
    group_id: str
    sender_id: str
    sender_name: str
    message_ids: list[str]
    text: str


def _short(text: str) -> str:
    return " ".join((text or "").split())[:60]


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(_FOLLOWUP_SLOTS)
    return _slots


async def ingest(post: IncomingPost, media: list[tuple[str, bytes]]) -> str:
    """Lọc + đưa 1 bài vào hàng chờ. Trả SKIPPED/DUPLICATE/NOT_SOURCE/QUEUED. ``media``
    bị làm rỗng sau khi nén để giải phóng bản gốc sớm (máy 512 MB)."""
    names = await facebook_group_names.labels(post.account_id)
    group = facebook_group_names.label(names, post.group_id)
    reason = skip_reason(
        post.text,
        bool(media),
        require_photo_and_caption=env_on("FACEBOOK_REQUIRE_PHOTO_AND_CAPTION"),
        skip_voucher=env_on("FACEBOOK_SKIP_VOUCHER_POSTS"),
        skip_non_sale=env_on("FACEBOOK_SKIP_NON_SALE_POSTS"),
    )
    if reason:
        logger.info(
            "Bỏ qua bài Zalo (%s) nhóm %s [%s] người gửi=%s: %.60s",
            reason,
            group,
            post.group_id,
            post.sender_id,
            _short(post.text),
        )
        facebook_intake_stats.record(reason, post.text, group=group)
        return SKIPPED
    # Nén ảnh TRƯỚC khi lưu (Supabase free tier 500 MB) và lấy dHash để chống trùng.
    # Xử lý tuần tự từng ảnh để giữ đỉnh RAM thấp trên máy 512 MB.
    stored_media: list[tuple[str, bytes]] = []
    image_hashes: list[int] = []
    while media:
        mime_type, body = media.pop(0)
        stored = await asyncio.to_thread(compact_image, mime_type, body)
        del body
        stored_media.append(stored)
        digest = await asyncio.to_thread(image_dhash, stored[1])
        if digest is not None:
            image_hashes.append(digest)
    fingerprint = facebook_dedup.build_fingerprint(
        post.text, image_hashes, group_id=post.group_id, sender_id=post.sender_id
    )
    try:
        post_id = await facebook_repository.create_post(
            account_id=post.account_id,
            group_id=post.group_id,
            sender_id=post.sender_id,
            sender_name=post.sender_name,
            source_message_ids=post.message_ids,
            content=post.text.strip(),
            media=stored_media,
            fingerprint=fingerprint,
        )
    except facebook_repository.DuplicatePostError as exc:
        match = exc.match
        origin = facebook_group_names.label(names, match.group_id) if match.group_id else ""
        logger.info(
            "Bỏ qua bài Zalo trùng (%s, bài #%s%s) nhóm %s người gửi=%s: %.60s",
            match.reason,
            match.post_id,
            f" ở nhóm {origin}" if origin else "",
            group,
            post.sender_id,
            _short(post.text),
        )
        facebook_intake_stats.record(
            facebook_intake_stats.duplicate_reason(match.reason),
            post.text,
            group=group,
            cross_group=match.cross_group,
        )
        return DUPLICATE
    finally:
        del stored_media  # ảnh đã nằm trong DB: bỏ tham chiếu trước việc nền
    if post_id is None:
        return NOT_SOURCE
    facebook_intake_stats.record(facebook_intake_stats.QUEUED, group=group)
    await _schedule_followup(post.account_id, post_id)
    return QUEUED


async def _schedule_followup(account_id: str, post_id: int) -> None:
    if len(_tasks) >= _MAX_BACKLOG:
        logger.warning(
            "Việc nền Facebook đang dồn %d bài: xử lý bài #%s ngay trong request.",
            len(_tasks),
            post_id,
        )
        await _followup(account_id, post_id)
        return
    task = asyncio.create_task(_followup(account_id, post_id), name=f"fb-intake-{post_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _followup(account_id: str, post_id: int) -> None:
    """Giữ trần hàng chờ, AI viết lại + gửi xem trước, dọn dữ liệu cũ (1 bài)."""
    async with _semaphore():
        note = None
        try:
            pruned = await facebook_repository.enforce_pending_cap(account_id)
        except Exception:
            logger.warning("Không giữ được trần hàng chờ Facebook.", exc_info=True)
            pruned = []
        if pruned:
            facebook_intake_stats.record(facebook_intake_stats.PRUNED, amount=len(pruned))
            shown = ", ".join(f"#{i}" for i in pruned[:10]) + ("..." if len(pruned) > 10 else "")
            note = (
                f"🧹 Hàng chờ vượt {facebook_repository.max_pending()} bài: "
                f"đã tự xoá {len(pruned)} bài chờ cũ nhất ({shown})."
            )
        if post_id not in pruned:
            try:
                await facebook_commands.prepare_post(account_id, post_id, note=note)
            except Exception:
                logger.warning(
                    "Không chuẩn bị/gửi được bản xem trước bài Facebook #%s (xem lại bằng /fb_xem).",
                    post_id,
                    exc_info=True,
                )
        try:
            await facebook_repository.prune_history()
        except Exception:
            logger.warning("Dọn dữ liệu Facebook cũ lỗi; thử lại lần sau.", exc_info=True)


async def drain(grace_sec: float = 20.0) -> None:
    """Chờ các việc nền đang chạy xong (tắt máy, test); quá ``grace_sec`` thì huỷ."""
    await stop_tracked_tasks(_tasks, timeout=grace_sec, logger=logger, label="facebook intake")
