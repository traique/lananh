"""Hoàn tất xoay khoá SETTINGS_ENC_KEY: mã hoá lại các giá trị ``enc:`` còn dùng
khoá cũ để SETTINGS_ENC_KEY_OLD bỏ được thật.

core.crypto dùng MultiFernet: khoá MỚI ở SETTINGS_ENC_KEY, khoá CŨ ở
SETTINGS_ENC_KEY_OLD thì giá trị cũ vẫn đọc được và giá trị ghi mới dùng khoá
mới - nhưng giá trị ít khi được ghi lại (API key override) cứ nằm ở khoá cũ mãi,
nên khoá cũ không bao giờ bỏ được. Module này quét các dòng ``enc:`` của bảng
settings (vài dòng, có trần), ``crypto.rotate_value`` từng giá trị còn ở khoá cũ
(plaintext không rời core.crypto) rồi ghi lại bằng compare-and-swap: gateway Zalo
có thể lưu lại session đúng lúc đó - giá trị mới hơn (đã mã hoá bằng khoá mới)
thắng, không bị ghi đè. core/crypto giữ nguyên là hàm thuần.

Lúc khởi động (monitor_service) chạy khi có SETTINGS_ENC_KEY_OLD: tự xoay nếu
SETTINGS_AUTO_ROTATE bật (mặc định), rồi báo chủ bot số giá trị còn ở khoá cũ /
không giải mã được - chỉ báo khi kết quả đổi so với lần trước (Render restart
thường xuyên). /admin có nút "Xoay khoá ngay".
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from core import alerts, config, crypto
from core import database as db
from core.envutil import env_on

logger = logging.getLogger(__name__)

_SCAN_LIMIT = 500
_KEYS_SHOWN = 20
_REPORT_KEY = "key_rotation:last_report"


@dataclass(frozen=True)
class RotationReport:
    total: int  # số giá trị đã mã hoá ("enc:")
    rotated: int  # đã mã hoá lại bằng khoá mới ở lượt này
    conflicts: int  # bị ghi đè đồng thời (giá trị mới đã dùng khoá mới)
    failed: int  # rotate_value lỗi
    remaining: int  # còn dùng khoá cũ sau lượt này
    unreadable: int  # không giải mã được bằng khoá nào (sai khoá/hỏng)
    keys_old: tuple[str, ...]  # tên key còn ở khoá cũ (tên không phải secret)
    keys_unreadable: tuple[str, ...]


def old_keys_configured() -> bool:
    return bool((config.SETTINGS_ENC_KEY_OLD or "").strip())


def auto_rotate_enabled() -> bool:
    return env_on("SETTINGS_AUTO_ROTATE")


def _readable(value: str) -> bool:
    try:
        crypto.decrypt(value)  # plaintext bỏ ngay, chỉ cần biết giải mã được không
    except RuntimeError:
        return False
    return True


def _classify(rows: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    old = [key for key, value in rows if crypto.needs_rotation(value)]
    old_set = set(old)
    unreadable = [key for key, value in rows if key not in old_set and not _readable(value)]
    return old, unreadable


def _report(rows, *, rotated=0, conflicts=0, failed=0) -> RotationReport:
    old, unreadable = _classify(rows)
    return RotationReport(
        total=len(rows),
        rotated=rotated,
        conflicts=conflicts,
        failed=failed,
        remaining=len(old),
        unreadable=len(unreadable),
        keys_old=tuple(old[:_KEYS_SHOWN]),
        keys_unreadable=tuple(unreadable[:_KEYS_SHOWN]),
    )


async def scan() -> RotationReport:
    """Đếm, không ghi gì."""
    return _report(await db.list_encrypted_settings(_SCAN_LIMIT))


async def rotate_all() -> RotationReport:
    """Mã hoá lại mọi giá trị còn ở khoá cũ (compare-and-swap từng dòng)."""
    rotated = conflicts = failed = 0
    for key, value in await db.list_encrypted_settings(_SCAN_LIMIT):
        if not crypto.needs_rotation(value):
            continue
        try:
            new_value = crypto.rotate_value(value)
        except Exception:
            failed += 1
            logger.warning("Không xoay được khoá cho settings %s.", key, exc_info=True)
            continue
        if await db.compare_and_set_setting(key, value, new_value):
            rotated += 1
        else:
            conflicts += 1
    report = _report(
        await db.list_encrypted_settings(_SCAN_LIMIT),
        rotated=rotated,
        conflicts=conflicts,
        failed=failed,
    )
    logger.info(
        "Xoay khoá settings: đã xoay %s, ghi đồng thời %s, lỗi %s, còn khoá cũ %s, "
        "không giải mã được %s.",
        rotated,
        conflicts,
        failed,
        report.remaining,
        report.unreadable,
    )
    return report


async def status() -> dict:
    """Ảnh chụp cho /admin."""
    report = await scan()
    return {
        "old_keys_configured": old_keys_configured(),
        "auto_rotate": auto_rotate_enabled(),
        **asdict(report),
    }


def report_text(report: RotationReport, *, auto: bool) -> str:
    lines = []
    if report.rotated:
        lines.append(
            f"🔑 Em đã mã hoá lại {report.rotated} giá trị settings bằng SETTINGS_ENC_KEY mới."
        )
    if report.remaining:
        hint = (
            "bấm 'Xoay khoá ngay' ở /admin → Hệ thống"
            if not auto
            else "lần khởi động sau em thử lại"
        )
        lines.append(
            f"🔑 Còn {report.remaining} giá trị dùng khoá cũ ({', '.join(report.keys_old)}) - {hint}."
        )
    if report.unreadable:
        lines.append(
            f"⚠️ {report.unreadable} giá trị không giải mã được bằng khoá nào "
            f"({', '.join(report.keys_unreadable)}) - sai khoá hoặc hỏng, anh nhập lại qua /admin."
        )
    if not report.remaining and not report.unreadable:
        lines.append(
            "✅ Mọi giá trị đã dùng khoá mới - anh xoá SETTINGS_ENC_KEY_OLD trên Render được rồi."
        )
    return "\n".join(lines)


async def on_startup() -> RotationReport | None:
    """Chạy 1 lần lúc khởi động khi có SETTINGS_ENC_KEY_OLD."""
    if not old_keys_configured():
        return None
    auto = auto_rotate_enabled()
    report = await (rotate_all() if auto else scan())
    signature = f"{report.remaining}|{report.unreadable}|{','.join(report.keys_old)}"
    if report.rotated or await db.get_setting(_REPORT_KEY) != signature:
        await db.set_setting(_REPORT_KEY, signature)
        alerts.send(report_text(report, auto=auto))
    return report
