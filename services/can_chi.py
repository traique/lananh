"""Can chi, nạp âm, tuổi hợp/xung, sao hạn, Tam tai, Kim lâu, Hoang ốc, ngày
hoàng đạo/hắc đạo và Thập nhị trực - toàn bộ là tra bảng cố định, không gọi AI.

Dùng chung cho /lich (services/calendar_query.py) và /tuvi (services/tuvi.py).
Chi đánh số 0=Tý ... 11=Hợi; can 0=Giáp ... 9=Quý.
"""

from __future__ import annotations

import datetime as dt

from services import lunar

CAN = lunar.CAN
CHI = lunar.CHI
CON_GIAP = ["Chuột", "Trâu", "Hổ", "Mèo", "Rồng", "Rắn", "Ngựa", "Dê", "Khỉ", "Gà", "Chó", "Lợn"]

# 30 cặp nạp âm theo thứ tự lục thập hoa giáp (Giáp Tý-Ất Sửu, Bính Dần-Đinh Mão...).
NAP_AM = [
    ("Hải Trung Kim", "Kim"),
    ("Lô Trung Hỏa", "Hỏa"),
    ("Đại Lâm Mộc", "Mộc"),
    ("Lộ Bàng Thổ", "Thổ"),
    ("Kiếm Phong Kim", "Kim"),
    ("Sơn Đầu Hỏa", "Hỏa"),
    ("Giản Hạ Thủy", "Thủy"),
    ("Thành Đầu Thổ", "Thổ"),
    ("Bạch Lạp Kim", "Kim"),
    ("Dương Liễu Mộc", "Mộc"),
    ("Tuyền Trung Thủy", "Thủy"),
    ("Ốc Thượng Thổ", "Thổ"),
    ("Tích Lịch Hỏa", "Hỏa"),
    ("Tùng Bách Mộc", "Mộc"),
    ("Trường Lưu Thủy", "Thủy"),
    ("Sa Trung Kim", "Kim"),
    ("Sơn Hạ Hỏa", "Hỏa"),
    ("Bình Địa Mộc", "Mộc"),
    ("Bích Thượng Thổ", "Thổ"),
    ("Kim Bạch Kim", "Kim"),
    ("Phú Đăng Hỏa", "Hỏa"),
    ("Thiên Hà Thủy", "Thủy"),
    ("Đại Trạch Thổ", "Thổ"),
    ("Thoa Xuyến Kim", "Kim"),
    ("Tang Đố Mộc", "Mộc"),
    ("Đại Khê Thủy", "Thủy"),
    ("Sa Trung Thổ", "Thổ"),
    ("Thiên Thượng Hỏa", "Hỏa"),
    ("Thạch Lựu Mộc", "Mộc"),
    ("Đại Hải Thủy", "Thủy"),
]

# Ngũ hành tương sinh / tương khắc.
_SINH = {"Mộc": "Hỏa", "Hỏa": "Thổ", "Thổ": "Kim", "Kim": "Thủy", "Thủy": "Mộc"}
_KHAC = {"Mộc": "Thổ", "Thổ": "Thủy", "Thủy": "Hỏa", "Hỏa": "Kim", "Kim": "Mộc"}

# Nhóm tam hợp (theo chi) và lục hợp, lục hại.
_TAM_HOP = [(8, 0, 4), (2, 6, 10), (5, 9, 1), (11, 3, 7)]  # Thân Tý Thìn, Dần Ngọ Tuất...
_LUC_HOP = {0: 1, 1: 0, 2: 11, 11: 2, 3: 10, 10: 3, 4: 9, 9: 4, 5: 8, 8: 5, 6: 7, 7: 6}
_LUC_HAI = {0: 7, 7: 0, 1: 6, 6: 1, 2: 5, 5: 2, 3: 4, 4: 3, 8: 11, 11: 8, 9: 10, 10: 9}
# Tứ hành xung: Tý Ngọ Mão Dậu / Dần Thân Tỵ Hợi / Thìn Tuất Sửu Mùi.
_TU_HANH_XUNG = [(0, 6, 3, 9), (2, 8, 5, 11), (4, 10, 1, 7)]

