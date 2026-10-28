"""Lệnh /lich <câu hỏi tự do>: tra lịch hoàn toàn bằng code, KHÔNG BAO GIỜ gọi AI.

Thứ tự thử (chữ đã bỏ dấu, xem ``answer``):
1. Sao hạn / Tam tai / Kim lâu / Hoang ốc theo năm sinh.
2. Tuổi hợp/xung (hai năm sinh, hoặc "tuổi Ngọ hợp tuổi nào").
3. Tuổi, mệnh, con giáp của 1 năm/ngày sinh; "năm 2027 là năm con gì".
4. Xem ngày theo việc (cưới hỏi, khai trương, động thổ...): hoàng đạo + Thập nhị trực.
   Ngày tốt xấu chung: hoàng đạo/hắc đạo, Tam nương, Nguyệt kỵ (1 ngày, cuối tuần, cả
   tháng - tháng này thì chỉ từ hôm nay).
5. Đếm ngày: "còn mấy ngày tới 30/4", "từ 1/1 đến 20/11 bao nhiêu ngày".
6. Ngày lễ theo tên ("trung thu", "vu lan năm sau") và danh sách lễ sắp tới.
7. Bảng lịch tháng (dương, hoặc tháng âm khi có chữ "âm").
8. Rằm / mùng 1 sắp tới; thứ trong tuần ("thứ 7 tuần sau", "cuối tuần").
9. Câu âm lịch chung (services/lunar_chat.py) và 1 ngày dương bất kỳ.
Ngày dương không có thật ("29/2/2027", "32/12") được báo đúng lỗi. Không hiểu ->
``understand`` trả None; ``answer`` (lệnh chat) trả gợi ý cách hỏi (vẫn không gọi AI),
web dùng câu gợi ý riêng (services/site_data.py).
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from zoneinfo import ZoneInfo

from services import can_chi, lunar, lunar_chat
from core.text_normalize import fold

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_MIN_YEAR, _MAX_YEAR = 1900, 2100

HELP = """📅 /lich <câu hỏi> - tra lịch, không tốn lượt AI. Ví dụ:
• /lich - hôm nay (âm lịch, can chi, giờ hoàng đạo, ngày tốt xấu)
• /lich 20/11, /lich mai, /lich thứ 7 tuần sau, /lich cuối tuần
• /lich rằm tháng 8, /lich âm 15/6n/2025 (n = tháng nhuận), /lich rằm tới
• /lich tháng này, /lich tháng 12/2026, /lich tháng 8 âm
• /lich trung thu, /lich vu lan năm sau, /lich các ngày lễ
• /lich còn mấy ngày tới 30/4, /lich từ 1/1 đến 20/11
• /lich ngày tốt tháng này, /lich hôm nay hoàng đạo không
• /lich xem ngày cưới tháng 12, /lich ngày khai trương, /lich xem ngày động thổ
• /lich tuổi 15/3/1990, /lich 1990 mệnh gì, /lich tuổi Ngọ hợp tuổi nào
• /lich 1990 và 1995 có hợp không
• /lich sao hạn 1990 nam năm 2027, /lich 1995 kim lâu năm 2027"""

_WEEKDAY_WORDS = {
    "hai": 0,
    "2": 0,
    "ba": 1,
    "3": 1,
    "tu": 2,
    "4": 2,
    "nam": 3,
    "5": 3,
    "sau": 4,
    "6": 4,
    "bay": 5,
    "7": 5,
}
_CHI_FOLDED = [fold(c) for c in can_chi.CHI]  # "ty", "suu", ... ("ty" hai lần: Tý/Tỵ)

# (tên hiển thị, âm lịch?, ngày, tháng, các cách gọi đã bỏ dấu)
HOLIDAYS = [
    ("Tết Dương lịch", False, 1, 1, ("tet duong lich", "tet tay")),
    ("Rằm tháng Giêng (Tết Nguyên tiêu)", True, 15, 1, ("nguyen tieu", "ram thang gieng")),
    ("Lễ Tình nhân (Valentine)", False, 14, 2, ("valentine", "le tinh nhan")),
    ("Tết Hàn thực", True, 3, 3, ("han thuc", "banh troi")),
    ("Quốc tế Phụ nữ", False, 8, 3, ("quoc te phu nu",)),
    ("Giỗ Tổ Hùng Vương", True, 10, 3, ("gio to", "hung vuong")),
    ("Giải phóng miền Nam", False, 30, 4, ("giai phong mien nam", "thong nhat")),
    ("Quốc tế Lao động", False, 1, 5, ("quoc te lao dong",)),
    ("Lễ Phật đản", True, 15, 4, ("phat dan",)),
    ("Quốc tế Thiếu nhi", False, 1, 6, ("thieu nhi",)),
    ("Tết Đoan ngọ", True, 5, 5, ("doan ngo", "giet sau bo")),
    ("Lễ Vu Lan (Rằm tháng 7)", True, 15, 7, ("vu lan", "xa toi", "ram thang 7")),
    ("Quốc khánh", False, 2, 9, ("quoc khanh",)),
    ("Tết Trung thu", True, 15, 8, ("trung thu",)),
    ("Ngày Phụ nữ Việt Nam", False, 20, 10, ("phu nu viet nam",)),
    ("Ngày Nhà giáo Việt Nam", False, 20, 11, ("nha giao",)),
    ("Giáng sinh (Noel)", False, 25, 12, ("giang sinh", "noel")),
    ("Ông Công ông Táo", True, 23, 12, ("ong cong", "ong tao", "tao quan")),
    ("Tết Nguyên đán", True, 1, 1, ("tet nguyen dan", "tet am", "tet")),
]

# Gợi ý cách hỏi cho trang web /lich/hoi (không có cú pháp lệnh, không xưng "em").
WEB_HELP = """Có thể hỏi theo các cách sau:
• Một ngày: 20/11, mai, thứ 7 tuần sau, cuối tuần này
• Âm lịch: rằm tháng 8, âm 15/6n/2025 (n = tháng nhuận), rằm tới
• Lịch tháng: tháng này, tháng 12/2026, tháng 8 âm
• Ngày lễ: trung thu, vu lan năm sau, Tết 2028, các ngày lễ
• Đếm ngày: còn mấy ngày tới 30/4, từ 1/1 đến 20/11
• Ngày tốt: ngày tốt tháng này, xem ngày cưới tháng 12, ngày khai trương, xem ngày động thổ
• Tuổi: tuổi 15/3/1990, 1990 mệnh gì, tuổi Ngọ hợp tuổi nào, 1990 và 1995 có hợp không
• Sao hạn: sao hạn 1990 nam năm 2027, 1995 kim lâu năm 2027"""

# Xem ngày theo việc: ngày hoàng đạo, không phạm Tam nương/Nguyệt kỵ, và trực (Thập nhị
# trực, can_chi.day_truc) hợp việc theo bảng phổ biến của lịch vạn sự.
# (tên việc, các trực hợp, cách gọi có dấu, cách gọi bỏ dấu) - xem _keyword.
_PURPOSES = [
    (
        "cưới hỏi",
        ("Định", "Thành", "Khai"),
        r"cưới|ăn hỏi|đám hỏi|kết hôn|rước dâu|lấy (?:vợ|chồng)|dạm ngõ",
        # "cuoi" trần trùng "cuối" (cuối tuần, cuối tháng) nên chỉ nhận khi đi sau ngày/để/xem.
        r"cuoi (?:hoi|vo|chong)|an hoi|dam hoi|dam cuoi|ket hon|ruoc dau|lay (?:vo|chong)|"
        r"dam ngo|(?:de|cho) cuoi|(?:ngay|xem) cuoi(?! (?:tuan|thang|nam|cung|ky))",
    ),
    (
        "khai trương",
        ("Mãn", "Thành", "Khai"),
        r"khai trương|mở (?:cửa hàng|hàng|quán|tiệm|shop|công ty)",
        r"khai truong|mo (?:cua hang|hang|quan|tiem|shop|cong ty)",
    ),
    (
        "động thổ, xây sửa nhà",
        ("Bình", "Định", "Chấp", "Thành"),
        r"động thổ|xây (?:nhà|dựng)|làm nhà|sửa nhà|cất nhà|đổ móng|đổ mái",
        r"dong tho|xay (?:nha|dung)|lam nha|sua nha|cat nha|do mong|do mai",
    ),
    (
        "nhập trạch, về nhà mới",
        ("Định", "Thành", "Khai"),
        r"nhập trạch|về nhà mới|chuyển nhà|dọn nhà|dọn về nhà",
        r"nhap trach|ve nha moi|chuyen nha|don nha|don ve nha",
    ),
    (
        "xuất hành, đi xa",
        ("Kiến", "Mãn", "Thành", "Khai"),
        r"xuất hành|đi xa|khởi hành|lên đường",
        r"xuat hanh|di xa|khoi hanh|len duong",
    ),
    (
        "ký kết, giao dịch",
        ("Mãn", "Định", "Chấp", "Thành"),
        r"ký (?:hợp đồng|kết)|giao dịch|mua (?:nhà|đất|xe)",
        r"ky (?:hop dong|ket)|giao dich|mua (?:nha|dat|xe)",
    ),
]
# Ngày tốt chung (không nói việc): nhắc cách lọc theo việc để khỏi hiểu nhầm.
_PURPOSE_HINT = (
    "Đây là ngày tốt chung, chưa lọc theo việc. Muốn lọc theo việc, hỏi ví dụ: "
    "ngày cưới tháng này, xem ngày khai trương, xem ngày động thổ."
)

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|2100)\b")
# Năm đứng riêng sau tên lễ ("Tết 2028"), không phải phần năm của 1 ngày "25/12/2026".
_BARE_YEAR_RE = re.compile(r"(?<![\d/-])(\d{4})\b")
# Mọi cụm dạng ngày/tháng(/năm) để báo ngày không có thật; bỏ qua khung giờ "13-15h".
_ANY_DATE_RE = re.compile(
    r"(?<![\d/:-])(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?(?![\d/-]|\s*(?:h|g|gio)\b)"
)
_WEEKEND_RE = re.compile(r"\bcuoi tuan\b|\bthu (?:bay|7)(?: va)? (?:chu nhat|cn)\b")
_FULL_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
_SOLAR_DATE_RE = re.compile(
    r"\b(?:ngay )?(?P<d>\d{1,2})(?:[/-]|\s+thang\s+)(?P<m>\d{1,2})"
    r"(?:(?:[/-]|\s+nam\s+)(?P<y>\d{4}))?\b(?!\s*(?:am|al|n\b|nhuan))"
)
# "âm 15/8", "âm 15/8 là ngày nào dương" (trước đây phải hết câu ngay sau ngày, nên
# câu có đuôi hỏi bị hiểu thành ngày DƯƠNG 15/8).
_LUNAR_PREFIX_RE = re.compile(
    r"^(?:ngay )?(?:am lich|am|al)\s+(?P<d>\d{1,2})[/-](?P<m>\d{1,2})(?P<leap>n)?(?:[/-](?P<y>\d{4}))?"
    r"(?P<leap2>\s*(?:nhuan|n)\b)?(?![\d/-])"
)
_MONTH_RE = re.compile(
    r"\bthang\s+(?P<rel>nay|sau|toi|truoc)\b|\bthang\s+(?P<m>\d{1,2})(?:[/-](?P<y>\d{4})|\s+nam\s+(?P<y2>\d{4}))?\b"
)


def _today() -> dt.date:
    return dt.datetime.now(_VN_TZ).date()


def _norm(text: str) -> str:
    # Cùng cách bỏ dấu với chat (giữ "ẩm", "một", tên "Mai" khỏi bị hiểu nhầm).
    t = lunar_chat.fold_question(text)
    t = re.sub(r"[?!,;:]+", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def holidays_on(day: dt.date) -> list[str]:
    ld, lm, _, leap = lunar.solar_to_lunar(day.day, day.month, day.year)
    return [
        name
        for name, is_lunar, d, m, _ in HOLIDAYS
        if (is_lunar and not leap and (d, m) == (ld, lm))
        or (not is_lunar and (d, m) == (day.day, day.month))
    ]


def describe_full(day: dt.date, label: str = "") -> str:
    head = f"📅 {_cap(label)}\n" if label else "📅 "
    holidays = holidays_on(day)
    tail = f"\n🎉 {', '.join(holidays)}" if holidays else ""
    return (
        f"{head}{lunar.describe_date(lunar.date_info(day))}\n"
        f"{_cap(can_chi.day_quality(day))}.{tail}"
    )


def lunar_to_solar_exact(ld: int, lm: int, ly: int, leap: bool = False) -> dt.date | None:
    """Ngày dương của 1 ngày âm; None nếu không tồn tại (ngày 30 của tháng thiếu,
    tháng nhuận năm không nhuận, năm ngoài 1900-2100)."""
    if not _MIN_YEAR <= ly <= _MAX_YEAR:
        return None
    solar = lunar.lunar_to_solar(ld, lm, ly, leap)
    if solar is None:
        return None
    day = dt.date(solar[2], solar[1], solar[0])
    if lunar.solar_to_lunar(day.day, day.month, day.year) != (ld, lm, ly, leap):
        return None
    return day


def _next_lunar(ld: int, lm: int, today: dt.date, year: int | None = None) -> dt.date | None:
    return lunar.find_lunar_date(ld, lm, year, False, today)[0]


def _next_solar(d: int, m: int, today: dt.date, year: int | None = None) -> dt.date | None:
    try:
        if year is not None:
            # Năm ngoài 1900-2100: lịch âm không hỗ trợ (năm 9999 làm next_tet crash).
            return dt.date(year, m, d) if _MIN_YEAR <= year <= _MAX_YEAR else None
        day = dt.date(today.year, m, d)
        return day if day >= today else dt.date(today.year + 1, m, d)
    except ValueError:
        return None


def holiday_entry_date(h, today: dt.date, year: int | None = None) -> dt.date | None:
    _, is_lunar, d, m, _ = h
    return (_next_lunar if is_lunar else _next_solar)(d, m, today, year)


def _find_holiday(t: str):
    for h in HOLIDAYS:
        if any(re.search(rf"\b{re.escape(alias)}\b", t) for alias in h[4]):
            return h
    return None


def _year_modifier(t: str, today: dt.date) -> int | None:
    """Năm trong câu ("năm 2027/nay/sau/ngoái"), tính theo năm DƯƠNG kể cả với
    lễ âm lịch: lễ của năm âm Y luôn rơi vào năm dương Y (hoặc đầu Y+1 với ông
    Táo), nên tháng 1 dương hỏi "trung thu năm nay" là trung thu sắp tới."""
    m = re.search(r"\bnam (\d{4}|nay|sau|toi|truoc|ngoai)\b", t)
    if not m:
        return None
    raw = m.group(1)
    if raw.isdigit():
        return int(raw)
    return today.year + {"nay": 0, "sau": 1, "toi": 1, "truoc": -1, "ngoai": -1}[raw]


def _holiday_year(t: str, today: dt.date) -> int | None:
    """Năm của lễ: "năm 2027/nay/sau" hoặc năm đứng riêng ("Tết 2028", "trung thu 2027")."""
    year = _year_modifier(t, today)
    if year is None:
        m = _BARE_YEAR_RE.search(t)
        year = int(m.group(1)) if m else None
    return year


def _weekend_days(t: str, today: dt.date) -> list[tuple[dt.date, str]] | None:
    """Thứ Bảy + Chủ Nhật của "cuối tuần (này/sau)", "thứ 7 chủ nhật"; bỏ ngày đã qua
    (Chủ Nhật hỏi "cuối tuần này" thì còn mỗi hôm nay)."""
    if not _WEEKEND_RE.search(t):
        return None
    saturday = today + dt.timedelta(days=(5 - today.weekday()) % 7)
    if today.weekday() == 6:
        saturday = today - dt.timedelta(days=1)
    if re.search(r"\btuan (?:sau|toi)\b", t):
        saturday += dt.timedelta(days=7)
    days = [(saturday, "Thứ Bảy"), (saturday + dt.timedelta(days=1), "Chủ Nhật")]
    return [(day, label) for day, label in days if day >= today]


def _relative_day(t: str, today: dt.date) -> tuple[dt.date, str] | None:
    """hôm nay/mai/hôm qua/ngày kia/thứ X tuần này|sau|trước/chủ nhật."""
    for word, offset, label in (
        ("hom nay", 0, "Hôm nay"),
        ("bua nay", 0, "Hôm nay"),
        ("ngay mai", 1, "Ngày mai"),
        ("hom qua", -1, "Hôm qua"),
        ("ngay kia", 2, "Ngày kia"),
    ):
        if re.search(rf"\b{word}\b", t):
            return today + dt.timedelta(days=offset), label
    week = re.search(
        r"\b(?:thu (hai|ba|tu|nam|sau|bay|[2-7])|(chu nhat|cn))\b(?: tuan (nay|sau|toi|truoc))?", t
    )
    if week:
        weekday = 6 if week.group(2) else _WEEKDAY_WORDS[week.group(1)]
        shift = {"sau": 7, "toi": 7, "truoc": -7}.get(week.group(3) or "", 0)
        monday = today - dt.timedelta(days=today.weekday())
        day = monday + dt.timedelta(days=weekday + shift)
        if not week.group(3) and day < today:
            day += dt.timedelta(days=7)  # "thứ 7" trần = thứ 7 sắp tới
        return day, lunar.WEEKDAYS[weekday] + (f" tuần {week.group(3)}" if week.group(3) else "")
    if re.fullmatch(r"(?:hom )?mai", t):
        return today + dt.timedelta(days=1), "Ngày mai"
    return None


def _asked_day(t: str, today: dt.date) -> tuple[dt.date, str] | None:
    """Như _relative_day, nhận thêm "mai" đầu câu ("mai xuất hành được không") hoặc sau
    "tốt/xấu/hoàng đạo" ("ngày tốt mai") - chỉ cho câu hỏi ngày tốt, không đổi bộ đọc
    giờ nhắc việc dùng chung _relative_day."""
    rel = _relative_day(t, today)
    if rel is None and re.search(r"^(?:hom )?mai\b|\b(?:tot|xau|dao) mai\b", t):
        rel = today + dt.timedelta(days=1), "Ngày mai"
    return rel


def relative_day(text: str, today: dt.date) -> dt.date | None:
    """Ngày của cụm thời gian tương đối ("mai", "ngày kia", "thứ 6 tuần sau"...)
    tính từ ``today`` - dùng chung cho hỏi thăm chủ động (proactive_service)."""
    found = _relative_day(_norm(text), today)
    return found[0] if found else None


def solar_dates(text: str, today: dt.date) -> list[dt.date]:
    """Các ngày dương dd/mm(/yyyy) trong câu (năm mặc định = năm của today)."""
    return _solar_dates(_norm(text), today)


def _solar_dates(t: str, today: dt.date) -> list[dt.date]:
    out = []
    for m in _SOLAR_DATE_RE.finditer(t):
        year = int(m.group("y") or today.year)
        if not _MIN_YEAR <= year <= _MAX_YEAR:
            continue
        try:
            out.append(dt.date(year, int(m.group("m")), int(m.group("d"))))
        except ValueError:
            continue
    return out


# Bộ đọc ngày dùng lại cho giờ nhắc việc (services/vn_time_parse.py): nhận chữ
# ĐÃ bỏ dấu/chuẩn hoá, relative_day_folded trả (ngày, nhãn). Khác relative_day/
# solar_dates công khai ở trên (tự chuẩn hoá, trả ngày) mà proactive_service dùng.
SOLAR_DATE_RE = _SOLAR_DATE_RE
relative_day_folded = _relative_day
solar_dates_folded = _solar_dates


def _gender(text: str) -> bool | None:
    low = text.lower()
    if re.search(r"\b(nữ|nu|gái|gai|con gái)\b", low):
        return False
    # "nam" có dấu (năm) đã bị loại; "nam" không dấu mà đứng trước số năm là "năm".
    if re.search(r"\b(nam|trai|con trai)\b(?!\s*\d{4})", low) and not re.search(
        r"\bnam\s+(nay|sau|toi|truoc)\b", low
    ):
        return True
    return None


def _birth_lunar_year(t: str) -> tuple[int, str] | None:
    """(năm âm lịch sinh, ghi chú) từ ngày sinh đầy đủ hoặc năm sinh."""
    full = _FULL_DATE_RE.search(t)
    if full:
        try:
            born = dt.date(int(full.group(3)), int(full.group(2)), int(full.group(1)))
        except ValueError:
            return None
        ly = can_chi.lunar_year_of(born)
        note = f" (sinh {born:%d/%m/%Y} dương, thuộc năm âm {ly})" if ly != born.year else ""
        return ly, note
    years = [int(y) for y in _YEAR_RE.findall(t)]
    if not years:
        return None
    return min(years), " (theo năm âm lịch; sinh trước Tết thì tính năm trước)"


def _keyword(text: str, t: str, accented: str, folded: str) -> bool:
    """Từ khoá so trên chữ CÒN DẤU nếu người dùng gõ có dấu (bỏ dấu thì "cưới"
    trùng "cuối", "hạn" trùng "hàn", "hợp" trùng "họp"); gõ không dấu mới
    dùng bản bỏ dấu."""
    low = text.lower()
    if fold(low) != low:
        return bool(re.search(accented, low))
    return bool(re.search(folded, t))


def _han_answer(text: str, t: str, today: dt.date) -> str | None:
    if not _keyword(
        text,
        t,
        r"\b(?:sao hạn|hạn|tam tai|kim lâu|hoang ốc|cưới|làm nhà|xây nhà)\b",
        r"\b(?:sao han|han|tam tai|kim lau|hoang oc|cuoi|lam nha|xay nha)\b",
    ):
        return None
    birth = _birth_lunar_year(t)
    if birth is None:
        return None
    birth_year, note = birth
    if birth_year >= can_chi.lunar_year_of(today):
        return None  # "cưới ngày 20/12/2026": ngày tổ chức, không phải năm sinh
    years = [int(y) for y in _YEAR_RE.findall(t)]
    full = _FULL_DATE_RE.search(t)
    if full:
        years = [y for y in years if y != int(full.group(3))]
    else:
        years = [y for y in years if y != birth_year]
    target = max(years) if years else can_chi.lunar_year_of(today)
    return can_chi.describe_han(birth_year, target, _gender(text)) + (
        f"\n({note.strip(' ()')})" if note else ""
    )


def _hop_answer(text: str, t: str) -> str | None:
    if not _keyword(text, t, r"\b(?:hợp|xung|kỵ nhau|khắc)\b", r"\b(?:hop|xung|ky nhau|khac)\b"):
        return None
    years = [int(y) for y in _YEAR_RE.findall(t)]
    if len(years) >= 2:
        return can_chi.compare_years(years[0], years[1]) + (
            "\n(Theo năm âm lịch; ai sinh trước Tết thì tính năm trước.)"
        )
    if len(years) == 1:
        return can_chi.describe_chi_relations(can_chi.year_chi(years[0]))
    m = re.search(r"\btuoi (ty|suu|dan|mao|thin|ngo|mui|than|dau|tuat|hoi)\b", t)
    if m:
        name = m.group(1)
        if name == "ty":
            low = text.lower()
            if re.search(r"\btuổi (?:tý|tí)\b", low):
                return can_chi.describe_chi_relations(0)
            if re.search(r"\btuổi (?:tỵ|tị)\b", low):
                return can_chi.describe_chi_relations(5)
            return "Tuổi Tý hay tuổi Tỵ? Gõ có dấu (/lich tuổi Tý hợp tuổi nào) hoặc theo năm sinh."
        return can_chi.describe_chi_relations(_CHI_FOLDED.index(name))
    return None


def _tuoi_answer(t: str, today: dt.date) -> str | None:
    m = re.search(
        r"\bnam (nay|sau|toi|truoc|\d{4}) (?:la )?(?:nam )?(?:con gi|tuoi gi|menh gi|can chi gi)", t
    )
    if m:
        year = _year_modifier(t, today)
        return can_chi.describe_year(year) if year and _MIN_YEAR <= year <= _MAX_YEAR else None
    if not re.search(r"\b(tuoi|menh|con gi|con giap|can chi)\b", t):
        return None
    birth = _birth_lunar_year(t)
    if birth is None:
        return None
    year, note = birth
    this_year = can_chi.lunar_year_of(today)
    if re.search(r"\b(?:bao nhieu|may) tuoi\b", t):
        # Hỏi tuổi: nói tuổi trước. "năm 2026 tôi bao nhiêu tuổi" không có năm sinh.
        years = [int(y) for y in _YEAR_RE.findall(t)]
        target = max(years) if len(years) > 1 else this_year
        if year >= target:
            return None
        when = f"Năm nay ({target})" if target == this_year else f"Năm {target}"
        return (
            f"{when} người sinh năm {year} là {can_chi.tuoi_am(year, target)} tuổi âm.\n"
            + can_chi.describe_year(year)
            + note
        )
    profile = can_chi.year_profile(year)
    return (
        can_chi.describe_year(year)
        + note
        + "\n"
        + can_chi.describe_chi_relations(profile["chi"])
        + f"\nNăm nay ({this_year}) {can_chi.tuoi_am(year, this_year)} tuổi âm."
    )


def month_days(year: int, month: int) -> list[dt.date]:
    first = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return [first + dt.timedelta(days=i) for i in range((nxt - first).days)]


def _is_good_day(day: dt.date, trucs: tuple[str, ...] | None = None) -> bool:
    """Hoàng đạo, không Tam nương/Nguyệt kỵ, và (khi xem theo việc) trực hợp việc."""
    ld = lunar.solar_to_lunar(day.day, day.month, day.year)[0]
    return (
        can_chi.day_god(day)[1]
        and ld not in can_chi.TAM_NUONG | can_chi.NGUYET_KY
        and (trucs is None or can_chi.day_truc(day) in trucs)
    )


def _good_day_line(day: dt.date) -> str:
    ld = lunar.solar_to_lunar(day.day, day.month, day.year)[0]
    god = can_chi.day_god(day)[0]
    return (
        f"• {lunar.WEEKDAYS[day.weekday()]} {day:%d/%m} (âm {ld}) - {god}, "
        f"trực {can_chi.day_truc(day)}"
    )


def _day_head(day: dt.date, label: str) -> str:
    """ "Thứ Bảy 31/10/2026" hoặc "Ngày mai (Thứ Tư 28/10/2026)" - không lặp tên thứ."""
    when = f"{lunar.WEEKDAYS[day.weekday()]} {day:%d/%m/%Y}"
    return when if label == lunar.WEEKDAYS[day.weekday()] else f"{label} ({when})"


def _day_range(t: str, today: dt.date) -> tuple[list[dt.date], str, bool] | None:
    """(các ngày còn xét, nhãn, là phần còn lại của tháng này?) cho câu có tháng; tháng
    này thì chỉ từ hôm nay. None = câu không nói tháng, hoặc tháng ngoài 1900-2100."""
    month = _MONTH_RE.search(t)
    if not month:
        return None
    year, mon = _resolve_month(month, today)
    if year is None:
        return None
    days = month_days(year, mon)
    if days[0] <= today <= days[-1]:
        label = f"từ hôm nay ({today:%d/%m}) đến hết tháng {mon}/{year}"
        return [d for d in days if d >= today], label, True
    return days, f"tháng {mon}/{year}" + (" (đã qua)" if days[-1] < today else ""), False


def _good_lines(days: list[dt.date], rest_of_month: bool, trucs=None) -> list[str]:
    """Dòng các ngày tốt; tháng này đã hết ngày tốt thì gợi ý luôn tháng sau."""
    good = [_good_day_line(day) for day in days if _is_good_day(day, trucs)]
    if good or not rest_of_month:
        return good or ["(Không có ngày nào hợp.)"]
    nxt = days[-1] + dt.timedelta(days=1)
    lines = [f"Tháng này không còn ngày hợp. Sang tháng {nxt.month}/{nxt.year}:"]
    good = [_good_day_line(d) for d in month_days(nxt.year, nxt.month) if _is_good_day(d, trucs)]
    return lines + (good or ["(Không có ngày nào hợp.)"])


def _good_days_answer(t: str, today: dt.date) -> str | None:
    if not re.search(r"\b(ngay tot|ngay xau|tot xau|hoang dao|hac dao|tam nuong|nguyet ky)\b", t):
        return None
    if re.search(r"\bgio hoang dao\b", t) and not re.search(r"\bngay (tot|hoang dao)\b", t):
        return None  # giờ hoàng đạo -> thông tin ngày (lunar_chat / describe_full)
    span = _day_range(t, today)
    if span:
        days, label, rest_of_month = span
        bad = [
            f"{day:%d/%m}"
            for day in days
            if lunar.solar_to_lunar(day.day, day.month, day.year)[0]
            in can_chi.TAM_NUONG | can_chi.NGUYET_KY
        ]
        lines = [f"📅 Ngày hoàng đạo {label} (đã bỏ Tam nương, Nguyệt kỵ):"]
        lines += _good_lines(days, rest_of_month)
        if bad:
            lines.append(f"Ngày Tam nương/Nguyệt kỵ nên tránh: {', '.join(bad)}.")
        lines.append(_PURPOSE_HINT)
        return "\n".join(lines)
    weekend = _weekend_days(t, today)
    if weekend:
        picked = weekend
    else:
        rel = _asked_day(t, today)
        dates = _solar_dates(t, today)
        picked = [
            rel
            if rel
            else ((dates[0], lunar.WEEKDAYS[dates[0].weekday()]) if dates else (today, "Hôm nay"))
        ]
    return "\n".join(
        f"{_day_head(day, label)}: {can_chi.day_quality(day)}." for day, label in picked
    )


def _find_purpose(text: str, t: str):
    for purpose in _PURPOSES:
        if _keyword(text, t, rf"\b(?:{purpose[2]})\b", rf"\b(?:{purpose[3]})\b"):
            return purpose
    return None


def _purpose_verdict(day: dt.date, label: str, name: str, trucs: tuple[str, ...]) -> str:
    """Một ngày có hợp việc không, kèm lý do."""
    god, good = can_chi.day_god(day)
    ld, lm = lunar.solar_to_lunar(day.day, day.month, day.year)[:2]
    truc = can_chi.day_truc(day)
    reasons = []
    if not good:
        reasons.append(f"ngày hắc đạo ({god})")
    if ld in can_chi.TAM_NUONG:
        reasons.append("phạm Tam nương")
    if ld in can_chi.NGUYET_KY:
        reasons.append("phạm Nguyệt kỵ")
    if truc not in trucs:
        reasons.append(f"trực {truc} không hợp {name}")
    head = f"{_day_head(day, label)}, âm {ld}/{lm}"
    if not reasons:
        return f"✅ {head}: hợp {name} - ngày hoàng đạo ({god}), trực {truc}."
    return f"⚠️ {head}: chưa hợp {name} - {', '.join(reasons)}."


def _purpose_answer(text: str, t: str, today: dt.date) -> str | None:
    """Xem ngày theo việc: "xem ngày cưới tháng 12", "ngày khai trương", "20/12 động
    thổ được không". Không áp cho câu đếm ngày ("còn mấy ngày tới đám cưới")."""
    purpose = _find_purpose(text, t)
    if purpose is None or not re.search(r"\b(?:ngay|xem|chon|coi|nen|tot|hop|duoc)\b", t):
        return None
    if re.search(r"\b(?:con|bao) (?:bao nhieu|may) ngay\b", t):
        return None
    name, trucs = purpose[0], purpose[1]
    rule = (
        f"(Cách chọn: ngày hoàng đạo, không phạm Tam nương/Nguyệt kỵ, trực "
        f"{'/'.join(trucs)} theo Thập nhị trực. Việc lớn nên xem thêm tuổi người làm.)"
    )
    picked = _weekend_days(t, today)
    if not picked and not re.search(r"\bthang (?:nay|sau|toi|truoc)\b", t):
        rel = _asked_day(t, today)
        dates = _solar_dates(t, today)
        picked = [rel] if rel else [(d, lunar.WEEKDAYS[d.weekday()]) for d in dates[:1]]
    if picked:
        lines = [_purpose_verdict(day, label, name, trucs) for day, label in picked]
        if not any(_is_good_day(day, trucs) for day, _ in picked):
            after = picked[-1][0] + dt.timedelta(days=1)
            nxt = next(
                (after + dt.timedelta(days=i) for i in range(60)
                 if _is_good_day(after + dt.timedelta(days=i), trucs)),
                None,
            )  # fmt: skip
            if nxt:
                lines.append(f"Ngày hợp gần nhất sau đó: {_good_day_line(nxt)[2:]}.")
        return "\n".join([*lines, rule])
    span = _day_range(t, today) or (
        [today + dt.timedelta(days=i) for i in range(30)],
        f"30 ngày tới ({today:%d/%m} - {today + dt.timedelta(days=29):%d/%m/%Y})",
        False,
    )
    days, label, rest_of_month = span
    return "\n".join(
        [f"📅 Ngày tốt cho {name}, {label}:", *_good_lines(days, rest_of_month, trucs), rule]
    )


