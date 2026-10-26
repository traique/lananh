"""Người dùng tự xem/sửa trí nhớ của mình (lệnh /memory, /quen, /nho, /ghim,
/notes, /xoaghichu, /xuatdulieu, /xoadulieu - xem services/memory_commands.py).

Mọi thao tác sửa chạy dưới memory_service.user_lock(user_id) để không giẫm lên
lượt trích nhớ nền đang đọc-rồi-ghi; lệnh người dùng chờ khoá có trần
(memory_service.interactive_lock) rồi báo bận thay vì treo cả lượt trả lời.
Chỉ đụng dữ liệu của CHÍNH user_id đó (Zalo: internal_user_id riêng, tách hẳn
Telegram).
"""

from __future__ import annotations

import functools
import io
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import ParamSpec
from zoneinfo import ZoneInfo

from core import database as db
from core.text_normalize import fold
from services import memory_guard, memory_service, personal_settings

logger = logging.getLogger(__name__)
P = ParamSpec("P")

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_MAX_AMBIGUOUS_SHOWN = 8
NOTES_LIST_LIMIT = 10
_NOTES_SCAN = 200
EXPORT_MAX_BYTES = 1_000_000
EXPORT_TEXT_MAX_CHARS = 5_000
_STYLE_PREFIX_RE = re.compile(
    r"^(?:cách trả lời|cach tra loi|khi trả lời|khi tra loi|trả lời|tra loi)\s*[:\-]?\s*",
    re.IGNORECASE,
)
_REMEMBER_PREFIX_RE = re.compile(
    r"^(?:em\s+)?(?:hãy\s+|hay\s+)?(?:ghi\s+)?(?:nhớ|nho)(?:\s+(?:giúp|giup|hộ|ho|dùm|dum))?"
    r"(?:\s+(?:anh|em|mình|minh|chị|chi|tôi|toi|cháu|chau))?(?:\s+(?:là|la|rằng|rang))?\s*[:,]?\s*",
    re.IGNORECASE,
)
_KEY_VALUE_RE = re.compile(r"^(.{2,60}?)(?:\s+là\s+|\s+la\s+|\s*[:=]\s*)(.+)$", re.IGNORECASE)
# Phần trước "là" chỉ là đại từ/cách xưng hô ("anh là người Hà Nội", "mình là
# dân IT"): không dùng làm key - mọi câu tự giới thiệu sẽ chung 1 key và lần
# /nho sau âm thầm thay lần trước.
_PRONOUN_KEYS = frozenset(
    {
        "anh",
        "minh",
        "toi",
        "chi",
        "chu",
        "bac",
        "chau",
        "ban",
        "tao",
        "ong",
        "anh_ay",
        "chi_ay",
        "em_ay",
        "ong_ay",
        "ba_ay",
    }
)
# Key ngắn hơn thế ("em", "co", "ba", "vo"...) cũng không dùng.
_MIN_KEY_CHARS = 3
# Token trông như giá trị bí mật (có chữ số, >= 4 ký tự: "abc12345", "Abc@2024").
_SECRET_VALUE_RE = re.compile(r"(?=\S*\d)\S{4,}")
_SECRET_REFUSAL = (
    "Cái này có vẻ là {label} nên em không lưu vào trí nhớ để giữ an toàn. "
    "Nên cất ở trình quản lý mật khẩu nhé."
)


