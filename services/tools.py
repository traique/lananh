"""Tool của lượt chat: router JSON chọn tool, Python thực thi, kết quả trung thực.

Router: ``orchestrator.utility_json`` (Groq json_object trước, Gemini utility dự
phòng - KHÔNG phải function-calling native của SDK: router9 native tool-calling
vẫn tắt theo lựa chọn của chủ bot, và cách "router JSON riêng" chạy NHẤT QUÁN dù
chat chính đang trả lời qua provider nào). Danh sách tool sinh từ danh bạ
``services/tool_registry.py`` (ToolSpec dùng chung với /agent).

Trung thực về việc đã làm (``ToolOutcome``, không bao giờ None im lặng):
- Lọc từ khoá rẻ trước (tin phiếm không tốn lượt router).
- Ý định GHI (nhắc việc, ghi chú, chi tiêu) nhận ra bằng regex trên chữ CÒN DẤU
  ("nhắc" khác "nhạc" - bỏ dấu thì trùng), độc lập với router.
- Router hỏng (mọi đường utility lỗi) -> thử đường code tất định (nhắc việc, ghi
  chú, chi tiêu đọc được bằng code), rồi 1 lượt router qua provider-chain; vẫn
  hỏng thì status "unavailable": lượt chat chèn [HỆ THỐNG] CHƯA lưu và nối câu
  "⚠️ Em chưa lưu được..." do code viết (services/channel_chat_service.py).
- Giờ nhắc do code tính (services/vn_time_parse.py), câu xác nhận do code soạn.
- ``allow_side_effects=False`` (lượt chỉ đọc, vd tin nhắn thoại): tool ghi dữ
  liệu không chạy, outcome "blocked" nói rõ lý do.

CHỦ Ý giữ ``stock_analysis.wants_full_analysis()`` / ``wants_price_quote()`` (giá/
phân tích cổ phiếu) bằng keyword ngoài router: fast-path giá REALTIME tất định,
không phụ thuộc 1 lượt LLM có thể lỗi.
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import json
import logging
import re
from contextlib import contextmanager
from datetime import date as date_cls, datetime, timedelta
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from core import config, urls
from core import database as db
from core.text_normalize import fold, nfc
from core.text_utils import strip_json_fence
from services import history_search  # noqa: F401 - đăng ký tool search_history
from services import lunar, prompt_redaction, turn_progress, weather
from services import tool_registry as reg
from services import nl_commands, write_intent
from services.reminder_delivery import current_target
from stock import analysis as stock_analysis

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
# Câu người dùng của lượt (handler set_reminder đọc lại cụm thời gian từ đây).
_turn_text: contextvars.ContextVar[str] = contextvars.ContextVar("tool_turn_text", default="")
# Người gọi là admin Zalo (vai trò "admin" của ToolSpec) - run_chat_turn đặt qua
# admin_scope() để giữ nguyên chữ ký maybe_run_tool(user_id, text) cho chỗ giả lập cũ.
_turn_admin: contextvars.ContextVar[bool] = contextvars.ContextVar("tool_turn_admin", default=False)
# Tool router KHÔNG được chọn ở lượt này (exclude_scope) - vd "read_link" khi
# run_chat_turn đã tự đọc link (services/chat_context.py) hoặc người gửi không
# được phép đọc link.
_turn_excluded: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "tool_turn_excluded", default=frozenset()
)
# Người gọi của lượt (ToolSpec "chạy lệnh" đang chờ bổ sung tham số theo người này).
_turn_user: contextvars.ContextVar[int] = contextvars.ContextVar("tool_turn_user", default=0)
_HISTORY_TURNS = 3
_HISTORY_LINE_MAX = 300

_ROUTER_HEADER = """Bạn là bộ định tuyến tool nội bộ của 1 trợ lý cá nhân. Đọc TIN NHẮN MỚI,
chọn tối đa 1 tool. Trả DUY NHẤT 1 object JSON (không markdown/code fence):
{"tool": "ten_tool_hoac_none", "args": {...}}

Tool:"""

_ROUTER_RULES = """- "none": không cần tool (chuyện phiếm, hỏi đáp thường, giá/phân tích cổ phiếu
  - đã xử lý riêng). args {}.
Quy tắc:
- Chỉ chọn tool khi TIN NHẮN MỚI có ý định RÕ RÀNG khớp mô tả; không chắc, kể
  chuyện đã qua ("hôm qua em nhắc anh rồi"), mơ hồ -> "none".
