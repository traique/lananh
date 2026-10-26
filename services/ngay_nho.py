"""Ngày nhớ: giỗ, sinh nhật, kỷ niệm - nhắc trước N ngày và nhắc đúng ngày.

Mỗi ngày nhớ là 1 chuỗi nhắc (reminder_series, kind='ngay_nho') lặp hằng năm
theo lịch dương (``yearly``) hoặc âm (``lunar_yearly``, chính sách tháng nhuận/
ngày 30 ở services/recurrence.py). Chỉ giữ 1 lần nhắc kế tiếp trong bảng
reminders: lần báo trước (nếu còn kịp) rồi tới lần đúng ngày; scheduler claim
lần nào thì chèn lần sau (services/reminder_service.schedule_next).

Đọc câu bằng code, không gọi AI: "/ngaynho giỗ ông nội 12/3 âm trước 3 ngày",
"sinh nhật vợ 20/11/1990", "kỷ niệm ngày cưới 5/6/2015 lúc 7h". Mục "ngay_nho"
của bản tin sáng (services/morning_brief.py) liệt kê ngày nhớ trong 7 ngày tới.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from core import database as db
from core.text_normalize import fold
from services import lunar, morning_brief, recurrence, vn_time_parse
from services.reminder_delivery import current_target

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
KIND = "ngay_nho"
MAX_TITLE_CHARS = 120
MAX_LEAD_DAYS = 30
DEFAULT_HOUR = 8
BRIEF_DAYS = 7
CATEGORY_LABEL = {"gio": "giỗ", "sinh_nhat": "sinh nhật", "ky_niem": "kỷ niệm", "khac": "ngày nhớ"}
# Báo trước mặc định khi người dùng không nói (giỗ cần chuẩn bị cỗ lâu hơn).
DEFAULT_LEAD = {"gio": 3, "sinh_nhat": 1, "ky_niem": 1, "khac": 0}

# (loại, mẫu trên chữ CÒN DẤU, mẫu khi người dùng gõ không dấu) - bỏ dấu thì
# "giỗ" trùng "giờ" ("lúc 7 giờ"), nên câu có dấu phải khớp đúng chữ có dấu.
_CATEGORY_RE = (
    (
        "gio",
        re.compile(r"\bgiỗ\b"),
        re.compile(
            r"^gio\b|\bcung gio\b|\bgio (?:ong|ba|bo|me|cha|cu|chu|bac|co|di|cau|anh|chi|em|to)\b"
        ),
    ),
    (
        "sinh_nhat",
        re.compile(r"\bsinh nhật\b|\bsn\b|\bthôi nôi\b"),
        re.compile(r"\bsinh nhat\b|\bsn\b|\bthoi noi\b"),
    ),
    (
        "ky_niem",
        re.compile(r"\bk[ỷỉ] niệm\b|\bngày cưới\b|\bngày quen\b|\bđám cưới\b"),
        re.compile(r"\bk[yi] niem\b|\bngay cuoi\b|\bngay quen\b|\bdam cuoi\b"),
    ),
)
_LEAD_RE = re.compile(r"\b(?:(?:nhac|bao) )?truoc (?P<n>\d{1,2}) ?(?:ngay|hom)\b")
_LEAP_POLICY_RE = re.compile(r"\btheo thang nhuan\b")
_EDGE_RE = re.compile(r"^[\s,.;:\-–]+|[\s,.;:\-–]+$")
_FILLER_RE = re.compile(r"(?:^|\s)(?:vao|ngay|luc|nhe|nha|nhac|la)$")


class NgayNhoError(ValueError):
    """Câu báo lỗi tiếng Việt, đưa thẳng cho người dùng."""


@dataclass(frozen=True)
class SpecialDate:
    title: str
    category: str
    rule: dict
    lead_days: int


def _category(text: str) -> str:
    low = text.lower()
    folded = fold(low)
    accented = folded != low
    for name, with_marks, plain in _CATEGORY_RE:
        if (with_marks if accented else plain).search(low if accented else folded):
            return name
    return "khac"


def parse(text: str) -> SpecialDate:
    """Đọc 1 ngày nhớ; raise NgayNhoError với câu hướng dẫn nếu thiếu ngày/tên."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    date = vn_time_parse.parse_calendar_date(clean)
    if date is None:
        raise NgayNhoError(
            "Chưa thấy ngày-tháng. Ví dụ: giỗ ông nội 12/3 âm trước 3 ngày, sinh nhật vợ 20/11."
        )
    folded = vn_time_parse.fold_keep(clean)
    spans = list(date.spans)
    lead_match = _LEAD_RE.search(folded)
    category = _category(clean)
    lead = DEFAULT_LEAD[category]
    if lead_match:
        lead = int(lead_match.group("n"))
        if lead > MAX_LEAD_DAYS:
            raise NgayNhoError(f"Chỉ báo trước tối đa {MAX_LEAD_DAYS} ngày.")
        spans.append((lead_match.start(), lead_match.end()))
    leap_match = _LEAP_POLICY_RE.search(folded)
    if leap_match:
        spans.append((leap_match.start(), leap_match.end()))
    hour, minute = DEFAULT_HOUR, 0
    tod = vn_time_parse.find_time_of_day(clean, tuple(spans))
    if tod is not None:
        hour, minute, tod_spans = tod
        if hour < 6 and not re.search(r"sang|dem|khuya", folded):
            hour += 12  # "lúc 3h" không nói buổi: chiều
        spans.extend(tod_spans)
    title = vn_time_parse.strip_spans(clean, tuple(spans))
    title = re.sub(r"\s+", " ", title)
    for _ in range(3):
        title = _EDGE_RE.sub("", title)
        tail = _FILLER_RE.search(vn_time_parse.fold_keep(title))
        if tail:
            title = title[: tail.start()]
    title = _EDGE_RE.sub("", title)[:MAX_TITLE_CHARS]
    if not title:
        raise NgayNhoError("Ngày nhớ cần tên, ví dụ: giỗ ông nội 12/3 âm.")
    rule: dict = {
        "type": "lunar_yearly" if date.lunar else "yearly",
        "month": date.month,
        "day": date.day,
        "at": f"{hour:02d}:{minute:02d}",
    }
    if date.lunar:
        rule["leap"] = "leap" if leap_match else "regular"
    if date.year:
        rule["year"] = date.year
    try:
        rule = recurrence.normalize(rule)
    except recurrence.RecurrenceError as exc:
        raise NgayNhoError(str(exc)) from exc
    return SpecialDate(title[:1].upper() + title[1:], category, rule, lead)