def _busy_safe(func: Callable[P, Awaitable[str]]) -> Callable[P, Awaitable[str]]:
    """Lệnh tương tác chờ khoá trí nhớ có trần: quá trần thì báo bận (không treo
    lượt trả lời khi trích nhớ nền đang giữ khoá trong lúc gọi AI)."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        try:
            return await func(*args, **kwargs)
        except memory_service.MemoryBusy:
            logger.info("Lệnh trí nhớ %s: khoá đang bận, báo người dùng thử lại.", func.__name__)
            return memory_service.MEMORY_BUSY_TEXT

    return wrapper


@dataclass
class MemoryItems:
    facts: list[dict] = field(default_factory=list)  # gồm cả "cách trả lời"
    highlights: list[dict] = field(default_factory=list)
    session_notes: list[dict] = field(default_factory=list)
    enabled: bool = True

    def numbered(self) -> list[tuple[int, str, dict]]:
        """[(số thứ tự, "fact"|"highlight", dòng)] - đúng thứ tự /memory hiển thị."""
        out: list[tuple[int, str, dict]] = []
        for row in self.facts:
            out.append((len(out) + 1, "fact", row))
        for row in self.highlights:
            out.append((len(out) + 1, "highlight", row))
        return out


def _label(key: str) -> str:
    return key.replace("_", " ")


def _vn(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_VN_TZ)


def _item_text(kind: str, row: dict) -> str:
    if kind == "fact":
        pin = "📌 " if row.get("pinned") else ""
        since = _vn(row.get("valid_from") or row.get("created_at"))
        when = f" (từ {since:%m/%Y})" if since else ""
        return f"{pin}{_label(str(row['key']))}: {row['value']}{when}"
    return str(row["content"])


async def list_items(user_id: int) -> MemoryItems:
    facts = await db.get_fact_rows(user_id)
    # Cách trả lời xếp cuối nhóm fact cho dễ đọc.
    facts.sort(key=lambda r: r["key"] == memory_service.STYLE_KEY)
    return MemoryItems(
        facts=facts,
        highlights=await db.get_highlight_rows(user_id, memory_service.MAX_HIGHLIGHTS_PER_USER),
        session_notes=await db.get_session_notes(user_id, 5),
        enabled=await memory_service.is_enabled(user_id),
    )


def format_listing(items: MemoryItems, *, about: str = "") -> str:
    """Danh sách đánh số cho /memory. ``about``: "anh"/"chị"... ("" = trung tính)."""
    title = f"🧠 Trí nhớ dài hạn của em về {about}" if about else "🧠 Trí nhớ dài hạn"
    status = "BẬT" if items.enabled else "TẮT"
    toggle = "/memory off để tắt" if items.enabled else "/memory on để bật lại"
    lines = [f"{title} ({status} - {toggle})"]
    numbered = items.numbered()
    if not numbered and not items.session_notes:
        lines.append("\nEm chưa nhớ gì dài hạn cả.")
        return "\n".join(lines)
    facts = [(n, k, r) for n, k, r in numbered if k == "fact"]
    plain = [(n, r) for n, k, r in facts if r["key"] != memory_service.STYLE_KEY]
    style = [(n, r) for n, k, r in facts if r["key"] == memory_service.STYLE_KEY]
    if plain:
        lines.append(f"\nThông tin ({len(plain)}/{memory_service.MAX_FACTS_PER_USER}):")
        lines.extend(f"{n}. {_item_text('fact', r)}" for n, r in plain)
    if style:
        lines.append("\nCách trả lời đã dặn:")
        lines.extend(f"{n}. {r['value']}" for n, r in style)
    highlights = [(n, r) for n, k, r in numbered if k == "highlight"]
    if highlights:
        lines.append("\nChi tiết đáng nhớ gần đây:")
        lines.extend(f"{n}. {r['content']}" for n, r in highlights)
    note_lines = []
    for note in items.session_notes:
        started = _vn(note.get("started_at") or note.get("updated_at"))
        title_text = str(note.get("title") or "").strip() or "(không tiêu đề)"
        note_lines.append(f"• {started:%d/%m}: {title_text}" if started else f"• {title_text}")
    if note_lines:
        lines.append("\nCác lần trò chuyện gần đây:")
        lines.extend(note_lines)
    lines.append(
        "\n/quen <số|từ khoá> - quên 1 mục · /nho <điều cần nhớ> - ghim 1 điều · "
        "/ghim, /boghim <số> · /forget - xoá hết trí nhớ"
    )
    return "\n".join(lines)


# ─── Số thứ tự ổn định cho /quen, /ghim <số> ─────────────────────────────────
# Số trong /memory tính lại mỗi lần đọc (fact xếp theo updated_at, highlight mới
# nhất trước) nên trích nhớ nền chen giữa /memory và /quen <số> có thể dời số ->
# xoá nhầm mục. Giữ bản đồ số -> (loại, id) của danh sách người dùng VỪA XEM (có
# trần người + hạn); số không có bản đồ còn hạn thì hỏi xác nhận trước khi xoá.
LISTING_TTL_SEC = 600.0
_LISTING_MAX_USERS = 256
_listings: OrderedDict[int, tuple[float, dict[int, tuple[str, int]]]] = OrderedDict()
_NUMBER_SELECTOR_RE = re.compile(r"^(\d{1,4})(?:\s+(xacnhan|xác nhận|xac nhan))?$", re.IGNORECASE)


def remember_listing(user_id: int, items: MemoryItems) -> None:
    """Ghi lại số thứ tự vừa hiện cho người dùng (/memory, danh sách khớp)."""
    mapping = {n: (kind, int(row["id"])) for n, kind, row in items.numbered() if "id" in row}
    _listings[user_id] = (time.monotonic(), mapping)
    _listings.move_to_end(user_id)
    while len(_listings) > _LISTING_MAX_USERS:
        _listings.popitem(last=False)


def forget_listing(user_id: int) -> None:
    _listings.pop(user_id, None)


def _shown_listing(user_id: int) -> dict[int, tuple[str, int]] | None:
    entry = _listings.get(user_id)
    if entry is None:
        return None
    stamp, mapping = entry
    if time.monotonic() - stamp > LISTING_TTL_SEC:
        _listings.pop(user_id, None)
        return None
    return mapping


def _row_by_id(items: MemoryItems, kind: str, row_id: int) -> dict | None:
    rows = items.facts if kind == "fact" else items.highlights
    return next((row for row in rows if int(row.get("id") or 0) == row_id), None)


def _changed_text(index: int) -> str:
    return (
        f"Mục số {index} đã thay đổi từ lúc xem /memory (em vừa cập nhật trí nhớ) - "
        "gõ /memory xem lại rồi chọn số nhé."
    )


def _resolve_shown(
    items: MemoryItems, shown: dict[int, tuple[str, int]], index: int
) -> tuple[int, str, dict] | str:
    """(số, loại, dòng) theo danh sách đã xem, hoặc câu báo lỗi."""
    target = shown.get(index)
    if target is None:
        return f"Không có mục số {index} trong danh sách vừa xem - gõ /memory để xem lại."
    row = _row_by_id(items, *target)
    if row is None:
        return _changed_text(index)
    return index, target[0], row


async def listing_text(user_id: int, *, about: str = "") -> str:
    """/memory: danh sách đánh số + ghi lại số thứ tự cho /quen, /ghim <số>."""
    items = await list_items(user_id)
    remember_listing(user_id, items)
    return format_listing(items, about=about)


def _match(items: MemoryItems, selector: str) -> list[tuple[int, str, dict]]:
    numbered = items.numbered()
    selector = selector.strip()
    if selector.isdigit():
        index = int(selector)
        return [item for item in numbered if item[0] == index]
    key = memory_service._normalize_key(selector)
    exact = [item for item in numbered if item[1] == "fact" and item[2]["key"] == key]
    if exact:
        return exact
    needle = fold(selector)
    if len(needle) < 2:
        return []
    return [item for item in numbered if needle in fold(_item_text(item[1], item[2]))]


@_busy_safe
async def forget_item(user_id: int, selector: str) -> str:
    """/quen <số|key|đoạn chữ>: khớp DUY NHẤT 1 mục thì xoá hẳn (cả giá trị cũ
    và highlight nhắc tới nó) và đưa vào danh sách "đừng học lại"."""
    selector = (selector or "").strip()
    if not selector:
        return "Cú pháp: /quen <số|từ khoá> - xem số thứ tự bằng /memory."
    numeric = _NUMBER_SELECTOR_RE.match(selector)
    async with memory_service.interactive_lock(user_id):
        items = await list_items(user_id)
        if numeric:
            index = int(numeric.group(1))
            shown = _shown_listing(user_id)
            if shown is None:
                # Chưa xem /memory (hoặc đã lâu): số có thể đã dời - hiện mục
                # đang mang số này và hỏi xác nhận, không xoá ngay.
                current = _match(items, str(index))
                if not current:
                    return f"Không có mục số {index} - gõ /memory để xem danh sách."
                remember_listing(user_id, items)
                _, kind, row = current[0]
                return (
                    f"Mục số {index} hiện là: {_item_text(kind, row)}\n"
                    f"Đúng mục cần quên thì gõ: /quen {index} xacnhan"
                )
            resolved = _resolve_shown(items, shown, index)
            if isinstance(resolved, str):
                return resolved
            found = [resolved]
        else:
            found = _match(items, selector)
        if not found:
            return f"Em không thấy mục nào khớp «{selector}». Gõ /memory để xem danh sách."
        if len(found) > 1:
            remember_listing(user_id, items)  # số trong câu hỏi lại dùng được ngay
            shown = "\n".join(
                f"{n}. {_item_text(kind, row)}" for n, kind, row in found[:_MAX_AMBIGUOUS_SHOWN]
            )
            return (
                f"Có {len(found)} mục khớp «{selector}» - chọn đúng số giúp em nhé:\n{shown}\n"
                "Ví dụ: /quen 3"
            )
        _, kind, row = found[0]
        if kind == "fact":
            key, value = str(row["key"]), str(row["value"])
            await db.delete_fact(user_id, key)
            # Cả highlight, ghi chú phiên, tóm tắt cũ và sự kiện hỏi thăm nhắc
            # tới giá trị này (đều được chèn vào prompt).
            await memory_service.scrub_forgotten(user_id, value)
            await memory_service.add_to_forget_list(user_id, key=key, text=value)
            return f"🗑️ Em quên rồi: {_label(key)}: {value}"
        content = str(row["content"])
        await db.delete_highlights(user_id, [int(row["id"])])
        await memory_service.add_to_forget_list(user_id, text=content)
        return f"🗑️ Em quên rồi: {content}"


def _split_key_value(text: str) -> tuple[str, str]:
    match = _KEY_VALUE_RE.match(text)
    if match and len(match.group(1).split()) <= 6:
        key = memory_service._normalize_key(match.group(1))
        value = match.group(2).strip()
        if key and value and len(key) >= _MIN_KEY_CHARS and key not in _PRONOUN_KEYS:
            return key, value
    return memory_service._normalize_key(" ".join(text.split()[:5])), text


def _secret_label(key: str, value: str) -> str | None:
    """Loại bí mật để báo người dùng, hoặc None. find_secret cần dấu tách
    ("mật khẩu là ...") nên xét thêm key đã tách ("mat_khau_wifi_nha_anh") như
    bộ trích xuất tự động (memory_guard.is_secret_fact)."""
    if memory_guard.is_secret_fact(key, value):
        return memory_guard.find_secret(f"{key.replace('_', ' ')}: {value}") or "thông tin bí mật"
    return None


@_busy_safe
async def remember(user_id: int, text: str) -> str:
    """/nho <điều cần nhớ> hoặc "nhớ giúp anh là...": fact GHIM do người dùng tự
    nhập (bộ trích xuất không sửa/xoá được). Bí mật (mật khẩu, OTP...) thì từ chối."""
    text = _REMEMBER_PREFIX_RE.sub("", (text or "").strip(), count=1).strip()
    if not text:
        return "Cú pháp: /nho <điều cần nhớ>, ví dụ /nho size giày của vợ là 38"
    secret = memory_guard.find_secret(text)
    if secret:
        return _SECRET_REFUSAL.format(label=secret)
    clean = memory_guard.clean(text)
    style = _STYLE_PREFIX_RE.match(clean)
    rule = clean[style.end() :].strip() if style else ""
    if rule:
        # "luôn kèm mật khẩu wifi abc12345" bị chặn; "đừng hỏi mật khẩu" thì không.
        leaked = memory_guard.names_secret(rule) and _SECRET_VALUE_RE.search(rule)
        secret = _secret_label("", rule) or ("thông tin bí mật" if leaked else None)
    else:
        key, value = _split_key_value(clean)
        secret = _secret_label(key, value) or _secret_label(key, clean)
    if secret:
        return _SECRET_REFUSAL.format(label=secret)
    async with memory_service.interactive_lock(user_id):
        rows = await db.get_fact_rows(user_id)
        if rule:
            current = next((r for r in rows if r["key"] == memory_service.STYLE_KEY), None)
            merged = f"{current['value']}\n- {rule}" if current else f"- {rule}"
            if len(merged) > memory_service.STYLE_RULES_MAX_CHARS:
                return (
                    "Danh sách cách trả lời đã đầy "
                    f"({memory_service.STYLE_RULES_MAX_CHARS} ký tự) - /quen bớt rồi thêm nhé."
                )
            await db.save_fact(
                user_id,
                memory_service.STYLE_KEY,
                merged,
                importance=3,
                category=memory_service.STYLE_KEY,
                source="user",
                pinned=True,
                protect=False,
            )
            return f"✅ Em sẽ trả lời theo cách này: {rule}"
        key, value = _split_key_value(clean)
        if not key:
            return "Em chưa hiểu cần nhớ điều gì, thử viết rõ hơn giúp em nhé."
        value = value[:200]
        existing = next((r for r in rows if r["key"] == key), None)
        pinned_count = sum(1 for r in rows if r.get("pinned"))
        if (existing is None or not existing.get("pinned")) and (
            pinned_count >= memory_service.MAX_PINNED_PER_USER
        ):
            return (
                f"Em đã ghim đủ {memory_service.MAX_PINNED_PER_USER} mục rồi - "
                "/boghim <số> bớt 1 mục rồi nhờ em nhớ lại nhé."
            )
        await db.save_fact(
            user_id,
            key,
            value,
            importance=3,
            category="khac",
            source="user",
            pinned=True,
            protect=False,
        )
        await db.trim_facts(user_id, memory_service.MAX_FACTS_PER_USER)
    previous = str(existing["value"]) if existing else ""
    if previous and previous != value:
        # Cùng key, giá trị khác: giá trị cũ thôi hiệu lực - nói rõ, không im lặng.
        return f"📌 Em nhớ rồi (đã cập nhật): {_label(key)}: {value} (trước đây: {previous})"
    return f"📌 Em nhớ rồi: {_label(key)}: {value}"


@_busy_safe
async def pin_item(user_id: int, selector: str, pinned: bool = True) -> str:
    command = "/ghim" if pinned else "/boghim"
    if not (selector or "").strip().isdigit():
        return f"Cú pháp: {command} <số> - xem số thứ tự bằng /memory."
    async with memory_service.interactive_lock(user_id):
        items = await list_items(user_id)
        shown = _shown_listing(user_id)
        if shown is not None:
            # Theo danh sách người dùng vừa xem (/memory) - không theo số đã dời.
            resolved = _resolve_shown(items, shown, int(selector))
            if isinstance(resolved, str):
                return resolved
            found = [resolved]
        else:
            found = _match(items, selector)
        if not found or found[0][1] != "fact":
            return (
                "Số này không phải 1 thông tin trong /memory (chi tiết đáng nhớ không ghim được)."
            )
        row = found[0][2]
        if pinned and not row.get("pinned"):
            count = sum(1 for r in items.facts if r.get("pinned"))
            if count >= memory_service.MAX_PINNED_PER_USER:
                return (
                    f"Đã ghim đủ {memory_service.MAX_PINNED_PER_USER} mục, /boghim bớt trước nhé."
                )
        await db.set_fact_pinned(user_id, int(row["id"]), pinned)
    verb = "Đã ghim" if pinned else "Đã bỏ ghim"
    return f"📌 {verb}: {_label(str(row['key']))}: {row['value']}"


# ─── Ghi chú ─────────────────────────────────────────────────────────────────


def _note_line(index: int, note: dict) -> str:
    when = _vn(note.get("created_at"))
    stamp = f" ({when:%H:%M %d/%m})" if when else ""
    return f"{index}. {note['content']}{stamp}"


async def notes_text(user_id: int, query: str = "") -> str:
    """/notes [từ khoá]: 10 ghi chú gần nhất, hoặc ghi chú khớp từ khoá (quét
    200 ghi chú gần nhất). Số thứ tự luôn theo danh sách mới nhất để
    /xoaghichu <số> xoá đúng ghi chú dù đang xem kết quả tìm kiếm."""
    notes = await db.get_recent_notes(user_id, _NOTES_SCAN)
    if not notes:
        return "📝 Chưa có ghi chú nào."
    indexed = list(enumerate(notes, start=1))
    query = (query or "").strip()
    if query:
        matched_ids = {id(n) for n in memory_service.relevant_notes(query, notes, limit=10)}
        hits = [(i, n) for i, n in indexed if id(n) in matched_ids]
        if not hits:
            return f"📝 Không có ghi chú nào khớp «{query}»."
        lines = [f"📝 Ghi chú khớp «{query}»:"]
        lines.extend(_note_line(i, n) for i, n in hits)
    else:
        lines = ["📝 Ghi chú gần đây:"]
        lines.extend(_note_line(i, n) for i, n in indexed[:NOTES_LIST_LIMIT])
        if len(notes) > NOTES_LIST_LIMIT:
            lines.append(
                f"(còn {len(notes) - NOTES_LIST_LIMIT} ghi chú cũ hơn - /notes <từ khoá> để tìm)"
            )
    lines.append("/xoaghichu <số> - xoá 1 ghi chú")
    return "\n".join(lines)


async def delete_note(user_id: int, argument: str) -> str:
    argument = (argument or "").strip().lower()
    if argument.startswith("tatca") or argument.startswith("tất cả"):
        if "xacnhan" not in argument:
            return "Xoá HẾT ghi chú thì gõ: /xoaghichu tatca xacnhan"
        count = await db.clear_notes(user_id)
        return f"🗑️ Đã xoá {count} ghi chú."
    if not argument.isdigit():
        return "Cú pháp: /xoaghichu <số> (số thứ tự trong /notes), hoặc /xoaghichu tatca xacnhan"
    notes = await db.get_recent_notes(user_id, _NOTES_SCAN)
    index = int(argument)
    if not 1 <= index <= len(notes):
        return "Không có ghi chú số này - xem lại /notes nhé."
    note = notes[index - 1]
    await db.delete_note(user_id, int(note["id"]))
    return f"🗑️ Đã xoá ghi chú: {note['content']}"


# ─── Xuất / xoá dữ liệu của chính mình ───────────────────────────────────────


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _clean_rows(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    return [{k: _jsonable(row.get(k)) for k in keys if k in row} for row in rows]


async def build_export(user_id: int) -> dict:
    """Dữ liệu cá nhân của CHÍNH user_id: trí nhớ, ghi chú, nhắc việc sắp tới,
    sự kiện hỏi thăm, lịch sử chat gần đây (đã có trần từng phần)."""
    now = datetime.now(timezone.utc)
    reminders = await db.list_reminders_between(user_id, now, now + timedelta(days=365), 50)
    return {
        "xuat_luc": now.astimezone(_VN_TZ).isoformat(),
        "tri_nho": {
            "dang_bat": await memory_service.is_enabled(user_id),
            "thong_tin": _clean_rows(
                await db.get_fact_rows(user_id),
                ("key", "value", "importance", "category", "pinned", "source", "valid_from"),
            ),
            "gia_tri_cu": _clean_rows(
                await db.get_superseded_facts(user_id, 60), ("key", "value", "invalid_at")
            ),
            "chi_tiet_dang_nho": _clean_rows(
                await db.get_highlight_rows(user_id, memory_service.MAX_HIGHLIGHTS_PER_USER),
                ("content", "created_at"),
            ),
            "cac_phien": _clean_rows(
                await db.get_session_notes(user_id, 100),
                ("title", "bullets", "summary", "started_at"),
            ),
            "tom_tat_cu": await db.get_summary(user_id),
        },
        "ghi_chu": _clean_rows(
            await db.get_recent_notes(user_id, 200, 2000), ("content", "created_at")
        ),
        "nhac_viec_sap_toi": [
            {"luc": _jsonable(due), "noi_dung": message} for due, message in reminders
        ],
        "su_kien_hoi_tham": _clean_rows(
            await db.list_user_events(user_id, 50), ("title", "when_text", "event_date", "status")
        ),
        "lich_su_chat": _clean_rows(
            list(reversed(await db.get_recent_chat_rows(user_id, 200, 2000))),
            ("role", "kind", "content", "created_at"),
        ),
    }


async def export_json_bytes(user_id: int) -> tuple[bytes, str]:
    """(nội dung .json có trần EXPORT_MAX_BYTES, tên file). Vượt trần thì bỏ
    bớt lịch sử chat cũ nhất trước."""
    data = await build_export(user_id)
    history = data["lich_su_chat"]
    raw = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    while len(raw) > EXPORT_MAX_BYTES and history:
        del history[: max(1, len(history) // 4)]
        raw = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    if len(raw) > EXPORT_MAX_BYTES:
        raw = raw[:EXPORT_MAX_BYTES]
    stamp = datetime.now(_VN_TZ).strftime("%Y%m%d")
    return raw, f"lananh_du_lieu_{stamp}.json"


def export_buffer(raw: bytes) -> io.BytesIO:
    buffer = io.BytesIO(raw)
    buffer.seek(0)
    return buffer


async def export_text(user_id: int) -> str:
    """Bản chữ có trần cho Zalo/Zoom (gateway chỉ gửi được chữ và ảnh)."""
    data = await build_export(user_id)
    memory = data["tri_nho"]
    lines = ["📦 Dữ liệu em đang lưu (bản rút gọn - Telegram có bản .json đầy đủ):"]
    if memory["thong_tin"]:
        lines.append("\nTrí nhớ:")
        lines.extend(f"• {_label(r['key'])}: {r['value']}" for r in memory["thong_tin"])
    if memory["chi_tiet_dang_nho"]:
        lines.append("\nChi tiết đáng nhớ:")
        lines.extend(f"• {r['content']}" for r in memory["chi_tiet_dang_nho"])
    if data["ghi_chu"]:
        lines.append(f"\nGhi chú ({len(data['ghi_chu'])}):")
        lines.extend(f"• {r['content']}" for r in data["ghi_chu"][:30])
    if data["nhac_viec_sap_toi"]:
        lines.append("\nNhắc việc sắp tới:")
        lines.extend(f"• {r['luc'][:16]} {r['noi_dung']}" for r in data["nhac_viec_sap_toi"])
    lines.append(f"\nLịch sử chat: {len(data['lich_su_chat'])} tin gần nhất (không in ra ở đây).")
    text = "\n".join(lines)
    if len(text) > EXPORT_TEXT_MAX_CHARS:
        text = text[: EXPORT_TEXT_MAX_CHARS - 40].rstrip() + "\n[... đã cắt bớt cho vừa tin nhắn]"
    return text


@_busy_safe
async def delete_own_data(user_id: int, argument: str = "") -> str:
    """/xoadulieu xacnhan. Zalo (id âm): xoá MỌI dữ liệu cá nhân của chính người
    đó (giữ pairing để vẫn chat tiếp). Chủ bot: chỉ trí nhớ, ghi chú, lịch sử
    chat, sự kiện hỏi thăm - KHÔNG đụng danh mục cổ phiếu/nhắc việc."""
    if "xacnhan" not in (argument or "").lower():
        scope = (
            "toàn bộ dữ liệu cá nhân (trí nhớ, ghi chú, lịch sử chat, nhắc việc, danh mục, "
            "cài đặt riêng)"
            if user_id < 0
            else "trí nhớ, ghi chú, lịch sử chat và sự kiện hỏi thăm (danh mục cổ phiếu, "
            "nhắc việc giữ nguyên)"
        )
        return (
            f"⚠️ Lệnh này xoá {scope} và KHÔNG khôi phục được. Chắc chắn thì gõ: /xoadulieu xacnhan"
        )
    async with memory_service.interactive_lock(user_id):
        if user_id < 0:
            from channels import zalo_users

            await zalo_users.delete_own_data(user_id)
        else:
            await db.clear_facts(user_id)
            await db.set_summary(user_id, "")
            await db.clear_highlights(user_id)
            await db.clear_session_notes(user_id)
            await db.clear_notes(user_id)
            await db.clear_chat(user_id)
            await db.clear_user_events(user_id)
            # Đánh giá 👍/👎 có bản chụp câu hỏi/câu trả lời (migrations/021).
            await db.answer_feedback_clear(user_id)
            for key in personal_settings.memory_user_keys(user_id):
                await db.set_setting(key, "")
    from services import chat_context

    chat_context.forget_pages(user_id)  # trang vừa đọc giữ trong RAM
    forget_listing(user_id)
    from services import answer_feedback

    answer_feedback.forget(user_id)
    logger.info("Người dùng user_id=%s tự xoá dữ liệu cá nhân.", user_id)
    return "🗑️ Đã xoá dữ liệu cá nhân em lưu. Từ giờ mình bắt đầu lại từ đầu nhé."


# ─── Trang quản trị: xem/sửa trí nhớ từng người ──────────────────────────────


async def admin_items(user_id: int) -> dict:
    """Fact hiện hành + highlight của 1 người cho trang /admin (có id để sửa)."""
    items = await list_items(user_id)
    return {
        "user_id": user_id,
        "enabled": items.enabled,
        "facts": [
            {
                "id": int(r["id"]),
                "key": r["key"],
                "value": r["value"],
                "pinned": bool(r.get("pinned")),
                "importance": int(r.get("importance") or 2),
                "source": r.get("source") or "auto",
            }
            for r in items.facts
        ],
        "highlights": [{"id": int(r["id"]), "content": r["content"]} for r in items.highlights],
    }


async def admin_edit(user_id: int, action: str, kind: str, item_id: int) -> bool:
    """action: forget | pin | unpin; kind: fact | highlight. True nếu đã đổi."""
    async with memory_service.user_lock(user_id):
        if kind == "highlight" and action == "forget":
            rows = await db.get_highlight_rows(user_id, memory_service.MAX_HIGHLIGHTS_PER_USER)
            row = next((r for r in rows if int(r["id"]) == item_id), None)
            if row is None:
                return False
            await db.delete_highlights(user_id, [item_id])
            await memory_service.add_to_forget_list(user_id, text=str(row["content"]))
            return True
        if kind != "fact":
            return False
        row = next((r for r in await db.get_fact_rows(user_id) if int(r["id"]) == item_id), None)
        if row is None:
            return False
        if action == "forget":
            await db.delete_fact(user_id, str(row["key"]))
            await memory_service.add_to_forget_list(
                user_id, key=str(row["key"]), text=str(row["value"])
            )
            return True
        if action in ("pin", "unpin"):
            return await db.set_fact_pinned(user_id, item_id, action == "pin")
    return False
