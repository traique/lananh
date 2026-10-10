"""Tự thu hồi dung lượng đĩa của bảng ảnh Facebook (Supabase free tier 500 MB).

Xoá ảnh chỉ đánh dấu chỗ trống để Postgres tái dùng; con số "Database size"
trên Supabase chỉ giảm sau ``VACUUM FULL``. Lệnh đó KHOÁ bảng và cần thêm dung
lượng tạm bằng phần dữ liệu còn sống, nên job này chỉ chạy khi an toàn:

1. Tối đa 1 lần/tuần, trong khung giờ vắng (mặc định 3h sáng Chủ nhật, giờ VN).
2. Chỉ khi lãng phí thật: phần thừa >= 50 MB VÀ >= 50% kích thước bảng.
3. Bỏ qua nếu đang có bài Facebook ở trạng thái POSTING.
4. Bỏ qua nếu DB + bản chép tạm có thể vượt giới hạn dung lượng.
5. ``lock_timeout`` 5 giây: không lấy được khoá thì bỏ lượt, không chặn bot.

Kết quả (trước/sau) được báo cho admin qua Telegram/Zoom.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from core import database as db

logger = logging.getLogger(__name__)
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MIB = 1024 * 1024
TABLE = "facebook_post_media"
_SETTING_KEY = "maintenance:vacuum_full:last_week"
_CHECK_INTERVAL_SEC = 1800
_VACUUM_TIMEOUT_SEC = 600

_task: asyncio.Task | None = None
_notify: Callable[[str], Awaitable[None]] | None = None


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def enabled() -> bool:
    return os.getenv("DB_AUTO_VACUUM_FULL", "1").strip() != "0"


def set_notifier(callback: Callable[[str], Awaitable[None]] | None) -> None:
    global _notify
    _notify = callback


@dataclass(frozen=True)
class TableUsage:
    total_bytes: int
    live_bytes: int
    database_bytes: int
    posting: int

    @property
    def wasted_bytes(self) -> int:
        return max(0, self.total_bytes - self.live_bytes)


def should_vacuum(usage: TableUsage) -> tuple[bool, str]:
    min_waste = _env_int("DB_VACUUM_MIN_WASTE_MB", 50, 1, 10_000) * MIB
    limit = _env_int("DB_SIZE_LIMIT_MB", 500, 50, 100_000) * MIB
    if usage.posting:
        return False, "đang có bài Facebook ở trạng thái POSTING"
    if usage.wasted_bytes < min_waste:
        return False, f"lãng phí {usage.wasted_bytes / MIB:.0f} MB, dưới ngưỡng"
    if usage.total_bytes and usage.wasted_bytes * 2 < usage.total_bytes:
        return False, "phần lãng phí chưa tới một nửa bảng"
    # VACUUM FULL chép phần còn sống sang file mới trước khi xoá file cũ.
    if usage.database_bytes + usage.live_bytes > limit * 0.95:
        return False, "không đủ dung lượng trống cho bản chép tạm"
    return True, "đủ điều kiện"


def in_window(now: datetime) -> bool:
    weekday = _env_int("DB_VACUUM_WEEKDAY", 6, 0, 6)  # 0 = Thứ 2 ... 6 = Chủ nhật
    hour = _env_int("DB_VACUUM_HOUR", 3, 0, 23)
    return now.weekday() == weekday and now.hour == hour


def _week_key(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


async def measure() -> TableUsage:
    pool = await db.get_pool()
    row = await pool.fetchrow(
        f"""
        SELECT
            pg_total_relation_size('{TABLE}') AS total_bytes,
            -- pg_column_size(content) đọc kích thước đã lưu (nén/TOAST) mà không
            -- giải nén ảnh; +64 byte/dòng cho phần đầu dòng và các cột nhỏ.
            (SELECT COALESCE(sum(pg_column_size(content)) + count(*) * 64, 0)
             FROM {TABLE}) AS live_bytes,
            pg_database_size(current_database()) AS database_bytes,
            (SELECT count(*) FROM facebook_post_queue WHERE status = 'POSTING') AS posting
        """
    )
    return TableUsage(
        int(row["total_bytes"]),
        int(row["live_bytes"]),
        int(row["database_bytes"]),
        int(row["posting"]),
    )


async def vacuum_full() -> tuple[int, int]:
    """Chạy VACUUM FULL trên 1 connection riêng; trả (kích thước trước, sau)."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        before = int(await conn.fetchval(f"SELECT pg_total_relation_size('{TABLE}')"))
        await conn.execute("SET lock_timeout = '5s'")
        try:
            # VACUUM không chạy được trong transaction; asyncpg execute không mở transaction.
            await conn.execute(f"VACUUM FULL {TABLE}", timeout=_VACUUM_TIMEOUT_SEC)
        finally:
            await conn.execute("RESET lock_timeout")
        after = int(await conn.fetchval(f"SELECT pg_total_relation_size('{TABLE}')"))
    return before, after


async def _send(text: str) -> None:
    if _notify is None:
        return
    try:
        await _notify(text)
    except Exception:
        logger.warning("Không gửi được báo cáo dọn DB tới admin.", exc_info=True)


async def run_if_due(now: datetime | None = None, *, force: bool = False) -> str | None:
    """Trả mô tả kết quả nếu đã kiểm tra trong lượt này, None nếu chưa tới giờ."""
    now = now or datetime.now(VN_TZ)
    if not force:
        if not enabled() or not in_window(now):
            return None
        if await db.get_setting(_SETTING_KEY) == _week_key(now):
            return None
    usage = await measure()
    ok, reason = should_vacuum(usage)
    # Ghi nhận đã kiểm tra tuần này dù chạy hay không: tránh đo lại mỗi 30 phút.
    await db.set_setting(_SETTING_KEY, _week_key(now))
    if not ok:
        message = (
            f"Bỏ qua VACUUM FULL {TABLE}: {reason} "
            f"(bảng {usage.total_bytes / MIB:.0f} MB, dữ liệu {usage.live_bytes / MIB:.0f} MB)."
        )
        logger.info(message)
        return message
    try:
        before, after = await vacuum_full()
    except Exception as exc:
        message = f"⚠️ VACUUM FULL {TABLE} không chạy được: {type(exc).__name__}: {exc}"
        logger.warning(message)
        await _send(message)
        return message
    message = (
        f"🧹 Đã thu hồi dung lượng DB: {TABLE} {before / MIB:.0f} MB → {after / MIB:.0f} MB "
        f"(giải phóng {(before - after) / MIB:.0f} MB)."
    )
    logger.info(message)
    await _send(message)
    return message


async def _loop() -> None:
    while True:
        try:
            await run_if_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Job dọn DB lỗi; thử lại lượt sau.")
        await asyncio.sleep(_CHECK_INTERVAL_SEC)


def start() -> None:
    global _task
    if not enabled():
        logger.info("DB_AUTO_VACUUM_FULL=0: tắt job tự VACUUM FULL.")
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
