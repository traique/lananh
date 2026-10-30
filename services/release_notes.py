"""Phiên bản bot cho người dùng: lệnh /phienban (lịch sử cập nhật) và tin "đã lên
bản mới" gửi chủ bot sau mỗi lần deploy. Số phiên bản và nội dung lấy từ
CHANGELOG.md (core/version.py).

Báo bản mới (``announce_if_new``): monitor_service gọi ở vòng giám sát đầu tiên sau
khi khởi động (MONITOR_INTERVAL_SEC, mặc định 5 phút) - lúc đó Render đã chuyển hẳn
sang bản mới; bản deploy hỏng bị huỷ trước đó thì không báo nhầm.

- Chỉ trên Render (RENDER=true): chạy local trỏ DB production không được báo hay ghi
  đè mốc của bản đang chạy thật.
- So mốc "phiên bản@commit" với settings ``release:announced``: khác thì báo. Restart
  thường, Render ngủ dậy, sửa env (cùng commit) không báo lại; deploy commit mới mà
  CHANGELOG chưa thêm mục vẫn báo (ghi rõ chưa có mục mới).
- Gửi Telegram, cộng Zoom khi Zoom bật và đã pair, qua notify_hub nguồn "release"
  (URGENT): ngoài giờ yên lặng gửi ngay, trong giờ yên lặng giữ tới khi hết. Chỉ ghi
  mốc khi ít nhất 1 kênh đã nhận; mỗi process gửi tối đa 1 lần (gửi hỏng thì lần khởi
  động sau thử lại; chưa đọc được DB thì vòng giám sát sau thử lại), không gửi dồn.
- RELEASE_NOTIFY=0 tắt.
"""

from __future__ import annotations

import logging
import os

from core import config, version
from core import database as db
from core.envutil import env_on
from services import notify_hub

logger = logging.getLogger(__name__)

ANNOUNCED_KEY = "release:announced"
SOURCE = "release"
HISTORY_DEFAULT = 3
HISTORY_MAX = 10
_TEXT_MAX = 3500
_ALL_WORDS = {"all", "het", "hết", "tatca", "tất cả"}

_attempted = False


def enabled() -> bool:
    return env_on("RELEASE_NOTIFY")


def _on_render() -> bool:
    return os.getenv("RENDER", "").strip().lower() == "true"


def _fmt_date(release: version.Release) -> str:
    return release.released.strftime("%d/%m/%Y")


def _release_block(release: version.Release) -> list[str]:
    return [f"{release.version} — {_fmt_date(release)}", *(f"• {note}" for note in release.notes)]


def _history_count(argument: str) -> int:
    arg = (argument or "").strip().lower()
    if arg in _ALL_WORDS:
        return HISTORY_MAX
    if arg.isdigit():
        return max(1, min(HISTORY_MAX, int(arg)))
    return HISTORY_DEFAULT


def history_text(argument: str = "") -> str:
    """Nội dung /phienban [số bản|all]: bản đang chạy + vài bản gần nhất."""
    items = version.releases()
    if not items:
        return f"🏷️ Lan Anh bản {version.label()} - chưa đọc được lịch sử phiên bản (CHANGELOG.md)."
    count = _history_count(argument)
    lines = [f"🏷️ Lan Anh đang chạy bản {version.label()}, phát hành {_fmt_date(items[0])}."]
    shown = 0
    for release in items[:count]:
        block = ["", *_release_block(release)]
        if shown and len("\n".join(lines + block)) > _TEXT_MAX:
            break
        lines += block
        shown += 1
    if len(items) > shown:
        # "/phienban all" chỉ giúp được khi lần này dừng vì số bản xin, không phải vì
        # đã chạm HISTORY_MAX hay trần độ dài tin.
        more = (
            "gõ /phienban all để xem thêm"
            if shown == count < HISTORY_MAX
            else "lịch sử đầy đủ ở CHANGELOG.md trong repo"
        )
        lines += ["", f"Còn {len(items) - shown} bản cũ hơn - {more}."]
    return "\n".join(lines)


def _previous_version(marker: str | None) -> str:
    return (marker or "").partition("@")[0]


def _version_key(text: str) -> tuple[int, ...] | None:
    parts = text.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def announcement_text(previous_marker: str | None) -> str:
    """Tin báo chủ bot sau deploy; ``previous_marker`` là mốc đã báo lần trước."""
    release = version.current()
    label = version.label()
    if release is None:
        return f"🆕 Em vừa được deploy bản mới ({label}) - chưa đọc được lịch sử phiên bản."
    previous = _previous_version(previous_marker)
    if previous == release.version:
        return (
            f"🔄 Em vừa được deploy lại bản {label} - chưa có mục mới trong lịch sử phiên "
            "bản (sửa nhỏ hoặc chạy lại). Xem lịch sử: /phienban"
        )
    since = f" (trước đó {previous})" if previous and previous != version.UNKNOWN else ""
    older, newer = _version_key(release.version), _version_key(previous)
    if older is not None and newer is not None and older < newer:
        # Rollback (Render "Rollback" hoặc revert): không gọi là "bản mới".
        return f"↩️ Em vừa quay về bản {label}{since}. Xem lịch sử: /phienban"
    lines = [f"🆕 Em đã lên bản mới {label}{since}.", "", "Có gì mới:"]
    lines += [f"• {note}" for note in release.notes]
    lines += ["", "Xem lịch sử cập nhật: /phienban"]
    return "\n".join(lines)[:_TEXT_MAX]


async def _channels() -> list[str]:
    """Telegram luôn; Zoom khi đã bật và pair (chưa pair mà vẫn gửi "zoom" thì hub
    quay về Telegram - chủ bot nhận 2 tin trùng)."""
    channels = ["telegram"]
    if config.ZOOM_ENABLED:
        try:
            if await db.zoom_get_pairing():
                channels.append("zoom")
        except Exception:
            logger.warning("Không đọc được pairing Zoom - chỉ báo bản mới qua Telegram.")
    return channels


async def announce_if_new() -> bool:
    """Báo bản mới nếu mốc phiên bản@commit khác lần đã báo. True = đã giao/xếp hàng."""
    global _attempted
    if _attempted or not enabled() or not _on_render() or not config.ALLOWED_USER_ID:
        return False
    marker = f"{version.version()}@{version.commit()}"
    # DB lỗi ở đây (pool chưa sẵn ngay sau deploy) thì raise ra monitor_service, vòng
    # sau thử lại; từ lúc đọc được mốc trở đi mỗi process chỉ gửi 1 lần.
    previous = await db.get_setting(ANNOUNCED_KEY)
    _attempted = True
    if previous == marker:
        return False
    text = announcement_text(previous)
    # Khoá chống trùng của hàng đợi giữ cả tin đã gửi: gắn mốc trước để quay lại 1
    # bản cũ (A -> B -> A) vẫn là 1 tin mới.
    dedup = f"{SOURCE}:{previous or '-'}>{marker}"
    delivered = False
    for channel in await _channels():
        sent = await notify_hub.send(
            channel, config.ALLOWED_USER_ID, text, source=SOURCE, dedup=dedup
        )
        delivered = delivered or sent
    if not delivered:
        logger.warning("Chưa báo được bản mới %s cho chủ bot - lần khởi động sau thử lại.", marker)
        return False
    await db.set_setting(ANNOUNCED_KEY, marker)
    logger.info("Đã báo chủ bot bản mới %s.", marker)
    return True
