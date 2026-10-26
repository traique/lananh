"""Trí nhớ DÀI HẠN của bot, khác với trí nhớ NGẮN HẠN theo phiên (lịch sử
chat_messages, xem core.database.get_session_messages()).

Lưu trong các bảng (core/database.py, core/repositories/memory.py):
- user_facts: "sự thật" bền về người dùng {key, value} kèm độ quan trọng
  (1-3), nhóm (basic_info, cong_viec, gia_dinh, so_thich, suc_khoe, dau_tu,
  lich_trinh, cach_tra_loi, khac), ghim và nguồn (auto = bộ trích xuất, user =
  người dùng tự bảo nhớ). Giá trị đổi thì bản cũ được VÔ HIỆU HOÁ (giữ vài bản
  làm lịch sử cho câu "trước đây..."), không xoá; chỉ fact hiện hành vào prompt.
  Fact ghim/do người dùng nhập thì bộ trích xuất không được sửa/xoá.
- user_memory_highlights: tối đa MAX_HIGHLIGHTS_PER_USER câu "chi tiết đáng
  nhớ" gần nhất (số liệu, ngày tháng, quyết định...), dòng cũ tự rơi ra.
- user_session_notes: mỗi phiên chat 1 dòng - tiêu đề + 2-4 ý (đưa vào đầu
  các phiên sau) và tóm tắt phiên đang chạy <= 800 ký tự (chèn giữa phiên khi
  các lượt đầu đã trôi khỏi cửa sổ lịch sử: "lúc nãy mình bàn gì?").
- user_memory_summary: tóm tắt "rolling" kiểu cũ - KHÔNG còn viết lại mỗi lượt
  (đã thay bằng ghi chú phiên), chỉ còn đọc như phần nền cũ ở đầu phiên.
- "cách trả lời" người dùng dặn ("ngắn thôi em", "đừng chèn emoji") là fact
  nhóm cach_tra_loi, đưa vào system prompt MỌI lượt (orchestrator.chat
  style_rules) - không đè quy tắc cổ phiếu hay cách xưng hô.

Không embedding/pgvector (đã bỏ, migration 014): tìm theo token bỏ dấu.

GHI (buffer + flush, thay cho 1 lượt trích xuất sau MỖI tin nhắn):
- services/conversation_log.record_exchange lưu lượt vào chat_messages rồi gọi
  ``after_exchange`` -> ``maybe_flush`` chạy nền. Watermark (id tin đã xử lý)
  lưu ở settings theo từng người. Flush khi có >= MEMORY_FLUSH_EXCHANGES lượt
  chưa xử lý (bỏ qua "ok", "ừm", "cảm ơn"), khi phiên cũ đã kết thúc, khi lượt
  chưa xử lý cũ nhất sắp trôi khỏi cửa sổ lịch sử, khi rảnh MEMORY_IDLE_FLUSH_SEC
  (vòng quét nền, cả lúc khởi động), trước /reset, hoặc ngay khi "nhớ giúp".
- 1 lượt utility_json đọc cả cửa sổ chưa xử lý (<= ~6k ký tự, kèm 1-2 lượt
  trước làm ngữ cảnh) và trả các thao tác ADD/UPDATE/INVALIDATE theo id fact
  đang có, FORGET (chỉ khi người dùng chủ động bảo quên - xoá hẳn), highlight,
  cách trả lời, ghi chú/tóm tắt phiên và (người được phép) sự kiện hỏi thăm.
- Chặn bí mật (services/memory_guard.py) ở MỌI lần ghi; không học lại thứ
  người dùng đã /quen (danh sách có trần ở settings).
- Gộp fact (tuỳ chọn, MEMORY_CONSOLIDATION): tối đa 1 lần/ngày/người khi số fact
  >= 80% trần, trong vòng quét nền; thao tác kiểm theo id, chỉ vô hiệu hoá.

ĐỌC (``build_turn_memory`` trước mỗi lượt chat, ráp trong MEMORY_MAX_CHARS -
từng mục vào trọn vẹn hoặc không vào, prompt_budget không phải cắt giữa câu):
- Đầu phiên: fact ghim/quan trọng (kèm mốc "từ 03/2026"), fact + ghi chú liên
  quan câu hỏi, ghi chú các phiên trước, highlight mới nhất, tóm tắt cũ cắt ở
  ranh giới câu.
- Giữa phiên: tóm tắt phiên này + fact/highlight/ghi chú liên quan câu hỏi
  (stopword mở rộng: "anh", "em", "không"... không còn khớp mọi câu).

Lượt gọi AI đi qua ai.orchestrator.utility_json() (Groq json_object trước,
Gemini utility dự phòng) dưới turn_deadline(MEMORY_UPDATE_TIMEOUT_SEC) và
Semaphore(2) chung cho mọi lượt trích nhớ nền.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ai.timeouts import TurnTimeoutError, bounded, turn_deadline
from core import config, database as db, trace
from core.envutil import env_int, env_on
from core.text_normalize import fold
from services import conversation_log, memory_guard, personal_settings
from services.background_tasks import stop_tracked_tasks

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_SETTING_ENABLED_PREFIX = "memory_enabled_"


async def _utility_json(prompt: str):
    """Groq json_object trước, Gemini utility dự phòng (ai.orchestrator.utility_json).
    Import lười để tránh vòng import ai.orchestrator -> services."""
    from ai import orchestrator

    return await orchestrator.utility_json(prompt)


async def is_enabled(user_id: int) -> bool:
    return await db.get_setting(f"{_SETTING_ENABLED_PREFIX}{user_id}") != "0"


async def set_enabled(user_id: int, enabled: bool) -> None:
    await db.set_setting(f"{_SETTING_ENABLED_PREFIX}{user_id}", "1" if enabled else "0")
    logger.info("Trí nhớ dài hạn user_id=%s: %s.", user_id, "bật" if enabled else "tắt")


# Khoá riêng / user cho mọi thao tác đọc-rồi-ghi trí nhớ (flush nền, /quen,
# /nho, /ghim...): 2 lượt song song của CÙNG 1 user có thể đọc cùng bản cũ rồi
# ghi đè lẫn nhau. Đếm số lượt đang dùng để DỌN lock khi không còn ai cần
# (không rò rỉ 1 Lock/user sống mãi tới khi restart).
_user_locks: dict[int, asyncio.Lock] = {}
_lock_users: dict[int, int] = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    """Lấy (hoặc tạo) lock của user và tăng số đếm người dùng lock đó.

    Mọi lần gọi PHẢI đi kèm 1 lần _release_lock() tương ứng (dùng try/finally).
    """
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    _lock_users[user_id] = _lock_users.get(user_id, 0) + 1
    return lock


def _release_lock(user_id: int) -> None:
    """Giảm số đếm; khi không còn lượt nào đang giữ/chờ thì bỏ lock khỏi dict."""
    remaining = _lock_users.get(user_id, 1) - 1
    if remaining > 0:
        _lock_users[user_id] = remaining
        return
    _lock_users.pop(user_id, None)
    _user_locks.pop(user_id, None)


@asynccontextmanager
async def user_lock(user_id: int) -> AsyncIterator[None]:
    """``async with user_lock(uid):`` - khoá đọc-rồi-ghi trí nhớ của 1 người."""
    lock = _lock_for(user_id)
    try:
        async with lock:
            yield
    finally:
        _release_lock(user_id)


# ─── Giới hạn ────────────────────────────────────────────────────────────────

# Trần fact HIỆN HÀNH / user (fact ghim không bị cắt).
MAX_FACTS_PER_USER = 40
# Người dùng tự ghim tối đa chừng này fact (/nho, /ghim).
MAX_PINNED_PER_USER = 20
# Trần số dòng "ý quan trọng" lưu / user - đã chốt N=25.
MAX_HIGHLIGHTS_PER_USER = 25

_MAX_FACT_KEY_LEN = 60
_MAX_FACT_VALUE_LEN = 200
_MAX_HIGHLIGHT_LEN = 200
STYLE_RULES_MAX_CHARS = 500
STYLE_KEY = "cach_tra_loi"
SESSION_SUMMARY_MAX_CHARS = 800
_SESSION_TITLE_MAX = 80
_SESSION_BULLET_MAX = 160
_FORGET_LIST_MAX = 30

CATEGORIES = (
    "basic_info",
    "cong_viec",
    "gia_dinh",
    "so_thich",
    "suc_khoe",
    "dau_tu",
    "lich_trinh",
    STYLE_KEY,
    "khac",
)

# Buffer + flush.
FLUSH_EXCHANGES = env_int("MEMORY_FLUSH_EXCHANGES", 3, 1, 20)
IDLE_FLUSH_SEC = env_int("MEMORY_IDLE_FLUSH_SEC", 1800, 60, 86_400)
SWEEP_INTERVAL_SEC = env_int("MEMORY_SWEEP_SEC", 600, 60, 86_400)
WINDOW_MAX_CHARS = 6000
_SCAN_ROWS = 60
_ROW_MAX_CHARS = 800
_CONTEXT_ROWS = 4
_MAX_GROUPS_PER_FLUSH = 2
# Tối đa chừng này lượt trích nhớ nền chạy cùng lúc (RAM 512 MB, quota free).
_slots = asyncio.Semaphore(2)
# Mỗi user tối đa 1 task flush đang chờ/chạy; tổng có trần.
_MAX_PENDING_TASKS = 20
_pending: dict[int, asyncio.Task] = {}
_background_tasks: set[asyncio.Task] = set()
_sweep_task: asyncio.Task | None = None


def _consolidation_enabled() -> bool:
    return env_on("MEMORY_CONSOLIDATION")


# ─── Lời người dùng tầm thường / ý định ─────────────────────────────────────

_TRIVIAL_RE = re.compile(
    r"^(?:ok(?:e+|ay|ie)?|okela|oki+|uh+|u+m+|um+|u+|vang|da|o|ho|ha+|hi+|he+|hihi|haha+|hehe+|"
    r"thanks?|thank you|thank|tks|thx|cam on(?: em| nhe| nha| nhieu)?|cam on em nhe|"
    r"ok em|ok cam on|duoc roi|roi|vay a|the a|that a|tuyet|hay qua|good|nice|"
    r"chuan|dung roi|vang a|da vang|bye|ngu ngon|chao|chao em)"
    r"[\s!.?~,]*$"
)
_EMOJI_ONLY_RE = re.compile(r"^[\W_]+$")
_FORGET_INTENT_RE = re.compile(
    r"\b(?:quen|xoa|dung nho|khong can nho|bo (?:cai|thong tin|di)|khong dung nua)\b"
)
_PAST_QUERY_RE = re.compile(r"\b(?:truoc day|hoi truoc|truoc kia|luc truoc|ngay truoc|cu)\b")


def is_trivial(text: str) -> bool:
    """ "ok", "ừm", "cảm ơn em", emoji... - không đáng tốn 1 lượt trích nhớ."""
    folded = fold(text or "")
    if not folded:
        return True
    return bool(_TRIVIAL_RE.match(folded) or _EMOJI_ONLY_RE.match(folded))


_REMEMBER_INTENT_RE = re.compile(
    r"\b(?:nho giup|nho dum|nho ho|ghi nho|em nho nhe|nho nhe|nho nha|nho cho (?:anh|em|minh))\b"
)


def wants_remember(text: str) -> bool:
    """Người dùng chủ động nhờ nhớ ("nhớ giúp anh là...") -> trích nhớ ngay."""
    return bool(_REMEMBER_INTENT_RE.search(fold(text or "")))


def _forget_intent(text: str) -> bool:
    return bool(_FORGET_INTENT_RE.search(fold(text or "")))


# ─── settings: watermark, danh sách "đừng học lại" ──────────────────────────


async def _get_watermark(user_id: int) -> int:
    raw = await db.get_setting(personal_settings.memory_watermark_key(user_id))
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


async def _set_watermark(user_id: int, value: int) -> None:
    await db.set_setting(personal_settings.memory_watermark_key(user_id), str(int(value)))


async def get_forget_list(user_id: int) -> list[dict]:
    raw = await db.get_setting(personal_settings.memory_forget_key(user_id))
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)][-_FORGET_LIST_MAX:]


async def add_to_forget_list(user_id: int, *, key: str = "", text: str = "") -> None:
    items = await get_forget_list(user_id)
    items.append({"k": key[:_MAX_FACT_KEY_LEN], "t": text[:80]})
    await db.set_setting(
        personal_settings.memory_forget_key(user_id),
        json.dumps(items[-_FORGET_LIST_MAX:], ensure_ascii=False),
    )


def _forgotten(key: str, value: str, forget: list[dict]) -> bool:
    folded = fold(f"{key} {value}")
    for item in forget:
        k = str(item.get("k") or "")
        t = fold(str(item.get("t") or ""))
        if k and k == key:
            return True
        if t and len(t) >= 4 and t in folded:
            return True
    return False


# ─── Phiên chat ──────────────────────────────────────────────────────────────


def _as_utc(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _session_starts(rows_desc: list[dict]) -> dict[int, tuple[int, bool]]:
    """id dòng -> (id dòng đầu phiên, biết chắc ranh giới không). Ranh giới không
    nằm trong số dòng đã đọc (phiên dài hơn cửa sổ quét) -> False."""
    asc = list(reversed(rows_desc))
    out: dict[int, tuple[int, bool]] = {}
    start, known = 0, len(rows_desc) < _SCAN_ROWS
    for index, row in enumerate(asc):
        if index == 0:
            start = int(row["id"])
        else:
            gap = (
                _as_utc(row["created_at"]) - _as_utc(asc[index - 1]["created_at"])
            ).total_seconds()
            if gap > config.CHAT_SESSION_TIMEOUT_SEC:
                start, known = int(row["id"]), True
        out[int(row["id"])] = (start, known)
    return out


async def _resolve_session_key(user_id: int, start: int, known: bool) -> int:
    """Phiên dài hơn cửa sổ quét: dùng lại khoá của ghi chú phiên gần nhất."""
    if known:
        return start
    try:
        notes = await db.get_session_notes(user_id, 1)
    except Exception:
        notes = []
    if notes and int(notes[0]["session_key"]) <= start:
        return int(notes[0]["session_key"])
    return start


def _session_expired(rows_desc: list[dict]) -> bool:
    if not rows_desc:
        return True
    age = (datetime.now(timezone.utc) - _as_utc(rows_desc[0]["created_at"])).total_seconds()
    return age > config.CHAT_SESSION_TIMEOUT_SEC


# ─── Trích xuất ──────────────────────────────────────────────────────────────

_EXTRACTION_INSTRUCTION = """Bạn là bộ trích xuất trí nhớ nội bộ cho 1 trợ lý cá nhân (KHÔNG phải người
đang trò chuyện với user). Đọc các lượt hội thoại CHƯA XỬ LÝ (cũ -> mới) cùng
các fact đang có (kèm id), rồi trả về DUY NHẤT 1 object JSON hợp lệ (không thêm
chữ nào khác, không markdown, không code fence):

