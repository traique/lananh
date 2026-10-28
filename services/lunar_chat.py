"""Trả lời câu hỏi âm lịch bằng code, KHÔNG gọi AI (không router, không chat).

Trước đây câu "rằm tháng 8 là ngày nào" đi router tool (1 lượt AI) rồi chat
(thêm 1 lượt AI, có khi cả tra web) dù kết quả tính offline hoàn toàn. Module
này nhận diện các mẫu câu hay gặp bằng regex trên chữ đã bỏ dấu:

- Tết: "còn mấy ngày nữa tết", "tết năm 2027 ngày nào", "bao giờ tết".
- Hôm nay/mai/qua: "hôm nay âm lịch ngày mấy", "mai giờ hoàng đạo",
  "hôm nay tiết khí gì", "hôm qua can chi".
- Ngày âm -> dương: "rằm tháng 8", "mùng 1 tết", "mùng 10 tháng 3 âm",
  "15/6 nhuận âm năm 2025", "rằm tháng giêng năm sau".
- Ngày dương -> âm: "20/11 là ngày mấy âm", "ngày 2 tháng 9 âm lịch là gì".

Câu nào không chắc (vd "mùng 2 tháng 9 là ngày gì" - không rõ âm hay dương)
trả None. ``maybe_handle`` (lối vào của các kênh chat) còn thử bộ tra lịch /lich
(calendar_query.chat_answer) cho câu RÕ là hỏi lịch ("20/11 là thứ mấy", "noel
còn mấy ngày", "1990 mệnh gì"); vẫn không được mới đi đường cũ (router tool
lunar_calendar + chat AI).
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from services import can_chi, lunar
from core.text_normalize import fold

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_MIN_YEAR, _MAX_YEAR = 1900, 2100

# Có ít nhất 1 dấu hiệu âm lịch mới xét tiếp (chữ đã bỏ dấu).
_LUNAR_HINT_RE = re.compile(
    r"\b(?:am lich|lich am|ngay am|am|al|tet|ram|mung|mong|can chi|hoang dao|tiet khi|"
    r"gieng|chap|nhuan|gio tot|gio dep)\b"
)
# "còn" tuỳ chọn: "bao nhiêu ngày nữa đến tết" (không "còn") từng lọt sang chat AI.
_TET_COUNTDOWN_RE = re.compile(
    r"\b(?:con )?(?:may|bao nhieu) ngay (?:nua )?(?:(?:la|toi|den|thi) )?tet\b|"
    r"\bbao (?:gio|lau)(?: nua)?(?: thi| la| moi)? (?:toi |den )?tet\b"
)
_YEAR_WORD = r"(?P<year>\d{4}|nay|sau|toi|truoc|ngoai)"
# Chỉ "ngày nào/ngày mấy/thứ mấy": "nghỉ tết mấy ngày" không phải hỏi ngày Tết.
_TET_DATE_RE = re.compile(
    r"\btet(?: nguyen dan)?(?: nam " + _YEAR_WORD + r")?"
    r"(?: (?:la|vao|roi vao|nham))? (?:ngay (?:nao|may|bao nhieu)|thu may)\b"
)
# "mốt" (ngày kia) được đổi sẵn thành "ngay kia" trong fold_question (bỏ dấu
# thì "mốt" trùng "một").
_RELATIVE_DAY_RE = re.compile(r"\b(hom nay|bua nay|ngay mai|mai|hom qua|ngay kia)\b")
_RELATIVE_OFFSET = {
    "hom nay": 0,
    "bua nay": 0,
    "ngay mai": 1,
    "mai": 1,
    "hom qua": -1,
    "ngay kia": 2,
}
# Chữ có dấu trùng nghĩa sau khi bỏ dấu: đổi thành token riêng TRƯỚC khi fold.
_COLLISIONS = (
    (re.compile(r"\bẩm\b"), "x_am"),  # độ ẩm != âm
    (re.compile(r"(?<!mười )(?<!tháng )\bmột\b"), "x_mot"),  # một != mốt (giữ "mười một")
    (re.compile(r"\bmốt\b"), "ngày kia"),
    (re.compile(r"\btháng sáu\b"), "tháng 6"),  # tháng sáu != tháng sau
)
# "Mai" viết hoa giữa câu là tên người, không phải "ngày mai".
_NAME_MAI_RE = re.compile(r"(?<=\S )Mai\b")
_YEAR_IN_TEXT_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# Phần còn lại của câu chỉ có từ đệm thì vẫn coi là hỏi ngày ("rằm tháng 8 ạ").
_FILLER_RE = re.compile(r"\b(?:a|ah|nhe|nha|nhi|vay|em|anh|chi|oi|ha|the|day|nhi)\b")
_ASK_DATE_RE = re.compile(
    r"\b(?:duong|ngay nao|thu may|vao ngay|roi vao|ngay may|ngay bao nhieu|bao gio|khi nao|"
    r"hom nao|la ngay|ngay gi)\b"
)
# Hỏi về âm lịch của 1 ngày dương: "âm lịch ngày mấy", "can chi", "hoàng đạo"...
_ASK_LUNAR_INFO_RE = re.compile(
    r"\b(?:am lich|lich am|ngay am|(?:may|bao nhieu|gi) am|am (?:may|bao nhieu|la)|"
    r"ra am|can chi|hoang dao|tiet khi|gio tot|gio dep)\b"
)
# Tên dài đứng trước ("muoi mot" trước "muoi") để regex không cắt mất chữ sau.
_MONTH_WORD = (
    r"(?P<month>\d{1,2}|gieng|chap|muoi mot|muoi hai|muoi|mot|hai|ba|tu|bon|nam|sau|bay|tam|chin)"
)
_MONTH_NAMES = {
    "gieng": 1,
    "mot": 1,
    "hai": 2,
    "ba": 3,
    "tu": 4,
    "bon": 4,
    "nam": 5,
    "sau": 6,
    "bay": 7,
    "tam": 8,
    "chin": 9,
    "muoi": 10,
    "muoi mot": 11,
    "chap": 12,
    "muoi hai": 12,
}
# "rằm tháng 8", "mùng 10 tháng 3", "ngày 15 tháng 6 nhuận", "mùng 1 tết", "rằm tháng
# sau" (tháng âm kế tiếp, trước đây bị đọc thành tháng 6).
# Tháng bắt buộc có chữ "tháng" (trừ "tết") để "rằm năm 2027" không bị hiểu
# "năm" là tháng 5.
_LUNAR_WORDS_RE = re.compile(
    r"\b(?:(?P<ram>ram)|(?P<mung>mung|mong|ngay) (?P<day>\d{1,2}))"
    r"(?: (?P<tet>tet)| thang (?:(?P<rel>nay|sau|toi|truoc)\b|" + _MONTH_WORD + r"))?"
    r"(?P<leap> nhuan)?(?P<am> (?:am lich|am|al)\b)?(?: nam " + _YEAR_WORD + r")?"
)
# "15/8 âm", "15/6 nhuận âm năm 2025", "20/11/2026 là ngày mấy âm"
_NUMERIC_DATE_RE = re.compile(
    r"\b(?P<day>\d{1,2})[/-](?P<month>\d{1,2})(?:[/-](?P<year>\d{4}))?"
    r"(?P<leap> nhuan)?(?P<am> (?:am lich|am|al)\b)?"
    r"(?: nam " + _YEAR_WORD.replace("?P<year>", "?P<year2>") + r")?"
)
# Chỉ cụm đòi ngày DƯƠNG rõ ràng: "ngày mấy"/"là ngày" dùng cho cả hai chiều
# ("20/11 âm lịch ngày mấy" là hỏi ngày âm) nên không nằm đây.
_ASK_SOLAR_RE = re.compile(r"\b(?:duong|ngay nao|thu may|vao ngay|roi vao)\b")
_ASK_FOCUS = (
    ("gio hoang dao", "hours"),
    ("gio tot", "hours"),
    ("gio dep", "hours"),
    ("tiet khi", "term"),
    ("can chi", "canchi"),
)
# Hỏi NGÀY tốt xấu (khác giờ hoàng đạo): trả hoàng đạo/hắc đạo, Tam nương...
_DAY_QUALITY_RE = re.compile(r"\b(?:ngay (?:hoang dao|hac dao|tot|xau)|hac dao|tot xau)\b")
# Cả câu chỉ là "hôm nay tốt hay xấu": hỏi ngày (câu có chủ đề khác thì không).
_GOOD_BAD_RE = re.compile(
    r"(?:hom nay|bua nay|ngay mai|mai|hom qua|ngay kia)(?: la)?(?: ngay)? (?:tot hay xau|xau hay tot)"
)


def _today() -> dt.date:
    return dt.datetime.now(_VN_TZ).date()


def _month_value(raw: str | None) -> int | None:
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    return _MONTH_NAMES.get(raw.strip())


def _year_value(raw: str | None, today: dt.date) -> int | None:
    """None = không nói năm. "năm nay/sau/ngoái" tính theo năm DƯƠNG: Tết của
    năm âm Y luôn rơi vào năm dương Y, nên tháng 1 dương (trước Tết) hỏi "Tết
    năm nay" vẫn là Tết sắp tới chứ không phải Tết đã qua."""
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    return today.year + {"nay": 0, "sau": 1, "toi": 1, "truoc": -1, "ngoai": -1}[raw]


