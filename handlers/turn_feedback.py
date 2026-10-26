"""Phản hồi Telegram trong lúc chờ lượt trả lời dài.

- StatusLine: lượt chat chạy quá TELEGRAM_STATUS_DELAY_SEC giây thì gửi 1 dòng
  trạng thái giọng Lan Anh theo giai đoạn (services/turn_progress.py); câu trả
  lời sửa đè lên đúng dòng đó (tg_format.reply_rich_edit_first).
- Reaction: tin tới lúc khoá lượt của chủ bot đang bận được thả 👀 ngay ở
  webhook; xử lý xong đổi 👌, hỏng hẳn đổi 😢. Telegram chỉ nhận danh sách emoji
  cố định (✅/⚠️ KHÔNG hợp lệ -> REACTION_INVALID).
- Lối nhanh trước inbox: /huy huỷ phần việc AI đang chạy (services.concurrency.
  cancel_turn) và lệnh chỉ đọc (services/turn_control.py) chạy ngay khi khoá
  đang bận - cả 2 worker inbox có thể đang kẹt sau khoá đó.
- DraftStreamer: TELEGRAM_STREAM_DRAFTS=1 thì hiện chữ đang sinh bằng
  sendMessageDraft (Bot API 9.5+, gọi qua bot.do_api_request).

Mọi lời gọi Telegram ở đây là best-effort: lỗi chỉ ghi log, không bao giờ làm
hỏng lượt trả lời hay khiến inbox thử lại sự kiện.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TypeVar

from core import config
from services import concurrency, turn_control, turn_progress
from services.concurrency import OWNER_TURN_KEY

logger = logging.getLogger(__name__)

T = TypeVar("T")

STATUS_TEXTS = {
    turn_progress.THINK: "✍️ Em đang soạn câu trả lời, anh đợi em xíu nha…",
    turn_progress.SEARCH: "🔎 Em đang tra web xíu nha anh…",
    turn_progress.READ: "📖 Em đang đọc trang anh gửi nha…",
    turn_progress.RAG: "📚 Em đang lục tài liệu riêng của anh nha…",
    turn_progress.STOCK: "📊 Em đang xem số liệu cổ phiếu nha anh…",
}

REACTION_QUEUED = "👀"
REACTION_DONE = "👌"
REACTION_FAILED = "😢"
REACTION_CANCEL_ACK = "🫡"

# Trần RAM: số tin đang mang 👀, số update đã đi lối nhanh (chống xử lý 2 lần khi
# Telegram gửi lại webhook), số task lối nhanh chạy cùng lúc.
_REACTED_MAX = 128
_FAST_SEEN_MAX = 256
MAX_FAST_TASKS = 4

_reacted: OrderedDict[int, tuple[int, int]] = OrderedDict()
_fast_seen: OrderedDict[object, None] = OrderedDict()
_tasks: set[asyncio.Task] = set()
_fast_tasks: set[asyncio.Task] = set()


def status_text(phase: str | None) -> str:
    return STATUS_TEXTS.get(phase or turn_progress.THINK, STATUS_TEXTS[turn_progress.THINK])


def _spawn(coro: Awaitable, bucket: set[asyncio.Task] | None = None) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    for target in (_tasks, bucket):
        if target is not None:
            target.add(task)
            task.add_done_callback(target.discard)
    return task


async def stop() -> None:
    """Huỷ task nền còn lại (shutdown)."""
    pending = [task for task in _tasks if not task.done()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    _tasks.clear()
    _fast_tasks.clear()


# ─── Dòng trạng thái ─────────────────────────────────────────────────────────


class StatusLine:
    """Sau ``delay`` giây lượt vẫn chạy thì gửi 1 dòng trạng thái. stop() trả
    tin đã gửi (để edit câu trả lời vào) hoặc None. Lời gửi đang dở được chờ
    xong chứ không huỷ giữa chừng - tránh tin trạng thái mồ côi."""

    def __init__(self, message, tracker: turn_progress.Tracker, delay: float) -> None:
        self.message = message
        self.tracker = tracker
        self.delay = delay
        self.sent = None
        self._task: asyncio.Task | None = None
        self._sending: asyncio.Future | None = None

    def start(self) -> None:
        if self.delay > 0 and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await asyncio.sleep(self.delay)
        self._sending = asyncio.ensure_future(
            self.message.reply_text(status_text(self.tracker.phase))
        )
        self.sent = await asyncio.shield(self._sending)

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.sent is None and self._sending is not None:
            try:
                self.sent = await asyncio.wait_for(asyncio.shield(self._sending), 10)
            except Exception:
                logger.info("Không gửi được dòng trạng thái (bỏ qua).", exc_info=True)
                self.sent = None
        return self.sent


# ─── Reaction ────────────────────────────────────────────────────────────────


async def _set_reaction(bot, chat_id: int, message_id: int, emoji: str | None) -> None:
    try:
        await bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=emoji)
    except Exception as exc:
        logger.info("Không đặt được reaction %s: %s", emoji, exc)


def _owner_message(update):
    message = getattr(update, "message", None)
    user = getattr(update, "effective_user", None)
    if message is None or user is None or user.id != config.ALLOWED_USER_ID:
        return None
    return message


def mark_queued(update, bot) -> None:
    """Gọi ở webhook ngay sau khi lưu inbox: khoá lượt chủ bot đang bận thì thả 👀."""
    if not config.TELEGRAM_BUSY_REACTIONS or not concurrency.is_busy(OWNER_TURN_KEY):
        return
    message = _owner_message(update)
    if message is None:
        return
    _reacted[update.update_id] = (message.chat_id, message.message_id)
    while len(_reacted) > _REACTED_MAX:
        _reacted.popitem(last=False)
    _spawn(_set_reaction(bot, message.chat_id, message.message_id, REACTION_QUEUED))


def finish_queued(update_id: int, bot, *, ok: bool, final: bool = True) -> None:
    """Xử lý xong tin đã mang 👀: 👌 khi ổn, 😢 khi hỏng hẳn. Lỗi còn được inbox
    thử lại (final=False) thì giữ 👀 tới lần thử sau."""
    if not ok and not final:
        return
    entry = _reacted.pop(update_id, None)
    if entry is not None:
        _spawn(_set_reaction(bot, *entry, REACTION_DONE if ok else REACTION_FAILED))


# ─── Lối nhanh trước inbox: /huy và lệnh chỉ đọc ─────────────────────────────


def seen_once(key) -> bool:
    """True nếu khoá (update_id Telegram, "zoom:<event_id>") đã đi lối nhanh rồi -
    webhook gửi lại không xử lý 2 lần. Lần đầu thì ghi nhận và trả False."""
    if key in _fast_seen:
        return True
    _fast_seen[key] = None
    while len(_fast_seen) > _FAST_SEEN_MAX:
        _fast_seen.popitem(last=False)
    return False


_seen = seen_once


def spawn(coro: Awaitable) -> asyncio.Task:
    """Chạy nền có theo dõi (huỷ khi shutdown), lỗi chỉ ghi log."""
    return _spawn(_logged(coro))


async def _logged(coro: Awaitable) -> None:
    try:
        await coro
    except Exception:
        logger.warning("Việc nền ở lối nhanh lỗi.", exc_info=True)


async def _ack_cancel(bot, message, cancelled: bool) -> None:
    if cancelled:
        # Lượt bị huỷ tự trả lời "em dừng rồi"; ở đây chỉ báo đã nhận /huy.
        await _set_reaction(bot, message.chat_id, message.message_id, REACTION_CANCEL_ACK)
        return
    try:
        await message.reply_text(turn_control.nothing_to_cancel_text(owner=True))
    except Exception:
        logger.info("Không trả lời được /huy.", exc_info=True)


def maybe_fast_path(update, bot, run_unlocked: Callable[[object], Awaitable[None]]) -> bool:
    """True nếu update đã được xử lý ở lối nhanh (webhook KHÔNG đưa vào inbox).

    /huy luôn đi lối nhanh; lệnh chỉ đọc chỉ đi lối nhanh khi khoá lượt chủ bot
    đang bận (lúc rảnh vẫn qua inbox để giữ thứ tự và được thử lại)."""
    message = _owner_message(update)
    text = getattr(message, "text", None) if message is not None else None
    if not text:
        return False
    if turn_control.is_cancel(text):
        if not _seen(update.update_id):
            cancelled = concurrency.cancel_turn(OWNER_TURN_KEY)
            _spawn(_ack_cancel(bot, message, cancelled))
        return True
    if turn_control.is_readonly(text) and concurrency.is_busy(OWNER_TURN_KEY):
        if len(_fast_tasks) >= MAX_FAST_TASKS:
            return False
        if not _seen(update.update_id):
            _spawn(_run_logged(run_unlocked, update), _fast_tasks)
        return True
    return False


async def _run_logged(run_unlocked, update) -> None:
    try:
        await run_unlocked(update)
    except Exception:
        logger.warning("Lệnh chỉ đọc ở lối nhanh lỗi (update %s).", update.update_id, exc_info=True)


async def owner_cancellable(message, factory: Callable[[], Awaitable[T]]) -> T | None:
    """Chạy phần việc AI của 1 lượt Telegram không phải chat chữ (chép lời tin
    thoại, hỏi đáp ảnh, viết prompt từ ảnh, đọc tài liệu) trong task con dưới
    khoá chủ bot, để /huy huỷ được như lượt chat (handlers/chat_router._ai_reply).

    Trả kết quả của ``factory`` (không bao giờ là None); bị huỷ -> trả lời "em
    dừng rồi" và trả None - handler dừng, kết thúc bình thường nên inbox không
    chạy lại lượt đã huỷ. Lỗi khác của ``factory`` đi tiếp như gọi thẳng. Phần
    gửi câu trả lời để NGOÀI (như _ai_reply): /huy tới lúc đang gửi không cắt
    ngang tin đã ghi lịch sử."""
    try:
        return await concurrency.run_cancellable(OWNER_TURN_KEY, factory)
    except concurrency.TurnCancelled:
        await message.reply_text(turn_control.cancelled_text(owner=True))
        return None


# ─── Bản nháp chữ đang sinh (sendMessageDraft) ───────────────────────────────

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
DRAFT_MIN_INTERVAL_SEC = 1.5
# Bản nháp tự biến mất sau ~30 giây không cập nhật: gửi lại chữ cũ định kỳ.
DRAFT_HEARTBEAT_SEC = 20.0
DRAFT_MAX_CHARS = 4096


def draft_text(text: str) -> str | None:
    """Chữ an toàn để hiện ở bản nháp: bỏ link (bản nháp không qua bộ lọc link),
    bỏ ký hiệu markdown; có chữ Trung/Nhật/Hàn (provider lỗi, sẽ bị loại) -> None."""
    if _CJK_RE.search(text or ""):
        return None
    from channels.zalo_text import to_plain_text

    cleaned = to_plain_text(_URL_RE.sub("…", text or ""))
    return cleaned[-DRAFT_MAX_CHARS:] if cleaned else None


class DraftStreamer:
    """Đẩy chữ đang sinh lên bản nháp, tối đa 1 lần / 1,5 giây. Telegram tự xoá
    bản nháp khi bot gửi tin cuối; stop() chờ lời gửi đang dở xong để bản nháp
    không hiện lại SAU câu trả lời."""

    def __init__(self, bot, chat_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = random.randint(1, 2**31 - 1)
        self.latest = ""
        self._sent: str | None = None
        self._wake = asyncio.Event()
        self._stopped = asyncio.Event()
        self._stopping = False
        self._disabled = False
        self._task: asyncio.Task | None = None

    def push(self, text: str) -> None:
        cleaned = draft_text(text)
        if cleaned:
            self.latest = cleaned
            self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _send(self, text: str) -> None:
        try:
            await self.bot.do_api_request(
                "sendMessageDraft",
                {"chat_id": self.chat_id, "draft_id": self.draft_id, "text": text},
            )
            self._sent = text
        except Exception as exc:
            from telegram.error import BadRequest

            if isinstance(exc, BadRequest):
                # Bot API cũ/không hỗ trợ: tắt bản nháp cho lượt này.
                self._disabled = True
            logger.info("Không gửi được bản nháp: %s", exc)

    async def _loop(self) -> None:
        # Bản nháp rỗng: Telegram hiện "Thinking..." ngay từ đầu.
        await self._send("")
        while not self._stopping and not self._disabled:
            try:
                await asyncio.wait_for(self._wake.wait(), DRAFT_HEARTBEAT_SEC)
            except TimeoutError:
                pass
            self._wake.clear()
            if self._stopping:
                break
            await self._send(self.latest)
            try:  # giãn cách giữa 2 lần cập nhật, nhưng stop() thì thoát ngay
                await asyncio.wait_for(self._stopped.wait(), DRAFT_MIN_INTERVAL_SEC)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopping = True
        self._stopped.set()
        self._wake.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), 5)
        except Exception:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