{
  "facts": [
    {"op": "add", "key": "snake_case_khong_dau", "value": "ngắn gọn", "importance": 2, "category": "cong_viec"},
    {"op": "update", "id": 12, "value": "giá trị mới", "importance": 2},
    {"op": "invalidate", "id": 7},
    {"op": "forget", "id": 9}
  ],
  "highlights": ["1 câu ngắn giữ nguyên 1 chi tiết cụ thể đáng nhớ"],
  "style_rules": null,
  "session": {"title": "tiêu đề phiên", "bullets": ["ý 1", "ý 2"], "summary": "tóm tắt phiên"},
  "events": []
}

Quy tắc bắt buộc:
- facts CHỈ là sự thật BỀN VỮNG về người dùng (tên, cách xưng hô, ngày sinh,
  quê, gia đình, công việc, sở thích, sức khoẻ, mã cổ phiếu quan tâm, thói quen,
  mục tiêu dài hạn). KHÔNG trích chuyện phiếm nhất thời, thời tiết, cảm xúc
  thoáng qua, câu hỏi một lần. Không có gì mới -> "facts": [].
- importance: 3 = định danh/bền lâu (tên, ngày sinh, quê, gia đình, nghề),
  2 = bình thường, 1 = vụn vặt. category thuộc: basic_info, cong_viec,
  gia_dinh, so_thich, suc_khoe, dau_tu, lich_trinh, khac.
