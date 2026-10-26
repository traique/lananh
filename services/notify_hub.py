"""Cửa chung cho tin Lan Anh TỰ nhắn: việc cần người quyết báo ngay, việc đã xong/
thông tin được gom lại báo một lần (ý từ ethanplusai/jarvis), có giờ yên lặng.

Mức của từng nguồn (``SOURCES``; nguồn gọi ``offer`` với tên của mình):

=====================  ======  =================================================
Nguồn                  Mức     Ghi chú
=====================  ======  =================================================
reminder               URGENT  Nhắc việc/ngày nhớ người dùng tự đặt giờ: scheduler
                               gửi thẳng như cũ, KỂ CẢ trong giờ yên lặng (không
                               qua hàng đợi - giữ nút hoãn, outbox reminder_id).
holding_alert          URGENT  Stop/target danh mục chạm trong phiên (price_alerts).
price_alert            URGENT  Giá chạm ngưỡng người dùng đặt (/canhbaogia).
portfolio_close        cả hai  Cảnh báo cuối phiên: URGENT khi có dòng stop/target,
                               NORMAL khi chỉ có breakout/lỗ sâu.
facebook_review        URGENT  Bài Facebook chờ duyệt (bản xem trước).
system_critical        URGENT  Cảnh báo hệ thống mở đầu 🚨 (core.alerts) - gửi cả
                               trong giờ yên lặng.
system_urgent          URGENT  core.alerts.send(urgent=True): hệ thống hồi phục,
                               kết quả lệnh nền người dùng vừa gọi, việc cần kiểm tra
                               tay.
portfolio_corp         NORMAL  Đã quy đổi vị thế theo sự kiện quyền.
portfolio_event        NORMAL  Báo trước lịch sự kiện quyền.
followup               NORMAL  Hỏi thăm chủ động (proactive_service).
system                 NORMAL  Cảnh báo nhẹ core.alerts (watchdog ⚠️/💽/⏰, lỗi mới 🐞,
                               tổng hợp lỗi, quota ⛽...).
morning_brief          NORMAL  Bản tin sáng; tin TỔNG HỢP hẹn giờ: vào hàng đợi rồi
                               xả NGAY (kèm tin NORMAL đang chờ) nếu ngoài giờ yên
portfolio_digest       NORMAL  lặng. Digest danh mục 8h cũng vậy.
=====================  ======  =================================================

Không qua hub (gửi như cũ): nhắc việc (trên), câu hỏi cấp quyền người Zalo lạ (kèm
nút, cần quyết ngay), bản tin RSS 8h và tổng kết nhóm Zalo 9h (nội dung định kỳ
ngoài giờ yên lặng, có guard riêng), báo cáo VACUUM của db_maintenance (3h sáng CN
nhưng chỉ khi thật sự dọn).

Cách chạy:
- URGENT: ``offer`` trả False -> nguồn gửi ngay theo đường cũ (giữ nguyên guard
  idempotent/at-least-once của nguồn). Trong giờ yên lặng chỉ ``reminder`` và
  ``system_critical`` được gửi; URGENT khác (bài FB chờ duyệt, stop/giá - thực tế
  chỉ xảy ra trong phiên) được GIỮ trong hàng đợi và gửi đầu tiên khi hết giờ yên
  lặng (bài FB giữ lại mất nút bấm nhưng tin vẫn có dòng /fb_ok, /fb_boqua).
- NORMAL: xếp vào bảng ``notify_queue`` (migrations/026, bền qua restart; tối đa
  NOTIFY_QUEUE_MAX_PER_USER tin chờ/người, vượt thì bỏ tin NORMAL cũ nhất và báo số
  tin bị lược). ``offer`` trả True = nguồn coi như đã giao (đánh dấu guard của
  nguồn), hàng đợi lo phần còn lại. ``dedup_key`` chống xếp 2 lần khi nguồn chạy lại.
- Xả (gộp mọi tin chờ của 1 người nhận thành MỘT tin, URGENT bị giữ đứng trước):
  vòng 5 phút xả người có tin cũ nhất đã chờ NOTIFY_BATCH_MINUTES (60), hoặc có tin
  ưu tiên, hoặc có tin xếp trong giờ yên lặng vừa hết; người dùng vừa nhắn bot thì
  xả sau vài giây (kể cả trong giờ yên lặng - họ đang thức); tin tổng hợp xả ngay.
- Gửi: Telegram (chủ bot) qua callback bot_app, Zoom qua pairing, Zalo qua
  zalo_outbox trong CÙNG transaction đánh dấu đã gửi (không bao giờ gửi đôi).
  Telegram/Zoom dùng lease: lỗi gửi thì nhả, vòng sau thử lại; sập đúng lúc giữa gửi
  và đánh dấu thì có thể lặp 1 lần (at-least-once như các nguồn cũ).
- Giờ yên lặng: mặc định NOTIFY_QUIET_HOURS (22:30-06:30 giờ VN), mỗi người chỉnh
  bằng /yenlang on|off|22-7 (chủ bot Telegram/Zoom dùng chung, mỗi Zalo riêng).
- NOTIFY_HUB_ENABLED=0: tắt hẳn, mọi nguồn gửi thẳng như trước. Hub chỉ nhận tin
  khi vòng xả đã chạy (``start``) - test/script không khởi động thì như tắt.
Không gọi AI ở đây.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from core import address, alerts, config, trace
from core import database as db
from core.envutil import env_int, env_on
from core.text_normalize import fold
from core.text_utils import parse_on_off
from services import personal_settings

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
URGENT, NORMAL = "urgent", "normal"
CHANNELS = ("telegram", "zalo", "zoom")
CRITICAL_PREFIX = "🚨"
_TICK_SEC = 300
_LEASE_SEC = 300
_BATCH_ROWS = 20
_BATCH_CHARS = 8000
_MAX_ROUNDS = 5
_ACTIVITY_DELAY_SEC = 8.0
_MAX_CONTENT_CHARS = 12000
_HINT_MAX = 500
_SEPARATOR = "\n\n- - -\n"


@dataclass(frozen=True)
class SourceSpec:
    label: str
    level: str
    quiet_exempt: bool = False  # URGENT vẫn gửi trong giờ yên lặng
    digest: bool = False  # tin tổng hợp hẹn giờ: xả ngay khi ngoài giờ yên lặng


SOURCES: dict[str, SourceSpec] = {
    "reminder": SourceSpec("Nhắc việc", URGENT, quiet_exempt=True),
    "holding_alert": SourceSpec("Stop/target danh mục", URGENT),
    "price_alert": SourceSpec("Giá chạm ngưỡng đã đặt", URGENT),
    "portfolio_close": SourceSpec("Cảnh báo danh mục cuối phiên", NORMAL),
    "facebook_review": SourceSpec("Bài Facebook chờ duyệt", URGENT),
    "system_critical": SourceSpec("Cảnh báo hệ thống nghiêm trọng", URGENT, quiet_exempt=True),
    "system_urgent": SourceSpec("Hệ thống cần xem", URGENT),
    "portfolio_corp": SourceSpec("Quy đổi sự kiện quyền", NORMAL),
    "portfolio_event": SourceSpec("Lịch sự kiện quyền", NORMAL),
    "followup": SourceSpec("Hỏi thăm", NORMAL),
    "system": SourceSpec("Cảnh báo hệ thống", NORMAL),
    "morning_brief": SourceSpec("Bản tin sáng", NORMAL, digest=True),
    "portfolio_digest": SourceSpec("Digest danh mục", NORMAL, digest=True),
}
_DIGEST_SOURCES = [name for name, spec in SOURCES.items() if spec.digest]

_telegram_sender: Callable[[int, str], Awaitable[None]] | None = None
_task: asyncio.Task | None = None
_started = False
_flush_lock = asyncio.Lock()
# (kênh, user_id) có thể đang có tin chờ - để tin nhắn thường của người dùng không
# tốn 1 câu SQL mỗi lần. Nạp lại mỗi vòng xả; có trần.
_pending_hint: set[tuple[str, int]] = set()
_activity_tasks: dict[int, asyncio.Task] = {}


def _now() -> dt.datetime:
    return dt.datetime.now(VN_TZ)


def set_telegram_sender(fn: Callable[[int, str], Awaitable[None]] | None) -> None:
    """Hàm gửi tin Telegram chủ động (bot_app đăng ký, như scheduler)."""
    global _telegram_sender
    _telegram_sender = fn


def enabled() -> bool:
    return env_on("NOTIFY_HUB_ENABLED")


def active() -> bool:
    """Hub đang nhận tin: bật trong env VÀ vòng xả đã chạy trong process này."""
    return _started and enabled()


def batch_minutes() -> int:
    return env_int("NOTIFY_BATCH_MINUTES", 60, 5, 24 * 60)


def queue_cap() -> int:
    return env_int("NOTIFY_QUEUE_MAX_PER_USER", 30, 5, 200)


# ─── Giờ yên lặng ────────────────────────────────────────────────────────────

Window = tuple[dt.time, dt.time]
_WINDOW_RE = re.compile(
    r"^\s*(\d{1,2})(?:[:h.](\d{2}))?\s*h?\s*(?:-|–|den|toi|->)\s*(\d{1,2})(?:[:h.](\d{2}))?\s*h?\s*$"
)


def parse_window(raw: str | None) -> Window | None:
    """ "22:30-06:30", "22-7", "22h-6h30" -> (bắt đầu, kết thúc); sai cú pháp -> None."""
    match = _WINDOW_RE.match(fold(raw or ""))
    if not match:
        return None
    h1, m1, h2, m2 = (int(g) if g else 0 for g in match.groups())
    if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
        return None
    start, end = dt.time(h1, m1), dt.time(0 if h2 == 24 else h2, m2)
    return None if start == end else (start, end)


def format_window(window: Window) -> str:
    return f"{window[0]:%H:%M}-{window[1]:%H:%M}"


def default_window() -> Window | None:
    raw = os.getenv("NOTIFY_QUIET_HOURS", "22:30-06:30").strip()
    if fold(raw) in {"", "0", "off", "tat"}:
        return None
    return parse_window(raw) or (dt.time(22, 30), dt.time(6, 30))


def in_window(moment: dt.time, window: Window) -> bool:
    start, end = window
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end  # qua nửa đêm


async def quiet_window(user_id: int) -> Window | None:
    """Giờ yên lặng của 1 người: cài riêng (/yenlang) hoặc mặc định env."""
    try:
        raw = (await db.get_setting(personal_settings.quiet_key(user_id)) or "").strip()
    except Exception:
        logger.warning("notify_hub: không đọc được giờ yên lặng, dùng mặc định.", exc_info=True)
        raw = ""
    if raw == "off":
        return None
    if raw and raw != "on":
        custom = parse_window(raw)
        if custom:
            return custom
    return default_window() or ((dt.time(22, 30), dt.time(6, 30)) if raw == "on" else None)


async def is_quiet(user_id: int, now: dt.datetime | None = None) -> bool:
    window = await quiet_window(user_id)
    if window is None:
        return False
    now = (now or _now()).astimezone(VN_TZ)
    return in_window(now.time(), window)


# ─── Nhận tin từ các nguồn ───────────────────────────────────────────────────


def _recipient_ok(channel: str, user_id: int) -> bool:
    if channel in ("telegram", "zoom"):
        return bool(user_id) and config.is_owner_user_id(user_id)
    return channel == "zalo" and user_id < 0


def _remember(channel: str, user_id: int) -> None:
    if len(_pending_hint) >= _HINT_MAX:
        _pending_hint.clear()  # vòng xả kế tiếp nạp lại từ DB
    _pending_hint.add((channel, user_id))


async def offer(
    channel: str,
    user_id: int,
    text: str,
    *,
    source: str,
    level: str | None = None,
    dedup: str | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """True = hub đã nhận tin (xếp hàng bền / đã xả), nguồn coi như đã giao và đánh
    dấu guard của mình. False = nguồn tự gửi ngay theo đường cũ (URGENT ngoài giờ
    yên lặng, hub tắt, người nhận lạ, hoặc DB lỗi - fail open). Không raise."""
    if not active() or not text or not _recipient_ok(channel, user_id):
        return False
    spec = SOURCES.get(source) or SourceSpec(source, NORMAL)
    level = level or spec.level
    try:
        quiet = await is_quiet(user_id, now)
        if level == URGENT and (not quiet or spec.quiet_exempt):
            return False
        new_id, dropped = await db.notify_enqueue(
            channel, user_id, level, source, text[:_MAX_CONTENT_CHARS], dedup, queue_cap()
        )
    except Exception:
        logger.warning("notify_hub: không xếp được tin %s, gửi thẳng.", source, exc_info=True)
        return False
    _remember(channel, user_id)
    if dropped:
        logger.info("notify_hub: %s:%s vượt trần, bỏ %d tin cũ.", channel, user_id, dropped)
    if new_id is None:
        logger.info("notify_hub: tin %s (%s) đã có trong hàng đợi, bỏ bản trùng.", source, dedup)
    elif spec.digest and not quiet:
        try:
            await flush_recipient(channel, user_id)
        except Exception:
            logger.warning(
                "notify_hub: xả ngay tin %s lỗi, vòng sau thử lại.", source, exc_info=True
            )
    return True


async def route_owner_alert(text: str, urgent: bool | None) -> None:
    """Router của core.alerts: 🚨 (trừ urgent=False) và urgent=True gửi ngay, còn lại gom."""
    if urgent is not False and text.lstrip().startswith(CRITICAL_PREFIX):
        source = "system_critical"
    elif urgent:
        source = "system_urgent"
    else:
        source = "system"
    owner = config.ALLOWED_USER_ID
    if owner and await offer("telegram", owner, text, source=source):
        return
    await alerts.deliver_now(text)


# ─── Gộp + xả ────────────────────────────────────────────────────────────────


def compose(items: list[dict], dropped: int = 0, *, you: str = "anh") -> str | None:
    """1 tin gộp từ các tin chờ (đã sắp: URGENT bị giữ trước, rồi theo thứ tự xếp)."""
    texts = [str(item.get("content") or "").strip() for item in items]
    texts = [t for t in texts if t]
    note = f"(Có {dropped} thông báo cũ hơn đã được lược bớt vì hàng chờ đầy.)" if dropped else ""
    if not texts:
        return note or None
    if len(texts) == 1 and not note:
        return texts[0]
    me = address.self_word(you)
    first = min(
        (item["created_at"] for item in items if isinstance(item.get("created_at"), dt.datetime)),
        default=None,
    )
    since = f" từ {first.astimezone(VN_TZ):%H:%M}" if first else ""
    held = sum(1 for item in items if item.get("level") == URGENT)
    head = f"🔔 {me.capitalize()} gom {len(texts)} thông báo{since} cho {you} nè:"
    if held:
        head += f" ({held} tin cần {you} xem đứng đầu, giữ lại trong giờ yên lặng)"
    parts = [head, _SEPARATOR.join(texts)]
    if note:
        parts.append(note)
    return "\n\n".join(parts)


def _fit(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(phần gửi lượt này, phần trả lại) theo trần ký tự; luôn gửi ít nhất 1 tin."""
    taken: list[dict] = []
    size = 0
    for index, row in enumerate(rows):
        length = len(str(row.get("content") or ""))
        if taken and size + length > _BATCH_CHARS:
            return taken, rows[index:]
        taken.append(row)
        size += length
    return taken, []


