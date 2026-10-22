"""Dữ liệu cho web công khai (trang /lich, /tu-vi, /than-so-hoc và /api/v1/*).

Hàm thuần, không gọi AI, không đụng DB. Kết quả là dict khoá camelCase (hợp
đồng JSON công khai của /api/v1 - chỉ THÊM trường mới, không đổi/bỏ trường
cũ). Lỗi đầu vào raise ``InputError`` (code máy đọc + câu tiếng Việt); tầng web
đổi thành HTTP 422 với cùng một dạng {"error": {code, message}}.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from services import calendar_query, can_chi, lunar, numerology, tuvi

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MIN_YEAR, MAX_YEAR = 1900, 2100
GENDERS = ("male", "female")
HOUR_BRANCHES = [
    (0, "Tý", "23h-1h"), (1, "Sửu", "1h-3h"), (2, "Dần", "3h-5h"), (3, "Mão", "5h-7h"),
    (4, "Thìn", "7h-9h"), (5, "Tỵ", "9h-11h"), (6, "Ngọ", "11h-13h"), (7, "Mùi", "13h-15h"),
    (8, "Thân", "15h-17h"), (9, "Dậu", "17h-19h"), (10, "Tuất", "19h-21h"), (11, "Hợi", "21h-23h"),
]  # fmt: skip


class InputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def today() -> dt.date:
    return dt.datetime.now(_VN_TZ).date()


def _check_year(year: int) -> None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise InputError("YEAR_OUT_OF_RANGE", f"Năm cần trong khoảng {MIN_YEAR}-{MAX_YEAR}.")


def parse_date(raw: str) -> dt.date:
    """'YYYY-MM-DD' (input type=date) hoặc 'DD/MM/YYYY'."""
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            value = dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        _check_year(value.year)
        return value
    raise InputError("INVALID_DATE", "Ngày không hợp lệ (dạng YYYY-MM-DD hoặc DD/MM/YYYY).")


def day_info(day: dt.date) -> dict:
    _check_year(day.year)
    info = lunar.date_info(day)
    god, is_good = can_chi.day_god(day)
    ld = info["lunar"]["day"]
    return {
        "date": info["date"],
        "weekday": info["weekday"],
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
                "weekday": day.weekday(),  # 0 = Thứ Hai
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


def calendar_answer(question: str) -> str:
    question = (question or "").strip()
    if len(question) > 200:
        raise InputError("QUESTION_TOO_LONG", "Câu hỏi tối đa 200 ký tự.")
    return calendar_query.answer(question)


def numerology_reading(birth: dt.date, name: str = "") -> dict:
    _check_year(birth.year)
    if len(name) > 80:
        raise InputError("NAME_TOO_LONG", "Họ tên tối đa 80 ký tự.")
    r = numerology.compute(birth, name)
    texts = numerology._texts()
    meaning = texts.get("so", {})

    def number(value: int | None) -> dict | None:
        return None if value is None else {"value": value, "meaning": meaning.get(value, "")}

    return {
        "birthDate": birth.isoformat(),
        "name": r.name,
        "lifePath": {
            "value": r.life_path,
            "meaning": texts.get("chu_dao", {}).get(r.life_path, ""),
        },
        "dayNumber": number(r.day_number),
        "attitude": number(r.attitude),
        "personalYear": {
            "year": r.for_year,
            "value": r.personal_year,
            "meaning": texts.get("nam_ca_nhan", {}).get(r.personal_year, ""),
        },
        "personalMonth": r.personal_month,
        "expression": number(r.expression),
        "soul": number(r.soul),
        "personality": number(r.personality),
        "chart": {str(d): r.counts[d] for d in range(1, 10)},
        "missingDigits": [d for d in range(1, 10) if not r.counts[d]],
        "arrows": [{"digits": a, "meaning": texts.get("mui_ten", {}).get(a, "")} for a in r.arrows],
        "emptyArrows": [
            {"digits": a, "meaning": texts.get("mui_ten_trong", {}).get(a, "")}
            for a in r.empty_arrows
        ],
        "disclaimer": numerology.DISCLAIMER,
    }


def parse_ymd(raw: str) -> tuple[int, int, int]:
    """'YYYY-MM-DD' -> (năm, tháng, ngày) KHÔNG kiểm tra theo lịch dương (ngày âm
    như 30/2 vẫn hợp lệ)."""
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", raw or "")
    if not m:
        raise InputError("INVALID_DATE", "Ngày có dạng YYYY-MM-DD.")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def tuvi_chart(
    year: int,
    month: int,
    day: int,
    hour_branch: int,
    gender: str,
    is_lunar: bool = False,
    is_leap: bool = False,
) -> dict:
    """Lá số từ ngày sinh dương, hoặc (is_lunar) ngày/tháng/năm ÂM."""
    if gender not in GENDERS:
        raise InputError("INVALID_GENDER", "Giới tính là male hoặc female.")
    if not 0 <= hour_branch <= 11:
        raise InputError("INVALID_HOUR", "Giờ sinh (chi) từ 0 (Tý) đến 11 (Hợi).")
    _check_year(year)
    male = gender == "male"
    if is_lunar:
        if not (1 <= day <= 30 and 1 <= month <= 12):
            raise InputError("INVALID_LUNAR_DATE", "Ngày âm 1-30, tháng âm 1-12.")
        solar = calendar_query.lunar_to_solar_exact(day, month, year, is_leap)
        if solar is None:
            raise InputError("LUNAR_DATE_NOT_FOUND", "Ngày âm lịch này không tồn tại.")
        c = tuvi.build_chart(day, month, year, hour_branch, male, is_leap, solar)
    else:
        try:
            birth = dt.date(year, month, day)
        except ValueError:
            raise InputError("INVALID_DATE", "Ngày dương lịch không tồn tại.") from None
        ld, lm, ly, leap = lunar.solar_to_lunar(birth.day, birth.month, birth.year)
        c = tuvi.build_chart(ld, lm, ly, hour_branch, male, leap, birth)
    born = can_chi.year_profile(c.lunar_year)
    texts = tuvi._texts()
    palaces = []
    for i in range(12):
        pos = (c.menh + i) % 12
        main, other = tuvi._split_stars(c.stars[pos])
        start_age, end_age = c.dai_han[pos]
        palaces.append(
            {
                "name": tuvi.PALACES[i],
                "branch": can_chi.CHI[pos],
                "branchIndex": pos,
                "stem": can_chi.CAN[c.palace_can(pos)],
                "mainStars": main,
                "minorStars": other,
                "isThan": pos == c.than,
                "hasTuan": pos in c.tuan,
                "hasTriet": pos in c.triet,
                "daiHan": {"from": start_age, "to": end_age},
                "trangSinh": c.trang_sinh[pos],
            }
        )
    menh_main, _ = tuvi._split_stars(c.stars[c.menh])
    menh_notes = [
        {"star": n.split(" (")[0], "meaning": texts.get("chinh_tinh", {}).get(n.split(" (")[0], "")}
        for n in menh_main
    ]
    return {
        "solarDate": c.solar.isoformat() if c.solar else None,
        "lunarDate": {
            "day": c.lunar_day,
            "month": c.lunar_month,
            "year": c.lunar_year,
            "isLeapMonth": c.leap,
            "text": lunar.lunar_text(c.lunar_day, c.lunar_month, c.lunar_year, c.leap),
        },
        "hourBranch": can_chi.CHI[c.hour_chi],
        "gender": gender,
        "yinYang": ("Dương" if c.can_y % 2 == 0 else "Âm") + (" Nam" if male else " Nữ"),
        "napAm": born["nap_am"],
        "element": born["menh"],
        "cuc": tuvi._CUC_NAME[c.cuc],
        "menhChu": tuvi._MENH_CHU[c.menh],
        "thanChu": tuvi._THAN_CHU[c.chi_y],
        "thanPalace": c.palace_at(c.than),
        "tuHoa": c.tu_hoa,
        "palaces": palaces,
        "menhNotes": menh_notes,
        "isMenhEmpty": not menh_main,
        "menhEmptyNote": texts.get("vo_chinh_dieu", "") if not menh_main else "",
        "disclaimer": tuvi.DISCLAIMER,
    }