- Lịch sử chỉ để hiểu "việc đó", "cái đó"... - không chọn tool vì yêu cầu cũ.
- Không bịa "content"/"message": lấy đúng ý người dùng, viết gọn được.
- Không tự cộng trừ giờ: chép nguyên văn cụm thời gian vào "when"."""

# ─── Ý định ghi dữ liệu (regex, độc lập router) ─────────────────────────────
# services/write_intent.py (dùng chung với bắt khoản chi từ chat). Không dùng
# chữ bỏ dấu cho câu có dấu: fold("nhạc") == fold("nhắc") == "nhac".
side_effect_intent = write_intent.side_effect_intent


# ─── Handler ────────────────────────────────────────────────────────────────


async def _tool_save_note(user_id: int, content: str = "", **_ignored) -> reg.ToolResult:
    content = (content or "").strip()
    if not content:
        return reg.ToolResult("Không có nội dung để ghi chú.", status=reg.STATUS_INVALID)
    await db.add_note(user_id, content, event_key=current_target(user_id).event_key)
    return reg.ToolResult(
        f'Đã ghi chú: "{content}"',
        confirmation=f'Đã ghi chú: "{content}"',
        confirmation_key=content,
    )


async def _tool_list_notes(user_id: int, **_ignored) -> str:
    notes = await db.get_notes(user_id, limit=10)
    if not notes:
        return "Chưa có ghi chú nào được lưu."
    lines = [f"- {content} (lúc {created_at:%H:%M %d/%m})" for content, created_at in notes]
    return "Các ghi chú gần đây:\n" + "\n".join(lines)


_REPEAT_TYPES = {
    "daily": "daily",
    "hang ngay": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
    "yearly": "yearly",
    "lunar_monthly": "lunar_monthly",
    "lunar_yearly": "lunar_yearly",
}


# Chữ lặp ghép vào "when" khi router để "repeat" riêng (còn dấu, không dấu) - để
# vn_time_parse tự đọc các thứ/ngày trong câu và tính lần đầu khớp quy tắc.
_REPEAT_WORDS = {
    "daily": ("mỗi ngày", "moi ngay"),
    "weekly": ("hằng tuần", "hang tuan"),
    "monthly": ("hằng tháng", "hang thang"),
    "yearly": ("hằng năm", "hang nam"),
    "lunar_monthly": ("hằng tháng", "hang thang"),
    "lunar_yearly": ("hằng năm", "hang nam"),
}


def _repeat_kind(repeat) -> str | None:
    return _REPEAT_TYPES.get(fold(str(repeat or "")).strip())


def _parse_with_repeat(when_text: str, kind: str, now: datetime):
    """parse("<when> hằng tuần"...) nếu ra đúng loại lặp và có lần nhắc; None nếu không."""
    from services import vn_time_parse

    accented, plain = _REPEAT_WORDS[kind]
    low = nfc(when_text).lower()
    again = vn_time_parse.parse(f"{when_text} {plain if fold(low) == low else accented}", now)
    rule = again.recurrence
    if rule is None or rule.get("type") != kind or again.due_at is None:
        return None
    return again


def _rule_from_args(repeat: str, due: datetime, weekdays, day, lunar_days) -> dict | None:
    """Quy tắc lặp từ trường "repeat" của router (khi code không tự đọc được)."""
    kind = _repeat_kind(repeat)
    if kind is None:
        return None
    rule: dict[str, Any] = {"type": kind, "at": f"{due:%H:%M}"}
    if kind == "weekly":
        days = [d for d in (weekdays or []) if isinstance(d, int) and 0 <= d <= 6]
        rule["days"] = days or [due.weekday()]
    elif kind == "monthly":
        rule["day"] = _as_int(day) or due.day
    elif kind == "yearly":
        rule.update(month=due.month, day=due.day)
    elif kind == "lunar_monthly":
        days = [d for d in (lunar_days or []) if isinstance(d, int) and 1 <= d <= 30]
        rule["days"] = days or [lunar.solar_to_lunar(due.day, due.month, due.year)[0]]
    else:
        ld, lm, _, _ = lunar.solar_to_lunar(due.day, due.month, due.year)
        rule.update(month=lm, day=ld)
    return rule


def _structured_due(date: str, time: str, in_minutes, now: datetime) -> tuple[datetime | None, str]:
    """{date, time} | {in_minutes} của router -> giờ VN (code tính, không tin LLM)."""
    from services import vn_time_parse

    minutes = _as_float(in_minutes)
    if minutes is not None:
        if minutes < 1:
            return None, "Khoảng thời gian quá ngắn (tối thiểu 1 phút)."
        due = now + timedelta(minutes=minutes)
        return (due, "") if due - now <= vn_time_parse.MAX_AHEAD else (None, "Quá 1 năm.")
    match = re.fullmatch(r"\s*(\d{1,2})[:hg](\d{2})?\s*", str(time or ""))
    if not match:
        return None, "Cần giờ nhắc cụ thể (vd 8h sáng)." if date else ""
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, "Giờ nhắc không hợp lệ."
    day = _parse_solar_date(str(date or "")) if date else None
    if date and day is None:
        return None, f"Ngày '{date}' không hợp lệ."
    due = datetime.combine(day or now.date(), dt.time(hour, minute), tzinfo=_VN_TZ)
    if due <= now + vn_time_parse.MIN_AHEAD:
        if day is not None:
            return None, "Thời điểm này đã qua rồi."
        due += timedelta(days=1)  # không nói ngày, giờ hôm nay đã qua -> ngày mai
    if due - now > vn_time_parse.MAX_AHEAD:
        return None, "Chỉ đặt nhắc trong vòng 1 năm tới."
    return due, ""


def _with_router_date(parsed, when_text: str, date: str, now: datetime):
    """Code đọc được giờ nhưng không có ngày ("8h sáng" + date router đưa riêng) hoặc
    gặp chữ ngày chưa hiểu ("tuần sau 9h"): ghép ngày YYYY-MM-DD của router vào câu
    rồi để vn_time_parse tính lại (giờ vẫn do code đọc). Ngày router là hôm nay/
    ngày mai thì giữ cách code tự tính (giờ đã qua -> mai)."""
    from services import vn_time_parse

    if parsed.recurrence is not None or parsed.has_date or not parsed.has_time:
        return parsed
    day = _parse_solar_date(date) if date else None
    if day is None or not when_text:
        return parsed
    if parsed.due_at is not None and day <= now.date() + timedelta(days=1):
        return parsed
    again = vn_time_parse.parse(f"{when_text} {day:%d/%m/%Y}", now)
    return again if again.has_date and again.found else parsed


async def _create_one(
    user_id: int, item: dict, now: datetime, *, parse_turn_text: bool, suffix: str
):
    """Đặt 1 nhắc việc; trả (Created | None, câu lỗi)."""
    from services import recurrence, reminder_service, vn_time_parse

    message = str(item.get("message") or "").strip()
    when_text = str(item.get("when") or "").strip()
    parsed = vn_time_parse.parse(when_text, now) if when_text else vn_time_parse.When()
    if not parsed.found and parse_turn_text:
        when_text = _turn_text.get()
        parsed = vn_time_parse.parse(when_text, now)
    parsed = _with_router_date(parsed, when_text, str(item.get("date") or ""), now)
    kind = _repeat_kind(item.get("repeat"))
    field = {"weekly": "weekdays", "monthly": "day", "lunar_monthly": "lunar_days"}.get(kind or "")
    # "6h sáng thứ 2, 4, 6" + repeat weekly: code đọc các thứ/ngày trong câu. Câu
    # không nói thứ/ngày mà router đưa weekdays/day riêng thì theo router.
    from_args = field is not None and bool(item.get(field)) and not parsed.has_date
    if kind is not None and parsed.recurrence is None and when_text and not from_args:
        parsed = _parse_with_repeat(when_text, kind, now) or parsed
    due: datetime | None = parsed.due_at
    rule = parsed.recurrence
    error = parsed.error
    if due is None and not error:
        due, error = _structured_due(
            str(item.get("date") or ""),
            str(item.get("time") or ""),
            item.get("in_minutes", item.get("minutes_from_now")),
            now,
        )
    if due is None:
        return None, error or "Chưa rõ thời điểm nhắc - cần hỏi lại giờ."
    if rule is None and kind is not None:
        rule = _rule_from_args(
            str(item.get("repeat")),
            due,
            item.get("weekdays"),
            item.get("day"),
            item.get("lunar_days"),
        )
        if rule is not None:
            # Lần đầu phải khớp quy tắc (weekdays [0] mà giờ "7h sáng" rơi Thứ Ba).
            try:
                rule = recurrence.normalize(rule)
                due = recurrence.next_occurrence(rule, now)
            except recurrence.RecurrenceError as exc:
                return None, str(exc)
            if due is None:
                return None, "Không tìm được lần nhắc kế tiếp."
    try:
        created = await reminder_service.create(
            user_id, message or "(không có nội dung cụ thể)", due, rule, key_suffix=suffix
        )
    except (reminder_service.ReminderError, recurrence.RecurrenceError) as exc:
        return None, str(exc)
    return created, ""


async def _tool_set_reminder(
    user_id: int,
    message: str = "",
    when: str = "",
    date: str = "",
    time: str = "",
    in_minutes=None,
    repeat: str = "",
    weekdays=None,
    day=None,
    lunar_days=None,
    items=None,
    minutes_from_now=None,
    **_ignored,
) -> reg.ToolResult:
    """Đặt 1 hoặc vài nhắc việc; giờ do code tính (vn_time_parse), không tin số
    phút LLM tự cộng. Giờ đã qua mà không nói ngày -> ngày mai; ngày cụ thể đã
    qua / thiếu giờ / quá 1 năm -> CHƯA lưu, hỏi lại."""
    from services import reminder_service

    now = datetime.now(_VN_TZ)
    single = {
        "message": message,
        "when": when,
        "date": date,
        "time": time,
        "in_minutes": in_minutes if in_minutes is not None else minutes_from_now,
        "repeat": repeat,
        "weekdays": weekdays,
        "day": day,
        "lunar_days": lunar_days,
    }
    batch = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    batch = batch[: reminder_service.MAX_ITEMS_PER_MESSAGE] or [single]
    created, errors = [], []
    for index, item in enumerate(batch):
        result, error = await _create_one(
            user_id,
            item,
            now,
            parse_turn_text=len(batch) == 1,
            suffix="" if len(batch) == 1 else f":{index + 1}",
        )
        if result is not None:
            created.append(result)
        else:
            label = str(item.get("message") or "").strip()
            errors.append(f"{label + ': ' if label else ''}{error}")
    if not created:
        return reg.ToolResult(
            "CHƯA đặt nhắc. " + " | ".join(errors) + " Hỏi lại người dùng thời điểm cụ thể.",
            status=reg.STATUS_INVALID,
        )
    lines = [c.confirmation(now) for c in created]
    if errors:
        lines.append("CHƯA đặt được: " + " | ".join(errors))
    if not any(c.recurrence for c in created):
        lines.append("(Nhắc 1 lần - KHÔNG nói là sẽ nhắc mỗi ngày/hằng tuần.)")
    return reg.ToolResult(
        "\n".join(lines),
        confirmation="\n".join(c.confirmation(now) for c in created),
        confirmation_key=created[0].confirmation_key(now),
    )


async def _tool_list_reminders(user_id: int, **_ignored) -> str:
    from services import reminder_service

    return reminder_service.format_items(await reminder_service.pending_items(user_id))


async def _tool_cancel_reminder(user_id: int, target: str = "", **_ignored) -> reg.ToolResult:
    from services import reminder_service

    if not str(target or "").strip():
        return reg.ToolResult(
            "Chưa rõ huỷ nhắc nào - hỏi lại (số thứ tự trong /nhac hoặc nội dung).",
            status=reg.STATUS_INVALID,
        )
    text, ok = await reminder_service.cancel(user_id, str(target))
    if not ok:
        return reg.ToolResult(text, status=reg.STATUS_INVALID)
    return reg.ToolResult(text, confirmation=text)


async def _tool_save_special_date(user_id: int, text: str = "", **_ignored) -> reg.ToolResult:
    from services import ngay_nho

    phrase = str(text or "").strip() or _turn_text.get()
    try:
        spec = ngay_nho.parse(phrase)
        confirmation, key = await ngay_nho.add(user_id, spec)
    except ngay_nho.NgayNhoError as exc:
        return reg.ToolResult(f"CHƯA lưu ngày nhớ: {exc}", status=reg.STATUS_INVALID)
    return reg.ToolResult(confirmation, confirmation=confirmation, confirmation_key=key)


async def _tool_list_special_dates(user_id: int, **_ignored) -> str:
    from services import ngay_nho

    today = datetime.now(_VN_TZ).date()
    return ngay_nho.format_items(await ngay_nho.items(user_id, today), today)


# Open-Meteo/wttr.in tự có timeout từng request; chặn thêm tổng thời gian để
# lượt chat không phải chờ geocode + fallback cộng dồn.
_WEATHER_TIMEOUT_SEC = 20


async def _tool_get_weather(user_id: int, location: str = "", days: int = 1, **_ignored) -> str:
    from services import weather_home

    # Không nói địa điểm -> thành phố mặc định riêng của người hỏi (nếu đã đặt).
    location = str(location or "").strip() or await weather_home.get(user_id)
    try:
        data = await asyncio.wait_for(weather.get_weather(location, days), _WEATHER_TIMEOUT_SEC)
    except (weather.WeatherError, TimeoutError):
        logger.warning("Tra thời tiết lỗi (location=%r)", location, exc_info=True)
        return (
            "Không lấy được dữ liệu thời tiết lúc này (Open-Meteo và wttr.in đều lỗi). "
            "Báo thật với người dùng, KHÔNG tự bịa số liệu thời tiết."
        )
    if data.get("error"):
        return f"{data['error']}. Hỏi lại người dùng tên tỉnh/thành phố cụ thể hơn."
    return (
        "Số liệu thời tiết thật vừa tra (dùng đúng các số này, không lấy số liệu thời "
        "tiết từ kết quả web nếu có):\n" + weather.describe_weather(data)
    )


def _parse_solar_date(raw: str) -> date_cls | None:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _tool_lunar_calendar(
    user_id: int,
    date: str = "",
    lunar_day=None,
    lunar_month=None,
    lunar_year=None,
    leap=False,
    **_ignored,
) -> str:
    today = datetime.now(_VN_TZ).date()
    ld, lm = _as_int(lunar_day), _as_int(lunar_month)
    if ld is None and lm is None:
        target = _parse_solar_date(str(date or "")) if date else today
        if target is None or not 1900 <= target.year <= 2100:
            return f"Ngày dương lịch '{date}' không hợp lệ (cần trong khoảng 1900-2100)."
        return "Thông tin lịch (tính offline, chính xác):\n" + lunar.describe_date(
            lunar.date_info(target)
        )

    if ld is None or lm is None:
        return "Ngày/tháng âm lịch không hợp lệ (ngày 1-30, tháng 1-12)."
    is_leap = leap is True or str(leap).strip().lower() in ("1", "true")
    day, year, error = lunar.find_lunar_date(ld, lm, _as_int(lunar_year), is_leap, today)
    if day is None:
        return error
    label = lunar.lunar_text(ld, lm, year, is_leap)
    return (
        f"{label[0].upper()}{label[1:]} âm lịch rơi vào (tính offline, chính xác):\n"
        + lunar.describe_date(lunar.date_info(day))
    )


async def _tool_get_portfolio(user_id: int, **_ignored) -> str:
    facts = await db.get_facts(user_id)
    # Tiêu chí "fact nào thuộc danh mục" lấy từ stock_analysis để chỉ có 1
    # nguồn duy nhất, thay vì copy cứng tuple từ khoá như trước đây.
    portfolio_facts = [f"{k}: {v}" for k, v in facts if stock_analysis.is_portfolio_fact(k)]
    if not portfolio_facts:
        return "Chưa ghi nhận danh mục đầu tư nào trong trí nhớ dài hạn."
    return "Danh mục đầu tư đã ghi nhận trong trí nhớ:\n" + "\n".join(portfolio_facts)


# Câu trả lời thành công của memory_control.remember (ghim fact / quy tắc trả lời);
# còn lại (từ chối bí mật, đầy chỗ ghim, thiếu nội dung) là CHƯA lưu.
_REMEMBER_OK_PREFIXES = ("📌", "✅")


async def _tool_remember_fact(user_id: int, content: str = "", **_ignored) -> reg.ToolResult:
    """Ghim 1 điều vào trí nhớ (logic + chặn bí mật ở services/memory_control.remember)."""
    from services import memory_control

    text = await memory_control.remember(user_id, str(content or ""))
    if text.startswith(_REMEMBER_OK_PREFIXES):
        return reg.ToolResult(text, confirmation=text)
    return reg.ToolResult(text, status=reg.STATUS_INVALID)


_RAG_RESULT_MAX_CHARS = 4000


async def _tool_rag_search(user_id: int, query: str = "", **_ignored) -> str:
    """Tra kho kiến thức riêng rag/ (chỉ chủ bot/admin - roles của ToolSpec)."""
    from services import rag_service

    query = str(query or "").strip() or _turn_text.get()
    matches = await asyncio.to_thread(rag_service.search, query)
    if not matches:
        return (
            "Không tìm thấy đoạn nào khớp trong kho kiến thức rag/ - nói thật là chưa có tài liệu."
        )
    parts = [f"【{m.chunk.display_name}】\n{m.chunk.content}" for m in matches]
    return (
        "Các đoạn khớp nhất trong kho kiến thức rag/ của chủ bot (chỉ trả lời dựa trên đây, "
        "nêu tên tài liệu):\n\n" + "\n\n---\n\n".join(parts)
    )[:_RAG_RESULT_MAX_CHARS]


_LINK_RESULT_MAX_CHARS = 5000


async def _tool_read_link(user_id: int, url: str = "", **_ignored) -> reg.ToolResult:
    """Đọc 1 link CÓ NGUYÊN VĂN trong tin nhắn của người dùng (model không tự dựng
    URL mới - tránh rò dữ liệu qua query string). Kết quả là nội dung ngoài: được
    rào core.untrusted và đánh dấu taint (chặn tool ghi dữ liệu sau đó)."""
    from core import untrusted
    from services import web_reader

    allowed = set(urls.extract_urls(_turn_text.get()))
    url = str(url or "").strip()
    if not url and len(allowed) == 1:
        url = next(iter(allowed))
    if not url or urls.normalize_url(url) not in allowed:
        return reg.ToolResult(
            "Chỉ đọc được link có nguyên văn trong tin nhắn của người dùng.",
            status=reg.STATUS_INVALID,
        )
    try:
        text = await web_reader.read_url(url)
    except web_reader.WebReaderError as exc:
        return reg.ToolResult(f"Không đọc được link: {exc}")
    wrapped = untrusted.wrap_untrusted(text, f"trang {url}", max_chars=_LINK_RESULT_MAX_CHARS)
    return reg.ToolResult(wrapped or "Trang không có nội dung đọc được.")


async def _tool_add_expense(user_id: int, text: str = "", items=None, **_ignored) -> reg.ToolResult:
    """Ghi khoản chi; số tiền LUÔN do code đọc và phải có trong chính tin nhắn người
    dùng (services/expense_service.py) - router chỉ tách từng khoản."""
    from services import expense_service

    phrases: list[str] = []
    if isinstance(items, list):
        phrases = [str(i.get("text") if isinstance(i, dict) else i) for i in items][:5]
    if not phrases:
        phrases = [str(text or "").strip() or _turn_text.get()]
    return await expense_service.record_from_tool(user_id, phrases, _turn_text.get())


async def _tool_expense_summary(user_id: int, period: str = "", **_ignored) -> str:
    from services import expense_service

    return await expense_service.summary_text(user_id, str(period or "") or _turn_text.get())


def _obj(props: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return reg.object_schema(props or {}, required)


# Mô tả tool NGẮN (kiểu EasyTool): 1 câu mô tả + dạng args + khi nào dùng/1 ví dụ.
# Prompt router và /agent phải vừa trần 8K token/phút của Groq gpt-oss-20b - test
# test_tool_catalog_budget đặt trần tổng ký tự, thêm tool thì viết gọn tương tự.
_CHAT_TOOLS = (
    reg.ToolSpec(
        name="save_note",
        description="Lưu 1 ghi chú tự do.",
        parameters=_obj({"content": reg.string("nội dung")}, ("content",)),
        handler=_tool_save_note,
        side_effect=True,
        effect_label="ghi chú",
        router_hint='args {"content": nội dung}. Khi RÕ RÀNG bảo ghi chú/note lại.',
    ),
    reg.ToolSpec(
        name="remember_fact",
        description="Ghim LÂU DÀI 1 điều về người dùng vào trí nhớ.",
        parameters=_obj({"content": reg.string("điều cần nhớ, giữ nguyên ý")}, ("content",)),
        handler=_tool_remember_fact,
        side_effect=True,
        effect_label="điều cần nhớ",
        router_hint=(
            'args {"content": giữ nguyên ý}. Khi bảo "nhớ giúp anh là...", "em nhớ nhé: ...".'
        ),
    ),
    reg.ToolSpec(
        name="list_notes",
        description="Xem các ghi chú gần đây.",
        parameters=_obj(),
        handler=_tool_list_notes,
        router_hint='args {}. Vd "em ghi chú gì rồi".',
    ),
    reg.ToolSpec(
        name="set_reminder",
        description="Đặt nhắc việc (1 lần hoặc lặp lại).",
        parameters=_obj(
            {
                "message": reg.string("việc cần nhắc"),
                "when": reg.string("cụm thời gian NGUYÊN VĂN"),
                "date": reg.string("YYYY-MM-DD hoặc rỗng"),
                "time": reg.string("HH:MM 24h hoặc rỗng"),
                "in_minutes": reg.number("số phút nữa ('N phút/tiếng nữa')"),
                "repeat": reg.string("none|daily|weekly|monthly|yearly|lunar_monthly|lunar_yearly"),
                "weekdays": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "0=Thứ Hai ... 6=Chủ nhật",
                },
                "day": reg.integer("ngày trong tháng"),
                "lunar_days": {"type": "array", "items": {"type": "integer"}},
                "items": {"type": "array", "items": {"type": "object"}},
            },
            ("message",),
        ),
        handler=_tool_set_reminder,
        side_effect=True,
        effect_label="nhắc việc",
        router_hint=(
            'args {"message": việc cần nhắc, "when": cụm thời gian NGUYÊN VĂN kể cả thứ/ngày '
            '(vd "8h tối mai", "30 phút nữa", "6h sáng thứ 2, 4, 6"), "repeat": '
            "none|daily|weekly|monthly|yearly|lunar_monthly|lunar_yearly}; weekdays 0=Thứ "
            'Hai ... 6=Chủ nhật; nhiều việc: {"items": [{"message", "when"}, ...]} (<= 5). '
            'Khi RÕ RÀNG nhờ nhắc; "việc đó" lấy từ lịch sử.'
        ),
    ),
    reg.ToolSpec(
        name="list_reminders",
        description="Xem nhắc việc đang chờ.",
        parameters=_obj(),
        handler=_tool_list_reminders,
        router_hint='args {}. Vd "em đặt những nhắc gì rồi".',
    ),
    reg.ToolSpec(
        name="cancel_reminder",
        description="Huỷ 1 nhắc việc (nhắc lặp: cả chuỗi).",
        parameters=_obj({"target": reg.string("số trong /nhac hoặc đoạn nội dung")}),
        handler=_tool_cancel_reminder,
        side_effect=True,
        effect_label="huỷ nhắc việc",
        router_hint='args {"target": số trong /nhac hoặc đoạn nội dung}. Vd "huỷ nhắc gọi mẹ".',
    ),
    reg.ToolSpec(
        name="save_special_date",
        description="Lưu ngày nhớ hằng năm (giỗ, sinh nhật, kỷ niệm).",
        parameters=_obj({"text": reg.string("câu mô tả nguyên văn")}),
        handler=_tool_save_special_date,
        side_effect=True,
        effect_label="ngày nhớ",
        router_hint=(
            'args {"text": nguyên văn, vd "giỗ ông nội 12/3 âm, nhắc trước 3 ngày"}. Khi muốn '
            "nhớ/nhắc 1 ngày lặp hằng năm."
        ),
    ),
    reg.ToolSpec(
        name="list_special_dates",
        description="Xem các ngày nhớ đã lưu.",
        parameters=_obj(),
        handler=_tool_list_special_dates,
        router_hint='args {}. Vd "sắp có giỗ ai không".',
    ),
    reg.ToolSpec(
        name="add_expense",
        description="Ghi khoản chi (hệ thống tự đọc số tiền).",
        parameters=_obj(
            {
                "text": reg.string("nguyên văn, vd 'cafe 45k'"),
                "items": {"type": "array", "items": {"type": "string"}},
            }
        ),
        handler=_tool_add_expense,
        side_effect=True,
        effect_label="khoản chi",
        router_hint=(
            'args {"text": nguyên văn, vd "đổ xăng 80k"} hoặc {"items": ["cafe 45k", ...]}. '
            "Khi muốn GHI LẠI tiền đã chi."
        ),
    ),
    reg.ToolSpec(
        name="expense_summary",
        description="Tổng chi tiêu theo danh mục.",
        parameters=_obj({"period": reg.string("hôm nay|tuần này|tháng này|tháng trước|tháng M")}),
        handler=_tool_expense_summary,
        router_hint=(
            'args {"period": hôm nay|hôm qua|tuần này|tháng này|tháng trước|tháng 10}. Vd '
            '"tháng này anh tiêu bao nhiêu".'
        ),
    ),
    reg.ToolSpec(
        name="get_portfolio",
        description="Xem danh mục đầu tư đã ghi nhận.",
        parameters=_obj(),
        handler=_tool_get_portfolio,
        router_hint='args {}. Vd "anh đang giữ mã nào".',
    ),
    reg.ToolSpec(
        name="get_weather",
        description="Tra thời tiết thật (hiện tại + dự báo).",
        parameters=_obj(
            {
                "location": reg.string("địa danh người dùng nói, rỗng nếu không nói"),
                "days": reg.integer("số ngày TÍNH CẢ HÔM NAY (1-7)"),
            }
        ),
        handler=_tool_get_weather,
        router_hint=(
            'args {"location": địa danh người dùng nói (KHÔNG tự điền), "days": số ngày TÍNH '
            "CẢ HÔM NAY (1-7; mai = 2, cuối tuần tính tới Chủ Nhật)}. Hỏi mưa/nắng/nhiệt độ."
        ),
    ),
    reg.ToolSpec(
        name="lunar_calendar",
        description="Tra/đổi âm lịch.",
        parameters=_obj(
            {
                "date": reg.string("YYYY-MM-DD dương ('' = hôm nay)"),
                "lunar_day": reg.integer("ngày âm"),
                "lunar_month": reg.integer("tháng âm"),
                "lunar_year": reg.integer("năm âm (tuỳ chọn)"),
                "leap": reg.boolean("tháng nhuận"),
            }
        ),
        handler=_tool_lunar_calendar,
        router_hint=(
            'args (a) {"date": "YYYY-MM-DD" dương, "" = hôm nay} - âm lịch, can chi, giờ hoàng '
            'đạo, tiết khí, ngày tới Tết; (b) {"lunar_day", "lunar_month", "lunar_year" (tuỳ '
            'chọn), "leap"} - đổi âm sang dương, vd "rằm tháng 8" -> 15, 8.'
        ),
    ),
    reg.ToolSpec(
        name="rag_search",
        description="Tra kho kiến thức riêng (rag/) của chủ bot.",
        parameters=_obj({"query": reg.string("câu cần tra")}),
        handler=_tool_rag_search,
        roles=reg.STAFF_ROLES,
        surfaces=frozenset({reg.CHAT, reg.AGENT}),
        router_hint='args {"query": câu cần tra}. CHỈ khi nhắc rõ tài liệu/kho kiến thức của mình.',
    ),
    reg.ToolSpec(
        name="read_link",
        description="Đọc nội dung 1 link người dùng gửi.",
        parameters=_obj({"url": reg.string("URL có nguyên văn trong tin")}),
        handler=_tool_read_link,
        untrusted_output=True,
        router_hint='args {"url": URL NGUYÊN VĂN trong tin mới}. Khi gửi link và hỏi nội dung.',
    ),
)
for _spec in _CHAT_TOOLS:
    reg.register(_spec)

# Tên cũ - giữ cho chỗ khác/test đang tra handler theo tên.
_HANDLERS = {spec.name: spec.handler for spec in _CHAT_TOOLS}


@contextmanager
def turn_text_scope(text: str) -> Iterator[None]:
    """Câu người dùng của lượt cho handler đọc lại (đường code của nl_commands)."""
    token = _turn_text.set(text)
    try:
        yield
    finally:
        _turn_text.reset(token)


@contextmanager
def admin_scope(is_admin: bool) -> Iterator[None]:
    token = _turn_admin.set(bool(is_admin))
    try:
        yield
    finally:
        _turn_admin.reset(token)


@contextmanager
def exclude_scope(names: Iterable[str]) -> Iterator[None]:
    """Ẩn vài tool khỏi router chat trong phạm vi này (không liệt kê trong prompt,
    router có chọn cũng coi như "none")."""
    token = _turn_excluded.set(frozenset(names))
    try:
        yield
    finally:
        _turn_excluded.reset(token)


def _chat_specs(role: str) -> list[reg.ToolSpec]:
    excluded = _turn_excluded.get()
    return [spec for spec in reg.specs(reg.CHAT, role) if spec.name not in excluded]


def _turn_specs(user_text: str, role: str) -> list[reg.ToolSpec]:
    """Tool router chat của lượt + ToolSpec "chạy lệnh" của nhóm khớp câu này
    (services/nl_commands.py - chỉ nhóm khớp mới vào prompt, giữ trần token)."""
    extra = nl_commands.router_specs(_turn_user.get(), user_text, role)
    excluded = _turn_excluded.get()
    return _chat_specs(role) + [spec for spec in extra if spec.name not in excluded]


def _role(user_id: int, is_admin: bool) -> str:
    if config.is_owner_user_id(user_id):
        return reg.ROLE_OWNER
    return reg.ROLE_ADMIN if is_admin else reg.ROLE_MEMBER


def _build_router_prompt(user_text: str, history_text: str = "", role: str | None = None) -> str:
    now = datetime.now(_VN_TZ)
    specs = _turn_specs(user_text, role or reg.ROLE_OWNER)
    tools = reg.router_tool_list(specs)
    rules = _ROUTER_RULES
    if any(spec.verbatim for spec in specs):
        rules += "\n" + nl_commands.ROUTER_RULE
    history = ""
    if history_text:
        history = (
            "### Vài lượt hội thoại gần nhất (CHỈ để hiểu tham chiếu, không phải yêu cầu mới)\n"
            f"{history_text}\n\n"
        )
    return (
        f"{_ROUTER_HEADER}\n{tools}\n{rules}\n\n"
        f"[Thời điểm hiện tại: {now:%H:%M} {lunar.WEEKDAYS[now.weekday()]} ngày "
        f"{now:%d/%m/%Y}, giờ Việt Nam]\n\n"
        f"{history}### Tin nhắn mới của người dùng\n{user_text}\n\n"
        "Trả về JSON theo đúng định dạng đã mô tả, không thêm gì khác."
    )


async def _history_text(user_id: int) -> str:
    """2-3 lượt gần nhất của CHÍNH user_id này (Zalo tách hẳn Telegram), cắt ngắn."""
    from services import web_search

    raw = await web_search.recent_history_text(user_id, _HISTORY_TURNS)
    return "\n".join(line[:_HISTORY_LINE_MAX] for line in raw.splitlines() if line.strip())


# Bộ lọc rẻ trước khi gọi LLM: các tool đều dùng những từ này khi người dùng
# diễn đạt ý định thật, nên tin nhắn không khớp từ nào coi như không cần tool -
# tiết kiệm 1 lượt gọi utility API cho phần lớn tin chat phiếm. Đánh đổi: bỏ lỡ
# câu lệnh diễn đạt lạ hoàn toàn không chứa từ khóa nào ở dưới.
_TOOL_INTENT_KEYWORDS = (
    "ghi chú",
    "ghi chu",
    "ghi lại",
    "ghi lai",
    "note",
    "nhắc",
    "nhac",
    "nhớ",
    "nho",
    "lưu lại",
    "luu lai",
    "danh mục",
    "danh muc",
    "đang giữ",
    "portfolio",
    "đặt lịch",
    "dat lich",
    "hẹn giờ",
    "hen gio",
    "remind",
    "todo",
    "thời tiết",
    "thoi tiet",
    "mưa",
    "nắng",
    "nhiệt độ",
    "nhiet do",
    "dự báo",
    "du bao",
    "bão",
    "âm lịch",
    "am lich",
    "lịch âm",
    "ngày âm",
    "ngay am",
    "tết",
    "rằm",
    "mùng",
    "can chi",
    "hoàng đạo",
    "tiết khí",
    "giỗ",
    "sinh nhật",
    "sinh nhat",
    "kỷ niệm",
    "ngày nhớ",
    "chi tiêu",
    "chi tieu",
    "tiêu hết",
    "đã tiêu",
    "xài hết",
    "tiêu bao nhiêu",
    "xài bao nhiêu",
    "tài liệu",
    "tai lieu",
    "kho kiến thức",
    # Hỏi lại chuyện cũ -> tool search_history (services/history_search.py).
    "hôm trước",
    "hom truoc",
    "lần trước",
    "lan truoc",
    "bữa trước",
    "hôm bữa",
    "em đã nói",
    "em nói gì",
    "em có nói",
    "anh đã hỏi",
    "anh có hỏi",
    "đã trao đổi",
    "tìm lại",
    "tim lai",
    "lịch sử chat",
)
# Câu hỏi tra dữ liệu RIÊNG của người dùng (đối tượng + từ hỏi): router hỏng thì
# lượt chat phải nói thật là chưa tra được. Hẹp hơn bộ lọc từ khoá ở trên - "nhớ"
# xuất hiện trong chuyện phiếm ("anh nhớ em quá") nên không tính.
_OWN_DATA = (
    ("nhắc việc", r"nhắc|lịch nhắc|hẹn giờ", r"nhac|lich nhac|hen gio"),
    ("ghi chú", r"ghi chú|note", r"ghi chu|note"),
    ("chi tiêu", r"chi tiêu|tiêu|xài|tiêu hết", r"chi tieu|xai"),
    ("ngày nhớ", r"ngày nhớ|giỗ|sinh nhật|kỷ niệm", r"ngay nho|sinh nhat|ky niem"),
)
_QUESTION = r"\?|\b(?:gì|nào|bao nhiêu|mấy|chưa|không|xem|liệt kê|danh sách)\b"
_QUESTION_PLAIN = r"\?|\b(?:gi|nao|bao nhieu|may|chua|khong|xem|liet ke|danh sach)\b"


def own_data_lookup(user_text: str) -> str:
    """Nhãn dữ liệu riêng người dùng đang hỏi ("nhắc việc", "chi tiêu"...) hoặc ""."""
    low = nfc(user_text or "").lower()
    plain = fold(low) == low
    if not re.search(_QUESTION_PLAIN if plain else _QUESTION, low):
        return ""
    for label, accented, unaccented in _OWN_DATA:
        if re.search(rf"\b(?:{unaccented if plain else accented})\b", low):
            return label
    return ""


# Link trong tin chỉ là lý do gọi router khi tool read_link còn dùng được ở lượt này.
_URL_KEYWORDS = ("http://", "https://")


def _looks_like_tool_intent(user_text: str) -> bool:
    lower = user_text.lower()
    if "read_link" in _turn_excluded.get():
        # read_link bị ẩn (run_chat_turn tự đọc link): slug URL ("nhac-vang",
        # "thoi-tiet") không được kéo theo 1 lượt router vô ích.
        lower = urls.URL_RE.sub(" ", lower)
    elif any(kw in lower for kw in _URL_KEYWORDS):
        return True
    # Câu web_search đã bỏ qua vì là hỏi thời tiết thì router PHẢI chạy, nếu
    # không lượt chat chẳng có số liệu nào.
    return any(kw in lower for kw in _TOOL_INTENT_KEYWORDS) or (
        weather.looks_like_weather_question(lower)
    )


async def _route_with_chain(prompt: str) -> dict | None:
    """Router dự phòng qua provider-chain (bounded theo hạn chót lượt) khi mọi
    đường utility JSON đều hỏng - chỉ dùng cho câu có ý định GHI dữ liệu."""
    from ai import orchestrator

    response = await orchestrator.ask(prompt)
    data = json.loads(strip_json_fence(getattr(response, "text", "") or ""))
    return data if isinstance(data, dict) else None


# Câu có chữ "nhắc" nhưng KHÔNG phải đặt nhắc mới: huỷ/dừng nhắc, kể chuyện đã qua.
# Đường code tất định bỏ qua (router qua provider-chain quyết định, hỏng thì báo
# chưa lưu được) - không đặt nhắc "Bỏ uống thuốc" mỗi ngày.
_NOT_NEW_REMINDER_RE = re.compile(
    r"\b(?:huỷ|hủy|xoá|xóa|bỏ|dừng|tắt|thôi|đừng|khỏi|không cần)\s+(?:cái\s+)?nhắc\b"
    r"|\b(?:hôm qua|hôm kia|hôm trước|bữa trước|lúc nãy|đã nhắc|rồi mà)\b"
    r"|\bnhắc (?:(?:anh|em|tôi|mình|chị) )?rồi\b"
)
_NOT_NEW_REMINDER_PLAIN_RE = re.compile(
    r"\b(?:huy|xoa|bo|dung|tat|thoi|khoi|khong can) (?:cai )?nhac\b"
    r"|\b(?:hom qua|hom kia|hom truoc|bua truoc|da nhac|roi ma)\b"
    r"|\bnhac (?:(?:anh|em|toi|minh|chi) )?roi\b"
)


def _not_new_reminder(user_text: str) -> bool:
    low = re.sub(r"\s+", " ", nfc(user_text or "")).strip().lower()
    plain = fold(low) == low
    return bool((_NOT_NEW_REMINDER_PLAIN_RE if plain else _NOT_NEW_REMINDER_RE).search(low))


async def _deterministic(user_id: int, user_text: str, intent: str) -> reg.ToolOutcome | None:
    """Đường code tất định khi router AI hỏng: nhắc việc / ghi chú / khoản chi đọc
    được bằng code thì vẫn lưu được - không bắt người dùng gửi lại. Câu huỷ nhắc /
    kể chuyện đã qua không đi đường này (không tự đặt nhắc mới)."""
    from services import expense_service, vn_time_parse

    if intent == "reminder" and not _not_new_reminder(user_text):
        extracted = vn_time_parse.extract_reminder(user_text)
        if extracted and extracted[1].due_at and not extracted[1].error:
            message = extracted[0] or "(không có nội dung cụ thể)"
            spec = reg.get("set_reminder")
            assert spec is not None
            item = {"message": message, "when": user_text}
            return await execute(spec, user_id, item, intent=intent)
    if intent == "note":
        m = re.search(
            r"(?:ghi chú|ghi lại|note lại|lưu lại)(?: giúp| giùm)?(?: anh| em)?\s*:?\s*(.+)$",
            user_text,
            re.IGNORECASE,
        )
        if m and m.group(1).strip():
            spec = reg.get("save_note")
            assert spec is not None
            return await execute(spec, user_id, {"content": m.group(1).strip()}, intent=intent)
    if intent == "expense" and expense_service.parse_expense(user_text, strict=False) is not None:
        spec = reg.get("add_expense")
        assert spec is not None
        return await execute(spec, user_id, {"text": user_text}, intent=intent)
    return None


async def execute(
    spec: reg.ToolSpec,
    user_id: int,
    args: dict,
    *,
    allow_side_effects: bool = True,
    intent: str = "",
) -> reg.ToolOutcome:
    """Chạy 1 tool của chat, không bao giờ raise: lỗi -> status "failed"."""
    base = {
        "name": spec.name,
        "side_effect": spec.side_effect,
        "intent": intent,
        "router_ran": True,
        "effect_label": spec.effect_label,
        "untrusted": spec.untrusted_output,
        "verbatim": spec.verbatim,
    }
    if spec.side_effect and not allow_side_effects:
        return reg.ToolOutcome(
            status=reg.STATUS_BLOCKED,
            text="lượt này chỉ đọc (vd tin nhắn thoại), không tự ghi dữ liệu - nhắn chữ để lưu",
            **base,
        )
    if spec.side_effect and reg.is_tainted():
        return reg.ToolOutcome(
            status=reg.STATUS_BLOCKED,
            text="lượt này đã đọc nội dung ngoài (web/link), không ghi dữ liệu theo nội dung đó",
            **base,
        )
    try:
        result = await spec.handler(user_id, **spec.allowed_args(args))
    except Exception:
        logger.warning(
            "Tool '%s' lỗi (user_id=%s, args=%s).", spec.name, user_id, args, exc_info=True
        )
        failure = "hệ thống lỗi khi lưu" if spec.side_effect else "hệ thống lỗi khi tra"
        return reg.ToolOutcome(status=reg.STATUS_FAILED, text=failure, **base)
    if spec.untrusted_output:
        reg.mark_tainted()
    if not isinstance(result, reg.ToolResult):
        result = reg.ToolResult(str(result))
    logger.info("Function calling: đã chạy tool '%s' cho user_id=%s", spec.name, user_id)
    return reg.ToolOutcome(
        status=result.status,
        text=result.text,
        confirmation=result.confirmation,
        confirmation_key=result.confirmation_key,
        extra=dict(result.extra),
        **base,
    )


async def maybe_run_tool(
    user_id: int,
    user_text: str,
    *,
    is_admin: bool | None = None,
    allow_side_effects: bool = True,
) -> reg.ToolOutcome:
    """Hỏi router có cần tool nào không, chạy tool và trả ToolOutcome (KHÔNG BAO
    GIỜ raise, không bao giờ None): none | ok | invalid | failed | unavailable |
    blocked. ``outcome.grounding()`` là khối chèn vào orchestrator.chat();
    channel_chat_service dùng status để nối câu "chưa lưu được" bằng code.

    ``allow_side_effects=False``: tool ghi dữ liệu (nhắc việc, ghi chú, chi tiêu,
    danh mục) không chạy, outcome "blocked" kèm lý do. ``is_admin`` None = theo
    admin_scope() của lượt (run_chat_turn đặt)."""
    if is_admin is None:
        is_admin = _turn_admin.get()
    intent = side_effect_intent(user_text)
    user_token = _turn_user.set(user_id)
    if (
        not intent
        and not _looks_like_tool_intent(user_text)
        and not nl_commands.router_groups(user_id, user_text, _role(user_id, is_admin))
    ):
        _turn_user.reset(user_token)
        return reg.ToolOutcome()
    token = _turn_text.set(user_text)
    try:
        with reg.taint_scope():
            return await _route_and_run(user_id, user_text, intent, is_admin, allow_side_effects)
    except Exception:
        logger.warning("Router tool lỗi ngoài dự kiến.", exc_info=True)
        lookup = "" if intent else own_data_lookup(user_text)
        return reg.ToolOutcome(status=reg.STATUS_FAILED, intent=intent, lookup=lookup)
    finally:
        _turn_text.reset(token)
        _turn_user.reset(user_token)


def wants_command_router(user_id: int, user_text: str, is_admin: bool) -> bool:
    """Câu có thể là yêu cầu chạy lệnh (nhóm lệnh khớp) - run_chat_turn chạy router
    TRƯỚC (không song song tra web/trí nhớ): router chọn lệnh thì gửi nguyên văn kết
    quả, khỏi tốn lượt tra web và lượt chat AI."""
    return bool(nl_commands.router_groups(user_id, user_text, _role(user_id, is_admin)))


async def _route_and_run(
    user_id: int, user_text: str, intent: str, is_admin: bool, allow_side_effects: bool
) -> reg.ToolOutcome:
    role = _role(user_id, is_admin)
    try:
        history = await _history_text(user_id)
    except Exception:
        history = ""
    prompt = _build_router_prompt(user_text, history, role)
    from ai import orchestrator

    # Provider chỉ thấy nhãn "[đã ẩn: mật khẩu]"; tham số tool (chạy trong bot, ghi DB
    # của chính người dùng) được đổi lại giá trị gốc - services/prompt_redaction.
    with prompt_redaction.scope() as redactor:
        try:
            data = await orchestrator.utility_json(prompt)
        except Exception:
            logger.warning("Router tool: utility_json lỗi.", exc_info=True)
            data = None
        if not data and intent:
            if allow_side_effects:
                handled = await _deterministic(user_id, user_text, intent)
                if handled is not None:
                    return handled
            try:
                data = await _route_with_chain(prompt)
            except Exception:
                logger.warning("Router tool dự phòng qua provider-chain lỗi.", exc_info=True)
                data = None
    if isinstance(data, dict):
        data = redactor.restore_obj(data)
    if not data:
        lookup = "" if intent else own_data_lookup(user_text)
        return reg.ToolOutcome(status=reg.STATUS_UNAVAILABLE, intent=intent, lookup=lookup)

    tool_name = str(data.get("tool", "none")).strip()
    spec = reg.get(tool_name)
    allowed = {s.name for s in _turn_specs(user_text, role)}
    if tool_name == "none" or spec is None or tool_name not in allowed:
        return reg.ToolOutcome(status=reg.STATUS_NONE, intent=intent, router_ran=True)
    args = data.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    turn_progress.report_tool(tool_name)  # "em đang lục tài liệu..." (Telegram)
    return await execute(spec, user_id, args, allow_side_effects=allow_side_effects, intent=intent)
