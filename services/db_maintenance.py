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

Ngoài ra mỗi ngày 1 lần dọn dữ liệu cũ của các bảng chỉ-thêm-không-bớt (xem
``prune_retention``): user_calls/provider_calls (thống kê), zalo_outbox đã gửi,
reminders đã gửi. Xoá theo lô nhỏ để không khoá bảng lâu; số ngày giữ chỉnh qua
biến môi trường, đặt 0 để tắt từng loại.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core import database as db
from core.envutil import env_int, env_on

logger = logging.getLogger(__name__)
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MIB = 1024 * 1024
TABLE = "facebook_post_media"
_SETTING_KEY = "maintenance:vacuum_full:last_week"
_RETENTION_KEY = "maintenance:retention:last_day"
_CHECK_INTERVAL_SEC = 1800
_VACUUM_TIMEOUT_SEC = 600
# Xoá theo lô: mỗi câu DELETE tối đa _RETENTION_BATCH dòng, mỗi bảng tối đa
# _RETENTION_MAX_BATCHES lô / ngày - phần còn lại để ngày sau (giới hạn thời gian
# giữ khoá và lượng WAL trên Supabase free tier).
_RETENTION_BATCH = 5000
_RETENTION_MAX_BATCHES = 20


@dataclass(frozen=True)
class RetentionRule:
    label: str
    env: str
    default_days: int
    # Câu DELETE theo lô: $1 = interval giữ lại, $2 = kích thước lô.
    sql: str


RETENTION_RULES = (
    RetentionRule(
        "user_calls",
        "RETENTION_USER_CALLS_DAYS",
        180,
        "DELETE FROM user_calls WHERE id IN (SELECT id FROM user_calls "
        "WHERE created_at < now() - $1::interval LIMIT $2)",
    ),
    RetentionRule(
        "provider_calls",
        "RETENTION_PROVIDER_CALLS_DAYS",
        180,
        "DELETE FROM provider_calls WHERE id IN (SELECT id FROM provider_calls "
        "WHERE created_at < now() - $1::interval LIMIT $2)",
    ),
    RetentionRule(
        "zalo_outbox",
        "RETENTION_ZALO_OUTBOX_DAYS",
        30,
        # Chỉ dòng ĐÃ GỬI; tin chưa gửi được không bao giờ bị xoá ở đây.
        "DELETE FROM zalo_outbox WHERE id IN (SELECT id FROM zalo_outbox "
        "WHERE sent_at IS NOT NULL AND sent_at < now() - $1::interval LIMIT $2)",
    ),
    RetentionRule(
        "zalo_outbox_failed",
        "RETENTION_ZALO_OUTBOX_DAYS",
        30,
        # Tin đã bỏ cuộc sau ZALO_OUTBOX_MAX_ATTEMPTS lần (cột failed_at do
        # migration 012 thêm) - giữ cùng số ngày với tin đã gửi để tra cứu.
        "DELETE FROM zalo_outbox WHERE id IN (SELECT id FROM zalo_outbox "
        "WHERE failed_at IS NOT NULL AND failed_at < now() - $1::interval LIMIT $2)",
    ),
    RetentionRule(
        "reminders",
        "RETENTION_REMINDERS_DAYS",
        90,
        "DELETE FROM reminders WHERE id IN (SELECT id FROM reminders "
        "WHERE sent = true AND due_at < now() - $1::interval LIMIT $2)",
    ),
)

_task: asyncio.Task | None = None
_notify: Callable[[str], Awaitable[None]] | None = None


def enabled() -> bool:
    return env_on("DB_AUTO_VACUUM_FULL")


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
    min_waste = env_int("DB_VACUUM_MIN_WASTE_MB", 50, 1, 10_000) * MIB
    limit = env_int("DB_SIZE_LIMIT_MB", 500, 50, 100_000) * MIB
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
    weekday = env_int("DB_VACUUM_WEEKDAY", 6, 0, 6)  # 0 = Thứ 2 ... 6 = Chủ nhật
    hour = env_int("DB_VACUUM_HOUR", 3, 0, 23)
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


def _parse_count(status: str) -> int:
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError, IndexError):
        return 0


async def prune_retention() -> dict[str, int]:
    """Xoá dữ liệu quá hạn giữ của từng bảng (theo lô có trần); trả số dòng đã xoá."""
    pool = await db.get_pool()
    deleted: dict[str, int] = {}
    for rule in RETENTION_RULES:
        days = env_int(rule.env, rule.default_days, 0, 3650)
        if days <= 0:
            continue
        total = 0
        try:
            for _ in range(_RETENTION_MAX_BATCHES):
                count = _parse_count(
                    await pool.execute(rule.sql, timedelta(days=days), _RETENTION_BATCH)
                )
                total += count
                if count < _RETENTION_BATCH:
                    break
        except Exception:
            # 1 bảng lỗi (vd chưa có index/khóa) không chặn các bảng còn lại.
            logger.warning(
                "Dọn dữ liệu cũ bảng %s lỗi; thử lại ngày sau.", rule.label, exc_info=True
            )
        deleted[rule.label] = total
    if any(deleted.values()):
        logger.info("Đã dọn dữ liệu cũ: %s", deleted)
    return deleted


async def run_retention_if_due(now: datetime | None = None) -> dict[str, int] | None:
    """Tối đa 1 lần/ngày (theo giờ VN); None nếu hôm nay đã chạy."""
    now = now or datetime.now(VN_TZ)
    day = now.date().isoformat()
    if await db.get_setting(_RETENTION_KEY) == day:
        return None
    result = await prune_retention()
    await db.set_setting(_RETENTION_KEY, day)
    return result


async def _loop() -> None:
    while True:
        try:
            await run_retention_if_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Job dọn dữ liệu cũ lỗi; thử lại lượt sau.")
        try:
            await run_if_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Job dọn DB lỗi; thử lại lượt sau.")
        await asyncio.sleep(_CHECK_INTERVAL_SEC)


def start() -> None:
    """Luôn chạy vòng bảo trì (dọn dữ liệu cũ hằng ngày); VACUUM FULL vẫn tôn
    trọng DB_AUTO_VACUUM_FULL=0 bên trong run_if_due()."""
    global _task
    if not enabled():
        logger.info("DB_AUTO_VACUUM_FULL=0: tắt job tự VACUUM FULL (vẫn dọn dữ liệu cũ).")
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
