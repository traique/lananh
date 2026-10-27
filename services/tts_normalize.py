"""Chuẩn hoá chữ tiếng Việt thành "chữ đọc được" trước khi gửi bộ đọc giọng /noi.

Gemini TTS/Live đọc NGUYÊN VĂN: "150k", "1tr2", "LNST", "ko", "FPT", "8h30",
"26/10", "+1,5%" để nguyên thì bị bỏ qua, đọc kiểu tiếng Anh hoặc đọc sai (người
nghe báo "từ viết tắt không được đọc ra"). Module này đổi chúng thành chữ, vd
"150k" -> "một trăm năm mươi nghìn", "FPT" -> "ép pê tê". Chỉ dùng cho bản ĐỌC
(services/voice_reply.py) - tin chữ gửi người dùng không đổi.

Thứ tự (mỗi bước chỉ đụng phần chưa thành chữ):
1. Ký hiệu: ▲ ▼ ± ~ ≥ ≤ & ...
2. Chỉ số: VN-Index/VNINDEX/VN30/HNX-Index/UPCoM-Index.
3. Ngày giờ: 26/10, 26/10/2026, 2026-10-26, 10/2026, 8h30, 8h, 15:05.
4. Số kèm đơn vị: tiền (150k, 1tr2, 3 tỷ, 2ty5, 1.299.000đ, 25,5 nghìn, $5, 10 USD),
   %, đo lường (km, kg, m², °C...), dấu +/- trước số, khoảng "10-20".
5. Số còn lại: số lớn và số thập phân kiểu Việt ("1.299.000", "25,5"); số bắt đầu
   bằng 0 hoặc quá dài (điện thoại, mã) đọc từng chữ số.
6. Viết tắt theo từ điển assets/tts_viet_tat.yaml; "k" đứng riêng là "không"
   (sau số thì đã thành "nghìn" ở bước 4).
7. Mã cổ phiếu / chữ in hoa 2-3 ký tự không có trong từ điển: đọc từng chữ cái kiểu
   Việt (FPT -> "ép pê tê"); câu viết HOA toàn bộ (kiểu nhấn mạnh) thì không đánh vần.

Hàm thuần, không I/O ngoài lần đọc từ điển đầu tiên (cache theo process, vài KB).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

DICT_PATH = Path(__file__).resolve().parent.parent / "assets" / "tts_viet_tat.yaml"

_DIGITS = ("không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín")
_GROUPS = ("", "nghìn", "triệu")
# Số nguyên dài hơn chừng này chữ số thì đọc từng chữ số (số tài khoản, mã đơn).
_MAX_INT_DIGITS = 15
_W = r"[^\W_]"  # 1 chữ/số (kể cả chữ có dấu)


# ─── Đọc số ──────────────────────────────────────────────────────────────────


def _two_digits(tens: int, unit: int, *, after_hundred: bool) -> list[str]:
    if tens == 0:
        if unit == 0:
            return []
        return ["linh", _DIGITS[unit]] if after_hundred else [_DIGITS[unit]]
    words = ["mười"] if tens == 1 else [_DIGITS[tens], "mươi"]
    if unit == 0:
        return words
    if unit == 5:
        return [*words, "lăm"]
    if unit == 1 and tens >= 2:
        return [*words, "mốt"]
    if unit == 4 and tens >= 2:
        return [*words, "tư"]
    return [*words, _DIGITS[unit]]


def _triple(value: int, *, full: bool) -> list[str]:
    """0..999; ``full`` = nhóm đứng sau nhóm lớn hơn (đọc "không trăm", "linh")."""
    hundreds, rest = divmod(value, 100)
    tens, unit = divmod(rest, 10)
    words: list[str] = []
    if hundreds or full:
        words += [_DIGITS[hundreds], "trăm"]
    words += _two_digits(tens, unit, after_hundred=bool(hundreds or full))
    return words


def _below_billion(value: int, *, full: bool) -> list[str]:
    groups: list[int] = []
    while value:
        value, part = divmod(value, 1000)
        groups.append(part)
    words: list[str] = []
    for index in range(len(groups) - 1, -1, -1):
        part = groups[index]
        if part == 0:
            continue
        lead = full or index < len(groups) - 1
        words += _triple(part, full=lead)
        if _GROUPS[index]:
            words.append(_GROUPS[index])
    return words


def read_int(value: int) -> str:
    """Số nguyên -> chữ: 1005 -> "một nghìn không trăm linh năm", 21 -> "hai mươi mốt"."""
    if value < 0:
        return "âm " + read_int(-value)
    if value == 0:
        return "không"
    billions, rest = divmod(value, 1_000_000_000)
    words: list[str] = []
    if billions:
        words += read_int(billions).split() + ["tỷ"]
    if rest:
        words += _below_billion(rest, full=bool(billions))
    return " ".join(words)


def read_digits(text: str) -> str:
    return " ".join(_DIGITS[int(ch)] for ch in text if ch.isdigit())


def _fraction(digits: str) -> str:
    """Phần sau dấu phẩy: "5" -> "năm", "25" -> "hai mươi lăm", "05"/"125" -> từng chữ số."""
    digits = digits.rstrip("0") or "0"
    if len(digits) <= 2 and not digits.startswith("0"):
        return read_int(int(digits))
    return read_digits(digits)


def parse_number(raw: str) -> tuple[str, str] | None:
    """Chuỗi số kiểu Việt/Anh -> (phần nguyên, phần thập phân) dạng chữ số.

    "1.299.000" / "1,299,000" -> ("1299000", ""); "25,5" -> ("25", "5"); "1.5" ->
    ("1", "5"); "1.500" (đúng 3 chữ số sau dấu chấm) -> nghìn. Dấu phẩy đứng 1 mình
    luôn là thập phân (quy ước tiếng Việt)."""
    raw = raw.strip()
    if not raw or not raw[0].isdigit():
        return None
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", raw):
        whole, _, frac = raw.partition(",")
        return whole.replace(".", ""), frac
    if re.fullmatch(r"\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?", raw):
        whole, _, frac = raw.partition(".")
        return whole.replace(",", ""), frac
    if re.fullmatch(r"\d+,\d+", raw):
        whole, frac = raw.split(",")
        return whole, frac
    if re.fullmatch(r"\d+\.\d+", raw):
        whole, frac = raw.split(".")
        return whole, frac
    if raw.isdigit():
        return raw, ""
    return None


def read_number(raw: str) -> str:
    """Đọc 1 chuỗi số (có thể có dấu nghìn/thập phân); không phải số thì trả nguyên."""
    parsed = parse_number(raw)
    if parsed is None:
        return raw
    whole, frac = parsed
    if (len(whole) > 1 and whole.startswith("0") and not frac) or len(whole) > _MAX_INT_DIGITS:
        return read_digits(whole)
    spoken = read_int(int(whole))
    if frac and frac.strip("0"):
        spoken += " phẩy " + _fraction(frac)
    return spoken


_NUM = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"


def _scaled(raw: str, scale: int, unit: str) -> str:
    """ "150" + nghìn -> "một trăm năm mươi nghìn"; "1,5" + triệu -> "một triệu năm
    trăm nghìn" (số lẻ quy ra giá trị thật thay vì đọc "phẩy")."""
    parsed = parse_number(raw)
    if parsed is None:
        return raw
    whole, frac = parsed
    if not frac.strip("0") or len(whole) > _MAX_INT_DIGITS:
        return f"{read_number(raw)} {unit}"
    try:
        value = round(float(f"{whole}.{frac}") * scale)
    except ValueError:
        return f"{read_number(raw)} {unit}"
    return read_int(value)