def fold_question(text: str) -> str:
    """Bỏ dấu để so khớp, nhưng giữ riêng các chữ trùng nghĩa sau khi bỏ dấu."""
    text = _NAME_MAI_RE.sub("x_mai", text or "")
    low = text.lower()
    for pattern, repl in _COLLISIONS:
        low = pattern.sub(repl, low)
    t = re.sub(r"[?!.,;:]+", " ", fold(low))
    return re.sub(r"\s+", " ", t).strip()


def _is_date_question(rest: str) -> bool:
    """Sau ngày âm, câu chỉ hỏi ngày dương (hoặc không hỏi gì thêm). "rằm tháng 7
    nên cúng gì" thì KHÔNG - để AI trả lời."""
    return not _FILLER_RE.sub(" ", rest).strip() or bool(_ASK_DATE_RE.search(rest))


def _asks_solar(rest: str) -> bool:
    """Phần sau ngày hỏi ra ngày dương ("là ngày nào", "dương lịch"...) hoặc trống."""
    return not rest.strip() or bool(_ASK_SOLAR_RE.search(rest))


def _quality_answer(day: dt.date, prefix: str) -> str:
    info = lunar.date_info(day)
    return f"{prefix} ({info['weekday']} {info['date_vn']}): {can_chi.day_quality(day)}."


