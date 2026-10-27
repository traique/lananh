"""Ghi MỌI câu trả lời người dùng nhìn thấy vào lịch sử chat (chat_messages).

Trước đây chỉ run_chat_turn ghi lịch sử, nên sau báo cáo cổ phiếu, thời tiết,
âm lịch, /rag, /agent... câu hỏi nối tiếp ("Sao em bảo giữ?", "thế còn ngày
mai?", "giải thích ý 2") tới model mà không có ngữ cảnh, và lượt sau còn bị coi
là đầu phiên mới. ``record_exchange`` lưu CẢ lượt trong 1 transaction
(core.database.add_chat_turn) kèm ``kind`` (migrations/017), best-effort: lỗi
DB chỉ log, không bao giờ chặn việc gửi câu trả lời.

Câu trả lời được rút gọn theo loại:
- stock (chủ bot/admin): mở đầu bằng nhãn "[Báo cáo hệ thống - hành động do
  policy quyết định: ...]" và giữ NGUYÊN VĂN dòng tín hiệu/kế hoạch của policy,
  để lượt sau giải thích action chứ không sửa nó. Thành viên Zalo (bản tham
  khảo): bỏ mọi dòng có nhãn hành động/ngôn ngữ kế hoạch - lưu lịch sử không
  được làm lộ phần full_access=False cố ý giấu.
- article, translate, image: chỉ lưu dòng tóm tắt ngắn (bài báo dài, bản dịch
  và prompt tiếng Anh sẽ chiếm hết ngân sách lịch sử, kéo câu trả lời sang
  tiếng Anh).
- vision, document (hỏi đáp về ảnh/tài liệu): nhãn + ~1200 ký tự đã làm sạch
  (core.untrusted.sanitize) để câu hỏi nối tiếp còn ngữ cảnh.
- còn lại: cắt ~1500 ký tự đầu.

``guard_stock_reply``: lượt chat ngay sau báo cáo cổ phiếu không được nói
ngược action của policy (chủ bot) hoặc đưa vùng mua/stop/target (thành viên).
Chỉ áp khi lượt chat còn dính tới báo cáo đó (nhắc đúng mã đã báo cáo, hoặc
báo cáo vừa gửi STOCK_GUARD_RECENT_SEC trước và câu hỏi không nêu mã khác) -
"chị nên giữ ấm cổ họng" 2 tiếng sau 1 câu báo giá không bị chặn. Vi phạm thì
bỏ đúng các dòng vi phạm (không thay cả câu trả lời) rồi nối câu báo; không
còn dòng nào thì trả câu báo kèm dòng số liệu đã lưu, không bao giờ chỉ có câu
báo trơ trọi. ``reference_only_reply``: cùng bộ lọc dòng cho câu chat của
thành viên Zalo có số liệu giá cổ phiếu (services/channel_chat_service.py).

Trích trí nhớ (services/memory_service.py) chỉ đọc các lượt MEMORY_KINDS.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TypeVar

from core import config, database as db, untrusted

logger = logging.getLogger(__name__)
T = TypeVar("T")

KIND_CHAT = "chat"
# Chat dựa trên dữ liệu ngoài (web, trang người dùng gửi, tin trích dẫn...):
# câu trả lời có thể mang prompt injection -> trích nhớ chỉ đọc lời người dùng.
KIND_CHAT_EXT = "chat_ext"
KIND_STOCK = "stock"
KIND_WEATHER = "weather"
KIND_LUNAR = "lunar"
KIND_DAILY = "daily"
KIND_PORTFOLIO = "portfolio"
KIND_ARTICLE = "article"
KIND_RAG = "rag"
KIND_AGENT = "agent"
KIND_TRANSLATE = "translate"
KIND_IMAGE = "image"
# Hỏi đáp về ảnh (services/vision_service.py) và tài liệu người dùng gửi
# (services/document_service.py): câu trả lời có thể chép chữ trong ảnh/file
# (nội dung ngoài) -> lưu bản đã làm sạch, KHÔNG trích trí nhớ dài hạn (số giấy
# tờ, hoá đơn, hợp đồng không tự vào trí nhớ).
KIND_VISION = "vision"
KIND_DOCUMENT = "document"
# Câu trả lời bằng code của nhắc việc ("hoãn 10p"/"xong") và ghi chi tiêu
# ("cafe 45k"): vào lịch sử cho câu hỏi nối tiếp, không trích trí nhớ.
KIND_REMINDER = "reminder"
KIND_EXPENSE = "expense"
# Ghi bữa ăn bằng câu tự nhiên ("trưa nay anh ăn bún chả", services/nutrition_service.py).
KIND_MEAL = "meal"
# Tin hỏi thăm chủ động (services/proactive_service.py) - dòng "user" là nhãn
# hệ thống, không phải lời người dùng.
KIND_FOLLOWUP = "followup"
# Lượt CHỈ ĐỌC (chữ chép từ tin thoại, 🔄/✂️ làm lại) có ý nhờ nhớ/ghi mà tool
# ghi bị chặn: câu trả lời đã nói "chưa lưu" -> bộ trích nhớ không được tự lưu
# từ lượt này (chữ chép có thể nghe nhầm).
KIND_CHAT_READONLY = "chat_readonly"
# Kết quả việc nền /viec và tác vụ định kỳ (services/background_jobs.py): dòng
# "user" là nhãn "[Việc nền #12] <yêu cầu>", câu trả lời có thể chép tin web -> lưu
# bản đã làm sạch cho câu hỏi nối tiếp, KHÔNG trích trí nhớ (yêu cầu lặp lại hằng
# tuần không được thành "sở thích").
KIND_BACKGROUND = "background"
# Kết quả lệnh chạy từ câu tự nhiên (services/nl_commands.py: "xem tử vi 15/3/1990 9h30
# nam", "em làm được gì"...): vào lịch sử cho câu hỏi nối tiếp, có thể chép nội dung
# ngoài (điểm tin RSS, tìm giá) -> lưu bản đã làm sạch, KHÔNG trích trí nhớ.
KIND_COMMAND = "command"

# Lượt được trích trí nhớ (lời người dùng) / được đọc cả câu trả lời của bot.
MEMORY_KINDS = frozenset({KIND_CHAT, KIND_CHAT_EXT, KIND_RAG, KIND_AGENT})
MEMORY_MODEL_KINDS = frozenset({KIND_CHAT})

REPLY_MAX_CHARS = 1500
USER_MAX_CHARS = 2000
_STUB_SOURCE_CHARS = 120

OWNER_STOCK_TAG = "[Báo cáo hệ thống - hành động do policy quyết định: {actions}]"
MEMBER_STOCK_TAG = "[Báo cáo tham khảo cho thành viên - không phải tín hiệu giao dịch]"
_OWNER_TAG_RE = re.compile(r"^\[Báo cáo hệ thống - hành động do policy quyết định: ([^\]]*)\]")
_SIGNAL_LINE_RE = re.compile(r"Tín hiệu hệ thống:\s*\*{0,2}([^*\n(]+)")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# ─── Bản lưu cho báo cáo cổ phiếu ────────────────────────────────────────────


def _label_to_action() -> dict[str, str]:
    from stock import analysis as stock_analysis

    out: dict[str, str] = {}
    for action, label in stock_analysis._ACTION_LABEL_VI.items():
        # Bỏ phần ngoặc ("ĐỨNG NGOÀI (NO_TRADE)") - regex dòng tín hiệu dừng ở "(".
        base = re.sub(r"\s*\(.*?\)", "", label).strip()
        out[base] = action
        # Nhãn không emoji ("GIỮ") cũng nhận.
        out[base.split(" ", 1)[-1].strip()] = action
    return out


def actions_in_report(text: str) -> list[str]:
    """Action policy (BUY/HOLD/...) nêu trong các dòng "Tín hiệu hệ thống: ..."."""
    mapping = _label_to_action()
    found: list[str] = []
    for match in _SIGNAL_LINE_RE.finditer(text or ""):
        label = match.group(1).strip()
        action = mapping.get(label) or mapping.get(label.split(" ", 1)[-1].strip())
        if action and action not in found:
            found.append(action)
    return found


def _signal_lines(text: str) -> list[str]:
    """Dòng tiêu đề mã + dòng tín hiệu + dòng kế hoạch ngay sau (nguyên văn)."""
    lines = (text or "").splitlines()
    kept: list[str] = []
    for index, line in enumerate(lines):
        if "Tín hiệu hệ thống:" not in line:
            continue
        if index > 0 and lines[index - 1].startswith("📊"):
            kept.append(lines[index - 1])
        kept.append(line)
        if index + 1 < len(lines):
            kept.append(lines[index + 1])
    return kept


def _reference_only(text: str) -> str:
    """Bỏ mọi dòng có nhãn hành động hoặc ngôn ngữ kế hoạch (bản thành viên)."""
    from stock import report_format as rfmt

    kept = [
        line
        for line in (text or "").splitlines()
        if not rfmt.find_action_conflicts(line, frozenset()) and not rfmt.find_plan_language(line)
    ]
    return "\n".join(kept)


def stock_record(outputs: list[str], *, full_access: bool) -> str:
    text = "\n\n".join(part.strip() for part in outputs if part and part.strip())
    if not full_access:
        return _clip(f"{MEMBER_STOCK_TAG}\n{_reference_only(text)}", REPLY_MAX_CHARS)
    actions = actions_in_report(text)
    head = OWNER_STOCK_TAG.format(actions=", ".join(actions) or "không có tín hiệu")
    key_lines = _signal_lines(text)
    body = "\n".join(key_lines)
    rest_budget = REPLY_MAX_CHARS - len(head) - len(body) - 4
    rest = _clip(text, rest_budget) if rest_budget > 200 else ""
    return "\n".join(part for part in (head, body, rest) if part)


def stored_user_text(user_text: str) -> str:
    """Tin người dùng ĐÚNG như record_exchange lưu vào chat_messages."""
    return _clip(user_text, USER_MAX_CHARS)


def compact_reply(reply: str, kind: str, user_text: str = "") -> str:
    reply = (reply or "").strip()
    if kind == KIND_ARTICLE:
        return "[Đã gửi nguyên văn bài báo cho người dùng đọc] " + _clip(
            untrusted.sanitize(reply), 600
        )
    if kind == KIND_TRANSLATE:
        return f"[Đã dịch giúp đoạn: «{_clip(user_text, _STUB_SOURCE_CHARS)}» - bản dịch không lưu]"
    if kind == KIND_IMAGE:
        return "[Đã viết prompt tạo ảnh từ ảnh người dùng gửi - nội dung tiếng Anh không lưu]"
    if kind == KIND_VISION:
        return "[Đã trả lời về ảnh người dùng gửi] " + _clip(untrusted.sanitize(reply), 1200)
    if kind == KIND_DOCUMENT:
        return "[Đã trả lời về tài liệu người dùng gửi] " + _clip(untrusted.sanitize(reply), 1200)
    if kind == KIND_BACKGROUND:
        return "[Kết quả việc chạy nền đã gửi người dùng] " + _clip(untrusted.sanitize(reply), 1200)
    if kind == KIND_COMMAND:
        return "[Kết quả lệnh đã gửi người dùng] " + _clip(untrusted.sanitize(reply), 1200)
    return _clip(reply, REPLY_MAX_CHARS)


async def record_exchange(
    user_id: int,
    user_text: str,
    reply: str,
    kind: str = KIND_CHAT,
    *,
    full_access: bool = True,
    stock_outputs: list[str] | None = None,
    read_only: bool = False,
    saved_ids: list[int] | None = None,
) -> bool:
    """Lưu 1 lượt (lời người dùng + câu trả lời đã rút gọn) rồi báo trí nhớ nếu
    là lượt cần trích. read_only (lượt chỉ đọc): không bao giờ trích nhớ NGAY vì
    "nhớ giúp..." - câu trả lời đã nói chưa lưu. saved_ids: nhận id 2 dòng
    chat_messages vừa lưu (🔄/✂️ xoá đúng cặp này khi làm lại). Không raise; trả
    True nếu đã lưu."""
    user_text = (user_text or "").strip()
    if not user_text:
        return False
    if kind == KIND_STOCK:
        stored = stock_record(stock_outputs or [reply], full_access=full_access)
    else:
        stored = compact_reply(reply, kind, user_text)
    if not stored:
        return False
    try:
        ids = await db.add_chat_turn(user_id, stored_user_text(user_text), stored, kind=kind)
    except Exception:
        logger.warning(
            "Không lưu được lịch sử chat user_id=%s kind=%s (DB lỗi) - vẫn gửi câu trả lời.",
            user_id,
            kind,
            exc_info=True,
        )
        return False
    if saved_ids is not None and ids:
        saved_ids.extend(ids)
    if kind in MEMORY_KINDS:
        from services import memory_service

        explicit = not read_only and memory_service.wants_remember(user_text)
        memory_service.after_exchange(user_id, explicit=explicit)
    return True


async def run_and_record(
    user_id: int, user_text: str, kind: str, call: Callable[[], Awaitable[T]]
) -> T:
    """Chạy ``call()`` (lệnh /agent, /dich...) rồi lưu lượt vào lịch sử. Dùng bên
    trong reply_cache.generate_once để lần gửi lại (webhook retry) không ghi trùng.
    Kết quả là tuple thì phần tử đầu là câu trả lời."""
    result = await call()
    text = result[0] if isinstance(result, tuple) else result
    await record_exchange(user_id, user_text, str(text or ""), kind)
    return result


# ─── Chặn câu chat mâu thuẫn báo cáo cổ phiếu ngay trước đó ──────────────────


async def last_model_row(user_id: int) -> dict | None:
    """Câu trả lời gần nhất của bot TRONG phiên hiện tại; DB lỗi -> None."""
    try:
        row = await db.get_last_model_row(user_id)
    except Exception:
        logger.warning("Không đọc được câu trả lời gần nhất user_id=%s.", user_id, exc_info=True)
        return None
    if not row:
        return None
    created = row.get("created_at")
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age > config.CHAT_SESSION_TIMEOUT_SEC:
            return None
    return row


# Báo cáo cổ phiếu còn tính là "ngay trước" trong chừng này giây: sau đó lượt
# chat chỉ bị chặn khi nhắc đúng mã đã báo cáo.
STOCK_GUARD_RECENT_SEC = 15 * 60
# Mã trong bản lưu: dòng tiêu đề "📊 **FPT** — ..." / "📊 **HPG**: ..." và dòng
# bảng danh mục "• **FPT**: giá ...".
_REPORTED_SYMBOL_RE = re.compile(r"(?:📊\s*\*{0,2}|•\s*\*\*)([A-Z][A-Z0-9]{2,9})(?![A-Za-z0-9])")
# Câu do code nối sau khi bỏ dòng - KHÔNG chứa cụm nào find_plan_language/
# find_action_conflicts bắt (lọc lần 2 không xoá chính câu báo).
REFERENCE_NOTE = (
    "📌 Phần nói về điểm vào lệnh hay kế hoạch giao dịch đã được lược bỏ - {ai} chỉ gửi "
    "thông tin tham khảo, không phải khuyến nghị giao dịch nha{tail}."
)
_OWNER_PARTIAL_NOTE = (
    "⚠️ Em đã bỏ câu nêu tín hiệu khác với hệ thống - tín hiệu vẫn do policy quyết định:"
)
_FALLBACK_LINES = 3


def reported_symbols(content: str) -> set[str]:
    """Các mã cổ phiếu nêu trong bản lưu của 1 lượt báo cáo (stock_record)."""
    return set(_REPORTED_SYMBOL_RE.findall(content or ""))


def _mentions(text: str, symbols: set[str]) -> bool:
    return any(
        re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])", text or "", re.IGNORECASE)
        for symbol in symbols
    )


def _row_age_sec(row: dict) -> float | None:
    created = row.get("created_at")
    if not isinstance(created, datetime):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()


def stock_guard_applies(last_row: dict | None, user_text: str = "", reply: str = "") -> bool:
    """Lượt chat này còn dính tới báo cáo cổ phiếu ngay trước không: người dùng
    hoặc câu trả lời nhắc đúng mã đã báo cáo, hoặc báo cáo vừa gửi (<=
    STOCK_GUARD_RECENT_SEC, không rõ giờ thì coi là vừa gửi) và câu hỏi không
    nêu mã nào khác ("sao em bảo giữ?", "vậy có nên bán không?")."""
    if not last_row or last_row.get("kind") != KIND_STOCK:
        return False
    symbols = reported_symbols(str(last_row.get("content") or ""))
    if symbols and (_mentions(user_text, symbols) or _mentions(reply, symbols)):
        return True
    age = _row_age_sec(last_row)
    if age is not None and age > STOCK_GUARD_RECENT_SEC:
        return False
    from stock import analysis as stock_analysis

    known, _unverified = stock_analysis.detect_symbol_candidates(user_text or "")
    return not (set(known) - symbols)


def _is_reference_violation(line: str) -> bool:
    from stock import report_format as rfmt

    return bool(rfmt.find_action_conflicts(line, frozenset()) or rfmt.find_plan_language(line))


def reference_only_reply(
    reply: str, fallback_lines: list[str], *, address_form: str | None = None
) -> str:
    """Bản thành viên (chỉ tham khảo): bỏ từng dòng có nhãn hành động/ngôn ngữ kế
    hoạch giao dịch rồi nối REFERENCE_NOTE. Không còn dòng nào thì trả câu báo
    kèm ``fallback_lines`` (dòng số liệu) - không bao giờ rỗng."""
    if not reply:
        return reply
    kept: list[str] = []
    dropped = 0
    for line in reply.splitlines():
        if _is_reference_violation(line):
            dropped += 1
            continue
        kept.append(line)
    if not dropped:
        return reply
    logger.warning("Đã bỏ %d dòng khuyến nghị giao dịch trong câu chat bản tham khảo.", dropped)
    from services import media_text

    note = media_text.say(REFERENCE_NOTE, owner=False, address_form=address_form)
    body = "\n".join(kept).strip()
    if body:
        return f"{body}\n\n{note}"
    from stock import analysis as stock_analysis

    data = "\n".join(line for line in fallback_lines if line.strip()).strip()
    return f"{stock_analysis._REFERENCE_DROPPED_NOTE}\n\n{data}" if data else note


def grounding_price_lines(grounding: str) -> list[str]:
    """Dòng giá "- FPT: 120.500 VND ..." trong khối build_price_grounding."""
    return [
        line.strip().rstrip("]")
        for line in (grounding or "").splitlines()
        if line.strip().startswith("- ")
    ]


def guard_stock_reply(
    reply: str, last_row: dict | None, *, user_text: str = "", address_form: str | None = None
) -> str:
    """Câu chat ngay sau báo cáo cổ phiếu (stock_guard_applies): chủ bot không
    được nhận action khác policy; thành viên không được nhận nhãn hành động/vùng
    mua/stop/target. Chỉ bỏ đúng các dòng vi phạm."""
    if not reply or not stock_guard_applies(last_row, user_text, reply):
        return reply
    assert last_row is not None
    from stock import analysis as stock_analysis
    from stock import report_format as rfmt

    content = str(last_row.get("content") or "")
    if content.startswith(MEMBER_STOCK_TAG):
        stored = [line for line in content.splitlines()[1:] if line.strip()]
        return reference_only_reply(reply, stored[:_FALLBACK_LINES], address_form=address_form)
    match = _OWNER_TAG_RE.match(content)
    actions = [a.strip() for a in match.group(1).split(",")] if match else []
    known = [a for a in actions if a in stock_analysis._COMPATIBLE_ACTIONS]
    if not known:
        return reply
    allowed: set[str] = set()
    for action in known:
        allowed |= stock_analysis._COMPATIBLE_ACTIONS[action]
    kept: list[str] = []
    conflicts: list[str] = []
    for line in reply.splitlines():
        found = rfmt.find_action_conflicts(line, frozenset(allowed))
        if found:
            conflicts.extend(found)
            continue
        kept.append(line)
    if not conflicts:
        return reply
    logger.warning("Câu chat nói ngược action policy %s: %s", known, conflicts[:5])
    signal = "\n".join(line for line in content.splitlines()[1:4] if line.strip())
    body = "\n".join(kept).strip()
    if body:
        return f"{body}\n\n{_OWNER_PARTIAL_NOTE}\n{signal}".strip()
    return f"{stock_analysis._CONFLICT_DROPPED_NOTE}\n\n{signal}".strip()