def _event_on_or_after(rule: dict, day: dt.date) -> dt.datetime | None:
    start = dt.datetime.combine(day, dt.time(), tzinfo=VN_TZ) - dt.timedelta(seconds=1)
    return recurrence.next_occurrence(rule, start)


def _count_suffix(series: dict, event: dt.date) -> str:
    rule = series["recurrence"]
    year = rule.get("year")
    if not year:
        return ""
    if rule["type"] == "lunar_yearly":
        current = lunar.solar_to_lunar(event.day, event.month, event.year)[2]
    else:
        current = event.year
    count = current - int(year)
    if count <= 0:
        return ""
    if series.get("category") == "sinh_nhat":
        return f" - tròn {count} tuổi"
    if series.get("category") == "ky_niem":
        return f" - tròn {count} năm"
    return ""


def _day_label(day: dt.date) -> str:
    return f"{lunar.WEEKDAYS[day.weekday()]} {day:%d/%m/%Y}"


def row_texts(series: dict, event: dt.datetime) -> tuple[str, str]:
    """(câu báo trước, câu đúng ngày) cho 1 lần của ngày nhớ."""
    title = series["message"]
    label = recurrence.date_label(series["recurrence"])
    suffix = _count_suffix(series, event.date())
    lead = int(series.get("lead_days") or 0)
    ahead = f"🗓 Còn {lead} ngày nữa là {title} ({label}, {_day_label(event.date())}){suffix}."
    today = f"🗓 Hôm nay là {title} ({label}){suffix}."
    return ahead, today


def next_row(series: dict, after: dt.datetime) -> tuple[dt.datetime, str] | None:
    """Lần nhắc sớm nhất SAU ``after``: báo trước (nếu còn kịp) rồi đúng ngày."""
    event = recurrence.next_occurrence(series["recurrence"], after)
    if event is None:
        return None
    ahead_text, today_text = row_texts(series, event)
    lead = int(series.get("lead_days") or 0)
    if lead:
        ahead_at = event - dt.timedelta(days=lead)
        if ahead_at > after:
            return ahead_at, ahead_text
    return event, today_text