# ─── Từ điển ─────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _dictionary() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    try:
        import yaml

        with DICT_PATH.open(encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}
    except Exception:
        logger.warning("Không đọc được %s - bỏ qua bước đọc viết tắt.", DICT_PATH, exc_info=True)
        data = {}

    def section(name: str) -> dict[str, str]:
        raw = data.get(name) or {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    return section("chinh_xac"), section("khong_phan_biet"), section("chu_cai")


@lru_cache(maxsize=1)
def _keep_words() -> frozenset[str]:
    """Từ in hoa đọc nguyên (không đánh vần): "ANH ơi" không thành "a en hát"."""
    try:
        import yaml

        with DICT_PATH.open(encoding="utf-8") as file:
            raw = (yaml.safe_load(file) or {}).get("doc_nguyen") or []
    except Exception:
        raw = []
    return frozenset(str(w).upper() for w in raw)


def _word_pattern(keys: list[str], flags: int = 0) -> re.Pattern[str] | None:
    if not keys:
        return None
    alternatives = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    return re.compile(rf"(?<!{_W})(?:{alternatives})(?!{_W})", flags)


@lru_cache(maxsize=1)
def _patterns() -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
    exact, loose, _ = _dictionary()
    return _word_pattern(list(exact)), _word_pattern(list(loose), re.IGNORECASE)


def reload_dictionary() -> None:
    _dictionary.cache_clear()
    _patterns.cache_clear()
    _keep_words.cache_clear()


# ─── Các bước ────────────────────────────────────────────────────────────────

_SYMBOLS = (
    ("▲", " tăng "),
    ("▼", " giảm "),
    ("±", " cộng trừ "),
    ("≥", " lớn hơn hoặc bằng "),
    ("≤", " nhỏ hơn hoặc bằng "),
    ("≈", " khoảng "),
    ("&", " và "),
    ("…", "."),
)
_INDEX_RE = (
    (re.compile(r"(?<!\w)vn[\s\-]?index(?!\w)", re.IGNORECASE), "vê en in đéc"),
    (re.compile(r"(?<!\w)vn[\s\-]?30(?!\w)", re.IGNORECASE), "vê en ba mươi"),
    (re.compile(r"(?<!\w)hnx[\s\-]?index(?!\w)", re.IGNORECASE), "hát en ích in đéc"),
    (re.compile(r"(?<!\w)hnx[\s\-]?30(?!\w)", re.IGNORECASE), "hát en ích ba mươi"),
    (re.compile(r"(?<!\w)upcom[\s\-]?index(?!\w)", re.IGNORECASE), "úp com in đéc"),
)

_ISO_DATE_RE = re.compile(r"(?<![\d/])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d/])")
_FULL_DATE_RE = re.compile(r"(?<![\d/.,])(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?![\d/])")
_DAY_MONTH_RE = re.compile(r"(?<![\d/.,])(\d{1,2})/(\d{1,2})(?![\d/])")
_MONTH_YEAR_RE = re.compile(r"(?<![\d/.,])(\d{1,2})/(\d{4})(?![\d/])")
_TIME_HM_RE = re.compile(
    r"(?<![\d.,])(\d{1,2})\s?(?:h|g|:)\s?(\d{2})(?:\s?(?:p|ph|phút)(?!\w))?(?![\d:])"
)
_TIME_H_RE = re.compile(r"(?<![\d.,])(\d{1,2})\s?h(?!\w)")
_RATIO_RE = re.compile(r"(?<![\d.,])(\d+):(\d+)(?![\d:])")


def _prefix_free(text: str, start: int, word: str) -> bool:
    """Ngay trước vị trí ``start`` KHÔNG phải chữ ``word`` (tránh "ngày ngày 26")."""
    return not text[:start].rstrip().lower().endswith(word)


def _dates(text: str) -> str:
    def iso(m: re.Match[str]) -> str:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return m.group(0)
        head = "ngày " if _prefix_free(m.string, m.start(), "ngày") else ""
        return f"{head}{read_int(d)} tháng {read_int(mo)} năm {read_int(y)}"

    def full(m: re.Match[str]) -> str:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return m.group(0)
        year = int(y) + 2000 if len(y) == 2 else int(y)
        head = "ngày " if _prefix_free(m.string, m.start(), "ngày") else ""
        return f"{head}{read_int(d)} tháng {read_int(mo)} năm {read_int(year)}"

    def month_year(m: re.Match[str]) -> str:
        mo, y = int(m.group(1)), int(m.group(2))
        if not 1 <= mo <= 12:
            return m.group(0)
        head = "tháng " if _prefix_free(m.string, m.start(), "tháng") else ""
        return f"{head}{read_int(mo)} năm {read_int(y)}"

    def day_month(m: re.Match[str]) -> str:
        d, mo = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return m.group(0)
        head = "ngày " if _prefix_free(m.string, m.start(), "ngày") else ""
        return f"{head}{read_int(d)} tháng {read_int(mo)}"

    text = _ISO_DATE_RE.sub(iso, text)
    text = _FULL_DATE_RE.sub(full, text)
    text = _MONTH_YEAR_RE.sub(month_year, text)
    return _DAY_MONTH_RE.sub(day_month, text)


def _times(text: str) -> str:
    def hm(m: re.Match[str]) -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 24 or mi > 59:
            return m.group(0)
        spoken = f"{read_int(h)} giờ"
        return f"{spoken} {read_int(mi)} phút" if mi else spoken

    def hour(m: re.Match[str]) -> str:
        h = int(m.group(1))
        return f"{read_int(h)} giờ" if h <= 24 else m.group(0)

    def ratio(m: re.Match[str]) -> str:
        return f"{read_number(m.group(1))} trên {read_number(m.group(2))}"

    text = _TIME_HM_RE.sub(hm, text)
    text = _TIME_H_RE.sub(hour, text)
    return _RATIO_RE.sub(ratio, text)


_MONEY_TAIL_RE = re.compile(
    rf"(?<![\w.,])({_NUM})\s?(tr|triệu|trieu|tỷ|tỉ|ty)(\d{{1,3}})?(?!{_W})", re.IGNORECASE
)
_THOUSAND_RE = re.compile(rf"(?<![\w.,])({_NUM})\s?(k|K|nghìn|ngàn)(?!{_W})")
_CURRENCY_RE = re.compile(
    rf"(?<![\w.,])({_NUM})\s?(đồng|đ|vnđ|vnd|VNĐ|VND|usd|USD|\$|đô la|đô)(?!{_W})"
)
_DOLLAR_PREFIX_RE = re.compile(rf"\$\s?({_NUM})(?![\d])")
_PERCENT_RE = re.compile(rf"({_NUM})\s?%")
_UNITS = (
    ("km/h", "ki lô mét trên giờ"),
    ("km²", "ki lô mét vuông"),
    ("km2", "ki lô mét vuông"),
    ("m²", "mét vuông"),
    ("m2", "mét vuông"),
    ("m³", "mét khối"),
    ("m3", "mét khối"),
    ("°C", "độ xê"),
    ("°F", "độ ép"),
    ("°", "độ"),
    ("km", "ki lô mét"),
    ("kg", "ki lô gam"),
    ("cm", "xen ti mét"),
    ("mm", "mi li mét"),
    ("ml", "mi li lít"),
    ("mg", "mi li gam"),
    ("ha", "héc ta"),
    ("GB", "ghi ga bai"),
    ("MB", "mê ga bai"),
    ("kWh", "ki lô oát giờ"),
    ("kW", "ki lô oát"),
    ("m", "mét"),
)
_UNIT_RE = re.compile(
    rf"(?<![\w.,])({_NUM})\s?(" + "|".join(re.escape(u) for u, _ in _UNITS) + rf")(?!{_W}|[²³])"
)
_UNIT_WORDS = dict(_UNITS)
# Khoảng "10-20", "1,5-2%": giữ chữ số (bước sau đọc kèm đơn vị). Không áp cho số
# bắt đầu bằng 0 (số điện thoại "0901-234-567").
_RANGE_RE = re.compile(r"(?<![\w.,])([1-9][\d.,]*|\d)\s?[-–]\s?([1-9][\d.,]*|\d)(?![\w.,])")
_SIGNED_RE = re.compile(r"(?<![\w.,])([+\-−])\s?(?=\d)")
_PLAIN_NUM_RE = re.compile(rf"(?<![\d.,])({_NUM})(?![\d])")
_BILLION = {"tỷ", "tỉ", "ty"}


def _money_and_units(text: str) -> str:
    def tail(m: re.Match[str]) -> str:
        is_billion = m.group(2).lower() in _BILLION
        scale = 1_000_000_000 if is_billion else 1_000_000
        if m.group(3):
            # "1tr2" = 1,2 triệu; "2ty5" = 2,5 tỷ; "1tr250" = 1.250.000.
            sub = m.group(3).ljust(3, "0")
            parsed = parse_number(m.group(1))
            whole = int(parsed[0]) if parsed else 0
            return read_int(whole * scale + int(sub) * (scale // 1000))
        return _scaled(m.group(1), scale, "tỷ" if is_billion else "triệu")

    def thousand(m: re.Match[str]) -> str:
        return _scaled(m.group(1), 1000, "nghìn")

    def currency(m: re.Match[str]) -> str:
        unit = m.group(2).lower()
        word = "đô la" if unit in ("usd", "$", "đô la", "đô") else "đồng"
        return f"{read_number(m.group(1))} {word}"

    def unit(m: re.Match[str]) -> str:
        return f"{read_number(m.group(1))} {_UNIT_WORDS[m.group(2)]}"

    text = _RANGE_RE.sub(lambda m: f"{m.group(1)} đến {m.group(2)}", text)
    text = _SIGNED_RE.sub(lambda m: " cộng " if m.group(1) == "+" else " âm ", text)
    text = _MONEY_TAIL_RE.sub(tail, text)
    text = _THOUSAND_RE.sub(thousand, text)
    text = _DOLLAR_PREFIX_RE.sub(lambda m: f"{read_number(m.group(1))} đô la", text)
    text = _CURRENCY_RE.sub(currency, text)
    text = _PERCENT_RE.sub(lambda m: f"{read_number(m.group(1))} phần trăm", text)
    text = _UNIT_RE.sub(unit, text)

    def plain(m: re.Match[str]) -> str:
        # Số dính sau chữ ("S24", "iPhone15"): tách 1 khoảng cho bộ đọc.
        glued = m.start() > 0 and m.string[m.start() - 1].isalpha()
        return (" " if glued else "") + read_number(m.group(1))

    return _PLAIN_NUM_RE.sub(plain, text)


_ACRONYM_RE = re.compile(r"(?<![\w])[A-ZĐ]{2,3}(?![\w])")
_UPPER_WORD_RE = re.compile(r"[^\W\d_]{2,}")


def _shouting(line: str) -> bool:
    """Câu viết HOA gần hết (nhấn mạnh) - không đánh vần từng từ in hoa trong đó."""
    words = _UPPER_WORD_RE.findall(line)
    if len(words) < 3:
        return False
    return sum(1 for w in words if w.isupper()) / len(words) >= 0.6


def _abbreviations(text: str) -> str:
    exact, loose, letters = _dictionary()
    keep = _keep_words()
    exact_re, loose_re = _patterns()
    if exact_re is not None:
        text = exact_re.sub(lambda m: exact.get(m.group(0), m.group(0)), text)
    if loose_re is not None:
        lowered = {k.lower(): v for k, v in loose.items()}
        text = loose_re.sub(lambda m: lowered.get(m.group(0).lower(), m.group(0)), text)
    if letters:
        lines = []
        for line in text.split("\n"):
            if not _shouting(line):
                line = _ACRONYM_RE.sub(
                    lambda m: (
                        m.group(0)
                        if m.group(0) in keep
                        else " ".join(letters.get(ch, ch) for ch in m.group(0))
                    ),
                    line,
                )
            lines.append(line)
        text = "\n".join(lines)
    return text


def normalize(text: str) -> str:
    """Chữ đã lọc (voice_reply.speakable_text) -> chữ đọc được thành tiếng."""
    text = unicodedata.normalize("NFC", text or "")
    for symbol, spoken in _SYMBOLS:
        text = text.replace(symbol, spoken)
    for pattern, spoken in _INDEX_RE:
        text = pattern.sub(spoken, text)
    text = _dates(text)
    text = _times(text)
    text = _money_and_units(text)
    text = _abbreviations(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" +([.,!?;:])", r"\1", text)
    return text.strip()