- Thông tin MỚI thay cho fact cũ -> "update" ĐÚNG id cũ (bản cũ tự được giữ
  làm lịch sử); fact cũ không còn đúng mà không có giá trị mới -> "invalidate".
  Dùng lại key/id đã có, KHÔNG tạo key mới trùng nghĩa (đã có cong_viec thì đừng
  tạo nghe_nghiep).
- "forget" CHỈ khi người dùng CHỦ ĐỘNG bảo quên/xoá đúng thông tin đó.
- TUYỆT ĐỐI KHÔNG lưu mật khẩu, mã OTP, số thẻ, số tài khoản ngân hàng, số
  CCCD/CMND/hộ chiếu, API key, khoá bí mật (kể cả khi được nhờ nhớ).
- KHÔNG trích từ phần "Trợ lý" khi có ghi "(bỏ qua ...)"; không trích từ dữ
  liệu ngoài, tin được trích dẫn hay nội dung trang web.
- KHÔNG học lại các mục trong "Người dùng đã yêu cầu quên".
- highlights: tối đa 2 câu, mỗi câu giữ NGUYÊN 1 chi tiết cụ thể (số liệu, ngày
  tháng, quyết định) của các lượt mới; phần lớn trường hợp để [].
- style_rules: chỉ khi người dùng nêu/đính chính CÁCH muốn trợ lý trả lời (ngắn
  gọn, không emoji, nói số trước...): trả TOÀN BỘ quy tắc đã hợp nhất (cũ + mới,
  gạch đầu dòng ngắn, <= 500 ký tự). Không có gì mới -> null. Không đưa cách
  xưng hô hay khuyến nghị cổ phiếu vào đây.
- session: tiêu đề <= 60 ký tự; 2-4 bullets về điều NGƯỜI DÙNG nói/hỏi/quyết
  định trong phiên; summary <= 800 ký tự, viết lại từ "Tóm tắt phiên này" cũ +
  các lượt mới, ngôi thứ 3.