def _invalid_date_message(t: str, today: dt.date) -> str | None:
    """Câu báo ngày dương không có thật: "29/2/2027", "32/12", "31/4", "5/13".
    Bỏ qua ngày âm ("âm 30/2", "ngày âm lịch 30/2", "30/2 âm", "15/6n") - phần âm lịch
    tự kiểm."""
    for m in _ANY_DATE_RE.finditer(t):
        if re.match(r"\s*(?:am|al|n|nhuan)\b", t[m.end() :]) or re.search(
            r"\b(?:am|al|am lich)\s*$", t[: m.start()]
        ):
            continue
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        if not 1 <= month <= 12:
            return f"Không có tháng {month}: tháng dương lịch chỉ từ 1 đến 12."
        if year is not None and not _MIN_YEAR <= year <= _MAX_YEAR:
            return f"Lịch chỉ tra được năm {_MIN_YEAR}-{_MAX_YEAR}."
        if year is None and (day, month) == (29, 2):
            continue  # 29/2 không ghi năm: có ở năm nhuận
        size = calendar.monthrange(year or 2000, month)[1]
        if day < 1:
            return f"Không có ngày {day}/{month}: ngày bắt đầu từ 1."
        if day > size:
            if month == 2 and day == 29:
                return f"Năm {year} không có ngày 29/2 (tháng 2/{year} chỉ có 28 ngày)."
            return f"Tháng {month} chỉ có {size} ngày, không có ngày {day}/{month}."
    return None


