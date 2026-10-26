"""Chi tiêu cá nhân: ghi bằng chat, ảnh hoá đơn, /chitieu - tiền luôn do code đọc/cộng.

- Chat: "cafe 45k", "đổ xăng 1tr2", "2 củ tiền điện", "hôm qua ăn phở 50k" được
  đọc TẤT ĐỊNH bằng Python (k, nghìn, tr, triệu, củ, m, 1tr2, 1m5, 1.200.000đ) -
  không tốn lượt AI. Bộ lọc chặt (câu ngắn, đúng 1 số tiền có đơn vị, không phải
  câu hỏi, không dính cổ phiếu/thu nhập) để không nuốt nhầm tin trò chuyện; tắt
  bằng EXPENSE_CHAT_CAPTURE=false (khi đó router AI vẫn ghi được khi người dùng
  nói rõ "ghi chi tiêu ...").
- Danh mục theo từ khoá (cafe -> ăn uống, xăng -> đi lại...), không khớp -> "khác".
- Lưu ngay, trả lời kèm số # và cách huỷ (/chitieu xoa <số> hoặc nhắn "huỷ"
  trong 10 phút) - không cần nút bấm (Telegram chưa nhận callback_query).
- Ảnh có chữ "hoá đơn"/"bill"/"chi tiêu" trong caption: orchestrator.analyze_image
  đọc JSON {amount_vnd, date, merchant}, Python kiểm số tiền rồi lưu dạng NHÁP.
- /chitieu [hôm nay|tuần này|tháng này|tháng trước|tháng 10]: tổng theo danh mục
  cộng bằng SQL. Mục "expenses" trong bản tin sáng (MORNING_BRIEF_EXPENSES).

Trần: 100 khoản/24 giờ mỗi người. Dữ liệu theo user_id của người gọi (Zalo,
kể cả Zalo của chủ bot, tách hẳn Telegram).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from core import address, config
from core import database as db
from core.text_normalize import fold, nfc
from core.text_utils import strip_json_fence
from services import morning_brief, reply_audience, write_intent
from services import tool_registry as reg
from services.reminder_delivery import current_event_key

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_AMOUNT = 10_000_000_000  # 10 tỷ: lớn hơn thế gần như chắc chắn là đọc nhầm
MIN_AMOUNT = 1_000
MAX_PER_DAY = 100
UNDO_WINDOW = dt.timedelta(minutes=10)
MAX_NOTE_CHARS = 120
RECEIPT_TIMEOUT_SEC = 90

CATEGORIES: dict[str, str] = {
    "an_uong": "Ăn uống",
    "di_lai": "Đi lại",
    "hoa_don": "Hoá đơn",
    "nha_cua": "Nhà cửa",
    "mua_sam": "Mua sắm",
    "suc_khoe": "Sức khoẻ",
    "giai_tri": "Giải trí",
    "giao_duc": "Học hành",
    "gia_dinh": "Gia đình",
    "khac": "Khác",
}
# Từ khoá danh mục viết CÒN DẤU; câu gõ không dấu thì so bản bỏ dấu (fold).
_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "hoa_don": (
        "tiền điện",
        "tiền nước",
        "điện nước",
        "internet",
        "wifi",
        "mạng",
        "4g",
        "cước",
        "điện thoại",
        "truyền hình",
        "netflix",
        "spotify",
        "youtube",
        "icloud",
        "google one",
    ),
    "nha_cua": (
        "tiền nhà",
        "thuê nhà",
        "phòng trọ",
        "tiền phòng",
        "chung cư",
        "phí quản lý",
        "sửa nhà",
        "nội thất",
    ),
    "an_uong": (
        "cafe",
        "cà phê",
        "cafê",
        "cf",
        "trà sữa",
        "trà",
        "ăn",
        "cơm",
        "phở",
        "bún",
        "bánh",
        "nước",
        "nhậu",
        "bia",
        "đồ ăn",
        "lẩu",
        "nướng",
        "chè",
        "kem",
        "sinh tố",
        "đi chợ",
        "siêu thị",
        "grabfood",
        "shopeefood",
        "highlands",
        "starbucks",
        "phúc long",
    ),
    "di_lai": (
        "xăng",
        "grab",
        "taxi",
        "xe ôm",
        "be",
        "gojek",
        "xanh sm",
        "vé xe",
        "vé tàu",
        "vé máy bay",
        "gửi xe",
        "rửa xe",
        "sửa xe",
        "thay nhớt",
        "cầu đường",
        "bảo dưỡng",
    ),
    "mua_sam": (
        "quần áo",
        "áo",
        "quần",
        "giày",
        "dép",
        "túi",
        "shopee",
        "lazada",
        "tiki",
        "mỹ phẩm",
        "son",
    ),
    "suc_khoe": (
        "thuốc",
        "khám",
        "bệnh viện",
        "nhà thuốc",
        "nha khoa",
        "răng",
        "vitamin",
        "gym",
        "xét nghiệm",
    ),
    "giai_tri": (
        "xem phim",
        "phim",
        "cgv",
        "karaoke",
        "du lịch",
        "game",
        "concert",
        "spa",
        "massage",
        "cắt tóc",
    ),
    "giao_duc": ("học phí", "tiền học", "học thêm", "sách", "khoá học", "khóa học", "lớp học"),
    "gia_dinh": (
        "bỉm",
        "sữa",
        "đồ chơi",
        "hiếu hỉ",
        "mừng cưới",
        "đám cưới",
        "phong bì",
        "biếu",
        "lì xì",
        "quà",
    ),
}

# 1tr2, 1m5, 2k5, 45k, 45 nghìn, 2 củ, 1.5 triệu, 50.000đ, 1.200.000, 120000.
_AMOUNT_RE = re.compile(
    r"(?<![\w.,])(?P<n>\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(?:(?P<u>k|nghìn|ngàn|ng|tr|triệu|củ|m|đ|vnđ|vnd|đồng)(?P<frac>\d{1,3})?(?![\w])|(?![\w]))",
    re.IGNORECASE,
)
_UNIT_VALUE = {
    "k": 1_000,
    "nghìn": 1_000,
    "ngàn": 1_000,
    "ng": 1_000,
    "tr": 1_000_000,
    "triệu": 1_000_000,
    "củ": 1_000_000,
    "m": 1_000_000,
    "đ": 1,
    "vnđ": 1,
    "vnd": 1,
    "đồng": 1,
}
# Câu hỏi / không phải khoản chi: không bắt bằng code, để chat AI trả lời.
# "không/chưa" chỉ là câu hỏi khi đứng cuối ("cafe không đường 30k" vẫn là khoản chi).
_NOT_EXPENSE_RE = re.compile(
    r"\?|\b(?:không|ko|chưa|hả|à)\s*$|\b(?:bao nhiêu|sao|nên|có nên|giá|lương|thu nhập|được|"
    r"nhận|lãi|lỗ|thưởng|cổ phiếu|mã|vàng|usd|đô la|tỷ giá|chứng khoán|bán|lời|kiếm|vay|nợ)\b",
    re.IGNORECASE,
)
# Câu gõ không dấu: chỉ giữ từ không trùng chữ khác khi bỏ dấu (lãi/lại, bán/bạn...).
_NOT_EXPENSE_PLAIN_RE = re.compile(
    r"\?|\b(?:khong|ko|chua)\s*$|\b(?:bao nhieu|luong|thu nhap|co phieu|chung khoan|ty gia|"
    r"vang|usd)\b"
)
_TICKER_RE = re.compile(r"\b[A-Z]{3}\b")
_SPEND_VERB_RE = re.compile(
    r"^(?:(?:ghi|thêm|lưu)\s+(?:khoản\s+|lại\s+)?chi(?:\s+tiêu)?\s*:?\s*|chi\s+tiêu\s+|chi\s+|tiêu\s+)",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(r"(?:^|\s)(?:hết|mất|tốn|là|nha|nhé|nhe|ạ|rồi)(?=\s|$)", re.IGNORECASE)
# Từ gọi đệm cuối câu ("cafe 45k nha em"); "quà cho em 200k" vẫn giữ chữ "em".
_CALL_TAIL_RE = re.compile(
    r"\s+(?:(?:nha|nhé|nhe|ơi)\s+(?:em|anh|chị)|(?:em|anh|chị)\s+(?:nha|nhé|nhe|ơi))\s*$",
    re.IGNORECASE,
)
_RECEIPT_CAPTION_RE = re.compile(
    r"\b(?:hoá đơn|hóa đơn|hoa don|bill|biên lai|bien lai|receipt|chi tiêu|chi tieu)\b",
    re.IGNORECASE,
)
_UNDO_WORDS = {
    "huỷ",
    "hủy",
    "huy",
    "xoá",
    "xóa",
    "huỷ khoản đó",
    "hủy khoản đó",
    "xoá khoản đó",
    "xóa khoản đó",
}


def chat_capture_enabled() -> bool:
    return config.EXPENSE_CHAT_CAPTURE


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"


def _you() -> str:
    return address.normalize(reply_audience.current().address_form) or "anh"


def _me() -> str:
    return address.self_word(_you())


def _amount_value(match: re.Match) -> int | None:
    raw = match.group("n")
    unit = (match.group("u") or "").lower()
    frac = match.group("frac")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw) and unit in ("", "đ", "vnđ", "vnd", "đồng"):
        value = float(re.sub(r"[.,]", "", raw))  # 1.200.000 / 50,000
    else:
        value = float(raw.replace(",", "."))
    if frac:
        if unit not in ("tr", "triệu", "củ", "m", "k"):
            return None
        value += float(f"0.{frac}")  # 1tr2 = 1,2 triệu; 2k5 = 2.500
    multiplier = _UNIT_VALUE.get(unit, 1)
    amount = int(round(value * multiplier))
    return amount if MIN_AMOUNT <= amount <= MAX_AMOUNT else None


def parse_amount(text: str, *, bare_thousands: bool = False) -> tuple[int, tuple[int, int]] | None:
    """(số tiền VND, vị trí) của số tiền DUY NHẤT trong câu; None nếu 0 hoặc >1 số.
    Số trần < 1.000 chỉ hiểu là nghìn đồng khi ``bare_thousands`` (lệnh /chitieu)."""
    found: list[tuple[int, tuple[int, int]]] = []
    for m in _AMOUNT_RE.finditer(text):
        unit = m.group("u")
        if not unit:
            raw = m.group("n")
            plain = (
                float(re.sub(r"[.,]", "", raw))
                if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw)
                else None
            )
            if plain is None:
                try:
                    plain = float(raw.replace(",", "."))
                except ValueError:
                    continue
            if plain < 1000:
                if not bare_thousands:
                    continue
                amount = int(round(plain * 1000))
            else:
                amount = int(plain)
            if MIN_AMOUNT <= amount <= MAX_AMOUNT:
                found.append((amount, (m.start(), m.end())))
            continue
        amount = _amount_value(m)
        if amount is not None:
            found.append((amount, (m.start(), m.end())))
    return found[0] if len(found) == 1 else None


def amounts_in(text: str) -> set[int]:
    """Mọi số tiền CÓ ĐƠN VỊ (hoặc >= 1.000) trong câu - để soát số tiền router đưa."""
    out: set[int] = set()
    for m in _AMOUNT_RE.finditer(nfc(text or "")):
        if m.group("u"):
            amount = _amount_value(m)
        else:
            raw = m.group("n")
            amount = (
                int(re.sub(r"[.,]", "", raw))
                if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+|\d+", raw)
                else None
            )
            if amount is not None and amount < MIN_AMOUNT:
                amount = None
        if amount is not None:
            out.add(amount)
    return out


def category_of(note: str) -> str:
    low = nfc(note).lower()
    plain = fold(low) == low
    hay = fold(low) if plain else low
    best: tuple[int, str] | None = None
    for category, words in _CATEGORY_WORDS.items():
        for word in words:
            needle = fold(word) if plain else word
            m = re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay)
            if m and (best is None or len(needle) > best[0]):
                best = (len(needle), category)
    return best[1] if best else "khac"


@dataclass(frozen=True)
class ParsedExpense:
    amount: int
    note: str
    category: str
    spent_on: dt.date


def _spent_on(low: str, today: dt.date) -> tuple[dt.date, str]:
    for pattern, days in ((r"\bhôm kia\b|\bhom kia\b", 2), (r"\bhôm qua\b|\bhom qua\b", 1)):
        m = re.search(pattern, low)
        if m:
            return today - dt.timedelta(days=days), low[: m.start()] + low[m.end() :]
    m = re.search(r"\b(?:hôm nay|hom nay|nay)\b", low)
    if m:
        return today, low[: m.start()] + low[m.end() :]
    return today, low


def parse_expense(
    text: str, *, today: dt.date | None = None, strict: bool = True
) -> ParsedExpense | None:
    """Đọc 1 khoản chi. ``strict`` (bắt từ chat thường): câu ngắn, không phải câu
    hỏi, không dính cổ phiếu/thu nhập, số tiền phải có đơn vị (45k, 1tr2...) hoặc
    >= 1.000. Lệnh /chitieu và tool router dùng strict=False."""
    today = today or dt.datetime.now(VN_TZ).date()
    clean = re.sub(r"\s+", " ", nfc(text or "")).strip()
    if not clean or len(clean) > (80 if strict else 200):
        return None
    if strict:
        low_check = clean.lower()
        plain = fold(low_check) == low_check
        if (_NOT_EXPENSE_PLAIN_RE if plain else _NOT_EXPENSE_RE).search(
            fold(low_check) if plain else low_check
        ):
            return None
        if _TICKER_RE.search(clean):
            return None
        if len(clean.split()) > 10:
            return None
        if not _SPEND_VERB_RE.match(clean) and write_intent.asks_remind_or_remember(clean):
            return None  # "nhắc anh 9h mai trả 500k": nhắc việc/ghi chú, để router tool lo
    found = parse_amount(clean, bare_thousands=not strict)
    if found is None:
        return None
    amount, (start, end) = found
    rest = (clean[:start] + " " + clean[end:]).strip()
    spent_on, rest = _spent_on(rest, today)
    rest = _SPEND_VERB_RE.sub("", rest.strip())
    rest = _CALL_TAIL_RE.sub("", rest)
    rest = _FILLER_RE.sub(" ", rest)
    note = re.sub(r"\s+", " ", rest).strip(" ,.;:-")
    if not re.search(r"[^\W\d_]", note):
        return None
    if strict and len(note.split()) > 6:
        return None
    note = note[:MAX_NOTE_CHARS]
    return ParsedExpense(amount, note[:1].upper() + note[1:], category_of(note), spent_on)


@dataclass(frozen=True)
class Saved:
    expense_id: int
    parsed: ParsedExpense
    draft: bool = False

    def line(self) -> str:
        day = (
            ""
            if self.parsed.spent_on == dt.datetime.now(VN_TZ).date()
            else f" ngày {self.parsed.spent_on:%d/%m}"
        )
        draft = f" (nháp từ ảnh - kiểm lại giúp {_me()})" if self.draft else ""
        return (
            f"#{self.expense_id} {money(self.parsed.amount)} - {self.parsed.note} "
            f"[{CATEGORIES[self.parsed.category]}]{day}{draft}"
        )


async def record(
    user_id: int,
    parsed: ParsedExpense,
    *,
    source: str = "chat",
    draft: bool = False,
    key_suffix: str = "",
) -> Saved:
    if await db.expense_count_since(user_id, dt.timedelta(hours=24)) >= MAX_PER_DAY:
        raise ValueError(
            f"Đã ghi {MAX_PER_DAY} khoản trong 24 giờ qua - tạm dừng để tránh ghi nhầm hàng loạt."
        )
    base_key = current_event_key()
    event_key = f"{base_key}:chi{key_suffix}" if base_key else None
    expense_id, _ = await db.expense_add(
        user_id,
        parsed.amount,
        category=parsed.category,
        note=parsed.note,
        spent_on=parsed.spent_on.isoformat(),
        source=source,
        draft=draft,
        event_key=event_key,
    )
    return Saved(expense_id, parsed, draft)


def _saved_reply(saved: list[Saved]) -> str:
    lines = [f"💸 Dạ {_me()} ghi rồi: {s.line()}" for s in saved]
    ids = " ".join(str(s.expense_id) for s in saved)
    lines.append(f'(Nhầm thì nhắn "huỷ" trong 10 phút hoặc /chitieu xoa {ids})')
    return "\n".join(lines)


async def maybe_handle_chat(user_id: int, text: str) -> str | None:
    """Câu trả lời nếu tin là 1 khoản chi ("cafe 45k") hoặc "huỷ" ngay sau khi
    ghi; None để tin đi tiếp vào chat như thường."""
    low = re.sub(r"\s+", " ", nfc(text or "")).strip().lower().rstrip(".! ")
    if low in _UNDO_WORDS:
        try:
            last = await db.expense_last(user_id, UNDO_WINDOW)
        except Exception:
            logger.warning("Không tra được khoản chi vừa ghi.", exc_info=True)
            return None
        if not last:
            return None
        await db.expense_delete(user_id, int(last["id"]))
        return f"🗑️ Dạ {_me()} huỷ khoản #{last['id']} {money(int(last['amount_vnd']))} - {last['note']} rồi."
    if not chat_capture_enabled():
        return None
    parsed = parse_expense(text)
    if parsed is None:
        return None
    try:
        saved = await record(user_id, parsed)
    except ValueError as exc:
        return f"❌ {exc}"
    return _saved_reply([saved])


async def record_from_tool(
    user_id: int, phrases: list[str], source_text: str = ""
) -> reg.ToolResult:
    """Tool add_expense của router: số tiền vẫn do code đọc; số tiền không có trong
    chính tin nhắn người dùng (``source_text``) bị từ chối - LLM không tự đặt số."""
    saved, errors = [], []
    allowed = amounts_in(source_text) if source_text else None
    for index, phrase in enumerate(p for p in phrases if p.strip()):
        parsed = parse_expense(phrase, strict=False)
        if parsed is None:
            errors.append(f'"{phrase[:60]}": không đọc được số tiền')
            continue
        if allowed is not None and parsed.amount not in allowed:
            errors.append(f"{money(parsed.amount)} không có trong tin nhắn của người dùng")
            continue
        try:
            saved.append(await record(user_id, parsed, key_suffix=f":{index + 1}"))
        except ValueError as exc:
            errors.append(str(exc))
    if not saved:
        return reg.ToolResult(
            "CHƯA ghi khoản chi: "
            + "; ".join(errors or ["thiếu số tiền"])
            + ". Hỏi lại số tiền cụ thể.",
            status=reg.STATUS_INVALID,
        )
    confirmation = _saved_reply(saved)
    text = confirmation + (("\nCHƯA ghi được: " + "; ".join(errors)) if errors else "")
    return reg.ToolResult(
        text, confirmation=confirmation, confirmation_key=money(saved[0].parsed.amount)
    )


# ─── Tổng hợp ──────────────────────────────────────────────────────────────


def period_range(text: str, today: dt.date) -> tuple[dt.date, dt.date, str]:
    """(từ, tới - không gồm, nhãn) cho "hôm nay|hôm qua|tuần này|tuần trước|tháng
    này|tháng trước|tháng M[/YYYY]|năm nay". Mặc định tháng này."""
    t = fold(nfc(text or "")).strip()
    if re.search(r"\bhom nay\b", t):
        return today, today + dt.timedelta(days=1), "hôm nay"
    if re.search(r"\bhom qua\b", t):
        return today - dt.timedelta(days=1), today, "hôm qua"
    monday = today - dt.timedelta(days=today.weekday())
    if re.search(r"\btuan (?:truoc|roi)\b", t):
        return monday - dt.timedelta(days=7), monday, "tuần trước"
    if re.search(r"\btuan\b", t):
        return monday, monday + dt.timedelta(days=7), "tuần này"
    if re.search(r"\bnam nay\b", t):
        return dt.date(today.year, 1, 1), dt.date(today.year + 1, 1, 1), f"năm {today.year}"
    first = today.replace(day=1)
    if re.search(r"\bthang (?:truoc|roi)\b", t):
        prev = (first - dt.timedelta(days=1)).replace(day=1)
        return prev, first, f"tháng {prev.month}/{prev.year}"
    m = re.search(r"\bthang (\d{1,2})(?:[/-](\d{4}))?\b", t)
    if m and 1 <= int(m.group(1)) <= 12:
        month = int(m.group(1))
        year = (
            int(m.group(2))
            if m.group(2)
            else (today.year if month <= today.month else today.year - 1)
        )
        start = dt.date(year, month, 1)
        end = dt.date(year + (month == 12), month % 12 + 1, 1)
        return start, end, f"tháng {month}/{year}"
    nxt = dt.date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return first, nxt, f"tháng {today.month}/{today.year}"