def _focus(text: str) -> str:
    """Phần người dùng hỏi; "date" = chỉ ngày âm (chat_skill: không kể thêm
    giờ hoàng đạo/tiết khí khi không ai hỏi)."""
    found = [key for word, key in _ASK_FOCUS if word in text]
    if len(found) == 1:
        return found[0]
    return "full" if found else "date"


def _lunar_label(ld: int, lm: int, ly: int, leap: bool) -> str:
    text = lunar.lunar_text(ld, lm, ly, leap)
    return text[0].upper() + text[1:]


def _describe_focused(day: dt.date, focus: str, prefix: str) -> str:
    info = lunar.date_info(day)
    lun = info["lunar"]
    if focus == "hours":
        return (
            f"{prefix} ({info['weekday']} {info['date_vn']}) giờ hoàng đạo: "
            + ", ".join(info["lucky_hours"])
            + "."
        )
    if focus == "term":
        return f"{prefix} ({info['weekday']} {info['date_vn']}) thuộc tiết {info['solar_term']}."
    if focus == "canchi":
        return (
            f"{prefix} ({info['weekday']} {info['date_vn']}) là ngày {lun['day_can_chi']}, "
            f"tháng {lun['month_can_chi']}, năm {lun['year_can_chi']}."
        )
    if focus == "date":
        return (
            f"{prefix} ({info['weekday']} {info['date_vn']}) là {lun['text']} âm lịch "
            f"(ngày {lun['day_can_chi']})."
        )
    return f"{prefix}:\n" + lunar.describe_date(info)