async def _send_lease_channel(channel: str, user_id: int, text: str) -> bool:
    """Gửi Telegram/Zoom; False = chưa gửi được (giữ trong hàng đợi)."""
    if channel == "telegram":
        if _telegram_sender is None:
            return False
        await _telegram_sender(user_id, text)
        return True
    from channels import zoom

    pairing = await db.zoom_get_pairing()
    if not pairing:
        await db.notify_drop_pending("zoom", user_id)
        return False
    await zoom.send_message(to_jid=pairing[0], text=text)
    return True


async def _flush_lease(channel: str, user_id: int) -> int:
    if channel == "zoom" and not config.ZOOM_ENABLED:
        return 0
    if channel == "telegram" and _telegram_sender is None:
        return 0
    sent = 0
    for _ in range(_MAX_ROUNDS):
        rows = await db.notify_claim(channel, user_id, _BATCH_ROWS, _LEASE_SEC)
        if not rows:
            break
        batch, rest = _fit(rows)
        if rest:
            await db.notify_release([r["id"] for r in rest])
        ids = [r["id"] for r in batch]
        try:
            dropped = await db.notify_take_dropped(channel, user_id)
            text = compose(batch, dropped)
            ok = await _send_lease_channel(channel, user_id, text) if text else True
        except Exception:
            logger.warning("notify_hub: gửi tin gộp %s lỗi, giữ lại.", channel, exc_info=True)
            await db.notify_release(ids)
            break
        if not ok:
            await db.notify_release(ids)
            break
        await db.notify_mark_sent(ids)
        sent += len(batch)
        if not rest and len(rows) < _BATCH_ROWS:
            break
    return sent


