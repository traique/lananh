"""Dữ liệu cho web công khai (trang /lich, /tu-vi, /than-so-hoc, /ky-mon và /api/v1/*).

Hàm thuần, không gọi AI, không đụng DB. Kết quả là dict khoá camelCase (hợp
đồng JSON công khai của /api/v1 - chỉ THÊM trường mới, không đổi/bỏ trường
cũ). Lỗi đầu vào raise ``InputError`` (code máy đọc + câu tiếng Việt); tầng web
đổi thành HTTP 422 với cùng một dạng {"error": {code, message}}.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from core.text_normalize import fold
from services import calendar_query, can_chi, kymon, lunar, numerology, tuvi

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MIN_YEAR, MAX_YEAR = 1900, 2100
GENDERS = ("male", "female")
HOUR_BRANCHES = [
    (0, "Tý", "0h-1h"), (1, "Sửu", "1h-3h"), (2, "Dần", "3h-5h"), (3, "Mão", "5h-7h"),
    (4, "Thìn", "7h-9h"), (5, "Tỵ", "9h-11h"), (6, "Ngọ", "11h-13h"), (7, "Mùi", "13h-15h"),
    (8, "Thân", "15h-17h"), (9, "Dậu", "17h-19h"), (10, "Tuất", "19h-21h"), (11, "Hợi", "21h-23h"),
]  # fmt: skip
# Ô chọn giờ trên web: thêm "Tý đầu" (23h-24h, an theo ngày hôm sau) với mã riêng.
EARLY_TY = 12
HOUR_OPTIONS = [*HOUR_BRANCHES, (EARLY_TY, "Tý đầu", "23h-24h, an theo ngày hôm sau")]


class InputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now() -> dt.datetime:
    return dt.datetime.now(_VN_TZ)


def today() -> dt.date:
    return now().date()


def _check_year(year: int) -> None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise InputError("YEAR_OUT_OF_RANGE", f"Năm cần trong khoảng {MIN_YEAR}-{MAX_YEAR}.")


_YMD_RE = re.compile(
    r"\s*(?:(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})|(?P<d2>\d{1,2})/(?P<m2>\d{1,2})/(?P<y2>\d{4}))\s*"
)


def parse_date(raw: str) -> dt.date:
    """'YYYY-MM-DD' (input type=date, không cần đệm 0) hoặc 'DD/MM/YYYY' -> ngày
    dương hợp lệ trong 1900-2100."""
    year, month, day = parse_ymd(raw)
    try:
        value = dt.date(year, month, day)
    except ValueError:
        raise InputError("INVALID_DATE", "Ngày không tồn tại trên lịch dương.") from None
    _check_year(value.year)
    return value


def day_info(day: dt.date) -> dict:
    _check_year(day.year)
    info = lunar.date_info(day)
    god, is_good = can_chi.day_god(day)
    ld = info["lunar"]["day"]
    return {
        "date": info["date"],
        "weekday": info["weekday"],  # tên thứ ("Thứ Tư")
        "weekdayIndex": day.weekday(),  # 0 = Thứ Hai
        "lunar": {
            "day": ld,
            "month": info["lunar"]["month"],
            "year": info["lunar"]["year"],
            "isLeapMonth": info["lunar"]["leap_month"],
            "text": info["lunar"]["text"],
            "dayCanChi": info["lunar"]["day_can_chi"],
            "monthCanChi": info["lunar"]["month_can_chi"],
            "yearCanChi": info["lunar"]["year_can_chi"],
        },
        "solarTerm": info["solar_term"],
        "luckyHours": info["lucky_hours"],
        "dayGod": god,
        "isHoangDao": is_good,
        "isTamNuong": ld in can_chi.TAM_NUONG,
        "isNguyetKy": ld in can_chi.NGUYET_KY,
        "holidays": calendar_query.holidays_on(day),
        "nextTet": info["next_tet"],
        "daysToTet": info["days_to_tet"],
    }


def month_grid(year: int, month: int) -> dict:
    _check_year(year)
    if not 1 <= month <= 12:
        raise InputError("INVALID_MONTH", "Tháng cần từ 1 đến 12.")
    days = []
    for day in calendar_query.month_days(year, month):
        ld, lm, _, leap = lunar.solar_to_lunar(day.day, day.month, day.year)
        _, is_good = can_chi.day_god(day)
        days.append(
            {
                "date": day.isoformat(),
                "day": day.day,
                "weekday": day.weekday(),  # 0 = Thứ Hai (số, khác /days trả tên)
                "weekdayName": lunar.WEEKDAYS[day.weekday()],
                "lunarDay": ld,
                "lunarMonth": lm,
                "isLeapMonth": leap,
                "isHoangDao": is_good,
                "holidays": calendar_query.holidays_on(day),
            }
        )
    prev_idx, next_idx = year * 12 + month - 2, year * 12 + month
    return {
        "year": year,
        "month": month,
        "days": days,
        "previous": f"{prev_idx // 12:04d}-{prev_idx % 12 + 1:02d}",
        "next": f"{next_idx // 12:04d}-{next_idx % 12 + 1:02d}",
    }


def lunar_to_solar(lunar_day: int, lunar_month: int, lunar_year: int, is_leap: bool) -> dict:
    _check_year(lunar_year)
    if not (1 <= lunar_day <= 30 and 1 <= lunar_month <= 12):
        raise InputError("INVALID_LUNAR_DATE", "Ngày âm 1-30, tháng âm 1-12.")
    day = calendar_query.lunar_to_solar_exact(lunar_day, lunar_month, lunar_year, is_leap)
    if day is None:
        raise InputError("LUNAR_DATE_NOT_FOUND", "Ngày âm lịch này không tồn tại.")
    return day_info(day)


def slug(text: str) -> str:
    """'Lễ Vu Lan (Rằm tháng 7)' -> 'le-vu-lan', 'động thổ, xây sửa nhà' -> 'dong-tho':
    bỏ phần trong ngoặc và sau dấu phẩy, bỏ dấu, nối bằng gạch."""
    base = re.sub(r"\s*\(.*?\)", "", text).split(",")[0]
    return re.sub(r"[^a-z0-9]+", "-", fold(base)).strip("-")


# Trang riêng cho từng ngày lễ (/lich/ngay-le/<slug>) và ngày tốt theo việc (?viec=<slug>).
HOLIDAY_SLUGS = {slug(h[0]): h for h in calendar_query.HOLIDAYS}
PURPOSE_SLUGS = {slug(name): (name, trucs) for name, trucs in calendar_query.purpose_rules()}


def holiday_page(key: str, today: dt.date, count: int = 5) -> dict | None:
    """Một ngày lễ: lần tới (kể cả hôm nay) và các lần sau; None nếu không có lễ này
    hoặc đã ra ngoài khoảng lịch tính được."""
    holiday = HOLIDAY_SLUGS.get(key)
    if holiday is None:
        return None
    dates: list[dt.date] = []
    start = today
    while len(dates) < count:
        day = calendar_query.holiday_entry_date(holiday, start)
        if day is None or day.year > MAX_YEAR:
            break
        dates.append(day)
        start = day + dt.timedelta(days=1)
    if not dates:
        return None
    name = holiday[0]
    short, _, aka = name.partition(" (")
    return {
        "slug": key,
        "name": name,
        "short": short,
        "aka": aka.rstrip(")"),
        "isLunar": holiday[1],
        "dates": [{**day_info(d), "days": (d - today).days} for d in dates],
    }


def good_days(year: int, month: int, purpose: str = "") -> dict:
    """Ngày tốt của 1 tháng: hoàng đạo, không Tam nương/Nguyệt kỵ; có việc (khoá trong
    PURPOSE_SLUGS) thì thêm trực hợp việc - cùng cách chọn với Hỏi lịch."""
    _check_year(year)
    if not 1 <= month <= 12:
        raise InputError("INVALID_MONTH", "Tháng cần từ 1 đến 12.")
    if purpose and purpose not in PURPOSE_SLUGS:
        raise InputError("INVALID_PURPOSE", "Chưa có cách xem ngày cho việc này.")
    name, trucs = PURPOSE_SLUGS[purpose] if purpose else ("", None)
    days = calendar_query.month_days(year, month)
    good = [
        {**day_info(d), "truc": can_chi.day_truc(d)}
        for d in days
        if calendar_query.is_good_day(d, trucs)
    ]
    avoid = [
        d.isoformat()
        for d in days
        if lunar.solar_to_lunar(d.day, d.month, d.year)[0] in can_chi.TAM_NUONG | can_chi.NGUYET_KY
    ]
    return {
        "year": year,
        "month": month,
        "purpose": purpose,
        "purposeName": name,
        "trucs": list(trucs or ()),
        "days": good,
        "avoid": avoid,
    }


def birth_year(year: int, today: dt.date) -> dict:
    """Tuổi theo năm sinh (năm âm lịch): can chi, mệnh, khoảng ngày dương của năm âm,
    hợp/xung kèm năm sinh gần, sao hạn năm nay và năm sau."""
    if not MIN_YEAR <= year <= today.year:
        raise InputError("YEAR_OUT_OF_RANGE", f"Năm sinh cần trong khoảng {MIN_YEAR}-{today.year}.")
    profile = can_chi.year_profile(year)
    first = calendar_query.lunar_to_solar_exact(1, 1, year)
    nxt = calendar_query.lunar_to_solar_exact(1, 1, year + 1)
    relations = can_chi.chi_relations(profile["chi"])

    def near(kind: str, chi_names: list[str]) -> list[dict]:
        # Mỗi chi kèm các năm sinh gần nhất (12 năm trước/sau) để đọc "hợp tuổi nào".
        return [
            {
                "kind": kind,
                "chi": name,
                "years": [
                    y
                    for y in range(year - 11, year + 12)
                    if y != year
                    and MIN_YEAR <= y <= today.year
                    and can_chi.CHI[can_chi.year_chi(y)] == name
                ],
            }
            for name in chi_names
        ]

    this_year = can_chi.lunar_year_of(today)
    return {
        "year": year,
        "canChi": profile["can_chi"],
        "conGiap": profile["con_giap"],
        "menh": profile["menh"],
        "napAm": profile["nap_am"],
        "amDuong": profile["am_duong"],
        "solarStart": first.isoformat() if first else "",
        "solarEnd": (nxt - dt.timedelta(days=1)).isoformat() if nxt else "",
        "ageNow": can_chi.tuoi_am(year, this_year),
        "thisYear": this_year,
        "hop": near("Tam hợp", relations["tam_hop"]) + near("Lục hợp", relations["luc_hop"]),
        # Tứ hành xung đã gồm chi xung đối: chỉ thêm 2 chi còn lại của nhóm.
        "ky": near("Xung", relations["xung"])
        + near("Tứ hành xung", [c for c in relations["tu_hanh_xung"] if c not in relations["xung"]])
        + near("Lục hại", relations["hai"]),
        "han": [can_chi.han_profile(year, y) for y in (this_year, this_year + 1) if y > year],
    }


def calendar_answer(question: str) -> str:
    """Trả lời ô "Hỏi lịch" trên web: cùng bộ tra với lệnh /lich nhưng câu gợi ý không
    có cú pháp lệnh chat."""
    question = (question or "").strip()
    if len(question) > 200:
        raise InputError("QUESTION_TOO_LONG", "Câu hỏi tối đa 200 ký tự.")
    reply = calendar_query.understand(question)
    if reply is None:
        return "Chưa hiểu câu hỏi này nên chưa tra được.\n\n" + calendar_query.WEB_HELP
    return reply.replace("/lich ", "")


def numerology_reading(birth: dt.date, name: str = "") -> dict:
    _check_year(birth.year)
    if len(name) > 80:
        raise InputError("NAME_TOO_LONG", "Họ tên tối đa 80 ký tự.")
    if name.strip() and not numerology.has_letters(name):
        raise InputError("INVALID_NAME", numerology.NAME_ERROR)
    return numerology.reading_data(numerology.compute(birth, name, today()))


def parse_ymd(raw: str) -> tuple[int, int, int]:
    """'YYYY-MM-DD' hoặc 'DD/MM/YYYY' -> (năm, tháng, ngày) KHÔNG kiểm tra theo
    lịch dương (ngày âm như 30/2 vẫn hợp lệ)."""
    if not (raw or "").strip():
        raise InputError("INVALID_DATE", "Thiếu ngày (birthDate), dạng YYYY-MM-DD hoặc DD/MM/YYYY.")
    m = _YMD_RE.fullmatch(raw)
    if not m:
        raise InputError("INVALID_DATE", "Ngày có dạng YYYY-MM-DD hoặc DD/MM/YYYY.")
    if m.group("y"):
        return int(m.group("y")), int(m.group("m")), int(m.group("d"))
    return int(m.group("y2")), int(m.group("m2")), int(m.group("d2"))


_TIME_RE = re.compile(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*")
_ISO_DATETIME_RE = re.compile(
    r"\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\s*"
)


def parse_time(raw: str) -> tuple[int, int]:
    """'HH:MM' (input type=time, có thể kèm giây) -> (giờ, phút)."""
    m = _TIME_RE.fullmatch(raw or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise InputError("INVALID_TIME", "Giờ có dạng HH:MM (00:00-23:59).")
    return int(m.group(1)), int(m.group(2))


def qimen_moment(date_raw: str = "", time_raw: str = "") -> dt.datetime:
    """Thời điểm lập bàn Kỳ Môn (giờ VN) từ ô ngày + ô giờ của trang /ky-mon;
    bỏ trống ngày = hôm nay, bỏ trống giờ = giờ hiện tại."""
    current = now()
    day = parse_date(date_raw) if (date_raw or "").strip() else current.date()
    hour, minute = (
        parse_time(time_raw) if (time_raw or "").strip() else (current.hour, current.minute)
    )
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=_VN_TZ)


def parse_datetime(raw: str) -> dt.datetime:
    """'YYYY-MM-DDTHH:MM[:SS][Z|±HH:MM]' (ISO 8601) cho JSON API. Không có múi giờ
    hiểu là giờ Việt Nam; có múi giờ thì giữ (lập bàn theo giờ đồng hồ múi đó)."""
    if not _ISO_DATETIME_RE.fullmatch(raw or ""):
        raise InputError(
            "INVALID_DATETIME", "dateTime có dạng YYYY-MM-DDTHH:MM, có thể kèm múi giờ (+07:00)."
        )
    try:
        value = dt.datetime.fromisoformat(raw.strip())
    except ValueError:
        raise InputError("INVALID_DATETIME", "dateTime không tồn tại trên lịch.") from None
    _check_year(value.year)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_VN_TZ)
    return value.replace(second=0, microsecond=0)


def qimen_chart(moment: dt.datetime) -> dict:
    """Bàn Kỳ Môn dạng dict (trang web/JSON API) cho thời điểm có múi giờ."""
    _check_year(moment.year)
    return kymon.chart_data(kymon.build_chart(moment))


def build_tuvi(
    year: int,
    month: int,
    day: int,
    hour_branch: int,
    gender: str,
    is_lunar: bool = False,
    is_leap: bool = False,
    early_ty: bool = False,
) -> tuvi.Chart:
    """Lá số (đối tượng Chart) từ ngày sinh dương, hoặc (is_lunar) ngày/tháng/năm ÂM.
    early_ty: sinh 23h-24h (giờ Tý đầu) - an theo ngày hôm sau, chỉ hợp lệ với
    hour_branch = 0. Raise InputError khi dữ liệu sai."""
    if gender not in GENDERS:
        raise InputError("INVALID_GENDER", "Giới tính là male hoặc female.")
    if not 0 <= hour_branch <= 11:
        raise InputError("INVALID_HOUR", "hourBranch là số 0 (Tý) đến 11 (Hợi).")
    if early_ty and hour_branch != 0:
        raise InputError("INVALID_HOUR", "Giờ Tý đầu (23h-24h) chỉ đi với giờ Tý (0).")
    _check_year(year)
    male = gender == "male"
    if is_lunar:
        if not (1 <= day <= 30 and 1 <= month <= 12):
            raise InputError("INVALID_LUNAR_DATE", "Ngày âm 1-30, tháng âm 1-12.")
        solar = calendar_query.lunar_to_solar_exact(day, month, year, is_leap)
        if solar is None:
            raise InputError("LUNAR_DATE_NOT_FOUND", "Ngày âm lịch này không tồn tại.")
    else:
        try:
            solar = dt.date(year, month, day)
        except ValueError:
            raise InputError("INVALID_DATE", "Ngày dương lịch không tồn tại.") from None
    return tuvi.chart_for(solar, hour_branch, male, early_ty)


def tuvi_chart(
    year: int,
    month: int,
    day: int,
    hour_branch: int,
    gender: str,
    is_lunar: bool = False,
    is_leap: bool = False,
    early_ty: bool = False,
) -> dict:
    """Lá số dạng dict (cho trang web/JSON API), xem build_tuvi."""
    return tuvi.chart_data(
        build_tuvi(year, month, day, hour_branch, gender, is_lunar, is_leap, early_ty)
    )
