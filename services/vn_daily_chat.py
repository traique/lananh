"""Giá vàng, tỷ giá, xổ số trả lời bằng code (không AI) + grounding cho chat.

- ``maybe_handle(text, user_id)``: câu hỏi gọn ("giá vàng hôm nay", "tỷ giá
  đô", "100 đô bằng bao nhiêu tiền Việt", "xổ số miền Nam hôm nay", "dò vé
  123456") -> trả lời thẳng, không tốn lượt AI nào. Câu khai báo nơi ở ("anh ở
  Hà Nội") -> lưu thành phố thời tiết riêng. Không chắc hoặc nguồn lỗi -> None
  để đi tiếp đường cũ (cổ phiếu, chat AI).
- ``grounding_for(text)``: câu rộng hơn ("vàng tăng vậy có nên mua không") ->
  số liệu thật làm grounding và BỎ tra web + forced search của lượt đó (khỏi
  trả 2 lần và khỏi mâu thuẫn "hệ thống không có dữ liệu"). Nguồn lỗi -> giữ
  đường tra web cũ nhưng buộc ghi rõ số liệu web là "tham khảo".

Xổ số KHÔNG BAO GIỜ được trả từ trí nhớ model: kết quả và dò vé luôn tính
bằng code. Nguồn lỗi thì dò vé báo lỗi thẳng; hỏi kết quả thì đi tra web thật
(forced search) với ghi chú "tham khảo", cấm nêu số từ trí nhớ.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from core.text_normalize import fold
from services import reply_audience, weather_home
from services import vn_market_data as market

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _today() -> dt.date:
    return dt.datetime.now(_VN_TZ).date()


def _low(text: str) -> str:
    """Chữ thường giữ dấu; bỏ dấu câu trừ dấu chấm/phẩy nằm giữa 2 chữ số."""
    t = (text or "").lower()
    t = re.sub(r"[?!;:()\"'“”]+", " ", t)
    t = re.sub(r"(?<!\d)[.,]|[.,](?!\d)", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ─── Nhận diện câu hỏi ──────────────────────────────────────────────────────

# Bỏ dấu thì "vâng" (dạ), "vắng", "vang" (rượu vang) đều thành "vang", "nhắn"
# thành "nhẫn": "vàng + chữ mơ hồ" chỉ so trên chữ CÓ DẤU. Bản bỏ dấu chỉ giữ cụm
# không thể nhầm; người gõ không dấu cả câu được so riêng (_GOLD_PLAIN_RE).
_GOLD_RE = re.compile(r"\bgia vang\b|\bvang (?:sjc|mieng|9999|24k)\b|\bsjc\b")
_GOLD_ACCENTED_RE = re.compile(r"\bvàng (?:sjc|miếng|nhẫn|9999|24k|hôm nay|bây giờ|bao nhiêu)\b")
_GOLD_PLAIN_RE = re.compile(r"(?<!ruou )\bvang (?:nhan|hom nay|bay gio|bao nhieu)\b")
_WORLD_GOLD_RE = re.compile(r"\b(?:the gioi|quoc te|ounce|kitco|spot)\b")
_FX_EXPLICIT_RE = re.compile(r"\bt[yi] gia\b")
# Câu tiền số/chứng khoán Mỹ có "usd" không phải hỏi tỷ giá ngân hàng.
_NOT_FX_RE = re.compile(
    r"\b(?:bitcoin|btc|eth|ethereum|crypto|coin|tien so|tien ao|usdt|chung khoan my|co phieu)\b"
)

# Tên tiền tệ. Chữ ngắn dễ trùng ("đô" ~ độ/đó, "yên" ~ yên tâm, "tệ", "bạt")
# chỉ so trên chữ CÓ DẤU và chỉ trong ngữ cảnh tiền (số tiền, "giá", "tỷ giá").
_ACCENTED_ALIASES = {
    "USD": ("usd", "đô la mỹ", "đô la", "đôla", "đô mỹ", "dollar", "đô"),
    "EUR": ("eur", "euro", "ơ rô"),
    "JPY": ("jpy", "yên nhật", "yên", "yen"),
    "CNY": ("cny", "nhân dân tệ", "tệ", "rmb"),
    "KRW": ("krw", "won", "uôn"),
    "GBP": ("gbp", "bảng anh"),
    "AUD": ("aud", "đô úc"),
    "SGD": ("sgd", "đô sing", "đô singapore"),
    "THB": ("thb", "bạt thái", "bạt", "baht"),
    "CAD": ("cad", "đô canada"),
    "HKD": ("hkd", "đô hồng kông", "đô hongkong"),
    "CHF": ("chf", "franc thụy sĩ"),
    "MYR": ("myr", "ringgit"),
    "RUB": ("rub", "rúp"),
    "INR": ("inr", "rupee"),
}
# Bản không dấu chỉ giữ tên không thể nhầm (người gõ không dấu).
_FOLDED_ALIASES = {
    "USD": ("usd", "do la my", "do la", "dola", "do my", "dollar"),
    "EUR": ("eur", "euro"),
    "JPY": ("jpy", "yen nhat"),
    "CNY": ("cny", "nhan dan te", "rmb"),
    "KRW": ("krw",),
    "GBP": ("gbp", "bang anh"),
    "AUD": ("aud", "do uc"),
    "SGD": ("sgd", "do sing", "do singapore"),
    "THB": ("thb", "bat thai", "baht"),
    "CAD": ("cad", "do canada"),
    "HKD": ("hkd", "do hong kong", "do hongkong"),
    "CHF": ("chf",),
    "MYR": ("myr", "ringgit"),
    "RUB": ("rub",),
    "INR": ("inr", "rupee"),
}


def _alias_map(table: dict[str, tuple[str, ...]]) -> tuple[dict[str, str], str]:
    by_alias = {alias: code for code, aliases in table.items() for alias in aliases}
    pattern = "|".join(re.escape(a) for a in sorted(by_alias, key=len, reverse=True))
    return by_alias, pattern


# "đô" trong các từ này không phải đô la ("tin tức thủ đô hôm nay", "đô thị").
_NOT_CURRENCY_DO_RE = re.compile(r"\b(?:thủ|kinh|cố) đô\b|\bđô (?:thị|vật|đốc|hộ)\b")
_ACC_BY_ALIAS, _ACC_ALIAS = _alias_map(_ACCENTED_ALIASES)
_FOLD_BY_ALIAS, _FOLD_ALIAS = _alias_map(_FOLDED_ALIASES)
_NUM = r"(?P<amt>\d+(?:[.,]\d+)*)\s*(?P<scale>k|nghìn|ngàn|nghin|ngan|triệu|trieu|tr|củ|cu|tỷ|ty)?"
_AMOUNT_CUR_ACC = re.compile(rf"(?<![\w.,]){_NUM}\s*(?P<cur>{_ACC_ALIAS})\b")
_AMOUNT_CUR_FOLD = re.compile(rf"(?<![\w.,]){_NUM}\s*(?P<cur>{_FOLD_ALIAS})\b")
_VND_TO_CUR_ACC = re.compile(
    rf"(?<![\w.,]){_NUM}\s*(?:đồng|vnd|vnđ|đ)?\s.{{0,30}}?\b(?:đổi|ra|bằng|được|sang|là)\b"
    rf".{{0,20}}?\b(?P<cur>{_ACC_ALIAS})\b"
)
_CONTEXT_CUR_ACC = re.compile(
    rf"\b(?:t[ỷỉ] giá|giá|đổi)\s+(?P<cur>{_ACC_ALIAS})\b|"
    rf"\b(?P<cur2>{_ACC_ALIAS})\s+(?:hôm nay|bây giờ|bao nhiêu)\b"
)
_CONTEXT_CUR_FOLD = re.compile(
    rf"\b(?:t[yi] gia|gia|doi)\s+(?P<cur>{_FOLD_ALIAS})\b|"
    rf"\b(?P<cur2>{_FOLD_ALIAS})\s+(?:hom nay|bay gio|bao nhieu)\b"
)
_SCALES = {
    "k": 1e3, "nghìn": 1e3, "ngàn": 1e3, "nghin": 1e3, "ngan": 1e3,
    "triệu": 1e6, "trieu": 1e6, "tr": 1e6, "củ": 1e6, "cu": 1e6, "tỷ": 1e9, "ty": 1e9,
}  # fmt: skip

# Bỏ dấu thì "vé" trùng "về" ("chuyển về 500000", "nói về số liệu"), "dò vé" trùng
# "đổ về": cụm có "vé" chỉ so trên chữ GIỮ DẤU (``low``), nhận "vé" có dấu hoặc
# "ve" gõ không dấu - "về" còn dấu nên không bao giờ khớp. Bản bỏ dấu chỉ giữ từ
# không thể nhầm (xổ số, xsmn, kqxs).
_LOTTERY_RE = re.compile(r"\bxo so\b|\bso xo\b|\bxs(?:mb|mn|mt|kt|hcm|tphcm)\b|\bkqxs\b")
_LOTTERY_ACCENTED_RE = re.compile(
    r"\bvé số\b|\bdò vé\b|\bdò số\b|"
    # "vé 200000 đồng" là giá vé, không phải số vé ("Đồng Nai/Tháp" vẫn là đài).
    r"\b(?:v[ée]|d[òo] v[ée]|d[òo] s[ốo]|v[ée] s[ốo]) \d{5,6}\b"
    r"(?! ?(?:đ|đồng|dong|vnd|vnđ|k|nghìn|ngàn)\b(?! (?:nai|th[áa]p)\b))|"
    r"\bv[ée] s[ốo] (?:mi[ềe]n|h[ôo]m|xs|đ[àa]i|dai)"
)
# Câu hỏi kiến thức/ý kiến về xổ số (không hỏi số) -> để AI, cấm nêu số.
_LOTTERY_BROAD_RE = re.compile(
    r"\b(?:la gi|nghia la|tai sao|vi sao|co nen|nen mua|nen choi|lam sao|cach choi|y nghia|"
    r"lich su|bao nhieu tien 1 ve|thue)\b"
)
_TICKET_RE = re.compile(r"(?<![\d/.,-])(\d{5,6})(?![\d/.,-])")
_REGION_RE = (
    ("mb", re.compile(r"\b(?:mien bac|xsmb|mb|thu do)\b")),
    ("mt", re.compile(r"\b(?:mien trung|xsmt|mt)\b")),
    ("mn", re.compile(r"\b(?:mien nam|xsmn|mn|xshcm|xstphcm)\b")),
)
# Tỉnh mở thưởng (tên bỏ dấu) -> miền; tỉnh khác miền Bắc chỉ còn "miền Bắc".
_PROVINCE_REGION = {
    **{p: "mn" for p in (
        "ho chi minh", "tp hcm", "tphcm", "sai gon", "dong thap", "ca mau", "ben tre",
        "vung tau", "bac lieu", "dong nai", "can tho", "soc trang", "tay ninh", "an giang",
        "binh thuan", "vinh long", "binh duong", "tra vinh", "long an", "binh phuoc",
        "hau giang", "tien giang", "kien giang", "da lat", "lam dong",
    )},
    **{p: "mt" for p in (
        "hue", "thua thien hue", "phu yen", "dak lak", "quang nam", "da nang", "khanh hoa",
        "binh dinh", "quang tri", "quang binh", "gia lai", "ninh thuan", "quang ngai",
        "dak nong", "kon tum",
    )},
    **{p: "mb" for p in (
        "ha noi", "quang ninh", "bac ninh", "hai phong", "nam dinh", "thai binh",
    )},
}  # fmt: skip
_PROVINCE_RE = re.compile(
    r"\b("
    + "|".join(re.escape(p) for p in sorted(_PROVINCE_REGION, key=len, reverse=True))
    + r")\b"
)
_DATE_RE = re.compile(r"(?<![\d.,])(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?(?![\d.,])")

# Từ không mang nội dung trong câu hỏi giá vàng/tỷ giá (đã bỏ dấu). Còn lại
# nhiều chữ khác -> câu rộng ("có nên mua không") -> để AI kèm grounding.
_PRICE_FILLER = [
    r"gia vang", r"vang", r"sjc", r"mieng", r"nhan", r"9999", r"24k", r"t[yi] gia", r"gia",
    r"hom nay", r"bay gio", r"hien tai", r"luc nay", r"sang nay", r"bao nhieu", r"the nao",
    r"ra sao", r"sao roi", r"sao", r"roi", r"la", r"nhieu", r"mot", r"chi", r"luong", r"cho",
    r"em", r"anh", r"minh", r"hoi", r"xem", r"tra", r"check", r"giup", r"voi", r"nhe", r"nha",
    r"a", r"oi", r"vay", r"di", r"ha", r"ngan hang", r"vietcombank", r"vcb", r"mua vao",
    r"ban ra", r"mua", r"ban", r"cua", r"o", r"tai", r"the", r"khong", r"ko", r"tien viet nam",
    r"tien viet", r"tien", r"vnd", r"vnđ", r"dong", r"doi", r"ra", r"bang", r"duoc", r"sang",
    r"do", r"yen", r"te", r"bat", r"won", r"rup", r"k", r"nghin", r"ngan", r"trieu", r"tr",
    r"cu", r"ty", r"d", r"bn", r"nhiu", r"dang", r"co", r"gium", r"may", r"dc", r"nao",
]  # fmt: skip
_PRICE_FILLER_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_PRICE_FILLER, key=len, reverse=True)) + r")\b"
)
# Chữ hỏi ý kiến/xu hướng quanh đúng con số code đã có ("có nên mua", "tăng hay
# giảm") - còn lại thêm chữ khác (bão, giá nhà...) thì lượt đó vẫn phải tra web.
_OPINION_FILLER = [
    r"co nen", r"nen", r"tang", r"giam", r"len", r"xuong", r"dau tu", r"xu huong",
    r"du bao", r"tiep", r"nua", r"luc nao", r"hay", r"chua", r"ngay mai", r"mai", r"sap toi",
    r"tuan toi", r"tuan sau",
]  # fmt: skip
_LOTTERY_FILLER = [
    r"xo so", r"so xo", r"xs\w*", r"kqxs", r"ket qua", r"mien", r"bac", r"nam", r"trung",
    r"mb", r"mn", r"mt", r"ve", r"do", r"so", r"dai", r"giai", r"hom qua", r"hom kia",
    r"toi nay", r"chieu nay", r"ngay", r"kiem tra",
]  # fmt: skip
_OPINION_FILLER_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_OPINION_FILLER, key=len, reverse=True)) + r")\b"
)
_LOTTERY_FILLER_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_LOTTERY_FILLER, key=len, reverse=True)) + r")\b"
)


@dataclass(frozen=True)
class FxQuery:
    codes: tuple[str, ...] = ()
    amount: float | None = None
    to_vnd: bool = True


@dataclass(frozen=True)
class LotteryQuery:
    region: str
    day: dt.date | None
    ticket: str = ""
    narrow: bool = True
    region_explicit: bool = False


def _amount(raw: str, scale: str | None) -> float | None:
    text = raw
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", text):
        text = re.sub(r"[.,]", "", text)  # 1.000.000 / 1,000 -> phân cách nghìn
    else:
        text = text.replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value * _SCALES.get(scale or "", 1.0)


def _code(match: re.Match, by_alias: dict[str, str]) -> str | None:
    alias = match.group("cur") if "cur" in match.re.groupindex else None
    if not alias and "cur2" in match.re.groupindex:
        alias = match.group("cur2")
    return by_alias.get(alias or "")


def wants_gold(text: str) -> bool:
    low = _low(text)
    folded = fold(low)
    if _GOLD_RE.search(folded) or _GOLD_ACCENTED_RE.search(low):
        return True
    # Cả câu không dấu: "vang hom nay" mới chắc là hỏi giá vàng ("rượu vang" có
    # dấu vẫn không có dấu thanh nên nhánh này KHÔNG chạy trên chữ có dấu).
    return low == folded and bool(_GOLD_PLAIN_RE.search(low))


def parse_fx(text: str) -> FxQuery | None:
    """Câu hỏi tỷ giá/đổi tiền, hoặc None."""
    low = _NOT_CURRENCY_DO_RE.sub(" ", _low(text))
    folded = fold(low)
    if _NOT_FX_RE.search(folded):
        return None
    for regex, by_alias in ((_AMOUNT_CUR_ACC, _ACC_BY_ALIAS), (_AMOUNT_CUR_FOLD, _FOLD_BY_ALIAS)):
        m = regex.search(low if regex is _AMOUNT_CUR_ACC else folded)
        if m:
            amount, code = _amount(m.group("amt"), m.group("scale")), _code(m, by_alias)
            if amount and code:
                return FxQuery((code,), amount, to_vnd=True)
    m = _VND_TO_CUR_ACC.search(low)
    if m:
        amount, code = _amount(m.group("amt"), m.group("scale")), _code(m, _ACC_BY_ALIAS)
        if amount and code:
            return FxQuery((code,), amount, to_vnd=False)
    codes: list[str] = []
    for regex, source, by_alias in (
        (_CONTEXT_CUR_ACC, low, _ACC_BY_ALIAS),
        (_CONTEXT_CUR_FOLD, folded, _FOLD_BY_ALIAS),
    ):
        for m in regex.finditer(source):
            code = _code(m, by_alias)
            if code and code not in codes:
                codes.append(code)
    if _FX_EXPLICIT_RE.search(folded):
        for m in re.finditer(rf"\b(?:{_ACC_ALIAS})\b", low):
            code = _ACC_BY_ALIAS.get(m.group(0))
            if code and code not in codes:
                codes.append(code)
        return FxQuery(tuple(codes))
    return FxQuery(tuple(codes)) if codes else None


def parse_lottery(text: str, today: dt.date | None = None) -> LotteryQuery | None:
    """Câu hỏi xổ số/dò vé, hoặc None."""
    low = _low(text)
    folded = fold(low)
    if not _LOTTERY_RE.search(folded) and not _LOTTERY_ACCENTED_RE.search(low):
        return None
    today = today or _today()
    ticket_match = _TICKET_RE.search(folded)
    ticket = ticket_match.group(1) if ticket_match else ""
    region, explicit = "", False
    for code, regex in _REGION_RE:
        if regex.search(folded):
            region, explicit = code, True
            break
    if not region:
        province = _PROVINCE_RE.search(folded)
        if province:
            region, explicit = _PROVINCE_REGION[province.group(1)], True
    if not region:
        region = "mb" if len(ticket) == 5 else "mn"
    day: dt.date | None = None
    if re.search(r"\bhom nay\b|\btoi nay\b|\bchieu nay\b", folded):
        day = today
    elif re.search(r"\bhom qua\b", folded):
        day = today - dt.timedelta(days=1)
    elif re.search(r"\bhom kia\b", folded):
        day = today - dt.timedelta(days=2)
    else:
        m = _DATE_RE.search(folded)
        if m:
            try:
                year = int(m.group(3)) if m.group(3) else today.year
                day = dt.date(year, int(m.group(2)), int(m.group(1)))
                if not m.group(3) and day > today:
                    day = day.replace(year=today.year - 1)
            except ValueError:
                day = None
    narrow = bool(ticket) or not _LOTTERY_BROAD_RE.search(folded)
    return LotteryQuery(region, day, ticket, narrow, explicit)


def _is_narrow_price_question(text: str) -> bool:
    """Câu chỉ hỏi số (không kèm ý kiến/phân tích) -> trả thẳng bằng code."""
    folded = fold(_low(text))
    if _WORLD_GOLD_RE.search(folded):
        return False
    rest = re.sub(rf"\b(?:{_FOLD_ALIAS})\b|\d+(?:[.,]\d+)*", " ", folded)
    rest = _PRICE_FILLER_RE.sub(" ", rest)
    return not rest.split()


# ─── Câu trả lời bằng code ──────────────────────────────────────────────────


async def gold_text(text: str = "") -> str:
    table = await market.get_gold()
    out = market.format_gold(table)
    if re.search(r"\bchỉ\b", (text or "").lower()) and table.rows[0].sell:
        row = table.rows[0]
        out += (
            f"\n1 chỉ = 1/10 lượng: {row.name} bán ≈ "
            f"{market.vnd((row.sell or 0) / 10_000)} nghìn/chỉ."
        )
    return out


async def fx_text(query: FxQuery) -> str:
    table = await market.get_fx()
    if query.amount and query.codes:
        converted = market.format_conversion(
            table, query.amount, query.codes[0], to_vnd=query.to_vnd
        )
        if converted:
            return converted
    return market.format_fx(table, list(query.codes) or None)


def _missing_day_text(query: LotteryQuery, draws: list[market.LotteryDraw]) -> str:
    region = market.REGION_NAMES[query.region]
    if query.day == _today():
        return (
            f"⏳ Hôm nay xổ số {region} chưa có kết quả (quay lúc "
            f"{market.DRAW_TIMES[query.region]}, nguồn cập nhật sau đó ít phút)."
        )
    day = query.day.strftime("%d/%m/%Y") if query.day else ""
    me = reply_audience.forms()[1]
    return (
        f"{me.capitalize()} chưa có kết quả xổ số {region} ngày {day} (nguồn chỉ giữ khoảng "
        f"{len(draws)} kỳ gần nhất)."
    )


def _named_province(text: str, region: str) -> tuple[str, str] | None:
    """(tên bỏ dấu, tên hiển thị) của đài miền Nam/Trung được nhắc trong câu.
    Miền Bắc quay chung 1 kỳ nên không tách theo tỉnh."""
    if region == "mb":
        return None
    low = _low(text)
    folded = fold(low)
    m = _PROVINCE_RE.search(folded)
    if not m or _PROVINCE_REGION[m.group(1)] != region:
        return None
    alias = m.group(1)
    if "hcm" in alias:
        return alias, "TP.HCM"
    shown = low[m.start() : m.end()] if len(low) == len(folded) else alias
    return alias, shown.title()


def _province_text(
    query: LotteryQuery, draws: list[market.LotteryDraw], alias: str, label: str
) -> str:
    """Kết quả/dò vé của ĐÚNG đài người dùng nhắc. Kỳ được chọn không có đài đó
    thì nói rõ, không bao giờ dò sang đài khác rồi phán "chưa trúng"."""
    region = market.REGION_NAMES[query.region]
    me = reply_audience.forms()[1]
    if query.day is None:
        # Mỗi đài quay 1 lần/tuần: lấy kỳ mới nhất CÓ đài đó, không phải kỳ mới nhất.
        hit = next(
            ((d, p) for d in draws if (p := market.find_province(d, alias)) is not None), None
        )
        if hit is None:
            note = (
                f"{me.capitalize()} chưa thấy kết quả đài {label} trong {len(draws)} kỳ xổ số "
                f"{region} gần nhất nguồn còn giữ"
            )
            return note + (" nên chưa dò vé này được nha." if query.ticket else ".")
        draw, province = hit
    else:
        found = market.pick_draw(draws, query.day)
        province = market.find_province(found, alias) if found else None
        if found is None or province is None:
            day = f"{query.day:%d/%m/%Y}"
            have = ", ".join(p.name for p in found.provinces) if found else ""
            if query.day == _today():
                msg = (
                    f"⏳ Đài {label} hôm nay chưa có kết quả (quay lúc "
                    f"{market.DRAW_TIMES[query.region]}, nguồn cập nhật sau đó ít phút) "
                    "hoặc hôm nay đài này không quay."
                    + (f" Kỳ hôm nay nguồn mới có: {have}." if have else "")
                )
            elif found is None:
                msg = _missing_day_text(query, draws)
            else:
                msg = f"Ngày {day} đài {label} không quay thưởng (kỳ đó có: {have})."
            return msg + (f" {me.capitalize()} chưa dò vé này được nha." if query.ticket else "")
        draw = found
    one = market.LotteryDraw(draw.region, draw.date, (province,), draw.link)
    if query.ticket:
        return market.format_ticket(query.ticket, one)
    return market.format_draw(one)


async def lottery_text(query: LotteryQuery, text: str = "") -> str:
    """Kết quả/dò vé bằng code. Raise MarketDataError khi nguồn lỗi."""
    draws = await market.get_lottery(query.region)
    named = _named_province(text, query.region)
    if named is not None:
        return _province_text(query, draws, *named)
    draw = market.pick_draw(draws, query.day)
    prefix = ""
    if draw is None:
        missing = _missing_day_text(query, draws)
        if query.ticket:
            # Dò vé đúng 1 ngày chưa có kết quả: không dò nhầm sang kỳ khác.
            you, me = reply_audience.forms()
            return f"{missing} Có kết quả rồi {you} nhắn {me} dò lại nha."
        draw = draws[0]
        prefix = f"{missing} Kết quả gần nhất ngày {draw.date:%d/%m}:\n\n"
    coverage = market.coverage(draw, draws, _today())
    if query.ticket:
        return market.format_ticket(query.ticket, draw, text, incomplete=coverage)
    province = market.find_province(draw, text)
    if province is not None and len(draw.provinces) > 1:
        draw = market.LotteryDraw(draw.region, draw.date, (province,), draw.link)
    elif coverage is not None:
        prefix += (
            f"⏳ Kỳ hôm nay nguồn mới có kết quả {coverage[0]}/{coverage[1]} đài, đài còn lại "
            "chưa cập nhật xong.\n\n"
        )
    return prefix + market.format_draw(draw)


def lottery_down() -> str:
    """Báo nguồn xổ số lỗi (dò vé không bao giờ đoán), xưng hô theo người nhận."""
    you, me = reply_audience.forms()
    return (
        f"❌ {me.capitalize()} chưa lấy được kết quả xổ số lúc này (nguồn xskt.com.vn lỗi) nên "
        f"chưa dò vé được. {me.capitalize()} không đoán số đâu ạ - {you} thử lại sau ít phút nha."
    )


async def maybe_handle(text: str, user_id: int | None = None) -> str | None:
    """Câu trả lời bằng code, hoặc None để đi tiếp đường cũ."""
    if user_id is not None:
        home = await weather_home.maybe_handle_statement(user_id, text)
        if home is not None:
            return home
    lottery = parse_lottery(text)
    if lottery is not None:
        if not lottery.narrow:
            return None  # hỏi kiến thức: AI trả lời, grounding_for cấm nêu số
        try:
            return await lottery_text(lottery, text)
        except market.MarketDataError:
            # Dò vé phải bằng code -> báo lỗi thẳng; hỏi kết quả -> tra web thật.
            return lottery_down() if lottery.ticket else None
    gold = wants_gold(text)
    fx = parse_fx(text)
    if not (gold or fx) or not _is_narrow_price_question(text):
        return None
    parts: list[str] = []
    try:
        if gold:
            parts.append(await gold_text(text))
        if fx is not None:
            parts.append(await fx_text(fx))
    except market.MarketDataError:
        return None  # nguồn lỗi -> đường tra web cũ ("tham khảo")
    return "\n\n".join(parts)


# ─── Grounding cho lượt chat AI ─────────────────────────────────────────────


@dataclass(frozen=True)
class DailyGrounding:
    """Grounding số liệu đời sống cho 1 lượt chat AI."""

    text: str = ""
    skip_search: bool = False  # số liệu code đã đủ: bỏ web_search + forced search
    force_real_search: bool = False  # xổ số mà nguồn lỗi: bắt tra web thật
    sources: tuple[str, ...] = field(default_factory=tuple)


def _reference_note(source: str) -> str:
    return (
        f"[Ghi chú hệ thống] Nguồn chính thức ({source}) đang lỗi nên chưa có số liệu "
        "tra bằng code. Nếu dùng số liệu từ kết quả tra web, PHẢI ghi rõ đó là số liệu "
        "THAM KHẢO, kèm nguồn và thời điểm; không có thì nói thật là chưa tra được, "
        "tuyệt đối không tự đoán số."
    )


_LOTTERY_NO_MEMORY = (
    "[Ghi chú hệ thống] TUYỆT ĐỐI không nêu bất kỳ con số kết quả xổ số / số trúng "
    "thưởng nào từ trí nhớ. Muốn xem kết quả hay dò vé, gợi ý người dùng nhắn "
    '"xổ số miền Nam hôm nay", "dò vé 123456" hoặc lệnh /xoso.'
)
_GOLD_FX_TERMS_RE = re.compile(
    rf"vàng|vang|sjc|gold|t[ỷỉyi] gi[áa]|\b(?:{_ACC_ALIAS})\b|\b(?:{_FOLD_ALIAS})\b"
)


async def grounding_for(text: str) -> DailyGrounding:
    """Grounding giá vàng/tỷ giá/xổ số cho câu hỏi rộng; rỗng nếu không liên quan.

    Không bao giờ raise: lỗi bất ngờ chỉ làm mất phần grounding này, lượt chat
    vẫn chạy đường tra web như cũ."""
    try:
        return await _build_grounding(text)
    except Exception:
        logger.exception("vn_daily_chat: dựng grounding lỗi, bỏ qua")
        return DailyGrounding()


async def _build_grounding(text: str) -> DailyGrounding:
    lottery = parse_lottery(text)
    gold = wants_gold(text)
    fx = parse_fx(text)
    if lottery is None and not gold and fx is None:
        return DailyGrounding()
    parts: list[str] = []
    all_ok = True
    force = False
    if lottery is not None:
        if not lottery.narrow:
            parts.append(_LOTTERY_NO_MEMORY)
        else:
            try:
                data = await lottery_text(lottery, text)
                parts.append(
                    f"[Kết quả xổ số thật vừa tra bằng code - chỉ dùng đúng số này]\n{data}"
                )
            except market.MarketDataError:
                all_ok, force = False, True
                parts.append(_reference_note("xskt.com.vn") + "\n" + _LOTTERY_NO_MEMORY)
    if gold:
        try:
            data = await gold_text(text)
            parts.append(
                "[Giá vàng SJC thật vừa tra bằng code - dùng ĐÚNG các số này, không lấy giá "
                f"vàng SJC từ trí nhớ hay web]\n{data}"
            )
        except market.MarketDataError:
            all_ok = False
            parts.append(_reference_note("SJC"))
    if fx is not None:
        try:
            data = await fx_text(fx)
            parts.append(
                "[Tỷ giá Vietcombank thật vừa tra bằng code - dùng ĐÚNG các số này]\n" + data
            )
        except market.MarketDataError:
            all_ok = False
            parts.append(_reference_note("Vietcombank"))
    from stock import analysis as stock_analysis

    # Còn hỏi thứ khác ngoài phần code đã có (bitcoin, giá vàng thế giới, lãi
    # suất, tin bão...) thì vẫn phải tra web cho phần đó.
    rest = _GOLD_FX_TERMS_RE.sub(" ", _low(text))
    partial = (
        stock_analysis.wants_external_market_data(rest)
        or bool(gold and _WORLD_GOLD_RE.search(fold(_low(text))))
        or bool(_uncovered_words(text, lottery=lottery is not None))
    )
    skip = all_ok and not partial and (lottery is None or lottery.narrow)
    return DailyGrounding("\n\n".join(parts), skip_search=skip, force_real_search=force)


def _uncovered_words(text: str, *, lottery: bool) -> list[str]:
    """Chữ còn lại sau khi bỏ tên tiền/vàng/xổ số, con số, chữ đệm và chữ hỏi ý
    kiến. Rỗng = câu chỉ xoay quanh số liệu code đã tra, bỏ tra web được."""
    rest = fold(re.sub(rf"\b(?:{_ACC_ALIAS})\b", " ", _low(text)))
    rest = re.sub(rf"\b(?:{_FOLD_ALIAS})\b|\d+(?:[.,/-]\d+)*", " ", rest)
    if lottery:
        rest = _LOTTERY_FILLER_RE.sub(" ", _PROVINCE_RE.sub(" ", rest))
    rest = _PRICE_FILLER_RE.sub(" ", _OPINION_FILLER_RE.sub(" ", rest))
    return rest.split()