async def add(user_id: int, spec: SpecialDate, *, key_suffix: str = "") -> tuple[str, str]:
    """Lưu ngày nhớ; trả (câu xác nhận, khoá kiểm tra trong câu trả lời)."""
    from services import reminder_service

    if await db.reminder_count_series(user_id) >= reminder_service.MAX_SERIES:
        raise NgayNhoError(
            f"Đã đủ {reminder_service.MAX_SERIES} nhắc lặp/ngày nhớ - xoá bớt bằng /ngaynho xoa <số> "
            "hoặc /nhac huy <số> rồi thêm lại nha."
        )
    now = dt.datetime.now(VN_TZ)
    series = {
        "message": spec.title,
        "category": spec.category,
        "recurrence": spec.rule,
        "lead_days": spec.lead_days,
    }
    first = next_row(series, now)
    if first is None:
        raise NgayNhoError("Không tính được lần nhắc kế tiếp cho ngày này.")
    target = current_target(user_id)
    await db.reminder_create_series(
        user_id,
        spec.title,
        kind=KIND,
        category=spec.category,
        recurrence=spec.rule,
        lead_days=spec.lead_days,
        channel=target.channel,
        recipient_id=target.recipient_id,
        account_id=target.account_id,
        user_jid=target.user_jid,
        event_key=f"{target.event_key}{key_suffix}" if target.event_key else None,
        first=[first],
    )
    event = _event_on_or_after(spec.rule, now.date())
    label = recurrence.date_label(spec.rule)
    when = f", lần tới {_day_label(event.date())}" if event else ""
    lead = f", báo trước {spec.lead_days} ngày" if spec.lead_days else ""
    note = ""
    if spec.rule.get("type") == "lunar_yearly" and spec.rule.get("leap") == "regular":
        note = " (âm lịch, năm có tháng nhuận vẫn theo tháng thường)"
    text = f"Đã lưu ngày nhớ: {spec.title} - {label}{when}{lead}, nhắc lúc {spec.rule['at']}{note}."
    return text, label.split(" ")[0]  # "12/3 âm lịch" -> câu trả lời chỉ cần có "12/3"


@dataclass(frozen=True)
class Item:
    series_id: int
    title: str
    category: str
    rule: dict
    lead_days: int
    event: dt.date | None


async def items(user_id: int, today: dt.date | None = None) -> list[Item]:
    today = today or dt.datetime.now(VN_TZ).date()
    out = []
    for row in await db.reminder_list_series(user_id, KIND, 50):
        try:
            event = _event_on_or_after(row["recurrence"], today)
        except recurrence.RecurrenceError:
            event = None
        out.append(
            Item(
                row["id"],
                row["message"],
                row.get("category") or "khac",
                row["recurrence"],
                int(row.get("lead_days") or 0),
                event.date() if event else None,
            )
        )
    out.sort(key=lambda i: (i.event is None, i.event or dt.date.max, i.series_id))
    return out


def format_items(rows: list[Item], today: dt.date, *, you: str = "anh") -> str:
    if not rows:
        return (
            "🗓 Chưa có ngày nhớ nào. Thêm: /ngaynho giỗ ông nội 12/3 âm trước 3 ngày, "
            "/ngaynho sinh nhật vợ 20/11/1990."
        )
    lines = [f"🗓 Ngày nhớ của {you} ({len(rows)}):"]
    for index, item in enumerate(rows, 1):
        label = recurrence.date_label(item.rule)
        when = ""
        if item.event:
            days = (item.event - today).days
            left = "hôm nay" if days == 0 else f"còn {days} ngày"
            when = f" ({_day_label(item.event)}, {left})"
        series = {"recurrence": item.rule, "category": item.category}
        suffix = _count_suffix(series, item.event) if item.event else ""
        lead = f", báo trước {item.lead_days} ngày" if item.lead_days else ""
        lines.append(f"{index}. {item.title} - {label}{when}{suffix}{lead}")
    lines.append("Xoá: /ngaynho xoa <số> · Thêm: /ngaynho <tên> <ngày> [âm] [trước N ngày]")
    return "\n".join(lines)


async def remove(user_id: int, target: str) -> str:
    rows = await items(user_id)
    target = target.strip()
    chosen: list[Item]
    if target.isdigit():
        index = int(target)
        chosen = [rows[index - 1]] if 1 <= index <= len(rows) else []
    else:
        key = fold(target)
        chosen = [i for i in rows if key and key in fold(i.title)]
    if not chosen:
        return "Không thấy ngày nhớ đó - gõ /ngaynho để xem danh sách và số thứ tự nha."
    if len(chosen) > 1:
        names = "; ".join(f"{rows.index(i) + 1}. {i.title}" for i in chosen[:5])
        return f"Có {len(chosen)} ngày nhớ khớp ({names}) - xoá theo số giúp em nha."
    ok = await db.reminder_delete_series(user_id, chosen[0].series_id)
    return f"🗑️ Đã xoá ngày nhớ: {chosen[0].title}." if ok else "Ngày nhớ này đã được xoá trước đó."


@morning_brief.register_section("ngay_nho", 35, timeout=10)
async def _brief_section(ctx: morning_brief.BriefContext) -> str | None:
    upcoming = [
        i
        for i in await items(ctx.user_id, ctx.today)
        if i.event and (i.event - ctx.today).days <= BRIEF_DAYS
    ]
    if not upcoming:
        return None
    parts = []
    for item in upcoming[:5]:
        days = (item.event - ctx.today).days if item.event else 0
        label = recurrence.date_label(item.rule)
        when = (
            "hôm nay"
            if days == 0
            else f"còn {days} ngày ({lunar.WEEKDAYS[item.event.weekday()]} {item.event:%d/%m})"
            if item.event
            else ""
        )
        parts.append(f"{item.title} {when} - {label}")
    return "🗓 Ngày nhớ: " + "; ".join(parts) + "."