def _target_date(
    fragment: str, today: dt.date, roll_forward: bool = True
) -> tuple[dt.date, str] | None:
    """Ngày trong 1 đoạn câu. roll_forward: ngày dương không ghi năm mà đã qua
    thì lấy năm sau ("còn mấy ngày tới 1/1"); tắt khi đếm "từ ... đến ..."."""
    holiday = _find_holiday(fragment)
    if holiday:
        day = holiday_entry_date(holiday, today, _holiday_year(fragment, today))
        return (day, holiday[0]) if day else None
    if re.search(r"\btet\b", fragment):
        tet = lunar.next_tet(today)
        return tet, "Tết Nguyên đán"
    rel = _relative_day(fragment, today)
    if rel:
        return rel
    m = _SOLAR_DATE_RE.search(fragment)
    if m:
        year = int(m.group("y")) if m.group("y") else None
        if year is None and not roll_forward:
            year = today.year
        day = _next_solar(int(m.group("d")), int(m.group("m")), today, year)
        if day:
            names = holidays_on(day)
            return day, names[0] if names else f"ngày {day:%d/%m/%Y}"
    return None


def _count_answer(t: str, today: dt.date) -> str | None:
    m = re.search(
        r"\btu (.+?) (?:den|toi) (.+?)(?: (?:la |co |duoc |het )?(?:bao nhieu|may) ngay.*)?$", t
    )
    if m and re.search(r"\d", m.group(1) + m.group(2)):
        a = _target_date(m.group(1), today, roll_forward=False)
        b = _target_date(m.group(2), today, roll_forward=False)
        if a and b and b[0] < a[0]:
            # "từ hôm nay đến 30/4": ngày cuối không ghi năm mà trước ngày đầu -> lần sắp tới.
            b = _target_date(m.group(2), today) or b
        if a and b:
            days = (b[0] - a[0]).days
            return f"Từ {a[0]:%d/%m/%Y} đến {b[0]:%d/%m/%Y} là {abs(days)} ngày."
    # "còn" tuỳ chọn: "bao nhiêu ngày nữa đến 30/4", "mấy ngày nữa là tết" cũng là đếm ngày
    # (trước đây chỉ nhận "còn ..." nên rơi sang tra một ngày, lấy 30/4 năm nay đã qua).
    m = re.search(
        r"\b(?:con )?(?:bao nhieu|may) ngay(?: nua)?(?: (?:la|thi))? (?:den|toi|la|thi) (.+)$", t
    )
    m = m or re.search(r"\b(?:den|toi) (.+?) con (?:bao nhieu|may) ngay", t)
    if not m:
        return None
    target = _target_date(m.group(1), today)
    if not target:
        return None
    day, label = target
    days = (day - today).days
    when = f"{lunar.WEEKDAYS[day.weekday()]} {day:%d/%m/%Y}"
    if days > 0:
        return f"Còn {days} ngày nữa là tới {label} ({when})."
    if days == 0:
        return f"Hôm nay là {label} ({when})!"
    return f"{label} ({when}) đã qua {-days} ngày."