async def summary_text(user_id: int, period: str = "") -> str:
    today = dt.datetime.now(VN_TZ).date()
    start, end, label = period_range(period, today)
    rows = await db.expense_totals(user_id, start.isoformat(), end.isoformat())
    if not rows:
        return f'💸 Chưa có khoản chi nào {label}. Ghi bằng cách nhắn kiểu "cafe 45k" hoặc /chitieu cafe 45k.'
    total = sum(int(r["total"]) for r in rows)
    lines = [f"💸 Chi tiêu {label}: {money(total)}"]
    for row in rows:
        share = int(row["total"]) * 100 // total if total else 0
        name = CATEGORIES.get(row["category"], row["category"])
        lines.append(f"• {name}: {money(int(row['total']))} ({row['items']} khoản, {share}%)")
    lines.append("Xem từng khoản: /chitieu ds · Xoá: /chitieu xoa <số>")
    return "\n".join(lines)


async def recent_text(user_id: int, limit: int = 10) -> str:
    rows = await db.expense_recent(user_id, limit)
    if not rows:
        return "💸 Chưa có khoản chi nào."
    lines = [f"💸 {len(rows)} khoản gần nhất:"]
    for row in rows:
        day = dt.date.fromisoformat(str(row["spent_on"]))
        draft = " (nháp)" if row.get("draft") else ""
        name = CATEGORIES.get(row["category"], row["category"])
        lines.append(
            f"#{row['id']} {day:%d/%m} {money(int(row['amount_vnd']))} - {row['note']} [{name}]{draft}"
        )
    return "\n".join(lines)