# Sao hạn cửu diệu theo tuổi âm (tuổi mụ) chia 9 dư 1..8, 0.
_SAO_NAM = ["Mộc Đức", "La Hầu", "Thổ Tú", "Thủy Diệu", "Thái Bạch", "Thái Dương", "Vân Hớn",
            "Kế Đô", "Thái Âm"]  # fmt: skip
_SAO_NU = ["Thủy Diệu", "Kế Đô", "Vân Hớn", "Mộc Đức", "Thái Âm", "Thổ Tú", "La Hầu",
           "Thái Dương", "Thái Bạch"]  # fmt: skip
_SAO_XAU = {"La Hầu", "Kế Đô", "Thái Bạch"}
_SAO_TOT = {"Thái Dương", "Thái Âm", "Mộc Đức"}

# Tam tai: nhóm tam hợp năm sinh -> 3 năm (chi) gặp tam tai.
_TAM_TAI = {
    (8, 0, 4): (2, 3, 4),  # Thân Tý Thìn -> Dần Mão Thìn
    (2, 6, 10): (8, 9, 10),  # Dần Ngọ Tuất -> Thân Dậu Tuất
    (11, 3, 7): (5, 6, 7),  # Hợi Mão Mùi -> Tỵ Ngọ Mùi
    (5, 9, 1): (11, 0, 1),  # Tỵ Dậu Sửu -> Hợi Tý Sửu
}
_KIM_LAU = {1: "Kim lâu Thân (hại bản thân)", 3: "Kim lâu Thê (hại vợ/chồng)",
            6: "Kim lâu Tử (hại con)", 8: "Kim lâu Lục súc (hại vật nuôi, làm ăn)"}  # fmt: skip
_HOANG_OC = ["Nhất Cát", "Nhì Nghi", "Tam Địa Sát", "Tứ Tấn Tài", "Ngũ Thọ Tử", "Lục Hoang Ốc"]
_HOANG_OC_XAU = {"Tam Địa Sát", "Ngũ Thọ Tử", "Lục Hoang Ốc"}

# 12 thần trực ngày: vị trí Thanh Long theo tháng âm (1&7 ở Tý, 2&8 ở Dần...).
_THAN_NGAY = ["Thanh Long", "Minh Đường", "Thiên Hình", "Chu Tước", "Kim Quỹ", "Kim Đường",
              "Bạch Hổ", "Ngọc Đường", "Thiên Lao", "Huyền Vũ", "Tư Mệnh", "Câu Trận"]  # fmt: skip
_THAN_HOANG_DAO = {"Thanh Long", "Minh Đường", "Kim Quỹ", "Kim Đường", "Ngọc Đường", "Tư Mệnh"}
TAM_NUONG = {3, 7, 13, 18, 22, 27}
NGUYET_KY = {5, 14, 23}
# Thập nhị trực (Kiến trừ): ngày có chi trùng chi của tháng TIẾT KHÍ là trực Kiến (tháng
# Giêng kiến Dần, đổi tháng ở Lập xuân, Kinh trập, Thanh minh...), rồi lần lượt theo chi
# ngày - nên ngày giao tiết lặp lại trực của hôm trước.
TRUC = ["Kiến", "Trừ", "Mãn", "Bình", "Định", "Chấp", "Phá", "Nguy", "Thành", "Thu", "Khai", "Bế"]


def jiazi_index(can: int, chi: int) -> int:
    """Vị trí 0..59 trong lục thập hoa giáp."""
    return (6 * can - 5 * chi) % 60


def year_can(lunar_year: int) -> int:
    return (lunar_year + 6) % 10


def year_chi(lunar_year: int) -> int:
    return (lunar_year + 8) % 12