def _holiday_answer(t: str, today: dt.date) -> str | None:
    if re.search(r"\b(cac ngay le|ngay le|le sap toi|le tet|cac le)\b", t):
        upcoming = []
        for h in HOLIDAYS:
            day = holiday_entry_date(h, today)
            if day is not None:
                upcoming.append((day, h[0], h[1]))
        upcoming.sort()
        lines = [
            f"• {name} ({'âm' if is_lunar else 'dương'}): {lunar.WEEKDAYS[day.weekday()]} "
            f"{day:%d/%m/%Y} - còn {(day - today).days} ngày"
            for day, name, is_lunar in upcoming[:12]
        ]
        return "🎉 Các ngày lễ sắp tới:\n" + "\n".join(lines)
    holiday = _find_holiday(t)
    if not holiday:
        return None
    day = holiday_entry_date(holiday, today, _holiday_year(t, today))
    if day is None:
        return None
    info = lunar.date_info(day)
    when = f"{info['weekday']} {info['date_vn']} dương lịch, tức {info['lunar']['text']} âm lịch"
    days = (day - today).days
    tail = (
        f" - còn {days} ngày"
        if days > 0
        else " - là hôm nay"
        if days == 0
        else f" - đã qua {-days} ngày"
    )
    return f"🎉 {holiday[0]}: {when}{tail}."