def _tet_countdown(today: dt.date) -> str:
    tet = lunar.next_tet(today)
    days = (tet - today).days
    year = lunar.year_can_chi(lunar.solar_to_lunar(tet.day, tet.month, tet.year)[2])
    weekday = lunar.WEEKDAYS[tet.weekday()]
    if days == 0:
        return f"Hôm nay là mùng 1 Tết Nguyên đán năm {year}!"
    return (
        f"Còn {days} ngày nữa là Tết Nguyên đán năm {year} "
        f"(mùng 1 tháng Giêng, {weekday} {tet:%d/%m/%Y})."
    )


def _tet_of_year(lunar_year: int) -> str | None:
    if not _MIN_YEAR <= lunar_year <= _MAX_YEAR:
        return None
    solar = lunar.lunar_to_solar(1, 1, lunar_year)
    if solar is None:
        return None
    tet = dt.date(solar[2], solar[1], solar[0])
    return (
        f"Tết Nguyên đán năm {lunar.year_can_chi(lunar_year)} ({lunar_year}) rơi vào "
        f"{lunar.WEEKDAYS[tet.weekday()]} {tet:%d/%m/%Y} dương lịch."
    )


def _lunar_day_line(day: dt.date, ld: int, lm: int, ly: int, leap: bool) -> str:
    info = lunar.date_info(day)
    return (
        f"{_lunar_label(ld, lm, ly, leap)} âm lịch rơi vào {info['weekday']} "
        f"{info['date_vn']} dương lịch (ngày {info['lunar']['day_can_chi']})."
    )


def _lunar_to_solar_answer(
    ld: int, lm: int, year: int | None, leap: bool, today: dt.date
) -> str | None:
    day, used_year, error = lunar.find_lunar_date(ld, lm, year, leap, today)
    if day is None:
        return error or None
    return _lunar_day_line(day, ld, lm, used_year, leap)


def _month_start(day: dt.date) -> dt.date:
    """Mùng 1 của tháng âm chứa ``day``."""
    return day - dt.timedelta(days=lunar.solar_to_lunar(day.day, day.month, day.year)[0] - 1)


def _relative_month_answer(ld: int, rel: str, today: dt.date) -> str:
    """ "rằm tháng này", "mùng 1 tháng sau": tháng ÂM chứa hôm nay, kế tiếp hoặc trước đó."""
    if not 1 <= ld <= 30:
        return "Ngày âm lịch chỉ từ 1 đến 30."
    start = _month_start(today)
    if rel in ("sau", "toi"):
        start = _month_start(start + dt.timedelta(days=30))
    elif rel == "truoc":
        start = _month_start(start - dt.timedelta(days=1))
    day = start + dt.timedelta(days=ld - 1)
    got, lm, ly, leap = lunar.solar_to_lunar(day.day, day.month, day.year)
    if got != ld:
        return f"Tháng {lm} âm lịch năm {lunar.year_can_chi(ly)} chỉ có 29 ngày."
    line = _lunar_day_line(day, ld, lm, ly, leap)
    if day < today:
        return f"{line} Đã qua {(today - day).days} ngày."
    return line if day == today else f"{line} Còn {(day - today).days} ngày."


def _solar_date(day: int, month: int, year: int | None, today: dt.date) -> dt.date | None:
    try:
        return dt.date(year or today.year, month, day)
    except ValueError:
        return None