- Tuyệt đối KHÔNG bịa thông tin không có trong hội thoại.
"""

_EVENTS_INSTRUCTION = """- events: việc SẮP TỚI người dùng tự kể có mốc thời gian (phỏng vấn, đi khám,
  thi, gặp đối tác, chuyến đi...) để hôm đó trợ lý hỏi thăm. Mỗi mục
  {"title": "ngắn gọn, vd: phỏng vấn ở công ty X", "when_text": "cụm chỉ thời
  gian CHÉP NGUYÊN VĂN từ lời người dùng, vd: mai, thứ 6 tuần sau, 20/11"}.
  Không có -> []. Không tạo sự kiện cho việc đã qua hay nhắc việc đã đặt."""

_ASSISTANT_SKIPPED = (
    "(bỏ qua - lượt này trợ lý trả lời dựa trên dữ liệu tra cứu bên ngoài; CHỈ trích "
    "từ lời User, không lấy thông tin nào từ phía trợ lý)"
)
_FOLLOWUP_USER_LABEL = "(trợ lý chủ động nhắn hỏi thăm - không phải lời người dùng)"
_READONLY_USER_LABEL = "(lượt chỉ đọc - người dùng chưa xác nhận lưu, KHÔNG trích gì từ lượt này)"


def _fmt_time(value) -> str:
    return _as_utc(value).astimezone(_VN_TZ).strftime("%H:%M %d/%m")


def _render_rows(rows: list[dict]) -> str:
    lines: list[str] = []
    for row in rows:
        kind = row.get("kind") or conversation_log.KIND_CHAT
        content = memory_guard.redact(str(row.get("content") or "")).strip()
        if row["role"] == "user":
            if kind == conversation_log.KIND_FOLLOWUP:
                content = _FOLLOWUP_USER_LABEL
            elif kind == conversation_log.KIND_CHAT_READONLY:
                content = _READONLY_USER_LABEL
            lines.append(f"[{_fmt_time(row['created_at'])}] User: {content}")
        else:
            if kind not in conversation_log.MEMORY_MODEL_KINDS:
                content = _ASSISTANT_SKIPPED
            lines.append(f"Trợ lý: {content}")
    return "\n".join(lines)


def _fit_window(rows: list[dict], max_chars: int) -> list[dict]:
    """Giữ các lượt MỚI nhất vừa ``max_chars`` (cắt theo cặp từ dòng cũ nhất)."""
    kept: list[dict] = []
    total = 0
    for row in reversed(rows):
        cost = len(str(row.get("content") or "")) + 30
        if kept and total + cost > max_chars:
            break
        kept.append(row)
        total += cost
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept


def _facts_block(facts: list[dict]) -> str:
    if not facts:
        return "(chưa có)"
    lines = []
    for row in facts:
        flags = []
        if row.get("pinned") or row.get("source") == "user":
            flags.append("người dùng tự nhập - không sửa")
        flags.append(f"quan trọng {row.get('importance', 2)}")
        if row.get("category"):
            flags.append(str(row["category"]))
        lines.append(f"- id {row['id']} | {row['key']} = {row['value']} ({', '.join(flags)})")
    return "\n".join(lines)


def build_extraction_prompt(
    *,
    facts: list[dict],
    style_rules: str,
    forget: list[dict],
    session_summary: str,
    context_rows: list[dict],
    window_rows: list[dict],
    with_events: bool,
) -> str:
    capacity = f"{len(facts)}/{MAX_FACTS_PER_USER}"
    hint = ""
    if len(facts) >= int(MAX_FACTS_PER_USER * 0.8):
        hint = " - GẦN ĐẦY: ưu tiên gộp key trùng nghĩa bằng update/invalidate thay vì add"
    forget_block = (
        "\n".join(f"- {item.get('k') or ''} {item.get('t') or ''}".strip() for item in forget)
        or "(không có)"
    )
    instruction = _EXTRACTION_INSTRUCTION
    if with_events:
        instruction = f"{instruction}{_EVENTS_INSTRUCTION}\n"
    else:
        instruction = f"{instruction}- events: luôn trả [].\n"
    from ai import official_client

    return (
        f"{instruction}\n"
        f"### Fact hiện có ({capacity}{hint})\n{_facts_block(facts)}\n\n"
        f"### Cách trả lời người dùng đã dặn\n{style_rules or '(chưa có)'}\n\n"
        f"### Người dùng đã yêu cầu quên (KHÔNG học lại)\n{forget_block}\n\n"
        f"### Tóm tắt phiên này đến giờ\n{session_summary or '(chưa có)'}\n\n"
        f"### Ngữ cảnh ngay trước (ĐÃ xử lý, chỉ để hiểu ý)\n"
        f"{_render_rows(context_rows) or '(không có)'}\n\n"
        f"### Các lượt CHƯA xử lý (cũ -> mới)\n{_render_rows(window_rows)}\n\n"
        f"{official_client.now_vn_context()}\n"
        "Trả về JSON theo đúng định dạng đã mô tả ở trên, không thêm gì khác."
    )


def _normalize_key(raw) -> str:
    key = fold(str(raw or ""))
    key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    return key[:_MAX_FACT_KEY_LEN]


def _importance(raw, default: int = 2) -> int:
    try:
        return max(1, min(3, int(raw)))
    except (TypeError, ValueError):
        return default


def _category(raw) -> str:
    value = str(raw or "").strip().lower()
    return value if value in CATEGORIES and value != STYLE_KEY else ""


@dataclass
class _FlushStats:
    facts: int = 0
    highlights: int = 0
    events: int = 0


async def _apply_facts(
    user_id: int,
    items,
    shown: dict[int, dict],
    *,
    forget: list[dict],
    allow_forget: bool,
    stats: _FlushStats,
) -> None:
    if not isinstance(items, list):
        return
    by_key = {row["key"]: row for row in shown.values()}
    for item in items:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or "").strip().lower()
        fact_id = item.get("id")
        target = shown.get(fact_id) if isinstance(fact_id, int) else None
        key = _normalize_key(item.get("key"))
        if not op:
            # Định dạng cũ {"key", "value", "delete"}: delete -> vô hiệu hoá (giữ lịch sử).
            if item.get("delete"):
                op, target = "invalidate", by_key.get(key)
            else:
                op = "add"
        if op in ("update", "invalidate", "forget") and target is None:
            continue
        if op == "invalidate":
            assert target is not None
            if await db.invalidate_fact(user_id, int(target["id"]), protect=True):
                stats.facts += 1
            continue
        if op == "forget":
            assert target is not None
            if not allow_forget or target.get("pinned"):
                continue
            await db.delete_fact(user_id, target["key"])
            await add_to_forget_list(user_id, key=target["key"], text=str(target["value"]))
            stats.facts += 1
            continue
        if op == "update":
            assert target is not None
            key = target["key"]
        elif op != "add" or not key:
            continue
        value = memory_guard.safe_memory_text(str(item.get("value") or ""))
        if not value or memory_guard.is_secret_fact(key, value):
            continue
        value = value[:_MAX_FACT_VALUE_LEN]
        if key == STYLE_KEY or _forgotten(key, value, forget):
            continue
        result = await db.save_fact(
            user_id,
            key,
            value,
            importance=_importance(
                item.get("importance"), int(target["importance"]) if target else 2
            ),
            category=_category(item.get("category")) or (str(target["category"]) if target else ""),
            source="auto",
            protect=True,
        )
        if result in ("added", "updated"):
            stats.facts += 1


async def _apply_style(user_id: int, raw, current: dict | None) -> None:
    if not isinstance(raw, str) or not raw.strip():
        return
    rules = memory_guard.safe_memory_text(raw)
    if not rules:
        return
    rules = rules[:STYLE_RULES_MAX_CHARS]
    await db.save_fact(
        user_id,
        STYLE_KEY,
        rules,
        importance=3,
        category=STYLE_KEY,
        source=str(current["source"]) if current else "auto",
        pinned=bool(current and current.get("pinned")),
        # Quy tắc cách trả lời luôn theo lời người dùng mới nhất, kể cả bản tự nhập.
        protect=False,
    )


async def _apply_session(user_id: int, raw, session_key: int, started_at: datetime) -> None:
    if not isinstance(raw, dict):
        return
    title = memory_guard.clean(memory_guard.redact(str(raw.get("title") or "")))[
        :_SESSION_TITLE_MAX
    ]
    bullets_raw = raw.get("bullets")
    bullets: list[str] = []
    if isinstance(bullets_raw, list):
        for item in bullets_raw[:4]:
            text = memory_guard.clean(memory_guard.redact(str(item or "")))
            if text:
                bullets.append(text[:_SESSION_BULLET_MAX])
    summary = memory_guard.clean(memory_guard.redact(str(raw.get("summary") or "")))
    summary = cut_at_sentence(summary, SESSION_SUMMARY_MAX_CHARS)
    if not (title or bullets or summary):
        return
    await db.upsert_session_note(
        user_id,
        session_key,
        title=title,
        bullets="\n".join(bullets),
        summary=summary,
        started_at=started_at,
    )


_HIGHLIGHT_MIN_OVERLAP_CHARS = 8


def _highlight_norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", fold(text)).split())


def _duplicate_highlight(norm: str, seen: list[str]) -> bool:
    """Trùng (hoặc chứa/nằm trong) 1 highlight đã có - bỏ dấu, dấu câu, hoa thường."""
    for other in seen:
        if norm == other:
            return True
        shorter, longer = (norm, other) if len(norm) <= len(other) else (other, norm)
        if len(shorter) >= _HIGHLIGHT_MIN_OVERLAP_CHARS and shorter in longer:
            return True
    return False


async def _apply_highlights(user_id: int, raw, forget: list[dict], stats: _FlushStats) -> None:
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
    if not items:
        return
    # Chạy lại cùng cửa sổ (ghi watermark lỗi, làm lại câu trả lời, AI trích lại
    # từ dòng "ĐÃ xử lý") không được làm đầy FIFO 25 dòng bằng bản trùng.
    existing = await db.get_highlight_rows(user_id, MAX_HIGHLIGHTS_PER_USER)
    seen = [_highlight_norm(str(row.get("content") or "")) for row in existing]
    for item in items[:2]:
        text = memory_guard.safe_memory_text(str(item or ""))
        if not text or _forgotten("", text, forget):
            continue
        text = text[:_MAX_HIGHLIGHT_LEN]
        norm = _highlight_norm(text)
        if not norm or _duplicate_highlight(norm, seen):
            continue
        await db.add_highlight(user_id, text, MAX_HIGHLIGHTS_PER_USER)
        seen.append(norm)
        stats.highlights += 1


async def _process_group(
    user_id: int,
    session_key: int,
    window: list[dict],
    context_rows: list[dict],
    *,
    with_events: bool,
) -> bool:
    """1 lượt utility_json cho 1 nhóm lượt chưa xử lý của 1 phiên. True nếu xong
    (kể cả khi không có gì đáng nhớ) - False khi AI lỗi/chưa cấu hình (giữ
    watermark để lần sau thử lại)."""
    fact_rows = await db.get_fact_rows(user_id)
    shown = {int(row["id"]): row for row in fact_rows if row["key"] != STYLE_KEY}
    style_row = next((row for row in fact_rows if row["key"] == STYLE_KEY), None)
    forget = await get_forget_list(user_id)
    note = await db.get_session_note(user_id, session_key)
    prompt = build_extraction_prompt(
        facts=list(shown.values()),
        style_rules=str(style_row["value"]) if style_row else "",
        forget=forget,
        session_summary=str(note["summary"]) if note else "",
        context_rows=context_rows,
        window_rows=window,
        with_events=with_events,
    )
    data = await bounded(lambda: _utility_json(prompt))
    if not isinstance(data, dict):
        return False
    stats = _FlushStats()
    user_texts = " ".join(str(r.get("content") or "") for r in window if r["role"] == "user")
    await _apply_facts(
        user_id,
        data.get("facts"),
        shown,
        forget=forget,
        allow_forget=_forget_intent(user_texts),
        stats=stats,
    )
    await db.trim_facts(user_id, MAX_FACTS_PER_USER)
    await _apply_highlights(user_id, data.get("highlights", data.get("highlight")), forget, stats)
    await _apply_style(user_id, data.get("style_rules"), style_row)
    started_at = _as_utc(window[0]["created_at"]) if window else datetime.now(timezone.utc)
    await _apply_session(user_id, data.get("session"), session_key, started_at)
    if with_events and isinstance(data.get("events"), list):
        from services import proactive_service

        stats.events = await proactive_service.add_extracted_events(
            user_id, data["events"], [r for r in window if r["role"] == "user"]
        )
    logger.info(
        "Trích nhớ user_id=%s: %d lượt, %d fact, %d highlight, %d sự kiện.",
        user_id,
        sum(1 for r in window if r["role"] == "user"),
        stats.facts,
        stats.highlights,
        stats.events,
    )
    return True


def _memory_user_rows(rows: list[dict]) -> list[dict]:
    return [
        r
        for r in rows
        if r["role"] == "user"
        and (r.get("kind") or conversation_log.KIND_CHAT) in conversation_log.MEMORY_KINDS
        and not is_trivial(str(r.get("content") or ""))
    ]


async def flush(user_id: int) -> bool:
    """Xử lý cửa sổ chưa trích nhớ của 1 người (gọi khi đã giữ khoá user). Trả
    True nếu watermark đã tiến (xử lý xong hoặc không có gì đáng xử lý)."""
    if not await is_enabled(user_id):
        await _skip_to_latest(user_id)
        return True
    recent = await db.get_recent_chat_rows(user_id, _SCAN_ROWS, _ROW_MAX_CHARS)
    if not recent:
        return True
    watermark = await _get_watermark(user_id)
    pending = [r for r in reversed(recent) if int(r["id"]) > watermark]
    if not pending:
        return True
    latest_id = int(pending[-1]["id"])
    if not _memory_user_rows(pending):
        await _set_watermark(user_id, latest_id)
        return True
    starts = _session_starts(recent)
    groups: dict[int, list[dict]] = {}
    for row in pending:
        start, known = starts[int(row["id"])]
        key = await _resolve_session_key(user_id, start, known)
        groups.setdefault(key, []).append(row)
    keys = sorted(groups)[-_MAX_GROUPS_PER_FLUSH:]
    from services import proactive_service

    with_events = await proactive_service.events_enabled(user_id)
    for key in keys:
        group = groups[key]
        if not _memory_user_rows(group):
            continue
        window = _fit_window(group, WINDOW_MAX_CHARS)
        if not window:
            continue
        first_id = int(window[0]["id"])
        context = [
            r
            for r in reversed(recent)
            if int(r["id"]) < first_id and starts[int(r["id"])][0] == starts[first_id][0]
        ][-_CONTEXT_ROWS:]
        done = await _process_group(user_id, key, window, context, with_events=with_events)
        if not done:
            return False
        await _set_watermark(user_id, int(group[-1]["id"]))
    await _set_watermark(user_id, latest_id)
    return True


async def _skip_to_latest(user_id: int) -> None:
    """Trí nhớ tắt (hoặc vừa /forget): bỏ qua lịch sử hiện có - bật lại không
    trích ngược những gì đã nói lúc tắt."""
    try:
        recent = await db.get_recent_chat_rows(user_id, 1, 0)
    except Exception:
        return
    if recent:
        await _set_watermark(user_id, int(recent[0]["id"]))


async def _should_flush(user_id: int, *, idle: bool) -> bool:
    recent = await db.get_recent_chat_rows(user_id, _SCAN_ROWS, 60)
    if not recent:
        return False
    watermark = await _get_watermark(user_id)
    pending = [r for r in reversed(recent) if int(r["id"]) > watermark]
    if not pending:
        return False
    meaningful = _memory_user_rows(pending)
    if not meaningful:
        # Chỉ toàn "ok"/lượt cổ phiếu, thời tiết...: tiến watermark, không gọi AI.
        await _set_watermark(user_id, int(pending[-1]["id"]))
        return False
    if len(meaningful) >= FLUSH_EXCHANGES:
        return True
    starts = _session_starts(recent)
    first_start = starts[int(meaningful[0]["id"])][0]
    if first_start != starts[int(recent[0]["id"])][0] or _session_expired(recent):
        return True  # phiên chứa lượt chưa xử lý đã kết thúc
    # Lượt chưa xử lý cũ nhất sắp trôi khỏi cửa sổ lịch sử của prompt.
    window_rows = max(2, config.CHAT_HISTORY_TURNS * 2)
    newer = sum(1 for r in recent if int(r["id"]) >= int(meaningful[0]["id"]))
    if newer >= window_rows - 2:
        return True
    if idle:
        age = (datetime.now(timezone.utc) - _as_utc(recent[0]["created_at"])).total_seconds()
        return age >= IDLE_FLUSH_SEC
    return False


async def maybe_flush(user_id: int, *, explicit: bool = False, idle: bool = False) -> bool:
    """Flush nếu tới ngưỡng. Không raise (tác vụ nền)."""
    try:
        async with _slots:
            with turn_deadline(config.MEMORY_UPDATE_TIMEOUT_SEC):
                async with user_lock(user_id):
                    if not explicit and not await _should_flush(user_id, idle=idle):
                        return False
                    return await flush(user_id)
    except TurnTimeoutError as exc:
        logger.warning("Trích nhớ nền hết hạn chót user_id=%s: %s", user_id, exc)
    except Exception:
        logger.warning(
            "Lỗi khi trích trí nhớ cho user_id=%s (bỏ qua, không ảnh hưởng chat chính).",
            user_id,
            exc_info=True,
        )
    return False


def _track(task: asyncio.Task) -> None:
    _background_tasks.add(task)

    def _done(completed: asyncio.Task) -> None:
        _background_tasks.discard(completed)
        for uid, pending in list(_pending.items()):
            if pending is completed:
                _pending.pop(uid, None)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            logger.exception("Tác vụ trích trí nhớ chạy nền bị lỗi")

    task.add_done_callback(_done)


def after_exchange(user_id: int, *, explicit: bool = False) -> None:
    """Gọi sau mỗi lượt cần trích nhớ đã lưu: lên lịch maybe_flush chạy nền
    (mỗi người tối đa 1 task đang chờ; tổng có trần - vòng quét nền bắt kịp)."""
    existing = _pending.get(user_id)
    if existing is not None and not existing.done():
        return
    if len(_pending) >= _MAX_PENDING_TASKS:
        return
    try:
        # Context RỖNG: task không thừa hưởng hạn chót của lượt chat đang chạy
        # (handle_channel_text/chat_router bọc cả lượt trong turn_deadline) -
        # maybe_flush tự đặt hạn chót MEMORY_UPDATE_TIMEOUT_SEC riêng.
        context = contextvars.Context()
        # Chỉ mang theo MÃ LƯỢT (log/provider_calls của lượt trích nhớ gom về câu
        # trả lời đã sinh ra nó), không mang số đo lượt hay hạn chót.
        context.run(trace.adopt, trace.current())
        task = asyncio.get_running_loop().create_task(
            maybe_flush(user_id, explicit=explicit), context=context
        )
    except RuntimeError:
        return
    _pending[user_id] = task
    _track(task)


async def update_memory(user_id: int, user_text: str = "", model_text: str = "") -> None:
    """Tương thích cũ: trích ngay cửa sổ chưa xử lý của người dùng (lượt đã nằm
    trong chat_messages). Không raise."""
    await maybe_flush(user_id, explicit=True)


async def flush_before_reset(user_id: int, timeout_sec: float = 20.0) -> None:
    """/reset sắp xoá lịch sử phiên: trích nốt các lượt chưa xử lý (có trần thời
    gian riêng, lệnh /reset không phải chờ lâu). Hết giờ/lỗi thì chấp nhận mất."""
    try:
        with turn_deadline(timeout_sec):
            # wait_for: chờ semaphore/khoá user không nằm trong bounded().
            await asyncio.wait_for(maybe_flush(user_id, explicit=True), timeout_sec + 5)
    except TimeoutError:
        logger.warning("Trích nhớ trước /reset quá %ss - bỏ qua.", timeout_sec)


async def stop_background_tasks() -> None:
    await stop_tracked_tasks(_background_tasks, timeout=15.0, logger=logger, label="memory flush")
    _pending.clear()


# ─── Gộp fact (tuỳ chọn) ────────────────────────────────────────────────────

_CONSOLIDATION_INSTRUCTION = """Bạn là bộ dọn trí nhớ nội bộ. Danh sách fact dưới đây sắp đầy. Tìm các fact
TRÙNG NGHĨA hoặc lỗi thời rõ ràng và trả về DUY NHẤT 1 JSON:
{"ops": [{"op": "update", "id": 3, "value": "giá trị đã gộp"}, {"op": "invalidate", "id": 8}]}
- Gộp 2 fact trùng nghĩa: "update" fact giữ lại (giá trị gộp), "invalidate" fact kia.
- Chỉ dùng id có trong danh sách; không chắc thì để "ops": [].
- Không bịa thông tin mới; không đụng fact "người dùng tự nhập".
"""


async def consolidate(user_id: int) -> int:
    """1 lượt gộp fact trùng nghĩa (gọi khi đã giữ khoá user). Trả số thao tác."""
    rows = await db.get_fact_rows(user_id)
    shown = {
        int(r["id"]): r
        for r in rows
        if r["key"] != STYLE_KEY and not r.get("pinned") and r.get("source") != "user"
    }
    if len(rows) < int(MAX_FACTS_PER_USER * 0.8) or len(shown) < 2:
        return 0
    prompt = f"{_CONSOLIDATION_INSTRUCTION}\n### Fact\n{_facts_block(list(shown.values()))}"
    data = await bounded(lambda: _utility_json(prompt))
    if not isinstance(data, dict):
        return 0
    stats = _FlushStats()
    await _apply_facts(
        user_id,
        [op for op in (data.get("ops") or []) if isinstance(op, dict) and op.get("op") != "add"],
        shown,
        forget=await get_forget_list(user_id),
        allow_forget=False,
        stats=stats,
    )
    return stats.facts


async def _consolidate_if_due(user_id: int) -> None:
    if not _consolidation_enabled():
        return
    today = datetime.now(_VN_TZ).date().isoformat()
    key = personal_settings.memory_consolidated_key(user_id)
    if await db.get_setting(key) == today:
        return
    async with _slots:
        with turn_deadline(config.MEMORY_UPDATE_TIMEOUT_SEC):
            async with user_lock(user_id):
                await db.set_setting(key, today)
                changed = await consolidate(user_id)
    if changed:
        logger.info("Gộp fact user_id=%s: %d thao tác.", user_id, changed)


async def sweep_once() -> None:
    """Quét nền: trích nhớ người đã rảnh có lượt chưa xử lý; gộp fact khi đầy."""
    since = datetime.now(timezone.utc) - timedelta(days=2)
    try:
        users = await db.users_with_chat_since(since, 50)
    except Exception:
        logger.warning("Quét trích nhớ nền: không đọc được danh sách người dùng.", exc_info=True)
        return
    for user_id in users:
        await maybe_flush(user_id, idle=True)
        try:
            await _consolidate_if_due(user_id)
        except TurnTimeoutError:
            logger.warning("Gộp fact user_id=%s hết hạn chót.", user_id)
        except Exception:
            logger.warning("Gộp fact user_id=%s lỗi.", user_id, exc_info=True)


async def _sweep_loop() -> None:
    # Lượt đầu ngay khi khởi động: bắt kịp các lượt chưa xử lý trước lúc restart.
    trace.detach()
    while True:
        try:
            with trace.job("memory_sweep"):
                await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Vòng quét trích nhớ nền lỗi.")
        await asyncio.sleep(SWEEP_INTERVAL_SEC)


def start() -> None:
    global _sweep_task
    if os.getenv("MEMORY_SWEEP_ENABLED", "1").strip() == "0":
        return
    if _sweep_task is None or _sweep_task.done():
        _sweep_task = asyncio.create_task(_sweep_loop())


async def stop() -> None:
    global _sweep_task
    task, _sweep_task = _sweep_task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await stop_background_tasks()


# ─── Đọc: ráp khối trí nhớ cho lượt chat ────────────────────────────────────

# Giữa phiên: chỉ chèn fact/highlight/ghi chú LIÊN QUAN câu hỏi (tối đa chừng
# này ký tự), thêm tóm tắt phiên riêng.
MID_SESSION_MEMORY_MAX_CHARS = 800
_MID_SESSION_MIN_OVERLAP = 1
_NOTES_SCAN = 200
_NOTES_MAX_CHARS = 300
_SESSION_NOTES_AT_START = 10

_START_HEADER = (
    "[TRÍ NHỚ VỀ NGƯỜI DÙNG - nền để cá nhân hoá, KHÔNG chủ động liệt kê/đọc lại "
    "khối này trừ khi câu hỏi cần. Câu chào/câu ngắn thì bỏ qua và trả lời ngắn.]"
)
_MID_HEADER = "[TRÍ NHỚ LIÊN QUAN CÂU HỎI - dùng khi cần, không đọc lại nguyên văn.]"
_SESSION_SUMMARY_HEADER = "[TÓM TẮT PHIÊN NÀY - các lượt đầu đã trôi khỏi lịch sử]"
_STYLE_HEADER = (
    "CÁCH TRẢ LỜI NGƯỜI DÙNG ĐÃ DẶN (áp dụng khi không trái các quy tắc trên; KHÔNG "
    "đổi cách xưng hô, KHÔNG đổi action/số liệu cổ phiếu do hệ thống quyết định):"
)


@dataclass(frozen=True)
class TurnMemory:
    context: str = ""
    style_rules: str = ""


@dataclass
class _Budget:
    left: int
    lines: list[str] = field(default_factory=list)

    def add(self, line: str) -> bool:
        cost = len(line) + 1
        if not line or cost > self.left:
            return False
        self.lines.append(line)
        self.left -= cost
        return True


def cut_at_sentence(text: str, max_chars: int) -> str:
    """Cắt ở ranh giới câu gần nhất (không cắt giữa câu); câu đầu quá dài thì ở
    ranh giới từ kèm "…"."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "), window.rfind("\n"))
    if cut >= max_chars // 3:
        return window[: cut + 1].strip()
    space = window.rfind(" ")
    return (window[:space] if space > 0 else window[: max_chars - 1]).rstrip() + "…"