def _resolve_month(m: re.Match, today: dt.date) -> tuple[int | None, int]:
    rel = m.group("rel")
    if rel:
        shift = {"nay": 0, "sau": 1, "toi": 1, "truoc": -1}[rel]
        idx = today.year * 12 + today.month - 1 + shift
        return idx // 12, idx % 12 + 1
    mon = int(m.group("m"))
    if not 1 <= mon <= 12:
        return None, mon
    year = int(m.group("y") or m.group("y2") or today.year)
    return (year if _MIN_YEAR <= year <= _MAX_YEAR else None), mon


def _month_table_answer(t: str, today: dt.date) -> str | None:
    m = _MONTH_RE.search(t)
    if not m or re.search(r"\b(ram|mung|mong|ngay \d)", t):
        return None
    lunar_month = re.search(r"\bthang (\d{1,2})( nhuan)? (?:am|al)\b", t)
    if lunar_month:
        lm, leap = int(lunar_month.group(1)), bool(lunar_month.group(2))
        # Năm có thể đứng bất cứ đâu: "tháng 2 âm năm 2027", "năm sau tháng 2 âm".
        ly = _year_modifier(t, today) or can_chi.lunar_year_of(today)
        start = lunar_to_solar_exact(1, lm, ly, leap) if 1 <= lm <= 12 else None
        if start is None:
            return None
        lines = [
            f"📅 Tháng {lm}{' nhuận' if leap else ''} âm lịch năm {lunar.year_can_chi(ly)} ({ly}):"
        ]
        day = start
        while True:
            ld, mm, _, is_leap = lunar.solar_to_lunar(day.day, day.month, day.year)
            if mm != lm or is_leap != leap or (day - start).days > 31:
                break
            mark = " ⭐ mùng 1" if ld == 1 else " 🌕 rằm" if ld == 15 else ""
            lines.append(f"Âm {ld:>2} - {lunar.WEEKDAYS[day.weekday()]} {day:%d/%m}{mark}")
            day += dt.timedelta(days=1)
        return "\n".join(lines)
    year, mon = _resolve_month(m, today)
    if year is None:
        return None
    lines = [f"📅 Tháng {mon}/{year} (dương - âm):"]
    for day in month_days(year, mon):
        ld, lm, _, leap = lunar.solar_to_lunar(day.day, day.month, day.year)
        lunar_txt = f"{ld}/{lm}{'n' if leap else ''}" if ld == 1 or day.day == 1 else f"{ld}"
        mark = " ⭐" if ld == 1 else " 🌕" if ld == 15 else ""
        good = "" if not can_chi.day_god(day)[1] else " ·hđ"
        wd = ("T2", "T3", "T4", "T5", "T6", "T7", "CN")[day.weekday()]
        lines.append(f"{day.day:>2} {wd} | âm {lunar_txt}{mark}{good}")
    lines.append("⭐ mùng 1, 🌕 rằm, ·hđ ngày hoàng đạo")
    return "\n".join(lines)


