"""Nhắc việc v2: tạo (1 lần / lặp lại), xem, huỷ, hoãn, "xong"; lần kế tiếp của chuỗi.

- Giờ nhắc luôn tính bằng code (services/vn_time_parse.py, giờ VN); router AI
  chỉ chép cụm thời gian hoặc trả {date, time}/{in_minutes}.
- Nhắc lặp và ngày nhớ (services/ngay_nho.py) là 1 chuỗi reminder_series; mỗi
  lần nhắc là 1 dòng reminders. Scheduler claim 1 lần của chuỗi thì
  ``schedule_next`` chèn lần kế tiếp SAU max(bây giờ, giờ hẹn) - máy chủ ngủ
  nhiều ngày cũng chỉ sinh đúng 1 lần kế, không dồn một loạt tin bù.
- Tin nhắc kết thúc bằng gợi ý trả lời "hoãn 10p" / "xong": trong 30 phút sau
  khi nhắc, ``maybe_handle_shortcut`` xử lý câu đó bằng code (không tốn lượt AI).
- Trần (RAM/DB free tier): 100 nhắc 1 lần đang chờ và 50 chuỗi lặp/ngày nhớ mỗi
  người (thành viên Zalo cũng vậy), tối đa 5 nhắc trong 1 tin nhắn, xa nhất 1 năm.

Mọi thao tác theo user_id của người gọi: Zalo (kể cả Zalo của chủ bot) tách
hẳn Telegram.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from core import address
from core import database as db
from core.text_normalize import fold, nfc
from services import recurrence, reply_audience, vn_time_parse
from services.reminder_delivery import current_target, notification_target

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_PENDING_ONCE = 100
MAX_SERIES = 50
MAX_ITEMS_PER_MESSAGE = 5
MAX_MESSAGE_CHARS = 300
LIST_ONCE_LIMIT = 20
SHORTCUT_WINDOW = dt.timedelta(minutes=30)
LATE_AFTER = dt.timedelta(minutes=10)
REPLY_HINT = '(trả lời "hoãn 10p" / "xong")'
KIND_REMINDER = "reminder"
KIND_NGAY_NHO = "ngay_nho"
NO_CONTENT = "(không có nội dung cụ thể)"

_SNOOZE_RE = re.compile(r"^(?:em\s+)?hoãn(?:\s+(?:lại|giúp|giùm))?(?P<rest>.*)$")
_SNOOZE_PLAIN_RE = re.compile(r"^(?:em\s+)?hoan(?:\s+(?:lai|giup|gium))?(?P<rest>.*)$")
_DONE_RE = re.compile(
    r"^(?:(?:em\s+)?(?:đã|da)\s+)?xong(?:\s+(?:rồi|roi))?(?:\s+(?:nha|nhé|nhe|ạ|a|em))*[.!]*$"
)
_FILLER_TAIL_RE = re.compile(r"[\s.!,]*(?:nha|nhé|nhe|ạ|em|đi|di)*[\s.!,]*$")


class ReminderError(ValueError):
    """Câu báo tiếng Việt cho người dùng (vượt trần, không tìm thấy...)."""


def you_word() -> str:
    """Cách gọi người nhận của lượt (core/address.py), mặc định "anh"."""
    return address.normalize(reply_audience.current().address_form) or "anh"


def me_word() -> str:
    """Lan Anh tự xưng tương ứng ("em", "cháu", "mình", "chị")."""
    return address.self_word(you_word())


_you = you_word
_me = me_word


def clean_message(message: str) -> str:
    text = re.sub(r"\s+", " ", nfc(message or "")).strip()
    return text[:MAX_MESSAGE_CHARS] or NO_CONTENT


@dataclass(frozen=True)
class Created:
    due_at: dt.datetime
    message: str
    recurrence: dict | None = None
    series_id: int | None = None

    def confirmation(self, now: dt.datetime | None = None) -> str:
        when = vn_time_parse.describe_due(self.due_at, now)
        if self.recurrence:
            return (
                f"Đã đặt nhắc lặp {recurrence.describe(self.recurrence)} (giờ VN), "
                f"lần đầu {when}: {self.message}"
            )
        return f"Đã đặt nhắc 1 lần lúc {when} (giờ VN): {self.message}"

    @property
    def key(self) -> str:
        """Các cách viết giờ chấp nhận được trong câu trả lời, nối bằng "|" ("20:00|20h|
        20 giờ|8h|8 giờ") - không có cách nào thì code nối câu xác nhận chuẩn."""
        due = self.due_at.astimezone(VN_TZ)
        hour, minute = due.hour, due.minute
        tail = f"{minute:02d}" if minute else ""
        forms = [f"{hour:02d}:{minute:02d}", f"{hour}h{tail}", f"{hour} giờ"]
        if hour > 12:
            forms += [f"{hour - 12}h{tail}", f"{hour - 12} giờ"]
        return "|".join(dict.fromkeys(forms))


async def create(
    user_id: int,
    message: str,
    due_at: dt.datetime,
    rule: dict | None = None,
    *,
    key_suffix: str = "",
) -> Created:
    """Lưu 1 nhắc việc (1 lần, hoặc chuỗi lặp nếu có ``rule``) tới kênh của lượt
    hiện tại. event_key của tin nhắn (+ hậu tố từng mục) giữ idempotent khi
    webhook gửi lại."""
    message = clean_message(message)
    target = current_target(user_id)
    event_key = f"{target.event_key}{key_suffix}" if target.event_key else None
    if rule:
        rule = recurrence.normalize(rule)
        if await db.reminder_count_series(user_id) >= MAX_SERIES:
            raise ReminderError(
                f"Đã đủ {MAX_SERIES} nhắc lặp/ngày nhớ - huỷ bớt bằng /nhac huy <số> rồi đặt lại nha."
            )
        series_id, _created = await db.reminder_create_series(
            user_id,
            message,
            kind=KIND_REMINDER,
            category="",
            recurrence=rule,
            lead_days=0,
            channel=target.channel,
            recipient_id=target.recipient_id,
            account_id=target.account_id,
            user_jid=target.user_jid,
            event_key=event_key,
            first=[(due_at, message)],
        )
        return Created(due_at.astimezone(VN_TZ), message, rule, series_id)
    if await db.reminder_count_pending_once(user_id) >= MAX_PENDING_ONCE:
        raise ReminderError(
            f"Đang có {MAX_PENDING_ONCE} nhắc việc chờ - huỷ bớt bằng /nhac huy <số> rồi đặt lại nha."
        )
    stored = await db.add_reminder(
        user_id,
        message,
        due_at,
        channel=target.channel,
        recipient_id=target.recipient_id,
        account_id=target.account_id,
        user_jid=target.user_jid,
        event_key=event_key,
    )
    return Created((stored or due_at).astimezone(VN_TZ), message)


# ─── Scheduler: lần kế tiếp, câu nhắc, nhãn trễ ────────────────────────────


def next_row(series: dict, after: dt.datetime) -> tuple[dt.datetime, str] | None:
    if series.get("kind") == KIND_NGAY_NHO:
        from services import ngay_nho

        return ngay_nho.next_row(series, after)
    due = recurrence.next_occurrence(series["recurrence"], after)
    return (due, series["message"]) if due else None


async def schedule_next(series_id: int, due_at: dt.datetime | None, now: dt.datetime) -> bool:
    """Chèn lần kế tiếp của chuỗi (gọi lúc scheduler claim 1 lần). Idempotent:
    claim lại sau lease (Zalo chờ ack) tính ra cùng 1 lần, ON CONFLICT bỏ qua."""
    series = await db.reminder_get_series(series_id)
    if series is None:
        return False  # chuỗi đã bị huỷ
    after = max(now, due_at) if due_at else now
    row = next_row(series, after)
    if row is None:
        return False
    return await db.reminder_add_occurrence(series, row[0], row[1])


def late_label(due_at: dt.datetime | None, now: dt.datetime) -> str:
    """'(nhắc trễ, lịch gốc HH:MM)' khi trễ quá 10 phút - không đoán nguyên nhân
    (redeploy, gateway Zalo chậm... chứ không chỉ máy chủ ngủ)."""
    if due_at is None or now - due_at <= LATE_AFTER:
        return ""
    due = due_at.astimezone(VN_TZ)
    when = f"{due:%H:%M}" if due.date() == now.astimezone(VN_TZ).date() else f"{due:%H:%M %d/%m}"
    return f" (nhắc trễ, lịch gốc {when})"


def delivery_text(message: str, kind: str, due_at: dt.datetime | None, now: dt.datetime) -> str:
    base = message if kind == KIND_NGAY_NHO else f"⏰ Nhắc việc: {message}"
    return f"{base}{late_label(due_at, now)}\n{REPLY_HINT}"


# ─── Danh sách / huỷ / hoãn ────────────────────────────────────────────────


@dataclass(frozen=True)
class Item:
    reminder_id: int | None  # lần đang chờ (1 lần, hoặc lần kế của chuỗi)
    series_id: int | None
    due_at: dt.datetime | None
    message: str
    rule: dict | None = None


async def pending_items(user_id: int) -> list[Item]:
    """Nhắc 1 lần đang chờ + chuỗi nhắc lặp, sắp theo lần nhắc kế tiếp."""
    once = await db.reminder_list_pending_once(user_id, LIST_ONCE_LIMIT)
    series = await db.reminder_list_series(user_id, KIND_REMINDER, MAX_SERIES)
    items = [Item(r["id"], None, r["due_at"], r["message"]) for r in once]
    items.extend(
        Item(s.get("next_id"), s["id"], s.get("next_due_at"), s["message"], s["recurrence"])
        for s in series
    )
    far = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    items.sort(key=lambda i: (i.due_at or far, i.series_id or 0))
    return items


def format_items(items: list[Item], now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(VN_TZ)
    you = _you()
    if not items:
        return (
            f"⏰ {you[:1].upper() + you[1:]} chưa có nhắc việc nào đang chờ. Đặt bằng lời "
            '("nhắc 8h tối mai gọi mẹ") hoặc /nhac 8h tối mai gọi mẹ.'
        )
    lines = [f"⏰ Nhắc việc đang chờ ({len(items)}):"]
    for index, item in enumerate(items, 1):
        if item.rule:
            nxt = f", lần tới {vn_time_parse.describe_due(item.due_at, now)}" if item.due_at else ""
            lines.append(f"{index}. {recurrence.describe(item.rule)}{nxt} — {item.message}")
        else:
            when = vn_time_parse.describe_due(item.due_at, now) if item.due_at else "?"
            lines.append(f"{index}. {when} — {item.message}")
    lines.append("Huỷ: /nhac huy <số> · Hoãn: /nhac hoan <số> 30p · Ngày nhớ: /ngaynho")
    return "\n".join(lines)


def _match(items: list[Item], target: str) -> list[Item]:
    target = (target or "").strip()
    if re.fullmatch(r"#?\d{1,3}", target):
        index = int(target.lstrip("#"))
        return [items[index - 1]] if 1 <= index <= len(items) else []
    key = fold(target)
    key = re.sub(r"^(?:nhac|cai nhac|viec|nhac viec)\s+", "", key).strip()
    if not key:
        return []
    return [i for i in items if key in fold(i.message)]


def _not_found() -> str:
    return (
        f"{_me().capitalize()} không thấy nhắc việc đó - gõ /nhac để xem danh sách và số "
        "thứ tự nha."
    )


def _ambiguous(items: list[Item], matches: list[Item]) -> str:
    names = "; ".join(f"{items.index(i) + 1}. {i.message}" for i in matches[:5])
    return f"Có {len(matches)} nhắc việc khớp ({names}) - chọn theo số giúp {_me()} nha."


async def cancel(user_id: int, target: str) -> tuple[str, bool]:
    """Huỷ theo số thứ tự (/nhac) hoặc 1 đoạn nội dung duy nhất. Trả (câu trả lời, đã huỷ?)."""
    items = await pending_items(user_id)
    matches = _match(items, target)
    if not matches:
        return _not_found(), False
    if len(matches) > 1:
        return _ambiguous(items, matches), False
    item = matches[0]
    if item.series_id is not None:
        ok = await db.reminder_delete_series(user_id, item.series_id)
        label = f"nhắc lặp {recurrence.describe(item.rule or {})}"
    else:
        ok = item.reminder_id is not None and await db.reminder_delete(user_id, item.reminder_id)
        label = "nhắc việc"
    if not ok:
        return "Nhắc việc này vừa được gửi hoặc đã huỷ trước đó rồi.", False
    return f"🗑️ Đã huỷ {label}: {item.message}", True


async def snooze_pending(user_id: int, target: str, when_text: str) -> tuple[str, bool]:
    """Dời 1 nhắc đang chờ (/nhac hoan <số> 30p | sáng mai)."""
    items = await pending_items(user_id)
    matches = _match(items, target)
    if not matches:
        return _not_found(), False
    if len(matches) > 1:
        return _ambiguous(items, matches), False
    item = matches[0]
    now = dt.datetime.now(VN_TZ)
    base = item.due_at if item.due_at and item.due_at > now else now
    due = vn_time_parse.parse_snooze(when_text, now, base=base)
    if due is None or item.reminder_id is None:
        return (
            f"{_me().capitalize()} chưa hiểu hoãn tới khi nào - ví dụ "
            '"/nhac hoan 2 30p", "/nhac hoan 2 sáng mai".',
            False,
        )
    if due - now > vn_time_parse.MAX_AHEAD:
        return "Chỉ hoãn trong vòng 1 năm tới thôi nha.", False
    if not await db.reminder_reschedule(user_id, item.reminder_id, due):
        return "Nhắc việc này vừa được gửi hoặc trùng giờ với lần nhắc khác của chuỗi.", False
    return f"⏰ Đã dời sang {vn_time_parse.describe_due(due, now)}: {item.message}", True


# ─── Nút hoãn dưới tin nhắc Telegram (handlers/callbacks.py) ───────────────


async def snooze_delivered(user_id: int, reminder_id: int, choice: str) -> str:
    """Nút [+10p] [+1h] [Sáng mai] dưới tin nhắc vừa gửi: đặt 1 nhắc mới cùng nội
    dung và đánh dấu lần nhắc cũ là xong. event_key "snooze:<id>" - bấm lại (hay
    inbox thử lại) không đặt thêm nhắc thứ 2."""
    from services import action_buttons

    when_text = action_buttons.SNOOZE_CHOICES.get(choice)
    row = await db.reminder_get(user_id, reminder_id)
    if row is None or when_text is None:
        return f"{_me().capitalize()} không còn thấy nhắc việc này nữa nha."
    if row.get("done_at") is not None:
        return "Nhắc việc này đã hoãn hoặc xong rồi nha."
    message = row["message"]
    if row.get("series_id") and message.startswith("🗓"):
        message = message.removeprefix("🗓").strip()
    now = dt.datetime.now(VN_TZ)
    due = vn_time_parse.parse_snooze(when_text, now)
    if due is None:  # pragma: no cover - 3 lựa chọn cố định đều hiểu được
        return f"{_me().capitalize()} chưa hoãn được nhắc này, thử /nhac hoan giúp {_me()} nha."
    target = dataclasses.replace(current_target(user_id), event_key=f"snooze:{reminder_id}")
    with notification_target(target):
        created = await create(user_id, message, due)
    await db.reminder_mark_done(reminder_id)
    when = vn_time_parse.describe_due(created.due_at, now)
    return f"⏰ Dạ {_me()} hoãn tới {when} nha: {message}"


# ─── "hoãn 10p" / "xong" ngay sau khi nhắc ────────────────────────────────


def _shortcut(text: str) -> tuple[str, str] | None:
    """("snooze", phần còn lại) | ("done", "") | None - so trên chữ CÒN DẤU:
    "hoàn thành", "hoan hô" không phải hoãn; câu không dấu mới dùng "hoan"."""
    low = re.sub(r"\s+", " ", nfc(text or "")).strip().lower()
    if not low or len(low) > 60:
        return None
    plain = fold(low) == low
    if _DONE_RE.fullmatch(low):
        return "done", ""
    m = (_SNOOZE_PLAIN_RE if plain else _SNOOZE_RE).fullmatch(low)
    if not m:
        return None
    rest = m.group("rest").strip()
    if rest and not re.match(
        r"[\d(]|n[uử]a|t[oớ]i|đ[eế]n|den|l[uú]c|s[aá]ng|tr[uư]a|chi[eề]u|t[oố]i|mai|m[oố]t|th[uứ]|1|m[oộ]t",
        rest,
    ):
        return None  # "hoãn chuyến đi Đà Lạt": không phải lệnh hoãn nhắc
    return "snooze", _FILLER_TAIL_RE.sub("", rest)


async def maybe_handle_shortcut(user_id: int, text: str) -> str | None:
    """Câu trả lời cho "hoãn 10p"/"hoãn 1 tiếng"/"hoãn sáng mai"/"xong" nếu người
    này vừa được nhắc trong 30 phút qua; None để tin đi tiếp vào chat như thường."""
    parsed = _shortcut(text)
    if parsed is None:
        return None
    try:
        fired = await db.reminder_last_fired(user_id, SHORTCUT_WINDOW)
    except Exception:
        logger.warning("Không tra được lần nhắc vừa gửi (bỏ qua lối tắt).", exc_info=True)
        return None
    if not fired:
        return None
    action, rest = parsed
    message = fired["message"]
    if fired.get("series_id") and message.startswith("🗓"):
        message = message.removeprefix("🗓").strip()
    if action == "done":
        await db.reminder_mark_done(fired["id"])
        return f"✅ Dạ {_me()} ghi nhận xong: {message}"
    now = dt.datetime.now(VN_TZ)
    due = vn_time_parse.parse_snooze(rest, now)
    if due is None:
        return f'Hoãn tới khi nào {_you()} ơi? Ví dụ "hoãn 10p", "hoãn 1 tiếng", "hoãn sáng mai".'
    if due - now > dt.timedelta(days=7):
        return f"Lối tắt hoãn chỉ dời tối đa 7 ngày - {_you()} đặt nhắc mới giúp {_me()} nha."
    await create(user_id, message, due, key_suffix=":hoan")
    await db.reminder_mark_done(fired["id"])
    return f"⏰ Dạ {_me()} hoãn tới {vn_time_parse.describe_due(due, now)} nha: {message}"
