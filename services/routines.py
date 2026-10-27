"""Tác vụ định kỳ bằng lời (/lichtacvu - ý từ amanimran786/jarvis-ai "routines").

"mỗi sáng thứ 2 8h tóm tắt tin về VNM cho anh", "mỗi tối thứ 6 9h tổng kết chi tiêu
tuần cho anh" -> 1 dòng ``routines`` (migrations/027). Khác nhắc việc (/nhac): nhắc
việc gửi 1 câu cố định đúng giờ; tác vụ chạy 1 YÊU CẦU AI (persona, trí nhớ, tra web,
số liệu code của đúng người đó) qua bộ chạy việc nền (services/background_jobs.py -
cùng hạn chót BACKGROUND_JOB_TIMEOUT_SEC, cùng semaphore MAX_CONCURRENT_TURNS) rồi
gửi kết quả qua services/notify_hub mức NORMAL (tin tổng hợp hẹn giờ: tôn trọng giờ
yên lặng). Model được trả "NO_REPLY" khi không có gì đáng báo -> không gửi gì.

- Thời gian/lặp lại đọc bằng code, dùng lại services/vn_time_parse.py (cùng bộ đọc
  với /nhac) và services/recurrence.py (cùng dạng JSON với reminder_series).
- Câu tự nhiên CHỈ được nhận khi chắc: có cụm "tóm tắt / tổng kết / báo cáo / kiểm
  tra / tra ... cho anh" + mốc lặp lại ("mỗi/hằng ..."), không có chữ "nhắc/hẹn giờ"
  (câu đó là nhắc việc). Có giờ + thứ nhưng thiếu "mỗi" ("tối thứ 6 9h tổng kết chi
  tiêu tuần cho anh") thì HỎI LẠI, người dùng nhắn "ok" mới đặt. Còn lại để chat/nhắc
  việc xử lý như cũ.
- Trần: tối đa ``MAX_PER_USER`` (10) tác vụ/người, 2 lần chạy cách nhau tối thiểu 1
  giờ (lặp theo ngày trở lên nên luôn thoả), tối đa ROUTINE_MAX_RUNS_PER_DAY lần
  chạy/ngày toàn bot (vượt thì bỏ lần đó, báo chủ bot 1 lần/ngày).
- Lỡ giờ (bot ngủ/redeploy): chạy bù ĐÚNG 1 lần nếu trễ chưa quá ``CATCHUP_HOURS``
  giờ, các lần lỡ khác bỏ qua; lịch kế tiếp tính từ bây giờ.
- Tác vụ không ghi dữ liệu (không đặt nhắc, không ghi danh mục...): lượt AI chạy với
  allow_side_effects=False.
- Thành viên Zalo: mặc định TẮT (ROUTINES_MEMBERS=1 để mở; mỗi lần chạy tính 1 lượt
  nặng, hết lượt nặng thì bỏ lần chạy đó). Admin Zalo, chủ bot dùng được.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from core import config
from core import database as db
from core.envutil import env_int, env_on
from core.text_normalize import fold, nfc
from services import attention, background_jobs, recurrence, reply_audience, vn_time_parse

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_PER_USER = 10
MIN_INTERVAL = dt.timedelta(hours=1)
CATCHUP_HOURS = 12
PROMPT_MAX_CHARS = 500
NL_MAX_CHARS = 300
CONFIRM_TTL_SEC = 600
_MAX_PENDING = 200
_LABEL_CHARS = 70


def enabled() -> bool:
    return env_on("ROUTINES_ENABLED")


def members_allowed() -> bool:
    """Thành viên Zalo (role user) được đặt tác vụ định kỳ không - mặc định KHÔNG."""
    return os.getenv("ROUTINES_MEMBERS", "0").strip() == "1"


def max_runs_per_day() -> int:
    return env_int("ROUTINE_MAX_RUNS_PER_DAY", 20, 1, 500)


def _now() -> dt.datetime:
    return dt.datetime.now(VN_TZ)


def _day_start(now: dt.datetime) -> dt.datetime:
    return now.astimezone(VN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)


def _forms(user_id: int) -> tuple[str, str]:
    if config.is_owner_user_id(user_id):
        return "anh", "em"
    return reply_audience.forms()


def _clip(text: str, limit: int = _LABEL_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _vn(value) -> dt.datetime | None:
    if not isinstance(value, dt.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(VN_TZ)


# ─── Đọc câu ────────────────────────────────────────────────────────────────

_VERB_RE = re.compile(
    r"\b(?:tom tat|tong ket|tong hop|bao cao|kiem tra|tra cuu|tra|diem tin|thong ke|"
    r"cap nhat|liet ke)\b"
)
_VERB_ACCENTED = frozenset(
    {
        "tóm tắt",
        "tổng kết",
        "tổng hợp",
        "báo cáo",
        "kiểm tra",
        "tra cứu",
        "tra",
        "điểm tin",
        "thống kê",
        "cập nhật",
        "liệt kê",
    }
)
_WHO = r"anh|chi|em|minh|toi|tui|co|chu|bac|ong|ba|me|bo|con|chau|sep|vo|chong"
_FOR_RE = re.compile(rf"\b(?:cho|giup|gium|dum|gui) (?:{_WHO})\b")
# Câu nhắc việc: để /nhac và router nhắc việc xử lý (gửi câu cố định).
_REMINDER_ACCENTED_RE = re.compile(r"\b(?:nhắc|hẹn giờ|báo thức|đặt lịch nhắc)\b")
_REMINDER_FOLDED_RE = re.compile(r"\b(?:nhac|hen gio|bao thuc|remind)\b")


def _lower(text: str) -> str:
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in text)


def looks_like_routine(text: str) -> bool:
    """Có cụm yêu cầu AI "tóm tắt/tổng kết/báo cáo/kiểm tra/tra ... cho anh" và KHÔNG
    phải câu nhắc việc (chưa xét mốc lặp lại)."""
    low = _lower(" ".join(nfc(text or "").split()))
    folded = vn_time_parse.fold_keep(low)
    accented = folded != low
    if (_REMINDER_ACCENTED_RE if accented else _REMINDER_FOLDED_RE).search(
        low if accented else folded
    ):
        return False
    verb = False
    for match in _VERB_RE.finditer(folded):
        if not accented or low[match.start() : match.end()] in _VERB_ACCENTED:
            verb = True
            break
    return verb and bool(_FOR_RE.search(folded))


@dataclass(frozen=True)
class Parsed:
    prompt: str = ""
    rule: dict | None = None
    first_run: dt.datetime | None = None
    error: str = ""


def parse_request(text: str, now: dt.datetime | None = None) -> Parsed | None:
    """(yêu cầu, quy tắc lặp, lần chạy đầu) từ câu có mốc lặp lại; None nếu không thấy
    thời điểm nào; Parsed(error=...) khi thấy nhưng chưa dùng được."""
    now = (now or _now()).astimezone(VN_TZ)
    extracted = vn_time_parse.extract_reminder(text, now)
    if extracted is None:
        return None
    prompt, when = extracted
    prompt = " ".join(prompt.split())
    if when.recurrence is None:
        return Parsed(prompt=prompt, error="no_recurrence")
    if not when.has_time:
        return Parsed(prompt=prompt, rule=when.recurrence, error="no_time")
    if when.error or when.due_at is None:
        return Parsed(prompt=prompt, rule=when.recurrence, error=when.error or "no_time")
    return Parsed(prompt=prompt, rule=when.recurrence, first_run=when.due_at)


def _weekly_proposal(text: str, now: dt.datetime) -> Parsed | None:
    """ "tối thứ 6 9h tổng kết ... cho anh" (thiếu "mỗi"): đề xuất lặp hằng tuần."""
    parsed = parse_request(f"mỗi {text}", now)
    if parsed is None or parsed.error or not parsed.rule or parsed.rule.get("type") != "weekly":
        return None
    return parsed


def interval_ok(rule: dict, now: dt.datetime) -> bool:
    """2 lần chạy liền nhau cách nhau tối thiểu MIN_INTERVAL (kiểm vài lần đầu)."""
    try:
        current = recurrence.next_occurrence(rule, now)
        for _ in range(4):
            if current is None:
                return True
            following = recurrence.next_occurrence(rule, current)
            if following is not None and following - current < MIN_INTERVAL:
                return False
            current = following
    except recurrence.RecurrenceError:
        return False
    return True


# ─── Tạo / xem / huỷ ─────────────────────────────────────────────────────────


def _allowed(user_id: int, is_admin: bool) -> bool:
    return user_id > 0 or is_admin or members_allowed()


async def create(
    user_id: int,
    channel: str,
    parsed: Parsed,
    *,
    is_admin: bool = False,
    now: dt.datetime | None = None,
) -> str:
    you, me = _forms(user_id)
    now = (now or _now()).astimezone(VN_TZ)
    if not enabled() or not background_jobs.enabled():
        return f"{me.capitalize()} chưa nhận tác vụ định kỳ lúc này nha {you}."
    if not _allowed(user_id, is_admin):
        return (
            f"Tác vụ định kỳ chưa mở cho {you} nha. Muốn {me} nhắc đúng giờ thì dùng /nhac, "
            f"cần hỏi gì {you} cứ nhắn thẳng {me}."
        )
    prompt = parsed.prompt
    if len(prompt) < 3 or parsed.rule is None or parsed.first_run is None:
        return usage_text(you)
    if len(prompt) > PROMPT_MAX_CHARS:
        return f"Yêu cầu dài quá (tối đa {PROMPT_MAX_CHARS} ký tự), {you} rút gọn giúp {me} nha."
    if not interval_ok(parsed.rule, now):
        return f"Tác vụ định kỳ phải cách nhau tối thiểu 1 giờ nha {you}."
    if await db.routine_count(user_id) >= MAX_PER_USER:
        return (
            f"{you.capitalize()} đã có {MAX_PER_USER} tác vụ định kỳ rồi (tối đa {MAX_PER_USER}). "
            f"/lichtacvu huy <số> bớt 1 cái rồi đặt tiếp nha."
        )
    routine_id = await db.routine_insert(user_id, channel, prompt, parsed.rule, parsed.first_run)
    return (
        f"✅ {me.capitalize()} đặt tác vụ định kỳ #{routine_id}: "
        f"{recurrence.describe(parsed.rule)} — «{_clip(prompt, 120)}».\n"
        f"Đến giờ {me} tự chạy rồi gửi kết quả cho {you} (không có gì đáng báo thì {me} "
        f"không nhắn). Lần đầu: {vn_time_parse.describe_due(parsed.first_run, now)}.\n"
        f"/lichtacvu để xem · /lichtacvu huy {routine_id} để huỷ"
    )


def usage_text(you: str = "anh") -> str:
    return (
        "🔁 Tác vụ định kỳ: em tự chạy 1 yêu cầu theo lịch rồi gửi kết quả (khác /nhac "
        "chỉ nhắc 1 câu cố định).\n"
        "/lichtacvu mỗi sáng thứ 2 8h tóm tắt tin về VNM cho anh\n"
        "/lichtacvu mỗi tối thứ 6 9h tổng kết chi tiêu tuần cho anh\n"
        f'(hoặc nhắn thẳng câu như trên, có "mỗi/hằng ..." và "... cho {you}")\n'
        "/lichtacvu — xem · /lichtacvu huy <số> · /lichtacvu tam dung <số> · "
        "/lichtacvu tiep <số>"
    )


def _last_run_label(row: dict) -> str:
    status = row.get("last_status")
    if not status:
        return ""
    if status == background_jobs.SUCCEEDED:
        label = "không có gì đáng báo" if row.get("last_no_reply") else "đã gửi"
    else:
        label = {
            background_jobs.QUEUED: "đang chờ chạy",
            background_jobs.RUNNING: "đang chạy",
            background_jobs.CANCELLED: "đã huỷ",
        }.get(str(status), "lỗi")
    at = _vn(row.get("last_job_at"))
    return f" · lần trước{f' {at:%H:%M %d/%m}' if at else ''}: {label}"


async def list_text(user_id: int, now: dt.datetime | None = None) -> str:
    you, me = _forms(user_id)
    now = (now or _now()).astimezone(VN_TZ)
    rows = await db.routine_list(user_id)
    if not rows:
        return f"{me.capitalize()} chưa có tác vụ định kỳ nào của {you}.\n\n" + usage_text(you)
    lines = [f"🔁 Tác vụ định kỳ của {you} ({len(rows)}/{MAX_PER_USER}):"]
    for row in rows:
        schedule = recurrence.describe(row.get("recurrence") or {})
        if row.get("status") == "paused":
            state = " · ⏸ tạm dừng"
        else:
            upcoming = _vn(row.get("next_run_at"))
            state = f" · lần tới {vn_time_parse.describe_due(upcoming, now)}" if upcoming else ""
        lines.append(
            f"• #{row['id']} {schedule} — «{_clip(str(row.get('prompt') or ''))}»{state}"
            f"{_last_run_label(row)}"
        )
    lines.append("/lichtacvu huy <số> · /lichtacvu tam dung <số> · /lichtacvu tiep <số>")
    return "\n".join(lines)


async def delete(user_id: int, routine_id: int) -> str:
    you, me = _forms(user_id)
    if not await db.routine_delete(user_id, routine_id):
        return f"{me.capitalize()} không thấy tác vụ định kỳ #{routine_id} nào của {you}."
    return f"🗑 {me.capitalize()} huỷ tác vụ định kỳ #{routine_id} rồi nha {you}."


async def pause(user_id: int, routine_id: int) -> str:
    you, me = _forms(user_id)
    if not await db.routine_set_status(user_id, routine_id, "paused"):
        return f"{me.capitalize()} không thấy tác vụ định kỳ #{routine_id} nào của {you}."
    return (
        f"⏸ {me.capitalize()} tạm dừng tác vụ #{routine_id}. /lichtacvu tiep {routine_id} "
        "để chạy lại."
    )


async def resume(user_id: int, routine_id: int, now: dt.datetime | None = None) -> str:
    you, me = _forms(user_id)
    now = (now or _now()).astimezone(VN_TZ)
    row = await db.routine_get(user_id, routine_id)
    if row is None:
        return f"{me.capitalize()} không thấy tác vụ định kỳ #{routine_id} nào của {you}."
    try:
        upcoming = recurrence.next_occurrence(row.get("recurrence") or {}, now)
    except recurrence.RecurrenceError:
        upcoming = None
    if upcoming is None:
        return f"Tác vụ #{routine_id} không còn lần chạy nào nữa, {you} đặt lại giúp {me} nha."
    await db.routine_set_status(user_id, routine_id, "active", upcoming)
    return (
        f"▶️ Tác vụ #{routine_id} chạy lại rồi nha {you}, lần tới "
        f"{vn_time_parse.describe_due(upcoming, now)}."
    )


_ACTION_RES = (
    ("delete", re.compile(r"^(?:huy|xoa|bo|cancel)\s+#?(\d{1,9})$")),
    ("pause", re.compile(r"^(?:tam dung|tamdung|dung|pause)\s+#?(\d{1,9})$")),
    ("resume", re.compile(r"^(?:tiep tuc|tieptuc|tiep|mo lai|mo|resume)\s+#?(\d{1,9})$")),
)


def parse_argument(argument: str) -> tuple[str, str]:
    """("list" | "help" | "delete" | "pause" | "resume" | "create", phần còn lại)."""
    raw = " ".join((argument or "").split())
    folded = fold(raw)
    if folded in {"", "ds", "xem", "list", "danh sach"}:
        return "list", ""
    if folded in {"help", "huong dan", "?"}:
        return "help", ""
    for action, pattern in _ACTION_RES:
        match = pattern.match(folded)
        if match:
            return action, match.group(1)
    return "create", raw


async def handle_command(
    command: str,
    argument: str,
    user_id: int,
    channel: str,
    *,
    is_admin: bool = False,
    now: dt.datetime | None = None,
) -> list[str] | None:
    """/lichtacvu dùng chung mọi kênh."""
    if command != "/lichtacvu":
        return None
    you, me = _forms(user_id)
    action, rest = parse_argument(argument)
    if action == "help":
        return [usage_text(you)]
    if not _allowed(user_id, is_admin):
        return [f"Tác vụ định kỳ chưa mở cho {you} nha. Muốn {me} nhắc đúng giờ thì dùng /nhac."]
    if action == "list":
        return [await list_text(user_id, now)]
    if action == "delete":
        return [await delete(user_id, int(rest))]
    if action == "pause":
        return [await pause(user_id, int(rest))]
    if action == "resume":
        return [await resume(user_id, int(rest), now)]
    parsed = parse_request(rest, now)
    if parsed is None or parsed.error == "no_recurrence":
        return [
            "Tác vụ định kỳ cần mốc lặp lại kèm giờ, vd: /lichtacvu mỗi sáng thứ 2 8h tóm tắt "
            f"tin về VNM cho {you}. Muốn {me} làm 1 lần ngay thì /viec <yêu cầu>."
        ]
    if parsed.error:
        return [_time_error(parsed, you, me)]
    return [await create(user_id, channel, parsed, is_admin=is_admin, now=now)]


def _time_error(parsed: Parsed, you: str, me: str) -> str:
    if parsed.error == "no_time":
        example = parsed.prompt[:1].lower() + parsed.prompt[1:] if parsed.prompt else "..."
        return (
            f"{you.capitalize()} muốn {me} chạy lúc mấy giờ? Nói rõ giúp {me} nha, vd "
            f"“mỗi sáng 8h {example}”."
        )
    return f"{me.capitalize()} chưa đặt được tác vụ: {parsed.error}"


# ─── Câu tự nhiên ───────────────────────────────────────────────────────────

# user_id -> (Parsed, channel, is_admin, hết hạn monotonic); có trần.
_pending: OrderedDict[int, tuple[Parsed, str, bool, float]] = OrderedDict()
_YES = frozenset(
    {"ok", "oke", "okay", "co", "u", "uh", "um", "dung", "dung roi", "dat di", "dong y", "yes", "y"}
)
_NO = frozenset({"thoi", "khong", "ko", "k", "bo", "huy", "no", "thoi khoi", "khoi"})


def _remember_pending(user_id: int, item: tuple[Parsed, str, bool, float]) -> None:
    _pending[user_id] = item
    _pending.move_to_end(user_id)
    while len(_pending) > _MAX_PENDING:
        _pending.popitem(last=False)


def _reply_word(text: str) -> str:
    folded = fold(text or "")
    folded = re.sub(r"[^\w\s]", " ", folded)
    words = folded.split()
    while words and words[-1] in {"em", "nha", "nhe", "a", "di"} and len(words) > 1:
        words.pop()
    if words and words[0] in {"da", "vang"} and len(words) > 1:
        words = words[1:]
    return " ".join(words)


async def maybe_handle_chat(
    user_id: int,
    text: str,
    channel: str,
    *,
    is_admin: bool = False,
    now: dt.datetime | None = None,
) -> str | None:
    """Câu tự nhiên đặt tác vụ định kỳ (hoặc trả lời "ok" cho câu em hỏi lại). None =
    không phải - đi tiếp xuống nhắc việc/chat như cũ."""
    if not enabled() or not text or text.startswith("/"):
        return None
    now = (now or _now()).astimezone(VN_TZ)
    pending = _pending.pop(user_id, None)
    if pending is not None and pending[3] > time.monotonic():
        word = _reply_word(text)
        if word in _YES:
            parsed, saved_channel, saved_admin, _ = pending
            if parsed.rule is not None:
                # Lần chạy đầu tính lại theo bây giờ (câu hỏi có thể đã chờ vài phút).
                upcoming = recurrence.next_occurrence(parsed.rule, now)
                parsed = Parsed(parsed.prompt, parsed.rule, upcoming)
            return await create(user_id, saved_channel, parsed, is_admin=saved_admin, now=now)
        if word in _NO:
            you, me = _forms(user_id)
            return f"Dạ, {me} không đặt tác vụ đó nữa nha {you}."
    if len(text) > NL_MAX_CHARS or not looks_like_routine(text):
        return None
    if not _allowed(user_id, is_admin):
        return None  # thành viên chưa được dùng: để chat trả lời như thường
    parsed = parse_request(text, now)
    if parsed is None:
        return None
    you, me = _forms(user_id)
    if parsed.error == "no_recurrence":
        proposal = _weekly_proposal(text, now)
        if proposal is None or proposal.rule is None or proposal.first_run is None:
            return None
        _remember_pending(
            user_id, (proposal, channel, is_admin, time.monotonic() + CONFIRM_TTL_SEC)
        )
        return (
            f"{you.capitalize()} muốn {me} làm việc này HẰNG TUẦN "
            f"({recurrence.describe(proposal.rule)}): «{_clip(proposal.prompt, 120)}» đúng không? "
            f"Nhắn “ok” để {me} đặt tác vụ định kỳ (đến giờ {me} tự chạy rồi gửi kết quả), "
            f"nhắn khác thì {me} bỏ qua."
        )
    if parsed.error:
        return _time_error(parsed, you, me)
    return await create(user_id, channel, parsed, is_admin=is_admin, now=now)


def forget(user_id: int) -> None:
    _pending.pop(user_id, None)


# ─── Vòng chạy ──────────────────────────────────────────────────────────────

_cap_alert_day: str | None = None


async def runs_today(now: dt.datetime | None = None) -> int:
    return await db.routine_runs_since(_day_start(now or _now()))


async def _can_run(routine: dict) -> bool:
    """Chủ nhân tác vụ còn được chạy không (Zalo bị gỡ/khoá, thành viên hết quyền/
    hết lượt nặng -> bỏ lần chạy này)."""
    from services import usage_quota

    user_id = int(routine["telegram_user_id"])
    recipient = await background_jobs.resolve(user_id, str(routine.get("channel") or ""))
    if recipient is None:
        return False
    if recipient.member:
        if not members_allowed():
            return False
        if await usage_quota.heavy_exhausted(user_id, channel="zalo", is_admin=False):
            return False
    return True


def _cap_alert(cap: int, day: str) -> None:
    global _cap_alert_day
    if _cap_alert_day == day:
        return
    _cap_alert_day = day
    from core import alerts

    alerts.send(
        f"🔁 Tác vụ định kỳ đã chạy đủ {cap} lần hôm nay (ROUTINE_MAX_RUNS_PER_DAY) - các lần "
        "còn lại hôm nay em bỏ qua."
    )


async def run_tick(now: dt.datetime | None = None) -> int:
    """Xếp các tác vụ tới giờ vào bộ chạy việc nền. Trả số lần đã xếp."""
    if not enabled() or not background_jobs.enabled():
        return 0
    now = (now or _now()).astimezone(VN_TZ)
    due = await db.routine_due(now, 20)
    if not due:
        return 0
    cap = max_runs_per_day()
    runs = await runs_today(now)
    fired = 0
    for routine in due:
        scheduled = _vn(routine.get("next_run_at"))
        if scheduled is None:
            continue
        try:
            # Lần kế tiếp SAU bây giờ: lỡ nhiều lần thì chỉ chạy bù 1 lần.
            upcoming = recurrence.next_occurrence(routine.get("recurrence") or {}, now)
        except recurrence.RecurrenceError:
            logger.warning("routines: quy tắc lặp lỗi ở tác vụ #%s.", routine.get("id"))
            upcoming = None
        enqueue = True
        if now - scheduled > dt.timedelta(hours=CATCHUP_HOURS):
            logger.info("routines: tác vụ #%s lỡ giờ quá lâu, bỏ lần này.", routine["id"])
            enqueue = False
        elif runs >= cap:
            _cap_alert(cap, now.date().isoformat())
            enqueue = False
        elif not await _can_run(routine):
            enqueue = False
        job_id = await db.routine_fire(routine, routine["next_run_at"], upcoming, enqueue=enqueue)
        if job_id is not None:
            runs += 1
            fired += 1
    if fired:
        background_jobs.wake()
    return fired


background_jobs.register_tick(run_tick)


# ─── /chuy ──────────────────────────────────────────────────────────────────


@attention.register_section("routines", 85, label="tác vụ định kỳ")
async def _attention_routines(ctx: attention.AttentionContext) -> str | None:
    start = _day_start(ctx.now)
    end = start + dt.timedelta(days=1)
    lines: list[tuple[dt.datetime, str]] = []
    for job in await db.routine_jobs_since(ctx.user_id, start):
        at = _vn(job.get("scheduled_for")) or _vn(job.get("created_at")) or ctx.now
        status = job.get("status")
        if status == background_jobs.SUCCEEDED:
            state = "không có gì đáng báo" if job.get("no_reply") else "đã gửi"
        elif status in (background_jobs.QUEUED, background_jobs.RUNNING):
            state = "đang chạy"
        elif status == background_jobs.CANCELLED:
            state = "đã huỷ"
        else:
            state = "lỗi"
        label = _clip(str(job.get("prompt") or ""), 50)
        lines.append((at, f"{at:%H:%M} #{job.get('routine_id') or '?'} «{label}» - {state}"))
    for row in await db.routine_list(ctx.user_id):
        upcoming = _vn(row.get("next_run_at"))
        if row.get("status") != "active" or upcoming is None or not ctx.now <= upcoming < end:
            continue
        label = _clip(str(row.get("prompt") or ""), 50)
        lines.append((upcoming, f"{upcoming:%H:%M} #{row['id']} «{label}» - sắp chạy"))
    if not lines:
        return None
    lines.sort(key=lambda item: item[0])
    shown = [f"• {text}" for _, text in lines[:6]]
    if len(lines) > 6:
        shown.append(f"• … và {len(lines) - 6} lần khác")
    return "\n".join(["🔁 Tác vụ định kỳ hôm nay:", *shown])
