"""Duyệt "cách trả lời" do bộ trích nhớ TỰ rút ra (STYLE_RULES_NEED_APPROVAL, mặc định bật).

Trước đây services/memory_service áp ngay quy tắc 'cach_tra_loi' bộ trích xuất
đoán từ hội thoại - đoán sai ("anh thích trả lời thật dài") là mọi lượt sau trả
lời lệch mà người dùng không biết vì sao. Giờ:

- Quy tắc bộ trích xuất đề xuất (thêm/sửa/bỏ dòng) lưu thành ĐỀ XUẤT CHỜ trong
  settings ``style_pending:<user_id>`` (JSON, 1 dòng/người, có trần dòng/ký tự,
  hết hạn sau PENDING_TTL_SEC). Đề xuất mới khi còn đề xuất cũ thì GỘP (diff
  dòng so với quy tắc đang áp lúc đề xuất -> áp được lên quy tắc hiện hành kể cả
  khi người dùng vừa tự dặn thêm).
- Bot hỏi MỘT lần cho mỗi đề xuất, tối đa 1 lần/ngày/người, KHÔNG chen giữa câu
  trả lời khác: tin riêng ngay sau câu trả lời chat. Telegram có nút [Ghi nhớ]
  [Bỏ] (verb "hoc" ở services/action_buttons.py); Zalo/Zoom trả lời "ghi nhớ"/
  "bỏ" ngay sau câu hỏi, hoặc /hoc ok|bo bất cứ lúc nào (mọi kênh).
- Câu người dùng TỰ nói rõ ("/nho cách trả lời: ...", "từ giờ trả lời ngắn
  thôi", "đừng chèn emoji nữa") vẫn áp ngay (``explicit_request``).
- Dòng đã bị "Bỏ" không đề xuất lại (danh sách có trần trong cùng JSON).

Mỗi người (Zalo user tách riêng, kể cả Zalo của chủ bot) có khoá riêng; khoá
nằm trong personal_settings.memory_user_keys nên /forget, /xoadulieu, /zaloxoa
xoá luôn.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from core import database as db
from core.envutil import env_on
from core.text_normalize import fold
from services import personal_settings, reply_audience

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
PENDING_TTL_SEC = 3 * 86_400
_MAX_LINES = 8
_LINE_MAX_CHARS = 160
_MAX_REJECTED = 20
# Trả lời "ghi nhớ"/"bỏ" bằng chữ chỉ tính khi là tin KẾ TIẾP sau câu hỏi, trong
# khoảng này; dict trong RAM có trần (mất khi restart thì vẫn còn /hoc ok|bo).
_AWAIT_SEC = 6 * 3600
_AWAIT_MAX = 256
_awaiting: OrderedDict[int, tuple[int, float]] = OrderedDict()
# user_id -> False khi chắc chắn không có gì cần hỏi (đỡ 1 lần đọc DB mỗi lượt chat).
# 1 worker uvicorn: mọi đề xuất đều ghi qua propose() của process này; restart thì
# chưa biết -> đọc DB 1 lần.
_HINT_MAX = 512
_nothing_to_ask: OrderedDict[int, bool] = OrderedDict()


def _remember(store: OrderedDict, user_id: int, value, limit: int) -> None:
    store[user_id] = value
    store.move_to_end(user_id)
    while len(store) > limit:
        store.popitem(last=False)


NONE_PENDING = "Hiện không có đề xuất cách trả lời nào chờ duyệt."
_HINT_TEXT = 'Nhắn "ghi nhớ" để em áp dụng, "bỏ" để thôi (hoặc /hoc ok, /hoc bo).'
_HINT_BUTTONS = "Bấm nút bên dưới (hoặc /hoc ok, /hoc bo)."

_APPROVE_RE = re.compile(
    r"^(?:ok |oke |da |vang |u |uh )?(?:ghi nho|nho nhe|nho nha|nho di|dong y|duyet|ap dung)"
    r"(?: (?:nhe|nha|di|em|nhe em|nha em|di em|luon))?[\s.!~]*$"
)
_REJECT_RE = re.compile(
    r"^(?:bo|bo di|bo qua|khong can|thoi|dung nho|khong nho)"
    r"(?: (?:nhe|nha|di|em|nhe em|nha em|di em))?[\s.!~]*$"
)
# Người dùng TỰ dặn rõ cách trả lời lâu dài: mốc thời gian + chuyện trình bày.
_EXPLICIT_TIME_RE = re.compile(
    r"\b(?:tu (?:gio|nay|bay gio|lan sau|sau)|lan sau|sau nay|ve sau|moi lan|luon luon|"
    r"lan nao cung|cach tra loi|khi tra loi)\b|\b(?:dung|khong|thoi|bot)\b.{0,40}\bnua\b"
)
_EXPLICIT_STYLE_RE = re.compile(
    r"\b(?:tra loi|rep|tl|noi|viet|ngan|dai|gon|emoji|icon|bieu tuong|gach dau dong|"
    r"markdown|xuong dong|chi tiet|so lieu truoc|tieng anh|tieng viet)\b"
)


def enabled() -> bool:
    return env_on("STYLE_RULES_NEED_APPROVAL")


def explicit_request(user_text: str) -> bool:
    """Lời người dùng tự dặn rõ cách trả lời lâu dài ("từ giờ trả lời ngắn thôi",
    "đừng chèn emoji nữa") - quy tắc rút ra từ đó áp ngay, không cần duyệt."""
    folded = fold(user_text or "")
    return bool(_EXPLICIT_TIME_RE.search(folded) and _EXPLICIT_STYLE_RE.search(folded))


def _key(user_id: int) -> str:
    return personal_settings.style_pending_key(user_id)


_INLINE_BULLET_RE = re.compile(r"\s+[-•]\s+")


def _lines(text: str) -> list[str]:
    """Các quy tắc (1 dòng/quy tắc). Bản lưu cũ bị gộp 1 dòng "- A - B" cũng tách được."""
    out = []
    for raw in (text or "").splitlines():
        for part in _INLINE_BULLET_RE.split(raw):
            line = part.strip().lstrip("-•*·+–— ").strip()
            if line:
                out.append(line[:_LINE_MAX_CHARS])
    return out


def _norm(line: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", fold(line)).split())


def _today() -> str:
    return datetime.now(_VN_TZ).date().isoformat()


def _parse(raw: str | None) -> dict:
    try:
        state = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def _active(state: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return bool(
        state.get("id")
        and (state.get("add") or state.get("del"))
        and now - float(state.get("created") or 0) < PENDING_TTL_SEC
    )


def _cleared(state: dict) -> dict:
    """Bỏ đề xuất, giữ ngày đã hỏi (trần 1 lần/ngày) và danh sách dòng đã bỏ."""
    return {key: state[key] for key in ("day", "rej") if state.get(key)}


async def _load(user_id: int) -> tuple[str, dict]:
    raw = await db.get_setting(_key(user_id)) or ""
    return raw, _parse(raw)


async def _save(user_id: int, state: dict) -> None:
    await db.set_setting(_key(user_id), json.dumps(state, ensure_ascii=False) if state else "")


async def pending(user_id: int) -> dict | None:
    """Đề xuất đang chờ (còn hạn) hoặc None."""
    _raw, state = await _load(user_id)
    return state if _active(state) else None


async def propose(user_id: int, rules: str, current: str) -> bool:
    """Lưu đề xuất (``rules`` = toàn bộ quy tắc bộ trích xuất muốn, ``current`` =
    quy tắc đang áp). Gộp với đề xuất còn hạn. True nếu có thay đổi cần hỏi.
    Gọi khi đã giữ khoá trí nhớ của người dùng (lượt trích nhớ nền)."""
    base = {_norm(line) for line in _lines(current)}
    proposed = _lines(rules)
    proposed_norms = {_norm(line) for line in proposed}
    _raw, state = await _load(user_id)
    rejected = set(state.get("rej") or [])
    added_new = [line for line in proposed if _norm(line) not in base | rejected]
    removed_new = [line for line in _lines(current) if _norm(line) not in proposed_norms]
    if not added_new and not removed_new:
        return False
    old_add: list[str] = list(state.get("add") or []) if _active(state) else []
    old_del: list[str] = list(state.get("del") or []) if _active(state) else []
    new_norms = {_norm(line) for line in added_new}
    removed_norms = {_norm(line) for line in removed_new}
    added: list[str] = []
    for line in [*old_add, *added_new]:
        norm = _norm(line)
        if norm and norm not in removed_norms and norm not in {_norm(x) for x in added}:
            added.append(line)
    removed: list[str] = []
    for line in [*old_del, *removed_new]:
        norm = _norm(line)
        if norm and norm not in new_norms and norm not in {_norm(x) for x in removed}:
            removed.append(line)
    added, removed = added[-_MAX_LINES:], removed[-_MAX_LINES:]
    if [_norm(x) for x in added] == [_norm(x) for x in old_add] and [_norm(x) for x in removed] == [
        _norm(x) for x in old_del
    ]:
        return False  # đề xuất y hệt đề xuất đang chờ: không hỏi lại
    created = time.time()
    state.update(
        {
            "id": int(created * 1000) % 10**12,
            "add": added,
            "del": removed,
            "created": created,
        }
    )
    await _save(user_id, state)
    _nothing_to_ask.pop(user_id, None)
    logger.info(
        "Đề xuất cách trả lời chờ duyệt user_id=%s: +%d/-%d dòng.",
        user_id,
        len(added),
        len(removed),
    )
    return True


def _describe(state: dict) -> str:
    you, me = reply_audience.forms()
    lines = [f"💡 {me.capitalize()} thấy {you} muốn {me} trả lời theo cách này:"]
    lines.extend(f"+ {line}" for line in state.get("add") or [])
    lines.extend(f"− thôi: {line}" for line in state.get("del") or [])
    return "\n".join(lines)


@dataclass(frozen=True)
class Question:
    proposal_id: int
    text: str


async def take_question(user_id: int, *, buttons: bool = False) -> Question | None:
    """Câu hỏi duyệt để gửi CUỐI lượt trả lời (tin riêng), hoặc None: không có đề
    xuất mới, đã hỏi đề xuất này, hoặc hôm nay đã hỏi rồi. Không raise."""
    if not enabled() or _nothing_to_ask.get(user_id):
        return None
    try:
        raw, state = await _load(user_id)
        today = _today()
        if not _active(state) or state.get("asked") == state["id"]:
            _remember(_nothing_to_ask, user_id, True, _HINT_MAX)
            return None
        if state.get("day") == today:
            return None  # hôm nay đã hỏi 1 lần: để mai hỏi đề xuất mới này
        updated = {**state, "asked": state["id"], "day": today}
        new_raw = json.dumps(updated, ensure_ascii=False)
        # CAS: lượt trích nhớ nền vừa ghi đề xuất mới thì để lần sau hỏi bản mới.
        if not await db.compare_and_set_setting(_key(user_id), raw, new_raw):
            return None
    except Exception:
        logger.warning("Không đọc được đề xuất cách trả lời user_id=%s.", user_id, exc_info=True)
        return None
    you, me = reply_audience.forms()
    text = _describe(state)
    text += f"\n{you.capitalize()} muốn {me} ghi nhớ không ạ? "
    text += _HINT_BUTTONS if buttons else _HINT_TEXT
    _remember(_nothing_to_ask, user_id, True, _HINT_MAX)
    _remember(_awaiting, user_id, (int(state["id"]), time.monotonic()), _AWAIT_MAX)
    return Question(int(state["id"]), text)


def _merge(current: str, state: dict, max_chars: int) -> list[str]:
    removed = {_norm(line) for line in state.get("del") or []}
    final = [line for line in _lines(current) if _norm(line) not in removed]
    seen = {_norm(line) for line in final}
    for line in state.get("add") or []:
        if _norm(line) not in seen:
            final.append(line)
            seen.add(_norm(line))
    # Quá trần: bỏ dòng CŨ nhất trước (lời dặn mới nhất thắng).
    while final and len("\n".join(f"- {line}" for line in final)) > max_chars:
        final.pop(0)
    return final


async def resolve(user_id: int, approve: bool, proposal_id: int | None = None) -> str:
    """[Ghi nhớ]/[Bỏ], "ghi nhớ"/"bỏ", /hoc ok|bo. ``proposal_id`` (nút bấm): chỉ
    nhận đúng đề xuất đó (nút cũ của đề xuất đã gộp/hết hạn thì báo hết hạn)."""
    from services import memory_service

    _awaiting.pop(user_id, None)
    try:
        async with memory_service.interactive_lock(user_id):
            _raw, state = await _load(user_id)
            if not _active(state) or (proposal_id is not None and state.get("id") != proposal_id):
                return NONE_PENDING
            you, me = reply_audience.forms()
            if not approve:
                rejected = list(state.get("rej") or [])
                rejected.extend(_norm(line) for line in state.get("add") or [])
                cleared = _cleared(state)
                cleared["rej"] = list(dict.fromkeys(rejected))[-_MAX_REJECTED:]
                await _save(user_id, cleared)
                return f"Dạ, {me} bỏ qua đề xuất đó, vẫn trả lời như cũ ạ."
            rows = await db.get_fact_rows(user_id)
            current = next((r for r in rows if r["key"] == memory_service.STYLE_KEY), None)
            final = _merge(
                str(current["value"]) if current else "",
                state,
                memory_service.STYLE_RULES_MAX_CHARS,
            )
            if final:
                await db.save_fact(
                    user_id,
                    memory_service.STYLE_KEY,
                    "\n".join(f"- {line}" for line in final),
                    importance=3,
                    category=memory_service.STYLE_KEY,
                    source=str(current["source"]) if current else "auto",
                    pinned=bool(current and current.get("pinned")),
                    protect=False,
                )
            elif current:
                await db.delete_fact(user_id, memory_service.STYLE_KEY)
            await _save(user_id, _cleared(state))
    except memory_service.MemoryBusy:
        return memory_service.MEMORY_BUSY_TEXT
    logger.info("Người dùng user_id=%s duyệt đề xuất cách trả lời.", user_id)
    shown = "\n".join(f"- {line}" for line in final) if final else "(không còn quy tắc nào)"
    return f"✅ {me.capitalize()} ghi nhớ rồi ạ. Cách trả lời {you} đã dặn:\n{shown}"


async def maybe_handle_reply(user_id: int, text: str) -> str | None:
    """ "ghi nhớ"/"bỏ" là tin KẾ TIẾP ngay sau câu hỏi duyệt -> xử lý bằng code (không
    tốn lượt AI). Tin khác thì bỏ trạng thái chờ (câu sau không còn tính)."""
    waiting = _awaiting.pop(user_id, None)
    if waiting is None:
        return None
    proposal_id, asked_at = waiting
    if time.monotonic() - asked_at > _AWAIT_SEC:
        return None
    folded = " ".join(fold(text or "").split())
    if _APPROVE_RE.match(folded):
        return await resolve(user_id, True, proposal_id)
    if _REJECT_RE.match(folded):
        return await resolve(user_id, False, proposal_id)
    return None


async def handle_command(argument: str, user_id: int) -> str:
    """/hoc [ok|bo]: xem hoặc duyệt đề xuất cách trả lời đang chờ."""
    choice = fold(argument or "").strip()
    if choice in ("ok", "co", "ghi nho", "dong y", "yes", "y"):
        return await resolve(user_id, True)
    if choice in ("bo", "khong", "no", "n", "huy"):
        return await resolve(user_id, False)
    state = await pending(user_id)
    if state is None:
        _you, me = reply_audience.forms()
        return (
            f"{NONE_PENDING} ({me.capitalize()} chỉ hỏi khi tự rút ra được lời dặn từ hội thoại.)"
        )
    return f"{_describe(state)}\n/hoc ok để ghi nhớ, /hoc bo để bỏ."