def _zalo_enabled() -> bool:
    on = os.getenv("ZALO_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    return on and bool(os.getenv("ZALO_BRIDGE_SECRET", "").strip())


async def _zalo_user(user_id: int):
    from channels import zalo_users

    for user in await zalo_users.list_users():
        if user.internal_user_id == user_id:
            return user
    return None


async def _flush_zalo(user_id: int) -> int:
    if not _zalo_enabled():
        return 0
    from channels import zalo_repository
    from channels.zalo_text import to_plain_text

    user = await _zalo_user(user_id)
    if user is None or not user.is_active:
        await db.notify_drop_pending("zalo", user_id)  # đã gỡ pairing / bị khoá
        return 0
    you = address.normalize(getattr(user, "address_form", None)) or "anh"
    account_id = await zalo_repository.resolve_active_account_id()
    await zalo_repository.ensure_schema()

    def build(rows: list[dict], dropped: int) -> str | None:
        text = compose(rows, dropped, you=you)
        return to_plain_text(text) if text else None

    sent = 0
    for _ in range(_MAX_ROUNDS):
        count = await db.notify_flush_zalo(
            user_id, _BATCH_ROWS, account_id, user.external_id, build
        )
        sent += count
        if count < _BATCH_ROWS:
            break
    return sent


async def flush_recipient(channel: str, user_id: int) -> int:
    """Xả mọi tin chờ của 1 người nhận (gộp). Trả số tin đã giao."""
    async with _flush_lock:
        if channel == "zalo":
            sent = await _flush_zalo(user_id)
        else:
            sent = await _flush_lease(channel, user_id)
    if sent:
        logger.info("notify_hub: đã xả %d tin cho %s:%s.", sent, channel, user_id)
    return sent


def _as_vn(value) -> dt.datetime | None:
    if not isinstance(value, dt.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(VN_TZ)


async def is_due(row: dict, now: dt.datetime) -> bool:
    """Tới lượt xả người nhận này chưa (``row`` từ notify_pending_recipients)."""
    user_id = int(row["user_id"])
    window = await quiet_window(user_id)
    if window is not None and in_window(now.time(), window):
        return False
    if row.get("has_priority"):
        return True
    oldest = _as_vn(row.get("oldest"))
    if oldest is None:
        return True
    if now - oldest >= dt.timedelta(minutes=batch_minutes()):
        return True
    # Tin xếp trong giờ yên lặng vừa hết: xả ngay lúc hết giờ, không chờ đủ lượt.
    return window is not None and in_window(oldest.time(), window)


async def run_tick(now: dt.datetime | None = None) -> int:
    """1 vòng: xả những người nhận tới lượt. Trả số tin đã giao."""
    if not active():
        return 0
    now = (now or _now()).astimezone(VN_TZ)
    rows = await db.notify_pending_recipients(_DIGEST_SOURCES)
    _pending_hint.clear()
    sent = 0
    for row in rows:
        channel, user_id = str(row["channel"]), int(row["user_id"])
        _remember(channel, user_id)
        try:
            if await is_due(row, now):
                sent += await flush_recipient(channel, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("notify_hub: xả %s:%s lỗi.", channel, user_id, exc_info=True)
    return sent


def _user_channels(user_id: int) -> list[str]:
    return ["zalo"] if user_id < 0 else ["telegram", "zoom"]


def note_activity(user_id: int) -> bool:
    """Người dùng vừa nhắn bot: xả tin chờ của họ sau vài giây (sau câu trả lời),
    kể cả trong giờ yên lặng. True nếu đã lên lịch xả."""
    if not active() or not user_id:
        return False
    channels = [ch for ch in _user_channels(user_id) if (ch, user_id) in _pending_hint]
    if not channels or user_id in _activity_tasks:
        return False

    async def run() -> None:
        trace.detach()
        try:
            await asyncio.sleep(_ACTIVITY_DELAY_SEC)
            for channel in channels:
                _pending_hint.discard((channel, user_id))
                await flush_recipient(channel, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("notify_hub: xả khi người dùng nhắn lỗi.", exc_info=True)
        finally:
            _activity_tasks.pop(user_id, None)

    try:
        _activity_tasks[user_id] = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return False
    return True


async def _loop() -> None:
    trace.detach()
    while True:
        try:
            with trace.job("notify_hub"):
                await run_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("notify_hub: vòng xả lỗi.", exc_info=True)
        await asyncio.sleep(_TICK_SEC)


def start() -> None:
    global _task, _started
    if not enabled():
        return
    _started = True
    alerts.set_router(route_owner_alert)
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task, _started
    _started = False
    alerts.set_router(None)
    tasks = [t for t in (_task, *_activity_tasks.values()) if t is not None]
    _task = None
    _activity_tasks.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ─── /yenlang ────────────────────────────────────────────────────────────────


def _status_text(window: Window | None, setting: str, you: str, owner: bool) -> str:
    me = address.self_word(you)
    if window is None:
        state = "TẮT - tin gom vẫn được xả mỗi lượt, kể cả ban đêm"
    else:
        state = f"BẬT {format_window(window)} (giờ VN)"
        if setting not in ("on", "off") and setting:
            state += ", giờ riêng"
    return (
        f"🌙 Giờ yên lặng: {state}.\n"
        f"Trong giờ này {me} chỉ nhắn ngay nhắc việc {you} tự đặt giờ"
        f"{' và cảnh báo hệ thống nghiêm trọng' if owner else ''}; tin khác {me} gom lại, hết "
        "giờ mới gửi 1 tin. Tin không "
        f"gấp ban ngày cũng được gom, tối đa {batch_minutes()} phút/lần, hoặc ngay khi {you} "
        f"nhắn {me}.\n"
        "Đổi: /yenlang on · /yenlang off · /yenlang 22-7 (hoặc 22:30-6:30)"
    )


async def yenlang(user_id: int, argument: str, *, you: str = "anh") -> str:
    """/yenlang [on|off|22-7]: giờ yên lặng của CHÍNH người gõ."""
    if not enabled():
        return "Tính năng gom thông báo đang tắt trên bot (NOTIFY_HUB_ENABLED=0)."
    key = personal_settings.quiet_key(user_id)
    arg = " ".join((argument or "").split())
    toggle = parse_on_off(arg)
    if toggle is not None:
        await db.set_setting(key, "on" if toggle else "off")
    elif arg:
        window = parse_window(arg)
        if window is None:
            return (
                f"Em chưa hiểu khung giờ “{arg}”. {you.capitalize()} gõ kiểu /yenlang 22-7 "
                "hoặc /yenlang 22:30-6:30 nha."
            )
        await db.set_setting(key, format_window(window))
    setting = (await db.get_setting(key) or "").strip()
    owner = config.is_owner_user_id(user_id)
    return _status_text(await quiet_window(user_id), setting, you, owner)