_RAM_MUNG1_RE = re.compile(
    r"(?:(?:ngay )?(?:ram|mung 1|mong 1)(?: (?:toi|sap toi|nay|ke tiep))?(?: va )?)+"
)


def _ram_mung1_answer(t: str, today: dt.date) -> str | None:
    if not _RAM_MUNG1_RE.fullmatch(t):
        return None
    ld, lm, ly, leap = lunar.solar_to_lunar(today.day, today.month, today.year)
    out = []
    for want, name in ((1, "Mùng 1"), (15, "Rằm")):
        if want == 15 and "ram" not in t and "mung" in t:
            continue
        if want == 1 and "mung" not in t and "mong" not in t and "ram" in t:
            continue
        day = today
        for _ in range(32):
            if lunar.solar_to_lunar(day.day, day.month, day.year)[0] == want and day >= today:
                break
            day += dt.timedelta(days=1)
        info = lunar.date_info(day)
        out.append(
            f"{name} sắp tới: {info['weekday']} {info['date_vn']} ({info['lunar']['text']} âm lịch)"
            + (" - hôm nay" if day == today else f" - còn {(day - today).days} ngày")
        )
    return "\n".join(out) or None


def _lunar_prefix_answer(t: str, today: dt.date) -> str | None:
    m = _LUNAR_PREFIX_RE.match(t)
    if not m:
        return None
    ld, lm = int(m.group("d")), int(m.group("m"))
    leap = bool(m.group("leap") or m.group("leap2"))
    day, year, error = lunar.find_lunar_date(
        ld, lm, int(m.group("y")) if m.group("y") else None, leap, today
    )
    if day is None:
        return error
    return describe_full(day, f"{lunar.lunar_text(ld, lm, year, leap)} âm lịch rơi vào:")


