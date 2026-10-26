"""Trạng thái sao lưu DB (workflow .github/workflows/backup.yml) và cảnh báo khi
bản sao lưu bị dừng.

Supabase free không có bản sao lưu tải về được; workflow GitHub Actions dump DB
mỗi đêm, mã hoá rồi lưu artifact, sau đó ghi 1 dòng ``backup_runs`` (nhịp tim).
GitHub tự TẮT lịch chạy của repo public sau 60 ngày không có commit, nên backup
có thể ngừng mà không ai hay: job bảo trì hằng ngày (services/db_maintenance.py)
gọi ``check_stale_if_due`` - bản sao lưu mới nhất cũ hơn BACKUP_STALE_DAYS ngày
thì báo chủ bot (1 lần/ngày). Bảng còn trống = chủ bot chưa thiết lập backup,
không báo. Chạy hoàn toàn trên GitHub, không tốn RAM/đĩa trên Render.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from core import alerts
from core import database as db
from core.envutil import env_int

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MIB = 1024 * 1024
_CHECK_KEY = "maintenance:backup_check:last_day"
# Diễn tập khôi phục chạy hằng tháng: quá ngần này ngày thì trang admin tô đỏ.
DRILL_STALE_DAYS = 40


def stale_days() -> int:
    """Số ngày tối đa giữa 2 bản sao lưu trước khi báo; 0 = tắt kiểm tra."""
    return env_int("BACKUP_STALE_DAYS", 3, 0, 60)


def _age_days(row: dict | None, now: datetime) -> float | None:
    if not row or not isinstance(row.get("finished_at"), datetime):
        return None
    return (now - row["finished_at"]).total_seconds() / 86400


def _iso(row: dict | None) -> str | None:
    if not row or not isinstance(row.get("finished_at"), datetime):
        return None
    return row["finished_at"].astimezone(VN_TZ).isoformat(timespec="minutes")


async def status(now: datetime | None = None) -> dict:
    """Ảnh chụp cho /admin/api/watchdog."""
    now = now or datetime.now(VN_TZ)
    backup = await db.latest_backup_run("backup")
    drill = await db.latest_backup_run("drill")
    backup_age, drill_age = _age_days(backup, now), _age_days(drill, now)
    size = backup.get("size_bytes") if backup else None
    limit = stale_days()
    return {
        "configured": backup is not None,
        "last_backup_at": _iso(backup),
        "backup_age_days": round(backup_age, 1) if backup_age is not None else None,
        "size_mib": round(size / MIB, 2) if isinstance(size, int) else None,
        "stale": bool(limit and backup_age is not None and backup_age > limit),
        "stale_days": limit,
        "last_drill_at": _iso(drill),
        "drill_stale": drill_age is not None and drill_age > DRILL_STALE_DAYS,
    }


def stale_text(age_days: float, finished_at: datetime) -> str:
    when = finished_at.astimezone(VN_TZ).strftime("%H:%M %d/%m")
    return (
        f"💾 Bản sao lưu DB gần nhất đã {age_days:.0f} ngày trước ({when}). Anh mở GitHub → "
        "Actions → workflow 'Backup DB' kiểm tra giúp em: repo public không có commit "
        "60 ngày thì GitHub tự tắt lịch chạy (bấm Enable workflow), hoặc lượt chạy gần "
        "đây bị lỗi (secret hết hạn, đổi mật khẩu role lananh_backup...)."
    )


async def check_stale_if_due(now: datetime | None = None) -> str | None:
    """Tối đa 1 lần/ngày: báo nếu bản sao lưu mới nhất quá BACKUP_STALE_DAYS ngày.
    Trả nội dung cảnh báo nếu đã báo, None nếu không có gì (hoặc chưa tới lượt)."""
    limit = stale_days()
    if not limit:
        return None
    now = now or datetime.now(VN_TZ)
    day = now.date().isoformat()
    if await db.get_setting(_CHECK_KEY) == day:
        return None
    latest = await db.latest_backup_run("backup")
    await db.set_setting(_CHECK_KEY, day)
    age = _age_days(latest, now)
    if latest is None or age is None or age <= limit:
        return None
    text = stale_text(age, latest["finished_at"])
    logger.warning("Bản sao lưu DB gần nhất đã %.1f ngày trước.", age)
    alerts.send(text, key="backup_stale")
    return text