def answer(text: str, today: dt.date | None = None) -> str | None:
    """Câu trả lời cho câu hỏi âm lịch, hoặc None nếu không chắc (để AI lo)."""
    t = fold_question(text)
    good_bad = bool(_GOOD_BAD_RE.fullmatch(t))
    if not t or len(t) > 160 or not (good_bad or _LUNAR_HINT_RE.search(t)):
        return None
    today = today or _today()

    if _TET_COUNTDOWN_RE.search(t):
        return _tet_countdown(today)
    tet_date = _TET_DATE_RE.search(t)
    if tet_date and not re.search(r"\b(?:mung|mong|ngay) \d", t):
        year = _year_value(tet_date.group("year"), today)
        if year is None:
            return _tet_countdown(today)
        return _tet_of_year(year)

    # Ngày âm viết bằng chữ: rằm / mùng N [tháng M | tết] ...
    match = _LUNAR_WORDS_RE.search(t)
    if match and (match.group("ram") or match.group("day")):
        ld = 15 if match.group("ram") else int(match.group("day"))
        lm = 1 if match.group("tet") else _month_value(match.group("month"))
        rest = t[match.end() :]
        if match.group("rel") and match.group("mung") != "ngay":
            if not _is_date_question(rest):
                return None  # "rằm tháng sau cúng gì": để AI trả lời
            return _relative_month_answer(ld, match.group("rel"), today)
        # Chắc chắn là ngày âm: rằm, tết, nhuận, giêng/chạp, "mùng N tháng M âm".
        # "ngày N tháng M âm lịch" chỉ chắc khi câu hỏi tiếp theo đòi ngày dương.
        explicit_lunar = bool(
            match.group("ram")
            or match.group("tet")
            or match.group("leap")
            or (match.group("month") or "") in ("gieng", "chap")
            or (match.group("am") and (match.group("mung") != "ngay" or _asks_solar(rest)))
        )
        if lm is not None and match.group("am") and not explicit_lunar:
            return None
        if lm is not None and explicit_lunar:
            if not _is_date_question(rest):
                return None
            year = _year_value(match.group("year"), today)
            return _lunar_to_solar_answer(ld, lm, year, bool(match.group("leap")), today)
        if lm is not None and match.group("day") and not explicit_lunar:
            # "mùng 2 tháng 9 là ngày mấy âm" -> ngày dương hỏi ra âm.
            if _ASK_LUNAR_INFO_RE.search(rest) and not _ASK_SOLAR_RE.search(rest):
                year = match.group("year")
                solar = _solar_date(ld, lm, int(year) if year and year.isdigit() else None, today)
                if solar:
                    return _describe_focused(solar, _focus(t), "Ngày dương lịch")
            return None

    # Ngày dạng số: 15/8 âm (âm -> dương) hoặc 20/11 là ngày mấy âm (dương -> âm)
    match = _NUMERIC_DATE_RE.search(t)
    if match:
        ld, lm = int(match.group("day")), int(match.group("month"))
        year_raw = match.group("year") or match.group("year2")
        rest = t[match.end() :]
        if match.group("leap") or (match.group("am") and _asks_solar(rest)):
            year = _year_value(year_raw, today)
            return _lunar_to_solar_answer(ld, lm, year, bool(match.group("leap")), today)
        if match.group("am"):
            return None  # "20/11 âm lịch là bao nhiêu": không rõ chiều, để AI hỏi lại.
        if _ASK_LUNAR_INFO_RE.search(rest):
            solar = _solar_date(
                ld, lm, int(year_raw) if year_raw and year_raw.isdigit() else None, today
            )
            if solar and _MIN_YEAR <= solar.year <= _MAX_YEAR:
                return _describe_focused(solar, _focus(t), "Ngày dương lịch")
        return None

    # Hôm nay / mai / hôm qua + âm lịch, can chi, hoàng đạo, tiết khí
    rel = _RELATIVE_DAY_RE.search(t)
    if rel and _YEAR_IN_TEXT_RE.search(t):
        return None  # "chị Mai sinh năm 1990 can chi là gì": hỏi về năm, không về ngày
    if rel and (_ASK_LUNAR_INFO_RE.search(t) or _DAY_QUALITY_RE.search(t) or good_bad):
        offset = _RELATIVE_OFFSET[rel.group(1)]
        day = today + dt.timedelta(days=offset)
        label = {0: "Hôm nay", 1: "Ngày mai", -1: "Hôm qua", 2: "Ngày kia"}[offset]
        if (_DAY_QUALITY_RE.search(t) or good_bad) and "gio" not in t:
            return _quality_answer(day, label)
        return _describe_focused(day, _focus(t), label)
    if not rel and re.fullmatch(r"(?:xem )?(?:am lich|lich am)(?: hom nay)?", t):
        return _describe_focused(today, "full", "Hôm nay")
    return None


async def maybe_handle(text: str) -> str | None:
    """Điểm vào cho các kênh chat; None = không phải câu hỏi lịch chắc chắn. Câu hỏi lịch
    dương ("20/11 là thứ mấy", "noel còn mấy ngày") nhờ bộ tra lịch /lich trả lời."""
    from services import calendar_query  # calendar_query import module này

    return answer(text) or calendar_query.chat_answer(text)