def understand(query: str, today: dt.date | None = None) -> str | None:
    """Câu trả lời tra lịch; None khi không hiểu câu hỏi. Không bao giờ gọi AI."""
    today = today or _today()
    text = (query or "").strip()
    t = _norm(text)
    if not t or t in ("hom nay", "nay", "am lich", "lich am", "lich"):
        return describe_full(today, "Hôm nay")
    invalid = _invalid_date_message(t, today)
    if invalid:
        return invalid
    for handler in (
        lambda: _lunar_prefix_answer(t, today),
        lambda: _han_answer(text, t, today),
        lambda: _hop_answer(text, t),
        lambda: _tuoi_answer(t, today),
        lambda: _purpose_answer(text, t, today),
        lambda: _good_days_answer(t, today),
        lambda: _count_answer(t, today),
        lambda: _holiday_answer(t, today),
        lambda: _ram_mung1_answer(t, today),
        lambda: _month_table_answer(t, today),
        lambda: lunar_chat.answer(text, today),
    ):
        reply = handler()
        if reply:
            return reply
    weekend = _weekend_days(t, today)
    if weekend:
        return "\n\n".join(describe_full(day, label) for day, label in weekend)
    rel = _asked_day(t, today)
    if rel:
        return describe_full(rel[0], rel[1])
    dates = _solar_dates(t, today)
    if dates:
        return describe_full(dates[0], f"Ngày {dates[0]:%d/%m/%Y}")
    year = _YEAR_RE.fullmatch(t) or re.fullmatch(r"nam (\d{4})", t)
    if year:
        return can_chi.describe_year(int(year.group(1)))
    return None


