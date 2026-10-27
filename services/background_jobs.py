"""Việc nền có trạng thái (/viec - ý từ ethanplusai/jarvis "runs", isair/jarvis Task
Centre) và BỘ CHẠY LƯỢT AI NỀN dùng chung với tác vụ định kỳ (services/routines.py).

- ``/viec <yêu cầu>``: xếp 1 dòng bg_jobs (migrations/027) rồi trả lời NGAY "em làm
  rồi báo anh". Việc chạy NGOÀI khoá lượt của người dùng (họ vẫn chat bình thường)
  nhưng vẫn chiếm 1 chỗ trong semaphore tổng MAX_CONCURRENT_TURNS
  (``concurrency.background_slot``: thành viên Zalo lấy cả chỗ thành viên, không bao
  giờ đụng chỗ dành riêng cho chủ bot). Chỉ có lệnh /viec chuyển việc ra nền; /agent,
  đọc tài liệu lớn vẫn chạy như cũ (tiêu chí đơn giản: người dùng tự chọn).
- Toàn bot chạy tối đa BACKGROUND_JOBS_CONCURRENCY (1) việc cùng lúc; mỗi người tối
  đa BACKGROUND_JOBS_MAX_PER_USER (3) việc đang chờ/chạy, cả hàng đợi tối đa
  ``GLOBAL_QUEUE_CAP`` việc chờ.
- Mỗi việc có hạn chót riêng BACKGROUND_JOB_TIMEOUT_SEC (600, trần 1800) thay cho
  CHAT_TURN_TIMEOUT_SEC: mọi lượt gọi AI vẫn đi qua ``ai.timeouts.bounded()`` nhờ
  ``turn_deadline``; thêm 1 lớp asyncio.wait_for dự phòng.
- Lượt AI = ``channel_chat_service.run_chat_turn`` với persona, trí nhớ, kế hoạch tra
  web (web_search.plan_search), số liệu code (vàng/tỷ giá/xổ số, tool đọc danh mục/
  chi tiêu, giá cổ phiếu nhắc tới) của ĐÚNG người đó, ``allow_side_effects=False``:
  việc nền không bao giờ ghi dữ liệu (không đặt nhắc, không ghi danh mục...).
- Kết quả gửi qua services/notify_hub: /viec mức URGENT (người dùng đang chờ; trong
  giờ yên lặng thì giữ tới sáng như mọi URGENT khác), tác vụ định kỳ mức NORMAL (tin
  tổng hợp hẹn giờ). Tác vụ định kỳ được trả "NO_REPLY" khi không có gì đáng báo ->
  không gửi gì. Kết quả lưu có trần ``RESULT_MAX_CHARS`` ký tự; /viec xem <số> xem lại.
- Huỷ (/viec huy <số>): dòng DB chuyển 'cancelled' ngay; việc đang chạy trong process
  này bị huỷ TASK CON (task worker vẫn sống, ghi trạng thái, nhận việc tiếp); instance
  khác đang chạy việc đó tự dừng ở nhịp gia hạn lease kế tiếp.
- Khởi động lại/redeploy: việc running có lease (gia hạn mỗi 30 giây). Process chết thì
  lease hết hạn, vòng dọn đưa việc về hàng đợi để CHẠY LẠI 1 LẦN; bị gián đoạn lần
  thứ 2 thì đánh failed và báo người dùng. Việc không ghi dữ liệu nên chạy lại an toàn.
- Thành viên Zalo: mặc định được dùng (BACKGROUND_JOBS_MEMBERS=0 để tắt), mỗi việc
  tính 1 lượt NẶNG trong services/usage_quota (user_calls kind "viec", ghi lúc việc
  chạy); admin Zalo và chủ bot không giới hạn.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from core import address, config, trace
from core import database as db
from core.envutil import env_int, env_on
from core.text_normalize import fold
from services import attention, concurrency, notify_hub, reply_audience

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
KIND_JOB, KIND_ROUTINE = "job", "routine"
QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, TIMED_OUT = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
)
LEASE_SEC = 120
HEARTBEAT_SEC = 30
POLL_SEC = 30
JANITOR_SEC = 60
MAX_ATTEMPTS = 2
RESULT_MAX_CHARS = 6000
PROMPT_MAX_CHARS = 1500
PROMPT_MIN_CHARS = 3
GLOBAL_QUEUE_CAP = 50
NO_REPLY = "NO_REPLY"
_HARD_GRACE_SEC = 30
_LABEL_CHARS = 80

JOB_HINT = (
    "Đây là việc người dùng giao chạy nền (/viec): họ không ngồi chờ, kết quả được gửi "
    "thành 1 tin riêng. Trả lời đầy đủ, có đầu mục rõ ràng; không hỏi lại - thiếu thông "
    "tin thì nêu giả định đã dùng. Không hứa sẽ làm thêm việc gì sau đó."
)
ROUTINE_HINT = (
    "Đây là tác vụ định kỳ người dùng đã đặt, chạy tự động theo lịch (không phải tin họ "
    "vừa nhắn). Đi thẳng vào điều đáng chú ý, ngắn gọn, có đầu mục; không chào hỏi dài. "
    f"Nếu không có gì mới hoặc đáng báo, chỉ trả lời đúng một từ {NO_REPLY}."
)


def enabled() -> bool:
    return env_on("BACKGROUND_JOBS_ENABLED")


def members_allowed() -> bool:
    """Thành viên Zalo (role user) được giao /viec không - mặc định có."""
    return env_on("BACKGROUND_JOBS_MEMBERS")


def job_timeout_sec() -> int:
    return env_int("BACKGROUND_JOB_TIMEOUT_SEC", 600, 60, 1800)


def worker_count() -> int:
    """Số việc nền chạy cùng lúc toàn bot (không vượt MAX_CONCURRENT_TURNS)."""
    return min(env_int("BACKGROUND_JOBS_CONCURRENCY", 1, 1, 3), concurrency.max_turns())


def per_user_cap() -> int:
    return env_int("BACKGROUND_JOBS_MAX_PER_USER", 3, 1, 10)


# ─── Người nhận ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Recipient:
    """Người sở hữu việc, dựng lại lúc chạy (vai trò Zalo có thể đã đổi)."""

    user_id: int
    channel: str
    is_admin: bool = False  # admin Zalo: persona admin; chủ bot nhận ra qua user_id
    owner: bool = False  # chủ bot (Telegram/Zoom) hoặc Zalo controller
    address_form: str | None = None

    @property
    def turn_key(self) -> str:
        return concurrency.OWNER_TURN_KEY if self.user_id > 0 else f"zalo:{self.user_id}"

    @property
    def member(self) -> bool:
        return self.user_id < 0 and not self.is_admin

    def audience(self) -> reply_audience.ReplyAudience:
        owner = self.owner

        async def check() -> bool:
            return owner

        return reply_audience.ReplyAudience(address_form=self.address_form, owner_check=check)

    def forms(self) -> tuple[str, str]:
        if self.user_id > 0:
            return "anh", "em"
        return reply_audience.forms(self.address_form)


async def _zalo_controller() -> str:
    env = os.getenv("ZALO_CONTROLLER_ID", "").strip()
    if env:
        return env
    from channels import zalo_session

    try:
        return await zalo_session.load_controller()
    except Exception:
        logger.warning("background_jobs: không đọc được Zalo controller.", exc_info=True)
        return ""


async def resolve(user_id: int, channel: str) -> Recipient | None:
    """Chủ bot (id dương = ALLOWED_USER_ID) hoặc thành viên Zalo còn hoạt động."""
    if user_id > 0:
        if not config.is_owner_user_id(user_id):
            return None
        return Recipient(
            user_id, channel if channel in ("telegram", "zoom") else "telegram", owner=True
        )
    from channels import zalo_users

    for user in await zalo_users.list_users():
        if user.internal_user_id == user_id:
            if not user.is_active:
                return None
            controller = await _zalo_controller()
            return Recipient(
                user_id,
                "zalo",
                is_admin=user.is_admin,
                owner=bool(controller) and user.external_id == controller,
                address_form=address.normalize(getattr(user, "address_form", None)),
            )
    return None


# ─── /viec ──────────────────────────────────────────────────────────────────

_INDEX_RE = re.compile(r"#?(\d{1,9})")
_LIST_WORDS = {"", "ds", "list", "xem", "danh sach"}
_CANCEL_WORDS = {"huy", "xoa", "dung", "cancel"}
_SHOW_WORDS = {"xem", "kq", "ketqua"}
_HELP_WORDS = {"help", "huong dan", "?"}


def parse_argument(argument: str) -> tuple[str, str]:
    """("list" | "cancel" | "show" | "help" | "submit", phần còn lại) của /viec."""
    raw = " ".join((argument or "").split())
    folded = fold(raw)
    if folded in _LIST_WORDS:
        return "list", ""
    if folded in _HELP_WORDS:
        return "help", ""
    head, _, rest = raw.partition(" ")
    fhead = fold(head)
    if _INDEX_RE.fullmatch(rest):
        if fhead in _CANCEL_WORDS:
            return "cancel", rest.lstrip("#")
        if fhead in _SHOW_WORDS:
            return "show", rest.lstrip("#")
    return "submit", raw


def is_submission(argument: str) -> bool:
    """/viec <tham số> có phải giao việc mới không (quota thành viên: lượt nặng)."""
    return parse_argument(argument)[0] == "submit"


def usage_text(you: str = "anh") -> str:
    return (
        f"🧰 /viec <yêu cầu> — giao việc cho em làm ở nền, {you} vẫn nhắn em bình thường; "
        "xong em gửi kết quả.\n"
        "Ví dụ: /viec so sánh 3 gói cước 5G Viettel, VinaPhone, MobiFone kèm giá\n"
        "/viec — xem việc đang chạy/đã xong · /viec xem <số> — xem kết quả · "
        "/viec huy <số> — huỷ"
    )


async def handle_command(
    command: str, argument: str, user_id: int, channel: str, *, is_admin: bool = False
) -> list[str] | None:
    """/viec dùng chung mọi kênh (handler Telegram, channel_command_service)."""
    if command != "/viec":
        return None
    action, rest = parse_argument(argument)
    you, _ = _forms(user_id)
    if action == "help":
        return [usage_text(you)]
    if action == "list":
        return [await list_text(user_id)]
    if action == "show":
        return [await show_text(user_id, int(rest))]
    if action == "cancel":
        return [await cancel(user_id, int(rest))]
    return [await submit(user_id, channel, rest, is_admin=is_admin)]


def _forms(user_id: int) -> tuple[str, str]:
    if config.is_owner_user_id(user_id):
        return "anh", "em"
    return reply_audience.forms()


async def submit(user_id: int, channel: str, prompt: str, *, is_admin: bool = False) -> str:
    you, me = _forms(user_id)
    if not enabled():
        return f"{me.capitalize()} chưa nhận việc chạy nền lúc này nha {you}."
    if user_id < 0 and not is_admin and not members_allowed():
        return (
            f"Việc chạy nền chưa mở cho {you} nha. {you.capitalize()} cứ nhắn thẳng câu hỏi, "
            f"{me} trả lời luôn."
        )
    prompt = " ".join((prompt or "").split())
    if len(prompt) < PROMPT_MIN_CHARS:
        return usage_text(you)
    if len(prompt) > PROMPT_MAX_CHARS:
        return (
            f"Yêu cầu dài quá ({len(prompt)} ký tự, tối đa {PROMPT_MAX_CHARS}). "
            f"{you.capitalize()} rút gọn giúp {me} nha."
        )
    job_id, reason = await db.bg_job_insert(
        user_id, channel, prompt, user_cap=per_user_cap(), global_cap=GLOBAL_QUEUE_CAP
    )
    if job_id is None:
        if reason == "user_cap":
            return (
                f"{you.capitalize()} đang có {per_user_cap()} việc nền chưa xong rồi. Đợi {me} "
                f"làm xong hoặc /viec huy <số> rồi giao tiếp nha."
            )
        return f"Hàng việc nền đang đầy, {you} giao lại sau ít phút giúp {me} nha."
    wake()
    try:
        ahead = await db.bg_job_position(job_id)
    except Exception:
        ahead = 0
    queue = f" (có {ahead} việc đang làm trước)" if ahead else ""
    return (
        f"⏳ {me.capitalize()} nhận việc #{job_id} rồi, {me} làm xong báo {you} nha{queue}. "
        f"Trong lúc chờ {you} cứ nhắn {me} bình thường.\n"
        f"/viec xem {job_id} · /viec huy {job_id}"
    )


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _vn(value) -> dt.datetime | None:
    if not isinstance(value, dt.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(VN_TZ)


def _minutes_since(value, now: dt.datetime | None = None) -> int:
    start = _vn(value)
    if start is None:
        return 0
    now = now or dt.datetime.now(VN_TZ)
    return max(0, int((now - start).total_seconds() // 60))


_STATUS_LABEL = {
    QUEUED: "🕒 đang chờ",
    RUNNING: "⏳ đang làm",
    SUCCEEDED: "✅ xong",
    FAILED: "❌ lỗi",
    CANCELLED: "🛑 đã huỷ",
    TIMED_OUT: "⏱ quá giờ",
}


def status_line(row: dict, now: dt.datetime | None = None) -> str:
    status = str(row.get("status") or "")
    label = _STATUS_LABEL.get(status, status)
    if status == RUNNING:
        label += f" ({_minutes_since(row.get('started_at'), now)} phút)"
    elif status in (SUCCEEDED, FAILED, TIMED_OUT, CANCELLED):
        finished = _vn(row.get("finished_at"))
        if finished:
            label += f" {finished:%H:%M %d/%m}"
    return f"#{row['id']} {label} - «{_clip(str(row.get('prompt') or ''), _LABEL_CHARS)}»"


async def list_text(user_id: int) -> str:
    you, me = _forms(user_id)
    rows = await db.bg_job_list(user_id, 10)
    if not rows:
        return f"{me.capitalize()} chưa có việc nền nào của {you}.\n\n" + usage_text(you)
    lines = [f"📋 Việc nền của {you}:"]
    lines += [f"• {status_line(row)}" for row in rows]
    lines.append("/viec xem <số> · /viec huy <số> · /viec <yêu cầu> để giao việc mới")
    return "\n".join(lines)


async def show_text(user_id: int, job_id: int) -> str:
    you, me = _forms(user_id)
    row = await db.bg_job_get(user_id, job_id)
    if row is None:
        return f"{me.capitalize()} không thấy việc #{job_id} nào của {you}."
    head = status_line(row)
    if row.get("status") == SUCCEEDED:
        if row.get("no_reply"):
            return f"{head}\n\n(Lần chạy này không có gì đáng báo nên {me} không gửi.)"
        return f"{head}\n\n{row.get('result') or ''}".strip()
    if row.get("status") in (FAILED, TIMED_OUT):
        reason = _failure_reason(row, owner=config.is_owner_user_id(user_id), me=me)
        return f"{head}\n\n{reason.capitalize()}."
    return head


async def cancel(user_id: int, job_id: int) -> str:
    you, me = _forms(user_id)
    previous = await db.bg_job_cancel(user_id, job_id)
    if previous is None:
        return f"{me.capitalize()} không thấy việc #{job_id} nào của {you} đang chờ hay đang làm."
    cancel_local(job_id)
    return f"🛑 {me.capitalize()} huỷ việc #{job_id} rồi nha {you}."


# ─── Bộ chạy ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Outcome:
    status: str
    result: str | None = None
    error: str | None = None
    no_reply: bool = False
    tokens: int | None = None
    provider: str | None = None


_running: dict[int, asyncio.Task] = {}
_workers: list[asyncio.Task] = []
_janitor_task: asyncio.Task | None = None
_wake_event: asyncio.Event | None = None
_started = False
# Gọi trong vòng dọn (services/routines.py đăng ký vòng xếp tác vụ tới giờ).
_tick_hooks: list[Callable[[], Awaitable[None]]] = []


def register_tick(hook: Callable[[], Awaitable[None]]) -> None:
    if hook not in _tick_hooks:
        _tick_hooks.append(hook)


def started() -> bool:
    return _started


def wake() -> None:
    if _wake_event is not None:
        _wake_event.set()


def cancel_local(job_id: int) -> bool:
    """Huỷ TASK CON đang chạy việc ``job_id`` trong process này (nếu có)."""
    task = _running.get(job_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def running_count() -> int:
    return sum(1 for task in _running.values() if not task.done())


def is_no_reply(text: str) -> bool:
    """Model báo "không có gì đáng báo": câu mở đầu bằng NO_REPLY (cho phép dấu câu/
    định dạng bao quanh, kèm 1 câu giải thích ngắn)."""
    stripped = re.sub(r"^[\s`*_\"'“”>.\-]+", "", text or "")
    head = stripped[: len(NO_REPLY)].upper().replace(" ", "_")
    return head == NO_REPLY and len(stripped) <= 200


async def _stock_grounding(prompt: str) -> str:
    """Số liệu giá chuẩn của mã cổ phiếu nhắc tới (như nhánh cổ phiếu của chat)."""
    from stock import analysis as stock_analysis

    try:
        symbols = await stock_analysis.find_valid_symbols(prompt)
        if not symbols:
            return ""
        return await stock_analysis.build_price_grounding(symbols[:3])
    except Exception:
        logger.warning("background_jobs: không lấy được số liệu cổ phiếu.", exc_info=True)
        return ""


async def run_ai_turn(job: dict, recipient: Recipient, budget_sec: float):
    """1 lượt AI nền như chat thường của đúng người đó, KHÔNG ghi dữ liệu."""
    from ai.timeouts import turn_deadline
    from services import channel_chat_service, usage_quota

    routine = job.get("kind") == KIND_ROUTINE
    prompt = str(job.get("prompt") or "")
    with turn_deadline(budget_sec):
        grounding = await _stock_grounding(prompt)
        return await channel_chat_service.run_chat_turn(
            recipient.user_id,
            prompt,
            grounding=grounding,
            channel=recipient.channel,
            is_admin=recipient.is_admin,
            address_form=recipient.address_form,
            allow_side_effects=False,
            style_hint=ROUTINE_HINT if routine else JOB_HINT,
            deadline_sec=budget_sec,
            record_history=False,
            telemetry_kind=usage_quota.KIND_ROUTINE if routine else usage_quota.KIND_JOB,
        )


def _outcome(job: dict, turn, tokens: int | None) -> Outcome:
    provider = str(getattr(turn, "provider", "") or "") or None
    if getattr(turn, "failed", False):
        status = TIMED_OUT if getattr(turn, "timed_out", False) else FAILED
        error = str(getattr(turn, "error_detail", "") or "") or (
            "timeout" if status == TIMED_OUT else "error"
        )
        return Outcome(status, error=error[:300], tokens=tokens, provider=provider)
    text = str(getattr(turn, "text", "") or "").strip()
    if not text:
        return Outcome(FAILED, error="empty", tokens=tokens, provider=provider)
    if job.get("kind") == KIND_ROUTINE and is_no_reply(text):
        return Outcome(SUCCEEDED, no_reply=True, tokens=tokens, provider=provider)
    if len(text) > RESULT_MAX_CHARS:
        text = text[: RESULT_MAX_CHARS - 40].rstrip() + "\n\n… (em đã cắt bớt cho vừa tin)"
    return Outcome(SUCCEEDED, result=text, tokens=tokens, provider=provider)


async def _execute(job: dict, recipient: Recipient) -> Outcome:
    trace.detach()
    timeout = float(job_timeout_sec())
    async with concurrency.background_slot(recipient.turn_key):
        with (
            trace.turn(f"bgjob:{job['id']}", channel=recipient.channel, user_kind="job"),
            reply_audience.use(recipient.audience()),
        ):
            try:
                turn = await asyncio.wait_for(
                    run_ai_turn(job, recipient, timeout), timeout + _HARD_GRACE_SEC
                )
            except TimeoutError:
                return Outcome(TIMED_OUT, error="hard_timeout")
            metrics = trace.metrics()
            tokens = metrics.tokens if metrics is not None else None
    return _outcome(job, turn, tokens)


async def _heartbeat(job_id: int, child: asyncio.Task) -> None:
    """Gia hạn lease; việc đã bị huỷ (dòng DB không còn running) thì huỷ task con."""
    while not child.done():
        await asyncio.sleep(HEARTBEAT_SEC)
        try:
            alive = await db.bg_job_renew(job_id, LEASE_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("background_jobs: không gia hạn được việc #%s.", job_id, exc_info=True)
            continue
        if not alive:
            child.cancel()
            return


async def run_job(job: dict) -> Outcome:
    """Chạy 1 việc đã nhận (status running): task con huỷ được, lưu kết quả, gửi tin."""
    job_id = int(job["id"])
    recipient = await resolve(int(job["telegram_user_id"]), str(job.get("channel") or ""))
    if recipient is None:
        outcome = Outcome(FAILED, error="recipient_gone")
        await _store(job_id, outcome)
        return outcome
    child = asyncio.create_task(_execute(job, recipient))
    _running[job_id] = child
    beat = asyncio.create_task(_heartbeat(job_id, child))
    try:
        try:
            outcome = await child
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # tắt app: lease hết hạn -> chạy lại ở lần khởi động sau
            outcome = Outcome(CANCELLED)
        except Exception as exc:
            logger.exception("background_jobs: việc #%s lỗi.", job_id)
            outcome = Outcome(FAILED, error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        beat.cancel()
        _running.pop(job_id, None)
    if outcome.status == CANCELLED:
        return outcome  # dòng DB đã là 'cancelled' (lệnh huỷ ghi trước)
    if await _store(job_id, outcome):
        await _after_success(job, recipient, outcome)
        await deliver(job, recipient, outcome)
    return outcome


async def _store(job_id: int, outcome: Outcome) -> bool:
    try:
        return await db.bg_job_finish(
            job_id,
            outcome.status,
            result=outcome.result,
            error=outcome.error,
            no_reply=outcome.no_reply,
            tokens=outcome.tokens,
            provider=outcome.provider,
        )
    except Exception:
        logger.warning("background_jobs: không lưu được kết quả việc #%s.", job_id, exc_info=True)
        return False


def _label(job: dict) -> str:
    if job.get("kind") == KIND_ROUTINE:
        return f"Tác vụ định kỳ #{job.get('routine_id') or '?'}"
    return f"Việc nền #{job['id']}"


async def _after_success(job: dict, recipient: Recipient, outcome: Outcome) -> None:
    """Lưu kết quả vào lịch sử chat (câu hỏi nối tiếp "chi tiết hơn ý 2" có ngữ cảnh)."""
    if outcome.status != SUCCEEDED or not outcome.result:
        return
    from services import conversation_log

    await conversation_log.record_exchange(
        recipient.user_id,
        f"[{_label(job)}] {job.get('prompt') or ''}",
        outcome.result,
        conversation_log.KIND_BACKGROUND,
        read_only=True,
    )


def _failure_reason(row: dict, *, owner: bool, me: str = "em") -> str:
    """Lý do thất bại cho người dùng; chi tiết kỹ thuật chỉ dành cho chủ bot."""
    error = str(row.get("error") or "")
    if row.get("status") == TIMED_OUT:
        return f"chạy quá {job_timeout_sec() // 60} phút nên {me} dừng"
    if error == "interrupted":
        return "bị gián đoạn vì bot khởi động lại"
    if error == "empty":
        return "AI không trả nội dung"
    reason = "AI đang lỗi"
    if owner and error and error != "error":
        reason += f" ({_clip(error, 160)})"
    return reason


def compose(job: dict, recipient: Recipient, outcome: Outcome) -> str | None:
    """Tin gửi người dùng cho 1 kết quả (None = không gửi)."""
    you, me = recipient.forms()
    label = _clip(str(job.get("prompt") or ""), _LABEL_CHARS)
    row = {"status": outcome.status, "error": outcome.error}
    if job.get("kind") == KIND_ROUTINE:
        routine_id = job.get("routine_id") or "?"
        if outcome.status == SUCCEEDED:
            if outcome.no_reply or not outcome.result:
                return None
            late = ""
            scheduled = _vn(job.get("scheduled_for"))
            if scheduled and _minutes_since(scheduled) >= 15:
                late = f" (chạy bù lịch {scheduled:%H:%M %d/%m})"
            return f"🔁 Tác vụ định kỳ #{routine_id}: «{label}»{late}\n\n{outcome.result}"
        if outcome.status in (FAILED, TIMED_OUT):
            return (
                f"⚠️ Tác vụ định kỳ #{routine_id} («{label}») lần này {me} chưa chạy được: "
                f"{_failure_reason(row, owner=recipient.owner, me=me)}. Tới lịch sau {me} chạy lại nha."
            )
        return None
    if outcome.status == SUCCEEDED and outcome.result:
        return f"✅ {me.capitalize()} làm xong việc #{job['id']} {you} giao: «{label}»\n\n{outcome.result}"
    if outcome.status in (FAILED, TIMED_OUT):
        return (
            f"😥 Việc #{job['id']} («{label}») {me} chưa làm xong được: "
            f"{_failure_reason(row, owner=recipient.owner, me=me)}. {you.capitalize()} giao lại giúp "
            f"{me} sau ít phút nha."
        )
    return None


async def deliver(job: dict, recipient: Recipient, outcome: Outcome) -> bool:
    text = compose(job, recipient, outcome)
    if not text:
        return False
    source = "routine" if job.get("kind") == KIND_ROUTINE else "background_job"
    return await notify_hub.send(
        recipient.channel, recipient.user_id, text, source=source, dedup=f"bgjob:{job['id']}"
    )


async def recover() -> int:
    """Vòng dọn: việc running hết lease -> chạy lại (lần 1) hoặc failed + báo."""
    failed = await db.bg_job_recover(MAX_ATTEMPTS)
    for row in failed:
        recipient = await resolve(int(row["telegram_user_id"]), str(row.get("channel") or ""))
        if recipient is not None:
            await deliver(row, recipient, Outcome(FAILED, error="interrupted"))
    wake()
    return len(failed)


async def _wait_wake(seconds: float) -> None:
    event = _wake_event
    if event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(event.wait(), seconds)
    except TimeoutError:
        pass
    event.clear()


async def _worker_loop(index: int) -> None:
    trace.detach()
    while True:
        try:
            job = await db.bg_job_claim(LEASE_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("background_jobs: worker %d không nhận được việc.", index, exc_info=True)
            job = None
        if job is None:
            await _wait_wake(POLL_SEC)
            continue
        try:
            await run_job(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background_jobs: worker %d lỗi khi chạy việc.", index)


async def _janitor_loop() -> None:
    trace.detach()
    while True:
        started_at = time.monotonic()
        try:
            with trace.job("background_jobs"):
                await recover()
                for hook in list(_tick_hooks):
                    await hook()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("background_jobs: vòng dọn lỗi.", exc_info=True)
        await asyncio.sleep(max(1.0, JANITOR_SEC - (time.monotonic() - started_at)))


def start() -> None:
    global _started, _wake_event, _janitor_task
    if not enabled():
        return
    _started = True
    _wake_event = asyncio.Event()
    while len([t for t in _workers if not t.done()]) < worker_count():
        _workers.append(asyncio.create_task(_worker_loop(len(_workers))))
    if _janitor_task is None or _janitor_task.done():
        _janitor_task = asyncio.create_task(_janitor_loop())


async def stop() -> None:
    global _started, _janitor_task, _wake_event
    _started = False
    tasks = [t for t in (*_workers, _janitor_task, *_running.values()) if t is not None]
    _workers.clear()
    _janitor_task = None
    _wake_event = None
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _running.clear()


# ─── /chuy và /admin ─────────────────────────────────────────────────────────


@attention.register_section("background_jobs", 80, label="việc nền")
async def _attention_jobs(ctx: attention.AttentionContext) -> str | None:
    rows = [r for r in await db.bg_job_active(ctx.user_id) if r.get("kind") == KIND_JOB]
    if not rows:
        return None
    lines = [f"• {status_line(row, ctx.now)}" for row in rows[:6]]
    return "\n".join([f"⏳ Việc nền {ctx.me} đang làm cho {ctx.you}:", *lines])


def _iso(value) -> str | None:
    found = _vn(value)
    return found.isoformat(timespec="seconds") if found else None


async def admin_overview() -> dict:
    """Trang /admin: việc gần nhất (đang chạy/chờ trước) + tác vụ định kỳ."""
    from services import recurrence, routines

    jobs = []
    for row in await db.bg_job_recent_all(30):
        started, finished = _vn(row.get("started_at")), _vn(row.get("finished_at"))
        jobs.append(
            {
                "id": row["id"],
                "user_id": row["telegram_user_id"],
                "channel": row["channel"],
                "kind": row["kind"],
                "routine_id": row.get("routine_id"),
                "status": row["status"],
                "prompt": _clip(str(row.get("prompt") or ""), _LABEL_CHARS),
                "created_at": _iso(row.get("created_at")),
                "duration_sec": int((finished - started).total_seconds())
                if started and finished
                else None,
                "tokens": row.get("tokens"),
                "provider": row.get("provider"),
                "attempts": row.get("attempts"),
                "no_reply": bool(row.get("no_reply")),
                "error": _clip(str(row.get("error") or ""), 120) or None,
            }
        )
    items = [
        {
            "id": row["id"],
            "user_id": row["telegram_user_id"],
            "channel": row["channel"],
            "prompt": _clip(str(row.get("prompt") or ""), _LABEL_CHARS),
            "schedule": recurrence.describe(row.get("recurrence") or {}),
            "status": row["status"],
            "next_run_at": _iso(row.get("next_run_at")),
        }
        for row in await db.routine_all(50)
    ]
    return {
        "enabled": enabled(),
        "running_here": running_count(),
        "concurrency": worker_count(),
        "timeout_sec": job_timeout_sec(),
        "jobs": jobs,
        "routines": items,
        "routine_runs_today": await routines.runs_today(),
        "routine_max_runs_per_day": routines.max_runs_per_day(),
    }