def _since(row: dict) -> str:
    value = row.get("valid_from") or row.get("created_at")
    if not isinstance(value, datetime):
        return ""
    return f" (từ {_as_utc(value).astimezone(_VN_TZ):%m/%Y})"


def _fact_line(row: dict, *, with_since: bool = True) -> str:
    pin = "📌 " if row.get("pinned") else ""
    since = _since(row) if with_since else ""
    return f"- {pin}{row['key']} = {row['value']}{since}"


def _tokens(text: str) -> set[str]:
    from services.rag_service import tokenize

    return tokenize(text)


def select_relevant_memory(
    query: str,
    facts: list[tuple[str, str]],
    highlights: list[str],
    *,
    max_chars: int = MID_SESSION_MEMORY_MAX_CHARS,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Chọn fact/highlight có token trùng với câu hỏi (bỏ dấu, bỏ stopword mở
    rộng - "anh", "em", "không"... không còn khớp mọi câu), xếp theo số token
    trùng giảm dần, cắt theo ngân sách ký tự."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return [], []
    scored_facts = []
    for key, value in facts:
        overlap = len(query_tokens & _tokens(f"{key.replace('_', ' ')} {value}"))
        if overlap >= _MID_SESSION_MIN_OVERLAP:
            scored_facts.append((overlap, key, value))
    scored_highlights = []
    for item in highlights:
        overlap = len(query_tokens & _tokens(item))
        if overlap >= _MID_SESSION_MIN_OVERLAP:
            scored_highlights.append((overlap, item))
    scored_facts.sort(key=lambda t: -t[0])
    scored_highlights.sort(key=lambda t: -t[0])
    budget = max_chars
    chosen_facts: list[tuple[str, str]] = []
    for _, key, value in scored_facts:
        cost = len(key) + len(value) + 3
        if cost > budget:
            break
        chosen_facts.append((key, value))
        budget -= cost
    chosen_highlights: list[str] = []
    for _, item in scored_highlights:
        if len(item) + 4 > budget:
            break
        chosen_highlights.append(item)
        budget -= len(item) + 4
    return chosen_facts, chosen_highlights


def relevant_notes(query: str, notes: list[dict], limit: int = 3) -> list[dict]:
    """Ghi chú có token trùng câu hỏi (nhiều token trùng trước, mới trước)."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    scored = []
    for index, note in enumerate(notes):
        overlap = len(query_tokens & _tokens(str(note.get("content") or "")))
        if overlap:
            scored.append((-overlap, index, note))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [note for _, _, note in scored[:limit]]


def _note_line(note: dict) -> str:
    created = note.get("created_at")
    when = (
        f" ({_as_utc(created).astimezone(_VN_TZ):%d/%m/%Y})"
        if isinstance(created, datetime)
        else ""
    )
    return f"- Ghi chú{when}: {note['content']}"


def _session_note_line(note: dict) -> str:
    started = note.get("started_at") or note.get("updated_at")
    day = f"{_as_utc(started).astimezone(_VN_TZ):%d/%m}" if isinstance(started, datetime) else ""
    bullets = "; ".join(
        b.strip("- ").strip() for b in str(note.get("bullets") or "").splitlines() if b.strip()
    )
    title = str(note.get("title") or "").strip()
    body = " — ".join(part for part in (title, bullets) if part)
    return f"- {day}: {body}" if body else ""


async def _current_session_key(user_id: int, recent: list[dict]) -> int | None:
    if not recent or _session_expired(recent):
        return None
    starts = _session_starts(recent)
    start, known = starts[int(recent[0]["id"])]
    return await _resolve_session_key(user_id, start, known)


def _style_text(fact_rows: list[dict]) -> str:
    row = next((r for r in fact_rows if r["key"] == STYLE_KEY), None)
    if not row:
        return ""
    return f"{_STYLE_HEADER}\n{str(row['value'])[:STYLE_RULES_MAX_CHARS]}"


async def build_turn_memory(user_id: int, query: str = "") -> TurnMemory:
    """Khối trí nhớ cho lượt chat + quy tắc cách trả lời. Fail-open: DB lỗi thì
    trả rỗng (lượt chat vẫn trả lời, chỉ thiếu trí nhớ)."""
    try:
        return await _build_turn_memory(user_id, query)
    except Exception:
        logger.warning(
            "Không đọc được trí nhớ dài hạn user_id=%s (DB lỗi) - trả lời không kèm trí nhớ.",
            user_id,
            exc_info=True,
        )
        return TurnMemory()


async def build_memory_context(user_id: int, query: str = "") -> str:
    """Chỉ phần khối trí nhớ (tương thích cũ) - xem build_turn_memory."""
    return (await build_turn_memory(user_id, query)).context


async def _build_turn_memory(user_id: int, query: str) -> TurnMemory:
    if not await is_enabled(user_id):
        return TurnMemory()
    recent = await db.get_recent_chat_rows(user_id, _SCAN_ROWS, 0)
    new_session = _session_expired(recent)
    # Ghi chú chỉ cần khi câu hỏi có token nội dung (tìm ghi chú liên quan).
    fact_rows, highlight_rows, notes = await asyncio.gather(
        db.get_fact_rows(user_id),
        db.get_highlight_rows(user_id, MAX_HIGHLIGHTS_PER_USER),
        db.get_recent_notes(user_id, _NOTES_SCAN, _NOTES_MAX_CHARS)
        if _tokens(query)
        else _no_rows(),
    )
    style = _style_text(fact_rows)
    facts = [r for r in fact_rows if r["key"] != STYLE_KEY]
    total = _memory_max_chars()
    if new_session:
        context = await _start_block(user_id, query, facts, highlight_rows, notes, total)
    else:
        context = await _mid_block(user_id, query, recent, facts, highlight_rows, notes, total)
    return TurnMemory(context=context, style_rules=style)


async def _no_rows() -> list[dict]:
    return []


def _memory_max_chars() -> int:
    from ai import prompt_budget

    return prompt_budget.MEMORY_MAX_CHARS


async def _start_block(
    user_id: int,
    query: str,
    facts: list[dict],
    highlight_rows: list[dict],
    notes: list[dict],
    total: int,
) -> str:
    summary = await db.get_summary(user_id)
    session_notes = await db.get_session_notes(user_id, _SESSION_NOTES_AT_START)
    if not facts and not summary and not highlight_rows and not session_notes:
        return ""
    budget = _Budget(total)
    budget.add(_START_HEADER)
    used: set[int] = set()
    # 1) Ghim + quan trọng nhất.
    core_rows = [r for r in facts if r.get("pinned") or int(r.get("importance") or 2) >= 3]
    if core_rows:
        budget.add("Thông tin bền về người dùng:")
        for row in core_rows:
            if budget.add(_fact_line(row)):
                used.add(int(row["id"]))
    # 2) Liên quan câu hỏi: fact + ghi chú.
    if query:
        chosen, _ = select_relevant_memory(
            query, [(r["key"], r["value"]) for r in facts if int(r["id"]) not in used], []
        )
        chosen_keys = {k for k, _ in chosen}
        for row in facts:
            if row["key"] in chosen_keys and budget.add(_fact_line(row)):
                used.add(int(row["id"]))
        for note in relevant_notes(query, notes):
            budget.add(_note_line(note))
    # 3) Fact còn lại (quan trọng -> mới), chừa chỗ cho ghi chú phiên/highlight.
    others = [r for r in facts if int(r["id"]) not in used]
    if others:
        fact_budget = _Budget(max(0, min(total // 3, budget.left - total // 3)))
        for row in others:
            fact_budget.add(_fact_line(row, with_since=False))
        if fact_budget.lines:
            budget.add("Thông tin khác:")
            for line in fact_budget.lines:
                budget.add(line)
    # 4) Các phiên trước.
    note_lines = [line for line in (_session_note_line(n) for n in session_notes) if line]
    if note_lines:
        budget.add("Các lần trò chuyện gần đây:")
        for line in note_lines:
            if not budget.add(line):
                break
    # 5) Highlight mới nhất.
    if highlight_rows:
        budget.add("Chi tiết đáng nhớ gần đây:")
        for row in highlight_rows:
            if not budget.add(f"- {row['content']}"):
                break
    # 6) Tóm tắt kiểu cũ: cắt ở ranh giới câu trong phần còn lại.
    if summary and budget.left > 120:
        budget.add("Tóm tắt cũ: " + cut_at_sentence(summary, budget.left - 20))
    return "\n".join(budget.lines) if len(budget.lines) > 1 else ""


async def _mid_block(
    user_id: int,
    query: str,
    recent: list[dict],
    facts: list[dict],
    highlight_rows: list[dict],
    notes: list[dict],
    total: int,
) -> str:
    budget = _Budget(total)
    session_key = await _current_session_key(user_id, recent)
    if session_key is not None:
        note = await db.get_session_note(user_id, session_key)
        summary = str(note["summary"]).strip() if note else ""
        if summary:
            budget.add(_SESSION_SUMMARY_HEADER)
            budget.add(cut_at_sentence(summary, SESSION_SUMMARY_MAX_CHARS))
    if not query:
        return "\n".join(budget.lines)
    relevant = _Budget(min(budget.left, MID_SESSION_MEMORY_MAX_CHARS))
    chosen, highlights = select_relevant_memory(
        query,
        [(r["key"], r["value"]) for r in facts],
        [str(r["content"]) for r in reversed(highlight_rows)],
    )
    if chosen:
        relevant.add("- " + "; ".join(f"{k}={v}" for k, v in chosen))
    for item in highlights:
        relevant.add(f"- {item}")
    for note in relevant_notes(query, notes):
        relevant.add(_note_line(note))
    if _PAST_QUERY_RE.search(fold(query)):
        old = await db.get_superseded_facts(user_id, 10)
        query_tokens = _tokens(query)
        for row in old:
            if query_tokens & _tokens(f"{str(row['key']).replace('_', ' ')} {row['value']}"):
                until = row.get("invalid_at")
                when = (
                    f" (đến {_as_utc(until).astimezone(_VN_TZ):%m/%Y})"
                    if isinstance(until, datetime)
                    else ""
                )
                relevant.add(f"- Trước đây: {row['key']} = {row['value']}{when}")
    if relevant.lines:
        budget.add(_MID_HEADER)
        for line in relevant.lines:
            budget.add(line)
    return "\n".join(budget.lines)


async def clear_memory(user_id: int) -> None:
    """Xoá sạch trí nhớ dài hạn (facts + summary + highlights + ghi chú phiên).
    Lịch sử phiên (chat_messages) KHÔNG bị ảnh hưởng - dùng /reset cho việc đó;
    watermark nhảy tới tin mới nhất để lịch sử cũ không bị trích lại."""
    async with user_lock(user_id):
        await db.clear_facts(user_id)
        await db.set_summary(user_id, "")
        await db.clear_highlights(user_id)
        await db.clear_session_notes(user_id)
        await _skip_to_latest(user_id)