# Câu chat thường (không phải lệnh /lich) chỉ đưa vào bộ tra lịch khi RÕ là hỏi lịch:
# từ hỏi đi kèm 1 mốc lịch. "nghỉ tết mấy ngày", "tết này anh về quê", "hẹn 20/11 đi
# ăn" vẫn để chat trả lời.
_CHAT_DAY_ASK_RE = re.compile(r"\b(?:thu may|ngay (?:may|bao nhieu)|ngay nao(?! cung))\b")
_CHAT_WHEN_ASK_RE = re.compile(r"\b(?:bao gio|khi nao|hom nao|luc nao)\b")
_CHAT_COUNT_ASK_RE = re.compile(
    r"\b(?:bao nhieu|may) ngay (?:nua )?(?:(?:la|thi) )?(?:den|toi|la|thi)\b|"
    r"\bcon (?:bao nhieu|may) ngay\b"
)
# "năm nay con gì cũng đắt" không phải hỏi con giáp; "mạng gì" xét trên chữ có dấu
# ("năm nay mang gì đi Đà Lạt").
_CHAT_YEAR_ASK_RE = re.compile(
    r"\b(?:con gi(?! cung)|tuoi gi|menh gi|menh nao|can chi gi|con giap gi|(?:bao nhieu|may) tuoi)\b"
)
_CHAT_HOP_ASK_RE = re.compile(r"\b(?:nao|khong|ko|gi)\b")
_CHAT_PURPOSE_ASK_RE = re.compile(r"\b(?:xem|coi|chon) ngay\b|\bngay nao tot\b")
_CHAT_TABLE_RE = re.compile(r"(?:xem |coi )?lich (?:thang|am|duong|van nien)\b.*")
_CHI_RE = re.compile(r"\btuoi (?:ty|suu|dan|mao|thin|ngo|mui|than|dau|tuat|hoi)\b")


def _chat_asks_calendar(text: str, t: str, today: dt.date) -> bool:
    holiday = bool(_find_holiday(t) or re.search(r"\btet\b", t))
    day = holiday or bool(_asked_day(t, today) or _ANY_DATE_RE.search(t))
    year = bool(_YEAR_RE.search(t) or re.search(r"\bnam (?:nay|sau|toi|truoc)\b", t))
    hop = _keyword(text, t, r"\b(?:hợp|xung)\b", r"\b(?:hop|xung)\b")
    year_ask = _CHAT_YEAR_ASK_RE.search(t) or _keyword(text, t, r"\bmạng gì\b", r"\bmang gi\b")
    return bool(
        (day and (_CHAT_DAY_ASK_RE.search(t) or _CHAT_COUNT_ASK_RE.search(t)))
        or (holiday and _CHAT_WHEN_ASK_RE.search(t))
        or (year and year_ask)
        or (hop and _CHAT_HOP_ASK_RE.search(t) and (year or _CHI_RE.search(t)))
        or (_CHAT_PURPOSE_ASK_RE.search(t) and _find_purpose(text, t))
        or _CHAT_TABLE_RE.fullmatch(t)
        or _LUNAR_PREFIX_RE.match(t)
        or _RAM_MUNG1_RE.fullmatch(t)
    )


def chat_answer(text: str, today: dt.date | None = None) -> str | None:
    """Tra lịch cho câu chat tự nhiên ("20/11 là thứ mấy", "noel còn mấy ngày", "1990
    mệnh gì"); None khi câu không rõ là hỏi lịch (để chat AI trả lời)."""
    today = today or _today()
    t = _norm(text)
    # "nghỉ tết mấy ngày", "bao giờ được nghỉ lễ": lịch nghỉ do nhà nước công bố, không tính được.
    if not t or len(t) > 100 or re.search(r"\bnghi\b", t):
        return None
    return understand(text, today) if _chat_asks_calendar(text, t, today) else None


def answer(query: str, today: dt.date | None = None) -> str:
    """Câu trả lời cho lệnh /lich <query>; không hiểu thì gợi ý cú pháp lệnh."""
    if _norm(query or "") in ("help", "huong dan", "?"):
        return HELP
    reply = understand(query, today)
    if reply is None:
        return "Em chưa hiểu câu này nên chưa tra được (lệnh /lich không dùng AI).\n\n" + HELP
    return reply
