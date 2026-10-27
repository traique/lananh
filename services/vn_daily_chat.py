"""Giá vàng, tỷ giá, xăng dầu, xổ số trả lời bằng code (không AI) + grounding cho chat.

- ``maybe_handle(text, user_id)``: câu hỏi gọn ("giá vàng hôm nay", "vàng thế
  giới", "giá vàng doji", "tỷ giá đô", "100 đô bằng bao nhiêu tiền Việt", "giá
  xăng hôm nay", "dầu diesel bao nhiêu", "xổ số miền Nam hôm nay", "xổ số tây
  ninh hôm nay", "kết quả đà lạt chủ nhật", "dò vé 123456 đồng nai") -> trả lời
  thẳng, không tốn lượt AI nào. Câu nhắc 1 đài đi feed riêng của đúng đài đó
  (services/lottery_provinces.py). Câu khai báo nơi ở ("anh ở Hà Nội") -> lưu
  thành phố thời tiết riêng. Không chắc hoặc nguồn lỗi -> None
  để đi tiếp đường cũ (cổ phiếu, chat AI).
- ``grounding_for(text)``: câu rộng hơn ("vàng tăng vậy có nên mua không") ->
  số liệu thật làm grounding và BỎ tra web + forced search của lượt đó (khỏi
  trả 2 lần và khỏi mâu thuẫn "hệ thống không có dữ liệu"). Nguồn lỗi -> giữ
  đường tra web cũ nhưng buộc ghi rõ số liệu web là "tham khảo".

Xăng dầu chỉ nhận câu RÕ là hỏi giá bán lẻ trong nước ("giá xăng", "dầu diesel bao
nhiêu"); "đổ xăng 100k", "hết xăng", "giá dầu thế giới" không bị bắt.

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
from services import fuel_prices, reply_audience, weather_home
from services import lottery_provinces as provinces
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
_GOLD_RE = re.compile(
    r"\bgia vang\b|\bvang (?:sjc|mieng|9999|24k|doji|pnj|bao tin|btmc|viettin|xau)\b|"
    r"\bsjc\b|\bxau ?usd\b"
)
_GOLD_ACCENTED_RE = re.compile(
    r"\bvàng (?:sjc|miếng|nhẫn|9999|24k|hôm nay|bây giờ|bao nhiêu|thế giới|quốc tế)\b"
)
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
# Tên đài (bỏ dấu) theo danh mục services/lottery_provinces.py.
_PROVINCE_RE = re.compile(provinces.alias_pattern())
# "kết quả đà lạt chủ nhật": không có chữ "xổ số" nhưng chỉ gồm tên đài + ngày.
_RESULT_PROVINCE_RE = re.compile(r"\b(?:ket qua|kq)\b")
# "xổ số tây ninh quay thứ mấy", "lịch quay đà lạt": nói lịch quay trước kết quả.
_SCHEDULE_QUESTION_RE = re.compile(
    r"\b(?:thu may|lich quay|lich mo thuong|quay ngay nao|ngay nao quay)\b"
)
_WEEKDAY_RE = re.compile(r"\b(?:thu (2|3|4|5|6|7|hai|ba|tu|nam|sau|bay)|(chu nhat))\b")
_WEEKDAY_WORDS = {"2": 0, "hai": 0, "3": 1, "ba": 1, "4": 2, "tu": 2, "5": 3, "nam": 3,
                  "6": 4, "sau": 4, "7": 5, "bay": 5}  # fmt: skip
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
    r"doji", r"pnj", r"bao tin", r"btmc", r"viettin", r"vn gold", r"vngold", r"the gioi",
    r"quoc te", r"xau", r"xauusd", r"usd", r"ounce", r"oz", r"ha noi", r"hcm", r"nu trang",
    r"xang", r"dau", r"diesel", r"hoa", r"e5", r"e10", r"ron", r"a95", r"lit", r"petrolimex",
    r"pvoil", r"vung", r"ky nay", r"muc", r"bang gia", r"loai",
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
    r"toi nay", r"chieu nay", r"ngay", r"kiem tra", r"kq", r"thu", r"hai", r"ba", r"tu",
    r"sau", r"bay", r"chu nhat", r"tuan truoc", r"tuan nay", r"moi nhat", r"gan nhat",
    r"tinh", r"quay", r"lich quay", r"lich mo thuong", r"trung khong", r"may", r"nao",
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
    province: str = ""  # khoá đài (lottery_provinces) khi câu nhắc đúng 1 đài miền Nam/Trung


@dataclass(frozen=True)
class FuelQuery:
    kind: str | None = None  # "xang" | "diesel" | "dau_hoa" | None = cả bảng
    narrow: bool = True


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


def _province_only_result(folded: str) -> bool:
    """ "kết quả đà lạt chủ nhật", "xstn hôm nay": không có chữ "xổ số" nhưng câu
    chỉ gồm tên đài (hoặc mã xs..) + ngày -> vẫn là hỏi kết quả xổ số. Còn chữ khác
    ("kết quả bóng đá Đà Nẵng") thì không nhận."""
    m = _PROVINCE_RE.search(folded)
    if m is None:
        return False
    if m.group(1).startswith("xs"):
        return True
    if not _RESULT_PROVINCE_RE.search(folded):
        return False
    rest = re.sub(r"\d+(?:[./-]\d+)*", " ", _PROVINCE_RE.sub(" ", folded))
    rest = _PRICE_FILLER_RE.sub(" ", _LOTTERY_FILLER_RE.sub(" ", rest))
    return not rest.split()


def _weekday_day(folded: str, today: dt.date) -> dt.date | None:
    """ "chủ nhật", "thứ 5", "thứ hai tuần trước" -> ngày đó gần nhất (<= hôm nay)."""
    m = _WEEKDAY_RE.search(folded)
    if m is None:
        return None
    weekday = 6 if m.group(2) else _WEEKDAY_WORDS[m.group(1)]
    day = today - dt.timedelta(days=(today.weekday() - weekday) % 7)
    if re.search(r"\btuan truoc\b", folded):
        day -= dt.timedelta(days=7)
    return day


def parse_lottery(text: str, today: dt.date | None = None) -> LotteryQuery | None:
    """Câu hỏi xổ số/dò vé, hoặc None."""
    low = _low(text)
    folded = fold(low)
    if (
        not _LOTTERY_RE.search(folded)
        and not _LOTTERY_ACCENTED_RE.search(low)
        and not _province_only_result(folded)
    ):
        return None
    today = today or _today()
    ticket_match = _TICKET_RE.search(folded)
    ticket = ticket_match.group(1) if ticket_match else ""
    region, explicit, province_key = "", False, ""
    named = provinces.match(folded)
    if named is not None:
        # Tên đài thắng tên miền ("/xoso mn đà nẵng" là đài Đà Nẵng miền Trung).
        region, explicit = named.region, True
        province_key = "" if named.region == "mb" else named.key
    else:
        for code, regex in _REGION_RE:
            if regex.search(folded):
                region, explicit = code, True
                break
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
        else:
            day = _weekday_day(folded, today)
    narrow = bool(ticket) or not _LOTTERY_BROAD_RE.search(folded)
    return LotteryQuery(region, day, ticket, narrow, explicit, province_key)


# ─── Xăng dầu ───────────────────────────────────────────────────────────────

# Chỉ nhận khi RÕ là hỏi giá xăng dầu bán lẻ trong nước: "đổ xăng 100k" (chi tiêu,
# đã bắt trước ở expense), "hết xăng rồi", "giá dầu thế giới", "dầu ăn" đều không.
_FUEL_TERM_RE = re.compile(
    r"\bgiá (?:xăng|dầu diesel|dầu do|dầu hỏa|dầu hoả|diesel)\b|"
    r"\bxăng (?:e5|e10|ron|a95|tăng|giảm|lên|xuống|hôm nay|bây giờ|bao nhiêu|giá|mấy)\b|"
    r"\bdầu (?:diesel|do|hỏa|hoả)\b|\bdiesel\b|\bxăng dầu\b"
)
_FUEL_TERM_FOLDED_RE = re.compile(
    r"\bgia xang\b|\bgia dau (?:diesel|hoa)\b|\bxang (?:e5|e10|ron|a95)\b|\bdiesel\b"
)
_FUEL_CONTEXT_RE = re.compile(
    r"\b(?:gia|bao nhieu|bao nhiu|bn|hom nay|bay gio|hien tai|tang|giam|len|xuong|may|"
    r"nhieu|bang gia|ky nay|dieu chinh|lit)\b"
)
# Có các chữ này thì cần nguồn khác (giá thế giới, cổ phiếu ngành) - không trả thẳng.
_FUEL_OUTSIDE_RE = re.compile(
    r"\b(?:the gioi|quoc te|brent|wti|dau tho|opec|dau an|co phieu|plx|pvs|gas|oil|my|"
    r"singapore|tai sao|vi sao|du bao|co nen)\b"
)


def parse_fuel(text: str) -> FuelQuery | None:
    """Câu hỏi giá xăng dầu trong nước, hoặc None."""
    low = _low(text)
    folded = fold(low)
    if not (_FUEL_TERM_RE.search(low) or _FUEL_TERM_FOLDED_RE.search(folded)):
        return None
    if not _FUEL_CONTEXT_RE.search(folded):
        return None
    # "dau do" bỏ dấu trùng "đầu đó": chỉ nhận khi cả câu gõ không dấu.
    diesel = bool(re.search(r"\bdiesel\b", folded) or re.search(r"\bdầu do\b", low)) or (
        low == folded and bool(re.search(r"\bdau do\b", folded))
    )
    kerosene = bool(re.search(r"\bdau hoa\b", folded))
    petrol = bool(re.search(r"\bxang\b", folded)) and not re.search(r"\bxang dau\b", folded)
    picked = [k for k, on in (("diesel", diesel), ("dau_hoa", kerosene), ("xang", petrol)) if on]
    kind = picked[0] if len(picked) == 1 else None
    return FuelQuery(kind, narrow=not _FUEL_OUTSIDE_RE.search(folded))


def _is_narrow_price_question(text: str) -> bool:
    """Câu chỉ hỏi số (không kèm ý kiến/phân tích) -> trả thẳng bằng code."""
    folded = fold(_low(text))
    rest = re.sub(rf"\b(?:{_FOLD_ALIAS})\b|\d+(?:[.,]\d+)*", " ", folded)
    rest = _PRICE_FILLER_RE.sub(" ", rest)
    return not rest.split()


# ─── Câu trả lời bằng code ──────────────────────────────────────────────────

_GOLD_BRAND_RES = (
    ("doji", re.compile(r"\bdoji\b")),
    ("pnj", re.compile(r"\bpnj\b")),
    ("baotin", re.compile(r"\bbao tin\b|\bbtmc\b|\bminh chau\b")),
    ("viettin", re.compile(r"\bviettin\b")),
    ("vngold", re.compile(r"\bvn ?gold\b")),
    ("world", _WORLD_GOLD_RE),
    ("world", re.compile(r"\bxau\b|\bxauusd\b")),
)


def gold_brands(text: str) -> set[str]:
    """Thương hiệu được hỏi ("giá vàng doji", "vàng thế giới", "nhẫn sjc"); rỗng = bảng chung."""
    low = _low(text)
    folded = fold(low)
    brands = {name for name, regex in _GOLD_BRAND_RES if regex.search(folded)}
    # "nhắn" bỏ dấu cũng là "nhan": chỉ nhận "nhẫn" có dấu hoặc câu gõ không dấu cả.
    ring = bool(re.search(r"\bnhẫn\b", low)) or (low == folded and "vang nhan" in folded)
    if ring:
        brands.add("nhan")
    elif re.search(r"\bsjc\b|\bvang mieng\b", folded):
        brands.add("sjc")
    return brands


async def gold_text(text: str = "") -> str:
    """Bảng giá vàng: vang.today (nhiều thương hiệu + thế giới); nguồn đó lỗi thì
    rơi về bảng SJC. Hỏi thương hiệu/thế giới mà chỉ còn SJC -> raise để tra web."""
    brands = gold_brands(text)
    per_chi = bool(re.search(r"\bchỉ\b", (text or "").lower()))
    try:
        table = await market.get_gold_live()
    except market.MarketDataError:
        if brands - {"sjc", "nhan"}:
            raise
        sjc = await market.get_gold()
        out = market.format_gold(sjc)
        if per_chi and sjc.rows[0].sell:
            row = sjc.rows[0]
            out += (
                f"\n1 chỉ = 1/10 lượng: {row.name} bán ≈ "
                f"{market.vnd((row.sell or 0) / 10_000)} nghìn/chỉ."
            )
        return out
    fx = None
    if (not brands or "world" in brands) and table.get("XAUUSD") is not None:
        try:
            fx = await market.get_fx()
        except market.MarketDataError:
            fx = None  # thiếu tỷ giá: bỏ dòng quy đổi, vẫn có giá USD/oz
    return market.format_gold_live(table, brands or None, fx=fx, per_chi=per_chi)


async def fx_text(query: FxQuery) -> str:
    table = await market.get_fx()
    if query.amount and query.codes:
        converted = market.format_conversion(
            table, query.amount, query.codes[0], to_vnd=query.to_vnd
        )
        if converted:
            return converted
    return market.format_fx(table, list(query.codes) or None)


async def fuel_text(query: FuelQuery | None = None) -> str:
    table = await fuel_prices.get_fuel()
    return fuel_prices.format_fuel(table, query.kind if query else None)


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


def schedule_line(province: provinces.Province) -> str:
    return (
        f"Đài {province.name} quay {provinces.schedule_text(province)} lúc "
        f"{provinces.DRAW_TIMES[province.region]}."
    )


async def province_draws(province: provinces.Province) -> list[market.LotteryDraw]:
    """Các kỳ của 1 đài: feed riêng của tỉnh; feed đó lỗi thì lọc từ feed miền
    (ít kỳ hơn). Cả hai lỗi -> MarketDataError."""
    try:
        return await market.get_province_lottery(province)
    except market.MarketDataError:
        if province.region == "mb":
            raise
        region_draws = await market.get_lottery(province.region)
    out = []
    for draw in region_draws:
        hit = next((p for p in draw.provinces if provinces.same(p.name, province)), None)
        if hit is not None:
            out.append(market.LotteryDraw(draw.region, draw.date, (hit,), draw.link))
    return out


@dataclass(frozen=True)
class ProvincePick:
    """Kỳ chọn được cho 1 đài. status: "latest" (không hỏi ngày), "exact" (đúng ngày),
    "not_drawn" (ngày đó đài không quay - draw = kỳ gần nhất TRƯỚC đó), "pending" (hôm
    nay có quay nhưng nguồn chưa có - draw = kỳ gần nhất), "missing" (ngày cũ hơn
    nguồn còn giữ), "empty" (nguồn chưa có kỳ nào)."""

    status: str
    draw: market.LotteryDraw | None = None


def pick_province_draw(
    province: provinces.Province,
    draws: list[market.LotteryDraw],
    day: dt.date | None,
    today: dt.date | None = None,
) -> ProvincePick:
    today = today or _today()
    if day is None:
        return ProvincePick("latest", draws[0]) if draws else ProvincePick("empty")
    draw = market.pick_draw(draws, day)
    if draw is not None:
        return ProvincePick("exact", draw)
    if not province.draws_on(day):
        return ProvincePick("not_drawn", next((d for d in draws if d.date < day), None))
    if day >= today:
        return ProvincePick("pending", draws[0] if draws else None)
    return ProvincePick("missing")


async def province_text(
    province: provinces.Province, day: dt.date | None = None, ticket: str = ""
) -> str:
    """Kết quả/dò vé của ĐÚNG 1 đài. Ngày đó đài không quay -> nói lịch quay và đưa
    kỳ gần nhất trước đó; không bao giờ dò sang đài khác rồi phán "chưa trúng".
    Raise MarketDataError khi nguồn lỗi."""
    draws = await province_draws(province)
    you, me = reply_audience.forms()
    today = _today()
    pick = pick_province_draw(province, draws, day, today)
    not_yet = f" {me.capitalize()} chưa dò vé này được nha." if ticket else ""
    prefix = ""
    if pick.status == "empty":
        return (
            f"{me.capitalize()} chưa thấy kết quả đài {province.name} trong các kỳ nguồn "
            f"còn giữ. {schedule_line(province)}{not_yet}"
        )
    if pick.status == "missing" and day is not None:
        return (
            f"{me.capitalize()} chưa có kết quả đài {province.name} ngày {day:%d/%m/%Y} "
            f"(nguồn chỉ giữ khoảng {len(draws)} kỳ gần nhất).{not_yet}"
        )
    if pick.status == "not_drawn" and day is not None:
        when = "Hôm nay" if day == today else f"{provinces.WEEKDAYS[day.weekday()]} {day:%d/%m}"
        msg = f"{when} đài {province.name} không quay. {schedule_line(province)}"
        if pick.draw is None:
            return msg + f" Nguồn chưa có kỳ nào gần đó.{not_yet}"
        verb = "dò theo" if ticket else "đây là"
        prefix = f"{msg} {me.capitalize()} {verb} kỳ gần nhất ({pick.draw.date:%d/%m}):\n\n"
    if pick.status == "pending":
        msg = (
            f"⏳ Đài {province.name} hôm nay quay lúc {provinces.DRAW_TIMES[province.region]}, "
            "nguồn chưa có kết quả (thường cập nhật sau giờ quay ít phút)."
        )
        if ticket:
            return msg + f" Có kết quả rồi {you} nhắn {me} dò lại nha."
        if pick.draw is None:
            return msg
        prefix = f"{msg} Kỳ gần nhất ngày {pick.draw.date:%d/%m}:\n\n"
    draw = pick.draw
    assert draw is not None
    if ticket:
        return prefix + market.format_ticket(ticket, draw)
    return prefix + market.format_draw(draw)


async def lottery_text(query: LotteryQuery, text: str = "") -> str:
    """Kết quả/dò vé bằng code. Raise MarketDataError khi nguồn lỗi."""
    named = provinces.BY_KEY.get(query.province) if query.province else None
    if named is not None:
        reply = await province_text(named, query.day, query.ticket)
        if _SCHEDULE_QUESTION_RE.search(fold(_low(text))) and "không quay" not in reply:
            reply = f"📅 {schedule_line(named)}\n\n{reply}"
        return reply
    draws = await market.get_lottery(query.region)
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
    fuel = parse_fuel(text)
    if fuel is not None and not fuel.narrow:
        return None  # giá dầu thế giới, cổ phiếu ngành...: AI + grounding + tra web
    if not (gold or fx or fuel) or not _is_narrow_price_question(text):
        return None
    parts: list[str] = []
    try:
        if gold:
            parts.append(await gold_text(text))
        if fx is not None:
            parts.append(await fx_text(fx))
        if fuel is not None:
            parts.append(await fuel_text(fuel))
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
# Bỏ thêm khi câu hỏi giá xăng dầu trong nước ("dầu" làm wants_external_market_data bật).
_FUEL_TERMS_RE = re.compile(r"xăng|dầu|diesel|xang|dau")


async def grounding_for(text: str) -> DailyGrounding:
    """Grounding giá vàng/tỷ giá/xăng dầu/xổ số cho câu hỏi rộng; rỗng nếu không liên quan.

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
    fuel = parse_fuel(text)
    if lottery is None and not gold and fx is None and fuel is None:
        return DailyGrounding()
    world_missing = False
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
                "[Giá vàng thật vừa tra bằng code - dùng ĐÚNG các số này, không lấy giá "
                f"vàng từ trí nhớ hay web]\n{data}"
            )
            # Bảng SJC dự phòng không có giá thế giới: phần đó vẫn phải tra web.
            world_missing = "XAU/USD" not in data and bool(_WORLD_GOLD_RE.search(fold(_low(text))))
        except market.MarketDataError:
            all_ok = False
            parts.append(_reference_note("vang.today/SJC"))
    if fx is not None:
        try:
            data = await fx_text(fx)
            parts.append(
                "[Tỷ giá Vietcombank thật vừa tra bằng code - dùng ĐÚNG các số này]\n" + data
            )
        except market.MarketDataError:
            all_ok = False
            parts.append(_reference_note("Vietcombank"))
    if fuel is not None:
        try:
            data = await fuel_text(fuel)
            parts.append(
                "[Giá xăng dầu bán lẻ trong nước thật vừa tra bằng code - dùng ĐÚNG các số "
                f"này, không lấy giá xăng từ trí nhớ]\n{data}"
            )
        except market.MarketDataError:
            all_ok = False
            parts.append(_reference_note("giaxanghomnay.com - bảng giá Petrolimex"))
    from stock import analysis as stock_analysis

    # Còn hỏi thứ khác ngoài phần code đã có (bitcoin, giá dầu thế giới, lãi
    # suất, tin bão...) thì vẫn phải tra web cho phần đó.
    rest = _GOLD_FX_TERMS_RE.sub(" ", _low(text))
    if fuel is not None and fuel.narrow:
        rest = _FUEL_TERMS_RE.sub(" ", rest)
    partial = (
        stock_analysis.wants_external_market_data(rest)
        or world_missing
        or bool(fuel is not None and not fuel.narrow)
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
