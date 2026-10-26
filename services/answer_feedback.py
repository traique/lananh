"""Đánh giá câu trả lời chat và làm lại câu trả lời mới nhất.

- Nút 👍/👎 dưới câu trả lời Telegram, lệnh /sai [lý do] trên mọi kênh: ghi bảng
  answer_feedback (migrations/021) kèm provider/model đã trả lời và bản chụp câu
  hỏi/câu trả lời (cắt 2000 ký tự) - prompts chỉ giữ 20 dòng/người nên không dựa
  vào nó lâu dài. Mỗi người chỉ đánh giá được câu trả lời của CHÍNH mình; dữ liệu
  Zalo (id âm) tách hẳn Telegram.
- /danhgia [Nd] (chủ bot): số 👍/👎 theo provider/model + vài 👎 gần đây của chính
  chủ bot (không hiện câu hỏi của thành viên Zalo).
- 🔄 Thử lại / ✂️ Ngắn hơn: chỉ câu trả lời chat MỚI NHẤT, câu không có ý định
  ghi dữ liệu (nhắc việc, ghi chú, chi tiêu...) và không kèm số liệu cổ phiếu.
  Xoá cặp lịch sử cũ rồi chạy lại run_chat_turn với tool ghi dữ liệu bị chặn và
  event_key của tin gốc - không bao giờ đặt trùng nhắc việc/ghi chú.

RAM: bản chụp gần đây giữ tối đa SNAPSHOT_MAX lượt (OrderedDict có trần).
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass

from core import address
from core import database as db
from services import reply_audience
from services.reminder_delivery import current_target, notification_target

logger = logging.getLogger(__name__)

SNAPSHOT_MAX = 64
TEXT_MAX = 2000
DEFAULT_DAYS = 30
SHORTER_HINT = (
    "YÊU CẦU LẦN NÀY: người dùng bấm “Ngắn hơn” - trả lời lại câu hỏi vừa rồi NGẮN GỌN "
    "hơn hẳn (tối đa 3-4 câu hoặc vài gạch đầu dòng), giữ đúng ý chính."
)
RETRY_HINT = (
    "YÊU CẦU LẦN NÀY: người dùng bấm “Thử lại” vì chưa ưng câu trả lời trước - trả lời "
    "lại theo cách khác, rõ ràng và sát câu hỏi hơn."
)


@dataclass(frozen=True)
class AnswerSnapshot:
    prompt_id: int
    user_id: int
    channel: str
    prompt_text: str
    answer_text: str
    provider: str = ""
    model: str = ""
    regenerable: bool = False
    quote_text: str | None = None
    event_key: str | None = None
    # Cặp ĐÚNG như chat_messages đang lưu (conversation_log rút gọn câu hỏi 2000,
    # câu trả lời 1500 ký tự) - tính từ bản đầy đủ trước khi cắt TEXT_MAX, để
    # 🔄/✂️ xoá được cặp cũ của câu trả lời dài.
    history_prompt: str = ""
    history_answer: str = ""


_recent: OrderedDict[int, AnswerSnapshot] = OrderedDict()
_latest: OrderedDict[int, int] = OrderedDict()  # user_id -> prompt_id câu mới nhất


def can_regenerate(text: str, *, grounding: str = "") -> bool:
    """Lượt chat thường làm lại được: không kèm số liệu cổ phiếu, không có ý định
    ghi dữ liệu (chạy lại có thể đặt trùng nhắc việc/ghi chú)."""
    if grounding:
        return False
    from services import tools

    return not tools.side_effect_intent(text)


def _history_pair(snapshot: AnswerSnapshot) -> tuple[str, str]:
    from services import conversation_log

    return (
        snapshot.history_prompt or conversation_log.stored_user_text(snapshot.prompt_text),
        snapshot.history_answer
        or conversation_log.compact_reply(snapshot.answer_text, conversation_log.KIND_CHAT),
    )


def remember_answer(snapshot: AnswerSnapshot) -> None:
    history_prompt, history_answer = _history_pair(snapshot)
    snapshot = dataclasses.replace(
        snapshot,
        prompt_text=snapshot.prompt_text[:TEXT_MAX],
        answer_text=snapshot.answer_text[:TEXT_MAX],
        history_prompt=history_prompt,
        history_answer=history_answer,
    )
    _recent[snapshot.prompt_id] = snapshot
    _recent.move_to_end(snapshot.prompt_id)
    while len(_recent) > SNAPSHOT_MAX:
        _recent.popitem(last=False)
    _latest[snapshot.user_id] = snapshot.prompt_id
    _latest.move_to_end(snapshot.user_id)
    while len(_latest) > SNAPSHOT_MAX:
        _latest.popitem(last=False)


def forget(user_id: int) -> None:
    """/xoadulieu: bỏ bản chụp trong RAM của người này."""
    for prompt_id in [pid for pid, snap in _recent.items() if snap.user_id == user_id]:
        _recent.pop(prompt_id, None)
    _latest.pop(user_id, None)


def latest_regenerable(user_id: int, prompt_id: int) -> AnswerSnapshot | None:
    """Bản chụp nếu ``prompt_id`` đúng là câu trả lời mới nhất và làm lại được."""
    snapshot = _recent.get(prompt_id)
    if snapshot is None or snapshot.user_id != user_id or not snapshot.regenerable:
        return None
    return snapshot if _latest.get(user_id) == prompt_id else None


def is_latest(user_id: int, prompt_id: int) -> bool:
    return _latest.get(user_id) == prompt_id


async def _snapshot(user_id: int, prompt_id: int) -> AnswerSnapshot | None:
    cached = _recent.get(prompt_id)
    if cached is not None and cached.user_id == user_id:
        return cached
    row = await db.answer_feedback_turn(user_id, prompt_id, TEXT_MAX)
    return _from_row(user_id, row) if row else None


def _from_row(user_id: int, row: dict) -> AnswerSnapshot:
    return AnswerSnapshot(
        prompt_id=int(row["prompt_id"]),
        user_id=user_id,
        channel=row.get("channel") or "telegram",
        prompt_text=row.get("prompt_text") or "",
        answer_text=row.get("answer_text") or "",
        provider=row.get("provider") or "",
        model=row.get("model") or "",
    )


async def _store(snapshot: AnswerSnapshot, rating: int, channel: str, reason: str | None) -> None:
    await db.answer_feedback_upsert(
        channel=channel,
        user_id=snapshot.user_id,
        prompt_id=snapshot.prompt_id,
        rating=rating,
        reason=(reason or "").strip()[:300] or None,
        provider=snapshot.provider or None,
        model=snapshot.model or None,
        prompt_text=snapshot.prompt_text,
        answer_text=snapshot.answer_text,
    )


async def rate(
    user_id: int, prompt_id: int, rating: int, *, channel: str, reason: str | None = None
) -> bool:
    """Ghi 👍 (1) / 👎 (-1) cho 1 câu trả lời của chính người đó. False nếu không
    tìm thấy lượt (đã bị dọn khỏi prompts và không còn trong RAM)."""
    snapshot = await _snapshot(user_id, prompt_id)
    if snapshot is None:
        return False
    await _store(snapshot, 1 if rating > 0 else -1, channel, reason)
    return True


def _forms(owner: bool) -> tuple[str, str]:
    if owner:
        return "anh", "em"
    form = address.normalize(reply_audience.current().address_form)
    return (form, address.self_word(form)) if form else ("", "em")


async def report_wrong(user_id: int, reason: str, *, channel: str) -> str:
    """/sai [lý do]: ghi 👎 cho câu trả lời chat gần nhất của người này."""
    you, me = _forms(await reply_audience.is_owner(user_id))
    tail = f" {you}" if you else ""
    snapshot = None
    prompt_id = _latest.get(user_id)
    if prompt_id is not None:
        snapshot = await _snapshot(user_id, prompt_id)
    if snapshot is None:
        row = await db.answer_feedback_last_chat(user_id, TEXT_MAX)
        snapshot = _from_row(user_id, row) if row else None
    if snapshot is None:
        return f"{me.capitalize()} chưa thấy câu trả lời nào gần đây để ghi nhận nha{tail}."
    await _store(snapshot, -1, channel, reason)
    note = f" (lý do: {reason.strip()[:300]})" if reason.strip() else ""
    return (
        f"📝 {me.capitalize()} ghi nhận câu trả lời vừa rồi chưa ổn{note}. "
        f"Cảm ơn{tail} góp ý nha, {me} sẽ cố hơn."
    )


def parse_days(argument: str) -> int:
    match = re.fullmatch(r"\s*(\d{1,3})\s*(?:d|ngay|ngày)?\s*", argument or "")
    return max(1, min(365, int(match.group(1)))) if match else DEFAULT_DAYS


async def stats_text(user_id: int, days: int = DEFAULT_DAYS) -> str:
    """Bảng 👍/👎 theo provider/model (mọi kênh, chỉ số đếm) + 👎 gần đây của chính
    người xem (chủ bot)."""
    rows = await db.answer_feedback_stats(days)
    if not rows:
        return (
            f"📊 Chưa có đánh giá câu trả lời nào trong {days} ngày qua. Bấm 👍/👎 dưới câu "
            "trả lời Telegram, hoặc gõ /sai <lý do> ngay sau câu chưa ổn."
        )
    lines = [f"📊 Đánh giá câu trả lời {days} ngày qua (theo provider):"]
    for row in rows:
        model = f" ({row['model']})" if row.get("model") else ""
        lines.append(f"• {row['provider']}{model}: 👍 {row['up']} · 👎 {row['down']}")
    recent = await db.answer_feedback_recent_down(user_id, days, 5)
    if recent:
        lines.append("")
        lines.append("👎 gần đây:")
        for row in recent:
            when = row["created_at"].strftime("%d/%m") if row.get("created_at") else ""
            reason = f" — lý do: {row['reason']}" if row.get("reason") else ""
            prompt = " ".join((row.get("prompt_text") or "").split())[:80]
            lines.append(f"• {when} {row.get('provider') or '?'}: “{prompt}”{reason}")
    return "\n".join(lines)


async def handle_command(
    command: str, argument: str, user_id: int, channel: str
) -> list[str] | None:
    """/sai, /danhgia cho Telegram/Zalo/Zoom; None nếu không phải lệnh của module."""
    if command == "/sai":
        return [await report_wrong(user_id, argument, channel=channel)]
    if command == "/danhgia":
        if not await reply_audience.is_owner(user_id):
            return ["Lệnh này chỉ dành cho chủ bot."]
        return [await stats_text(user_id, parse_days(argument))]
    return None


async def regenerate(snapshot: AnswerSnapshot, *, shorter: bool):
    """Chạy lại câu hỏi của câu trả lời mới nhất (🔄 Thử lại / ✂️ Ngắn hơn) - gọi
    trong khoá lượt của người đó. Trả ChatTurn."""
    from services import channel_chat_service

    history_prompt, history_answer = _history_pair(snapshot)
    try:
        deleted = await db.delete_last_chat_pair(snapshot.user_id, history_prompt, history_answer)
    except Exception:
        logger.warning("Không xoá được cặp lịch sử cũ trước khi làm lại.", exc_info=True)
    else:
        if not deleted:
            logger.info(
                "Làm lại câu trả lời user_id=%s: cặp lịch sử cũ không còn là cặp mới nhất.",
                snapshot.user_id,
            )
    target = (
        notification_target(
            dataclasses.replace(current_target(snapshot.user_id), event_key=snapshot.event_key)
        )
        if snapshot.event_key
        else nullcontext()
    )
    with target:
        return await channel_chat_service.run_chat_turn(
            snapshot.user_id,
            snapshot.prompt_text,
            channel=snapshot.channel,
            quote_text=snapshot.quote_text,
            allow_side_effects=False,
            style_hint=SHORTER_HINT if shorter else RETRY_HINT,
        )