async def delete_text(user_id: int, raw: str) -> str:
    raw = raw.strip().lstrip("#")
    if not raw.isdigit():
        last = await db.expense_last(user_id, UNDO_WINDOW)
        if not last:
            return "Xoá khoản nào? Ví dụ /chitieu xoa 12 (số # trong /chitieu ds)."
        raw = str(last["id"])
    removed = await db.expense_delete(user_id, int(raw))
    if not removed:
        return f"Không thấy khoản #{raw} của {_you()}."
    return (
        f"🗑️ Đã xoá khoản #{removed['id']} {money(int(removed['amount_vnd']))} - {removed['note']}."
    )


USAGE = (
    "💸 /chitieu — tổng chi tháng này theo danh mục\n"
    "/chitieu hôm nay | tuần này | tháng trước | tháng 10 — tổng theo kỳ\n"
    "/chitieu cafe 45k — ghi 1 khoản (1tr2, 2 củ, 50.000đ đều được); /chitieu ds — 10 khoản gần nhất\n"
    "/chitieu xoa <số> — xoá (hoặc /chitieu_xoa <số>)\n"
    'Nhắn thường "cafe 45k", "đổ xăng 80k" là Lan Anh ghi luôn; gửi ảnh kèm chữ "hoá đơn" để đọc hoá đơn.'
)
_PERIOD_WORDS_RE = re.compile(
    r"^(?:hom nay|hom qua|tuan( nay| truoc| roi)?|thang( nay| truoc| roi| \d{1,2}(/\d{4})?)?|nam nay)$"
)


