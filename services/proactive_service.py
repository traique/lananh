"""Lan Anh chủ động hỏi thăm dựa trên trí nhớ ("mai anh đi phỏng vấn" -> tối
mai: "Phỏng vấn hôm nay thế nào anh?").

- Sự kiện: bộ trích nhớ (services/memory_service) trả ``events`` [{title,
  when_text}] - CHỈ cho người được phép (``events_enabled``). when_text phải
  xuất hiện nguyên văn trong lời người dùng (chống AI bịa), ngày tính BẰNG CODE
  (services/calendar_query) theo giờ VN của chính tin nhắn đó, chỉ nhận ngày
  trong 0-60 ngày tới. Lưu bảng user_events (migrations/018, tối đa 50/người).
- Ai nhận: chủ bot qua Telegram (mặc định bật, /hoithan off để tắt); thành
  viên Zalo phải tự bật /hoithan on và nhận trên CHÍNH Zalo của mình. Kênh giao
  suy từ user_id (id âm = Zalo) nên sự kiện nguồn Zalo không bao giờ sang
  Telegram. Bot không bao giờ nhắn trong nhóm Zalo.
- Vòng lặp mỗi PROACTIVE_TICK_SEC (30 phút), chỉ trong giờ hoạt động
  (PROACTIVE_ACTIVE_HOURS, mặc định 08:00-21:30 VN); sự kiện ngày D được hỏi từ
  PROACTIVE_FOLLOWUP_HOUR giờ ngày D, lỡ thì hỏi bù trong ngày D+1, sau đó bỏ.
  Lượt rỗng chỉ chạy 1 câu SQL - không gọi AI.
- Mỗi lần gửi: claim nguyên tử (pending -> asked), bỏ qua nếu người đó vừa chat
  trong PROACTIVE_IDLE_MIN phút hoặc đang có lượt chạy (concurrency.is_busy),
  tối đa PROACTIVE_DAILY_CAP tin/ngày/người (đếm ở settings). 1 lượt AI
  (provider chain, dưới assistant_turn của người đó + hạn chót riêng) viết câu
  hỏi thăm ngắn hoặc trả NO_REPLY. Gửi lỗi thì trả claim về pending. Gửi xong
  lưu vào lịch sử (kind followup) để câu trả lời của người dùng có ngữ cảnh.
- "Đừng nhắc/hỏi nữa" ngay sau tin hỏi thăm: các sự kiện còn chờ -> done và tạm
  dừng PAUSE_DAYS ngày (run_chat_turn nhận diện bằng regex, không cần AI).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

from core import address, config, database as db, trace
from core.envutil import env_int, env_on
from core.text_normalize import fold
from services import memory_guard, personal_settings

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_EVENT_DAYS = 60
MAX_EVENTS_PER_FLUSH = 3
_TITLE_MAX = 120
PAUSE_DAYS = 7
_COMPOSE_TIMEOUT_SEC = 60
_MAX_USERS_PER_TICK = 20
_REPLY_MAX_CHARS = 400
NO_REPLY = "NO_REPLY"
# Dòng "user" trong lịch sử cho tin chủ động (không phải lời người dùng).
FOLLOWUP_USER_LABEL = "[Hệ thống: Lan Anh chủ động nhắn hỏi thăm]"

_notify_callback: Callable[[int, str], Awaitable[None]] | None = None
_task: asyncio.Task | None = None

_OPT_OUT_RE = re.compile(
    r"\b(?:dung|khong can|thoi|khoi) (?:nhac|hoi|hoi tham|nhan)(?: lai| nua| ve| chuyen)?"
    r"(?: (?:nua|chuyen do|chuyen nay|nhe|nha|di))?\b"
)


def set_notify_callback(fn: Callable[[int, str], Awaitable[None]]) -> None:
    """Hàm gửi tin Telegram chủ động (bot_app đăng ký, giống scheduler)."""
    global _notify_callback
    _notify_callback = fn


def enabled() -> bool:
    return env_on("PROACTIVE_FOLLOWUPS")


def _active_hours() -> tuple[dt.time, dt.time]:
    raw = os.getenv("PROACTIVE_ACTIVE_HOURS", "08:00-21:30").strip()
    try:
        start_raw, end_raw = raw.split("-", 1)
        start = dt.time.fromisoformat(start_raw.strip())
        end = dt.time.fromisoformat(end_raw.strip())
        if start < end:
            return start, end
    except ValueError:
        pass
    return dt.time(8, 0), dt.time(21, 30)


def within_active_hours(now: dt.datetime) -> bool:
    start, end = _active_hours()
    return start <= now.time() <= end


def _zalo_enabled() -> bool:
    on = os.getenv("ZALO_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    return on and bool(os.getenv("ZALO_BRIDGE_SECRET", "").strip())


# ─── Bật/tắt theo người ──────────────────────────────────────────────────────


async def _state(user_id: int) -> str:
    return (await db.get_setting(personal_settings.followup_key(user_id)) or "").strip()


async def events_enabled(user_id: int, *, today: dt.date | None = None) -> bool:
    """Có trích + hỏi thăm sự kiện cho người này không: chủ bot mặc định bật,
    thành viên Zalo phải tự /hoithan on; đang tạm dừng ("đừng nhắc nữa") -> tắt."""
    if not enabled():
        return False
    if config.is_owner_user_id(user_id):
        default_on = True
    elif user_id < 0:
        default_on = False
        if not _zalo_enabled():
            return False
    else:
        return False
    state = await _state(user_id)
    if state.startswith("pause:"):
        today = today or dt.datetime.now(_VN_TZ).date()
        try:
            return dt.date.fromisoformat(state[6:]) <= today
        except ValueError:
            return default_on
    if state in ("0", "1"):
        return state == "1"
    return default_on


async def set_followups(user_id: int, on: bool) -> None:
    await db.set_setting(personal_settings.followup_key(user_id), "1" if on else "0")
    if not on:
        await db.finish_user_events(user_id)


async def opt_out(user_id: int, *, today: dt.date | None = None) -> None:
    """ "Đừng nhắc nữa": bỏ các sự kiện đang chờ và tạm dừng PAUSE_DAYS ngày."""
    today = today or dt.datetime.now(_VN_TZ).date()
    await db.finish_user_events(user_id)
    until = today + dt.timedelta(days=PAUSE_DAYS)
    await db.set_setting(personal_settings.followup_key(user_id), f"pause:{until.isoformat()}")


def is_opt_out(text: str) -> bool:
    return bool(_OPT_OUT_RE.search(fold(text or "")))


OPT_OUT_NOTE = (
    "[Hệ thống: người dùng vừa bảo đừng hỏi thăm/nhắc chuyện đó nữa - đã tắt hỏi thăm chủ "
    f"động {PAUSE_DAYS} ngày. Xác nhận ngắn gọn, nhẹ nhàng; không hỏi thêm về chuyện đó.]"
)


# ─── Sự kiện từ bộ trích nhớ ─────────────────────────────────────────────────


def resolve_when(when_text: str, anchor: dt.date) -> dt.date | None:
    """Ngày của cụm thời gian người dùng nói, tính từ ngày của tin nhắn."""
    from services import calendar_query

    folded = fold(when_text)
    if not folded:
        return None
    day = calendar_query.relative_day(when_text, anchor)
    if day is not None:
        return day
    dates = calendar_query.solar_dates(when_text, anchor)
    if dates:
        found = dates[0]
        if found < anchor and not re.search(r"\d{4}", when_text):
            try:
                found = found.replace(year=found.year + 1)
            except ValueError:
                return None
        return found
    if re.search(r"\bmai\b", folded):
        return anchor + dt.timedelta(days=1)
    if re.search(r"\b(?:sang|trua|chieu|toi|dem) nay\b", folded):
        return anchor
    match = re.search(r"\b(\d{1,2}) ngay (?:nua|toi)\b", folded)
    if match:
        return anchor + dt.timedelta(days=int(match.group(1)))
    if re.search(r"\btuan (?:sau|toi)\b", folded):
        return anchor + dt.timedelta(days=7)
    return None


def _anchor_row(when_text: str, user_rows: list[dict]) -> dict | None:
    """Tin nhắn chứa NGUYÊN VĂN cụm thời gian (bỏ dấu khi so) - mới nhất trước."""
    needle = fold(when_text)
    if len(needle) < 2:
        return None
    for row in reversed(user_rows):
        if needle in fold(str(row.get("content") or "")):
            return row
    return None


def _vn_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(_VN_TZ).date()
    return dt.datetime.now(_VN_TZ).date()


async def add_extracted_events(user_id: int, events: list, user_rows: list[dict]) -> int:
    """Kiểm + lưu sự kiện bộ trích xuất trả về. Trả số sự kiện mới."""
    if not events or not await events_enabled(user_id):
        return 0
    today = dt.datetime.now(_VN_TZ).date()
    added = 0
    for item in events[:MAX_EVENTS_PER_FLUSH]:
        if not isinstance(item, dict):
            continue
        title = memory_guard.safe_memory_text(str(item.get("title") or ""))
        when_text = memory_guard.clean(str(item.get("when_text") or ""))
        if not title or not when_text:
            continue
        row = _anchor_row(when_text, user_rows)
        if row is None:
            continue  # cụm thời gian không có trong lời người dùng -> có thể bịa
        anchor = _vn_date(row.get("created_at"))
        day = resolve_when(when_text, anchor)
        if day is None or day < today or day > anchor + dt.timedelta(days=MAX_EVENT_DAYS):
            continue
        if await db.add_user_event(user_id, title[:_TITLE_MAX], when_text[:60], day):
            added += 1
    return added


# ─── Vòng lặp gửi ────────────────────────────────────────────────────────────


def _turn_key(user_id: int) -> str:
    from services.concurrency import OWNER_TURN_KEY

    return OWNER_TURN_KEY if config.is_owner_user_id(user_id) else f"zalo:{user_id}"


async def _recently_active(user_id: int) -> bool:
    rows = await db.get_recent_chat_rows(user_id, 1, 0)
    if not rows:
        return False
    last = rows[0]["created_at"]
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    idle_sec = env_int("PROACTIVE_IDLE_MIN", 20, 0, 24 * 60) * 60
    return (dt.datetime.now(dt.timezone.utc) - last).total_seconds() < idle_sec


async def _sent_today(user_id: int, today: dt.date) -> int:
    raw = await db.get_setting(personal_settings.followup_sent_key(user_id)) or ""
    day, _, count = raw.partition(":")
    if day != today.isoformat():
        return 0
    try:
        return int(count)
    except ValueError:
        return 0


async def _bump_sent(user_id: int, today: dt.date) -> None:
    count = await _sent_today(user_id, today) + 1
    await db.set_setting(
        personal_settings.followup_sent_key(user_id), f"{today.isoformat()}:{count}"
    )


def _compose_prompt(event: dict, now: dt.datetime, form: str) -> str:
    me = address.self_word(form)
    day = event.get("event_date")
    day_text = day.strftime("%d/%m") if isinstance(day, dt.date) else str(day or "")
    return (
        f'Bạn là Lan Anh, trợ lý cá nhân thân thiết - xưng "{me}", gọi người dùng là "{form}".\n'
        f'Trước đó người dùng có kể sắp có việc: "{event["title"]}" (họ nói mốc: '
        f'"{event.get("when_text") or ""}", tức ngày {day_text}). Bây giờ là '
        f"{now:%H:%M} ngày {now:%d/%m}.\n"
        "Viết DUY NHẤT 1 tin nhắn hỏi thăm ngắn (1-2 câu, tự nhiên, ấm áp, không sến, tối đa "
        "1 emoji, không hỏi dồn, không nhắc là đọc từ trí nhớ) như người quen nhắn hỏi việc đó "
        f"diễn ra thế nào. Nếu việc này không hợp để hỏi thăm (quá riêng tư, chuyện buồn, mơ hồ) "
        f"thì trả đúng chữ {NO_REPLY}."
    )


async def _compose(event: dict, now: dt.datetime, form: str) -> str | None:
    """1 lượt AI qua provider chain (hạn chót riêng). None = NO_REPLY/rỗng."""
    from ai import orchestrator
    from ai.timeouts import turn_deadline

    with turn_deadline(_COMPOSE_TIMEOUT_SEC):
        response = await orchestrator.ask(_compose_prompt(event, now, form))
    text = (getattr(response, "text", "") or "").strip()
    if not text or NO_REPLY in text.upper():
        return None
    return text[:_REPLY_MAX_CHARS]


async def _send(user_id: int, text: str, zalo_user, event_id: int = 0) -> None:
    """Gửi trên ĐÚNG kênh của người đó (id âm -> outbox Zalo, chủ bot -> Telegram).
    Hỏi thăm là tin NORMAL: qua hàng đợi gom services/notify_hub (bền, dedup theo
    sự kiện) nếu hub đang chạy, không thì gửi thẳng. Raise khi lỗi."""
    from services import notify_hub

    channel = "zalo" if user_id < 0 else "telegram"
    dedup = f"followup:{event_id}" if event_id else None
    if await notify_hub.offer(channel, user_id, text, source="followup", dedup=dedup):
        return
    if user_id < 0:
        from channels import zalo_repository
        from channels.zalo_text import to_plain_text

        account_id = await zalo_repository.resolve_active_account_id()
        await zalo_repository.enqueue_outbox(account_id, zalo_user.external_id, to_plain_text(text))
        return
    if _notify_callback is None:
        raise RuntimeError("chưa đăng ký notify_callback")
    await _notify_callback(user_id, text)


async def _zalo_user(user_id: int):
    from channels import zalo_users

    for user in await zalo_users.list_users():
        if user.internal_user_id == user_id:
            return user
    return None


SENT, DONE, RETRY = "sent", "done", "retry"


async def _deliver(user_id: int, event: dict, now: dt.datetime) -> str:
    """Viết + gửi 1 tin hỏi thăm cho sự kiện đã claim: SENT, DONE (không gửi
    nhưng không thử lại) hoặc RETRY (trả claim về pending)."""
    from services.concurrency import assistant_turn

    zalo_user = None
    form = "anh"
    if user_id < 0:
        zalo_user = await _zalo_user(user_id)
        if zalo_user is None or not zalo_user.is_active:
            await db.finish_user_events(user_id)  # đã gỡ pairing/bị khoá
            return DONE
        form = address.normalize(getattr(zalo_user, "address_form", None)) or "anh"
    async with assistant_turn(_turn_key(user_id)):
        if await _recently_active(user_id):
            return RETRY  # người dùng vừa nhắn trong lúc chờ khoá
        try:
            text = await _compose(event, now, form)
        except Exception:
            logger.warning("proactive: viết tin hỏi thăm lỗi (user %s).", user_id, exc_info=True)
            return RETRY
        if text is None:
            await db.mark_user_event_asked(int(event["id"]))  # AI chọn không hỏi
            return DONE
        if user_id < 0:
            # Outbox Zalo: INSERT không idempotent -> đánh dấu TRƯỚC (thà mất 1 tin
            # còn hơn gửi đôi), như bản tin sáng.
            await db.mark_user_event_asked(int(event["id"]))
        try:
            await _send(user_id, text, zalo_user, int(event["id"]))
        except Exception:
            logger.warning("proactive: gửi tin hỏi thăm lỗi (user %s).", user_id, exc_info=True)
            return DONE if user_id < 0 else RETRY
        if user_id > 0:
            await db.mark_user_event_asked(int(event["id"]))
        await _bump_sent(user_id, now.date())
        try:
            await db.add_chat_turn(user_id, FOLLOWUP_USER_LABEL, text, kind="followup")
        except Exception:
            logger.warning("proactive: không lưu được tin hỏi thăm vào lịch sử.", exc_info=True)
    logger.info("proactive: đã hỏi thăm user %s về sự kiện %s.", user_id, event["id"])
    return SENT


async def run_tick(now: dt.datetime | None = None) -> int:
    """1 vòng: claim + gửi các sự kiện tới hạn. Trả số tin đã gửi."""
    if not enabled():
        return 0
    now = now or dt.datetime.now(_VN_TZ)
    if not within_active_hours(now):
        return 0
    today = now.date()
    await db.expire_user_events(today - dt.timedelta(days=1))
    follow_hour = env_int("PROACTIVE_FOLLOWUP_HOUR", 18, 8, 21)
    on_or_before = today if now.hour >= follow_hour else today - dt.timedelta(days=1)
    cap = env_int("PROACTIVE_DAILY_CAP", 2, 0, 10)
    from services.concurrency import is_busy

    sent = 0
    for user_id in await db.users_with_due_events(on_or_before, _MAX_USERS_PER_TICK):
        try:
            if not await events_enabled(user_id, today=today):
                continue
            if cap <= 0 or await _sent_today(user_id, today) >= cap:
                continue
            if is_busy(_turn_key(user_id)) or await _recently_active(user_id):
                continue
            event = await db.claim_due_user_event(user_id, on_or_before)
            if event is None:
                continue
            result = await _deliver(user_id, event, now)
            if result == RETRY:
                await db.release_user_event(int(event["id"]))
            elif result == SENT:
                sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("proactive: lỗi xử lý hỏi thăm user %s", user_id)
    return sent


async def _loop() -> None:
    trace.detach()
    while True:
        try:
            with trace.job("proactive"):
                await run_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("proactive: vòng lặp hỏi thăm lỗi.")
        await asyncio.sleep(env_int("PROACTIVE_TICK_SEC", 1800, 300, 6 * 3600))


def start() -> None:
    global _task
    if not enabled():
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ─── Lệnh /hoithan ───────────────────────────────────────────────────────────


async def command(user_id: int, argument: str) -> str:
    """/hoithan [on|off]: bật/tắt hỏi thăm chủ động của CHÍNH người gõ + xem sự kiện."""
    from core.text_utils import parse_on_off

    if not enabled():
        return "Tính năng hỏi thăm chủ động đang tắt trên bot (PROACTIVE_FOLLOWUPS=0)."
    if user_id > 0 and not config.is_owner_user_id(user_id):
        return "Lệnh này chưa hỗ trợ tài khoản này."
    if user_id < 0 and not _zalo_enabled():
        return "Zalo chưa bật nên chưa hỏi thăm qua Zalo được."
    toggle = parse_on_off(argument)
    if toggle is True:
        await set_followups(user_id, True)
        return (
            "✅ Đã bật hỏi thăm: khi kể việc sắp tới (phỏng vấn, đi khám...), hôm đó em sẽ "
            "nhắn hỏi thăm (tối đa vài tin/ngày, trong giờ hoạt động). /hoithan off để tắt."
        )
    if toggle is False:
        await set_followups(user_id, False)
        return "🔕 Đã tắt hỏi thăm chủ động và bỏ các việc đang chờ hỏi."
    on = await events_enabled(user_id)
    lines = [f"Hỏi thăm chủ động: {'BẬT' if on else 'TẮT'} (/hoithan on|off để đổi)."]
    events = [e for e in await db.list_user_events(user_id, 10) if e.get("status") == "pending"]
    if events:
        lines.append("Việc sắp tới em sẽ hỏi thăm:")
        for event in events:
            day = event.get("event_date")
            day_text = day.strftime("%d/%m") if isinstance(day, dt.date) else str(day)
            lines.append(f"• {day_text}: {event['title']}")
    return "\n".join(lines)


# ─── Mục bản tin sáng: việc hôm nay ──────────────────────────────────────────


def _register_brief_section() -> None:
    from services import morning_brief

    @morning_brief.register_section("followups", 35, timeout=5)
    async def _today_events(ctx: morning_brief.BriefContext) -> str | None:
        rows = await db.user_events_on(ctx.user_id, ctx.today)
        if not rows:
            return None
        lines = ["📅 Việc hôm nay đã kể với em:"]
        lines.extend(f"• {row['title']}" for row in rows)
        return "\n".join(lines)


_register_brief_section()