def nap_am(can: int, chi: int) -> tuple[str, str]:
    """(tên nạp âm, hành) của 1 cặp can chi."""
    return NAP_AM[jiazi_index(can, chi) // 2]


def ngu_hanh_kind(a: str, b: str) -> str:
    """Quan hệ ngũ hành của a đối với b dạng mã: "same", "sinh" (a sinh b), "duoc_sinh"
    (b sinh a), "khac" (a khắc b), "bi_khac" (b khắc a)."""
    if a == b:
        return "same"
    if _SINH[a] == b:
        return "sinh"
    if _SINH[b] == a:
        return "duoc_sinh"
    return "khac" if _KHAC[a] == b else "bi_khac"


def ngu_hanh_relation(a: str, b: str) -> str:
    """Quan hệ ngũ hành của a đối với b."""
    if a == b:
        return "bình hòa (cùng hành)"
    if _SINH[a] == b:
        return f"{a} sinh {b} (tương sinh)"
    if _SINH[b] == a:
        return f"{b} sinh {a} (tương sinh)"
    if _KHAC[a] == b:
        return f"{a} khắc {b} (tương khắc)"
    return f"{b} khắc {a} (tương khắc)"


def lunar_year_of(date: dt.date) -> int:
    """Năm âm lịch của 1 ngày dương (sinh trước Tết thuộc năm âm trước)."""
    return lunar.solar_to_lunar(date.day, date.month, date.year)[2]


def year_profile(lunar_year: int) -> dict:
    can, chi = year_can(lunar_year), year_chi(lunar_year)
    name, element = nap_am(can, chi)
    return {
        "year": lunar_year,
        "can_chi": f"{CAN[can]} {CHI[chi]}",
        "chi": chi,
        "con_giap": CON_GIAP[chi],
        "nap_am": name,
        "menh": element,
        "am_duong": "Dương" if can % 2 == 0 else "Âm",
    }


def describe_year(lunar_year: int) -> str:
    p = year_profile(lunar_year)
    return (
        f"Năm {p['year']} âm lịch là năm {p['can_chi']} (con {p['con_giap']}), "
        f"mệnh {p['menh']} - {p['nap_am']}."
    )


def chi_relations(chi: int) -> dict[str, list[str]]:
    tam_hop = next(group for group in _TAM_HOP if chi in group)
    xung = next(group for group in _TU_HANH_XUNG if chi in group)
    return {
        "tam_hop": [CHI[c] for c in tam_hop if c != chi],
        "luc_hop": [CHI[_LUC_HOP[chi]]],
        "xung": [CHI[(chi + 6) % 12]],
        "tu_hanh_xung": [CHI[c] for c in xung if c != chi],
        "hai": [CHI[_LUC_HAI[chi]]],
    }


def describe_chi_relations(chi: int) -> str:
    r = chi_relations(chi)
    return (
        f"Tuổi {CHI[chi]}: tam hợp với {', '.join(r['tam_hop'])}; lục hợp với "
        f"{r['luc_hop'][0]}; xung (đối) với {r['xung'][0]}; cùng nhóm tứ hành xung "
        f"{', '.join(r['tu_hanh_xung'])}; lục hại với {r['hai'][0]}."
    )


def compare_years(year_a: int, year_b: int) -> str:
    """So hai năm sinh âm lịch theo chi (hợp/xung) và nạp âm (sinh/khắc)."""
    a, b = year_profile(year_a), year_profile(year_b)
    ca, cb = a["chi"], b["chi"]
    notes = []
    if any(ca in g and cb in g for g in _TAM_HOP) and ca != cb:
        notes.append("tam hợp")
    if _LUC_HOP[ca] == cb:
        notes.append("lục hợp")
    if (ca + 6) % 12 == cb:
        notes.append("lục xung (đối nhau)")
    elif any(ca in g and cb in g for g in _TU_HANH_XUNG) and ca != cb:
        notes.append("tứ hành xung")
    if _LUC_HAI[ca] == cb:
        notes.append("lục hại")
    if ca == cb:
        notes.append("cùng tuổi")
    chi_text = ", ".join(notes) if notes else "không hợp cũng không xung"
    return (
        f"{a['can_chi']} ({year_a}, mệnh {a['menh']} - {a['nap_am']}) và "
        f"{b['can_chi']} ({year_b}, mệnh {b['menh']} - {b['nap_am']}):\n"
        f"• Theo địa chi: {chi_text}.\n"
        f"• Theo mệnh: {ngu_hanh_relation(a['menh'], b['menh'])}."
    )


def tuoi_am(birth_lunar_year: int, target_lunar_year: int) -> int:
    """Tuổi mụ (tuổi âm) trong năm target."""
    return target_lunar_year - birth_lunar_year + 1


def sao_han(age: int, male: bool) -> str:
    table = _SAO_NAM if male else _SAO_NU
    star = table[age % 9]
    tag = " (sao xấu)" if star in _SAO_XAU else " (sao tốt)" if star in _SAO_TOT else ""
    return star + tag


def tam_tai_years(birth_chi: int) -> tuple[int, int, int]:
    group = next(g for g in _TAM_TAI if birth_chi in g)
    return _TAM_TAI[group]


def kim_lau(age: int) -> str | None:
    return _KIM_LAU.get(age % 9)


def hoang_oc(age: int) -> str:
    tens, units = divmod(age, 10)
    return _HOANG_OC[(tens + units - 1) % 6]


def describe_han(birth_lunar_year: int, target_lunar_year: int, male: bool | None) -> str:
    """Sao hạn, Tam tai, Kim lâu, Hoang ốc của 1 tuổi trong năm target."""
    age = tuoi_am(birth_lunar_year, target_lunar_year)
    if age < 1:
        return "Năm xem phải sau năm sinh."
    born = year_profile(birth_lunar_year)
    target = year_profile(target_lunar_year)
    lines = [
        f"Tuổi {born['can_chi']} ({birth_lunar_year}) năm {target['can_chi']} "
        f"({target_lunar_year}): {age} tuổi âm."
    ]
    if male is None:
        lines.append(
            f"• Sao hạn: nam gặp {sao_han(age, True)}, nữ gặp {sao_han(age, False)} "
            "(thêm nam/nữ để xem riêng)."
        )
    else:
        lines.append(f"• Sao hạn ({'nam' if male else 'nữ'}): {sao_han(age, male)}.")
    tai = tam_tai_years(born["chi"])
    if target["chi"] in tai:
        lines.append(f"• Tam tai: CÓ (tuổi {CHI[born['chi']]} gặp tam tai các năm "
                     f"{', '.join(CHI[c] for c in tai)}).")  # fmt: skip
    else:
        lines.append(f"• Tam tai: không (gặp vào các năm {', '.join(CHI[c] for c in tai)}).")
    kl = kim_lau(age)
    lines.append(f"• Kim lâu (cưới hỏi): {'CÓ - ' + kl if kl else 'không'}.")
    ho = hoang_oc(age)
    lines.append(
        f"• Hoang ốc (làm nhà): {ho}{' - nên tránh' if ho in _HOANG_OC_XAU else ' - tốt'}."
    )
    return "\n".join(lines)


def day_god(date: dt.date) -> tuple[str, bool]:
    """(thần trực ngày, là ngày hoàng đạo?) theo tháng âm và chi ngày."""
    _, lm, _, _ = lunar.solar_to_lunar(date.day, date.month, date.year)
    start = ((lm - 1) % 6) * 2  # tháng 1&7 -> Tý, 2&8 -> Dần, ...
    day_chi = (lunar._jd_from_date(date.day, date.month, date.year) + 1) % 12
    god = _THAN_NGAY[(day_chi - start) % 12]
    return god, god in _THAN_HOANG_DAO


def month_chi_by_term(date: dt.date) -> int:
    """Chi của tháng tiết khí chứa ``date``: Lập xuân..Kinh trập -> Dần, Kinh trập..Thanh
    minh -> Mão... Tiết đầu tháng là các tiết số lẻ trong lunar.TIET_KHI (Lập xuân = 21)."""
    term = lunar.TIET_KHI.index(lunar.solar_term(date.day, date.month, date.year))
    jie = term if term % 2 else term - 1
    return ((jie - 21) // 2 + 2) % 12


def day_truc(date: dt.date) -> str:
    """Trực của ngày trong Thập nhị trực (Kiến, Trừ, Mãn...)."""
    day_chi = (lunar._jd_from_date(date.day, date.month, date.year) + 1) % 12
    return TRUC[(day_chi - month_chi_by_term(date)) % 12]


def day_quality(date: dt.date) -> str:
    """Một dòng: hoàng đạo/hắc đạo + tam nương/nguyệt kỵ."""
    god, good = day_god(date)
    ld = lunar.solar_to_lunar(date.day, date.month, date.year)[0]
    parts = [f"ngày {'hoàng đạo' if good else 'hắc đạo'} ({god})"]
    if ld in TAM_NUONG:
        parts.append("ngày Tam nương")
    if ld in NGUYET_KY:
        parts.append("ngày Nguyệt kỵ")
    return ", ".join(parts)