async def handle_command(command: str, argument: str, user_id: int) -> list[str] | None:
    """/chitieu, /chitieu_xoa; None nếu không phải lệnh của module này."""
    if command in {"/chitieu_xoa", "/chitieuxoa"}:
        return [await delete_text(user_id, argument)]
    if command != "/chitieu":
        return None
    arg = " ".join(argument.split())
    head, _, rest = arg.partition(" ")
    key = fold(head)
    if fold(arg) in {"help", "huong dan", "?"}:
        return [USAGE]
    if key in {"xoa", "huy", "del"}:
        return [await delete_text(user_id, rest)]
    if key in {"ds", "list", "xem"} and not rest:
        return [await recent_text(user_id)]
    if not arg or _PERIOD_WORDS_RE.match(fold(arg)):
        return [await summary_text(user_id, arg)]
    parsed = parse_expense(arg, strict=False)
    if parsed is None:
        return [f"{_me().capitalize()} chưa đọc được số tiền.\n\n" + USAGE]
    try:
        saved = await record(user_id, parsed, source="command")
    except ValueError as exc:
        return [f"❌ {exc}"]
    return [_saved_reply([saved])]


# ─── Ảnh hoá đơn ───────────────────────────────────────────────────────────


def is_receipt_caption(caption: str) -> bool:
    return bool(_RECEIPT_CAPTION_RE.search(nfc(caption or "")))


_RECEIPT_INSTRUCTION = (
    "Đây là ảnh hoá đơn/biên lai thanh toán ở Việt Nam. CHỈ trả về 1 object JSON, không "
    'thêm chữ nào khác: {"amount_vnd": tổng số tiền phải trả (số nguyên VND, không dấu '
    'chấm), "date": "YYYY-MM-DD" hoặc "", "merchant": "tên cửa hàng ngắn gọn" hoặc ""}. '
    "Không đọc được tổng tiền thì amount_vnd = 0. Không bịa số."
)


def parse_receipt_json(raw: str, today: dt.date) -> ParsedExpense | None:
    try:
        data = json.loads(strip_json_fence(raw))
    except (TypeError, ValueError):
        match = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    try:
        amount = int(float(str(data.get("amount_vnd") or 0).replace(".", "").replace(",", "")))
    except ValueError:
        return None
    if not MIN_AMOUNT <= amount <= MAX_AMOUNT:
        return None
    spent_on = today
    try:
        spent = dt.date.fromisoformat(str(data.get("date") or ""))
        if today - dt.timedelta(days=60) <= spent <= today:
            spent_on = spent  # ngày lạ (tương lai, quá cũ) -> coi là hôm nay
    except ValueError:
        pass
    merchant = re.sub(r"\s+", " ", str(data.get("merchant") or "")).strip()[:60]
    note = f"Hoá đơn {merchant}".strip() if merchant else "Hoá đơn"
    return ParsedExpense(amount, note, category_of(merchant) if merchant else "khac", spent_on)


async def analyze_receipt(user_id: int, image_path: str) -> str:
    """Đọc ảnh hoá đơn (1 lượt AI có hạn chót) -> lưu NHÁP; câu trả lời cho người dùng."""
    from ai import orchestrator
    from ai.timeouts import turn_deadline

    try:
        with turn_deadline(RECEIPT_TIMEOUT_SEC):
            response = await orchestrator.analyze_image(_RECEIPT_INSTRUCTION, image_path)
    except Exception:
        logger.warning("Đọc ảnh hoá đơn lỗi.", exc_info=True)
        return (
            f"❌ {_me().capitalize()} chưa đọc được hoá đơn lúc này. {_you().capitalize()} nhắn số "
            f'tiền kiểu "ăn trưa 85k" để {_me()} ghi nha.'
        )
    parsed = parse_receipt_json(getattr(response, "text", "") or "", dt.datetime.now(VN_TZ).date())
    if parsed is None:
        return (
            f"{_me().capitalize()} không thấy tổng tiền rõ ràng trên ảnh - {_you()} nhắn kiểu "
            f'"ăn trưa 85k" giúp {_me()} nha.'
        )
    try:
        saved = await record(user_id, parsed, source="receipt", draft=True)
    except ValueError as exc:
        return f"❌ {exc}"
    return _saved_reply([saved])


# ─── Bản tin sáng ──────────────────────────────────────────────────────────


def brief_enabled() -> bool:
    return config.MORNING_BRIEF_EXPENSES


@morning_brief.register_section("expenses", 45, timeout=10)
async def _brief_section(ctx: morning_brief.BriefContext) -> str | None:
    """Tổng chi tháng này của CHÍNH người nhận (chỉ hiện khi đã ghi khoản nào)."""
    if not brief_enabled():
        return None
    start, end, label = period_range("tháng này", ctx.today)
    rows = await db.expense_totals(ctx.user_id, start.isoformat(), end.isoformat())
    if not rows:
        return None
    total = sum(int(r["total"]) for r in rows)
    yesterday = ctx.today - dt.timedelta(days=1)
    y_rows = (
        await db.expense_totals(ctx.user_id, yesterday.isoformat(), ctx.today.isoformat())
        if yesterday >= start
        else []
    )
    y_total = sum(int(r["total"]) for r in y_rows)
    tail = f" (hôm qua {money(y_total)})" if y_total else ""
    return f"💸 Chi tiêu {label}: {money(total)}{tail}."
