"""Chuẩn hoá cách TRÌNH BÀY số liệu trong báo cáo phân tích cổ phiếu.

Module thuần hàm, không I/O, không phụ thuộc provider - để test trực tiếp
từng lỗi trình bày đã từng lọt tới người dùng thật.

Nguyên tắc: module này KHÔNG thay đổi bất kỳ con số nào do stock/policy.py
quyết định (stop, target, tỷ trọng, R:R). Nó chỉ quyết định số đó được viết
ra như thế nào và kèm cảnh báo gì.
"""

import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Mốc xa hơn ngưỡng này không còn dùng được làm điểm vào/ra ngắn hạn nữa.
NEAR_LEVEL_MAX_PCT = 7.0


def _vn_number(text: str) -> str:
    """Đổi dấu phân cách kiểu Anh sang kiểu Việt Nam."""
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_price(value: float | None) -> str:
    """Giá/khối lượng: dấu chấm phân cách nghìn, không phần thập phân."""
    if value is None:
        return "N/A"
    return f"{value:,.0f}".replace(",", ".")


def fmt_number(value: float | None, decimals: int = 2) -> str:
    """Số thập phân theo chuẩn VN: 1.234,56 thay vì 1,234.56."""
    if value is None:
        return "N/A"
    return _vn_number(f"{value:,.{decimals}f}")


def fmt_pct(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{fmt_number(value, decimals)}%"


def fmt_signed_pct(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else ""
    return f"{sign}{fmt_number(value, decimals)}%"


def level_distance_pct(price: float | None, level: float | None) -> float | None:
    """Mốc cách giá hiện tại bao nhiêu phần trăm (dương = nằm trên giá)."""
    if not price or price <= 0 or level is None:
        return None
    return round((level - price) / price * 100, 2)


def format_level(price: float | None, level: float | None, touches: int | None = None) -> str:
    """Một mốc giá LUÔN kèm khoảng cách % so với giá hiện tại.

    Báo cáo FPT/GEX từng nêu "hỗ trợ 70.370", "kháng cự 31.900" như nhau,
    dù một mốc cách giá 1,6% và mốc kia cách tới 28%. Thiếu khoảng cách %,
    người đọc không thể biết mốc nào dùng được để vào/ra lệnh.
    """
    if level is None:
        return "chưa xác định"
    text = f"{fmt_price(level)} VND"
    dist = level_distance_pct(price, level)
    if dist is not None:
        text += f" ({fmt_signed_pct(dist)} so với giá hiện tại)"
    if touches:
        text += f", {touches} lần test"
    return text


def _nearest(
    levels: list[tuple[float, int]],
    price: float,
    above: bool,
) -> tuple[float, int] | None:
    candidates = [lv for lv in levels if (lv[0] > price if above else lv[0] < price)]
    if not candidates:
        return None
    return min(candidates, key=lambda lv: abs(lv[0] - price))


def _level_note(dist: float, atr_pct: float | None) -> str:
    """Hai cảnh báo trái ngược nhau cho cùng một mốc.

    Quá xa: không dùng được làm điểm vào/ra ngắn hạn. Quá gần so với biên
    động một phiên (ATR): rất dễ bị xuyên qua bởi nhiễu, không phải tín
    hiệu đổi xu hướng.
    """
    if abs(dist) > NEAR_LEVEL_MAX_PCT:
        return (
            f" - cách quá xa (>{fmt_number(NEAR_LEVEL_MAX_PCT, 0)}%), "
            "KHÔNG dùng làm điểm vào/ra ngắn hạn"
        )
    if atr_pct and abs(dist) < atr_pct:
        return (
            f" - nằm trong biên nhiễu một phiên (ATR {fmt_pct(atr_pct)}), "
            "dễ bị xuyên qua mà chưa đổi xu hướng"
        )
    return ""


def nearest_levels_line(
    price: float,
    supports: list[tuple[float, int]],
    resistances: list[tuple[float, int]],
    atr_pct: float | None = None,
) -> str:
    """Dòng riêng cho MỐC GẦN NHẤT hai phía, kèm cảnh báo dùng được hay không.

    GEX ngày 05/08/2026: kháng cự gần nhất cách tới +28% nhưng vẫn được
    trình bày như vùng chốt lọi khả thi trong ngắn hạn.
    """
    parts = []
    near_support = _nearest(supports, price, above=False)
    near_resistance = _nearest(resistances, price, above=True)
    if near_support:
        dist = level_distance_pct(price, near_support[0])
        note = _level_note(dist, atr_pct) if dist is not None else ""
        level_text = format_level(price, near_support[0], near_support[1])
        parts.append(f"Hỗ trợ gần nhất {level_text}{note}")
    else:
        parts.append("Chưa có mốc hỗ trợ swing nào dưới giá hiện tại")
    if near_resistance:
        dist = level_distance_pct(price, near_resistance[0])
        note = _level_note(dist, atr_pct) if dist is not None else ""
        level_text = format_level(price, near_resistance[0], near_resistance[1])
        parts.append(f"Kháng cự gần nhất {level_text}{note}")
    else:
        parts.append("Chưa có mốc kháng cự swing nào trên giá hiện tại")
    prefix = "MỐC GẦN NHẤT (dùng mốc này khi nói về điểm vào/ra): "
    return prefix + " | ".join(parts)


def macd_strength_line(macd_line: float, histogram: float, price: float | None) -> str:
    """MACD quy theo % giá.

    MACD tuyệt đối không so sánh được giữa các mã: histogram +24,44 trên cổ
    phiếu 13.950đ chỉ là 0,18% giá, gần như không đáng kể, nhưng đọc số trơ
    thì dễ tưởng là tín hiệu đảo chiều thật (ca CII ngày 05/08/2026).
    """
    if not price or price <= 0:
        return (
            f"MACD: line {fmt_number(macd_line)}, histogram "
            f"{fmt_number(histogram)} (chưa quy đổi được theo % giá)"
        )
    hist_pct = abs(histogram) / price * 100
    if hist_pct < 0.2:
        strength = "rất yếu, gần như không đáng kể"
    elif hist_pct < 0.5:
        strength = "yếu"
    elif hist_pct < 1.5:
        strength = "trung bình"
    else:
        strength = "mạnh"
    if histogram > 0:
        direction = "dương"
    elif histogram < 0:
        direction = "âm"
    else:
        direction = "cân bằng"
    return (
        f"MACD: histogram {fmt_number(histogram)} = {fmt_pct(hist_pct)} "
        f"giá hiện tại, {direction}, {strength}"
    )


def adx_direction_line(
    adx: float,
    di_plus: float,
    di_minus: float,
    trending: bool,
    available: bool = True,
) -> str:
    """ADX BẮT BUỘC kèm hướng.

    ADX chỉ đo độ mạnh, không đo hướng. Báo cáo FPT từng viết "ADX 43,7 -
    xu hướng tương đối mạnh" mà không nói mạnh theo chiều nào, người đọc
    mặc định hiểu là chiều tăng - rất dễ dẫn tới mua vào một mã đang giảm
    mạnh.
    """
    if not available:
        return "ADX: chưa đủ dữ liệu H/L thật để tính"
    if di_plus > di_minus:
        bias = "nghiêng TĂNG (+DI > -DI)"
    elif di_minus > di_plus:
        bias = "nghiêng GIẢM (-DI > +DI)"
    else:
        bias = "chưa rõ hướng (+DI xấp xỉ -DI)"
    strength = "xu hướng rõ" if trending else "sideway, chưa thành xu hướng"
    return (
        f"ADX {fmt_number(adx, 1)}: {strength}, {bias} "
        f"(+DI {fmt_number(di_plus, 1)} vs -DI {fmt_number(di_minus, 1)})"
    )


def title_mentions_symbol(title: str, symbol: str) -> bool:
    """Tiêu đề tin có nhắc ĐÚNG mã này không (so khớp theo biên từ)."""
    if not title or not symbol:
        return False
    pattern = rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])"
    return bool(re.search(pattern, title, re.IGNORECASE))


def is_news_relevant(title: str, symbol: str, confirmed: bool | None = None) -> bool:
    """Tin chính chủ đã xác nhận được ưu tiên; nguồn crawl fallback theo title."""
    return bool(confirmed) if confirmed is not None else title_mentions_symbol(title, symbol)


def relevant_news_impact(items: list[tuple], symbol: str) -> float:
    """news_impact chỉ dùng tin thực sự liên quan đúng mã.

    Mỗi item nhận ``(title, score)`` hoặc ``(title, score, confirmed)``. Tin
    VCI/company-news đã gắn ``confirmed=True`` vẫn được tính dù tiêu đề không
    chứa ticker; Google News chưa xác nhận tiếp tục phải match ticker ở title.
    """
    relevant = []
    for item in items:
        if len(item) < 2:
            continue
        title, score = item[0], item[1]
        confirmed = item[2] if len(item) >= 3 else None
        if is_news_relevant(title, symbol, confirmed):
            relevant.append(score)
    if not relevant:
        return 0.0
    avg = sum(relevant) / len(relevant)
    return round(max(-2.0, min(2.0, avg * math.log(len(relevant) + 1))), 2)


def fmt_news_date(raw: str) -> str:
    """Ngày đăng tin theo dd/mm/yyyy để không trích tin cũ như tin mới."""
    if not raw:
        return ""
    text = raw.strip()
    patterns = ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
    for pattern in patterns:
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(_VN_TZ)
        return parsed.strftime("%d/%m/%Y")
    return text


# Những thói quen văn phong làm loãng báo cáo tiền thật: tự giới thiệu lại ở
# mọi tin nhắn, danh xưng thân mật quá đà, và câu "nhiệm vụ của em xong rồi".
_SELF_INTRO_RE = re.compile(
    r"^\s*(anh|chị)\s*ơi[,!\s]*em\s+lan\s+anh\s+đây\s*(ạ)?\s*[!.,]*\s*",
    re.IGNORECASE,
)
_PET_NAME_RE = re.compile(r"\s*,?\s*anh\s+yêu\b", re.IGNORECASE)
_TASK_DONE_RE = re.compile(
    r"[^\n]*nhiệm\s+vụ[^\n]*(xong|hoàn thành)[^\n]*\n?",
    re.IGNORECASE,
)

# Cụm từ MẠNH chỉ xuất hiện trong câu disclaimer. Bản cũ còn tính cả cụm
# lỏng ("cân nhắc kỹ trước khi", "quyết định cuối cùng", "khuyến nghị đầu
# tư") nên 1 câu kết luận kiểu "MUA FPT, anh cân nhắc kỹ trước khi vào lệnh"
# bị coi là disclaimer: đoạn phân tích phía sau chứa "khuyến nghị đầu tư" bị
# XOÁ, còn disclaimer chuẩn thì không được thêm.
_DISCLAIMER_HINTS = (
    "chỉ là tham khảo",
    "chỉ mang tính tham khảo",
    "mang tính tham khảo",
    "không phải khuyến nghị",
    "không phải là khuyến nghị",
    "không phải lời khuyên",
    "tự chịu trách nhiệm",
    "không đảm bảo kết quả",
    "trong quá khứ không đảm bảo",
)
# Đoạn chứa từ hành động/kế hoạch hoặc chữ số là NỘI DUNG phân tích, không
# bao giờ bị coi là disclaimer thuần (không bao giờ bị xoá).
_ANALYSIS_WORDS_RE = re.compile(
    r"(?<!\w)(mua|bán|giữ|gom|đứng ngoài|theo dõi|cắt lỗ|chốt lời|kết luận|vùng|stop|target|"
    r"buy|sell|hold|watch|no_trade|tỷ trọng|r:r)(?!\w)",
    re.IGNORECASE,
)

# Disclaimer CỐ ĐỊNH, không phụ thuộc LLM diễn giải lại mỗi lần. Giữ câu
# chữ trung tính vì backtest là dữ liệu TÙY CHỌN: runtime Render hiện chặn
# heavy backtest và báo cáo chỉ được nhắc backtest khi có thống kê thật cho
# đúng setup. clean_analysis_output() lo việc LLM tự thêm/lặp disclaimer;
# ensure_disclaimer() bên dưới LUÔN đặt đúng 1 dòng chuẩn này ở cuối.
STANDARD_DISCLAIMER = (
    "Thông tin trên chỉ mang tính tham khảo, không phải khuyến nghị đầu tư; "
    "quyết định cuối cùng và rủi ro thuộc về anh nha."
)

# Đoạn dài hơn ngưỡng này được coi là nội dung phân tích thật, không phải
# disclaimer - tránh xoá oan một đoạn lập luận chỉ vì có chữ "tham khảo".
_DISCLAIMER_MAX_LEN = 300


def _is_disclaimer(block: str) -> bool:
    """Đoạn THUẦN disclaimer: ngắn, có cụm disclaimer mạnh, không có chữ số
    và không có từ hành động/kế hoạch. Chỉ đoạn thuần mới được gộp/xoá."""
    if len(block) > _DISCLAIMER_MAX_LEN:
        return False
    if any(ch.isdigit() for ch in block):
        return False
    lowered = block.lower()
    if not any(hint in lowered for hint in _DISCLAIMER_HINTS):
        return False
    return not _ANALYSIS_WORDS_RE.search(block)


def clean_analysis_output(text: str) -> str:
    """Làm sạch đầu ra LLM trước khi gửi cho người dùng.

    Prompt đã yêu cầu những điều này, nhưng LLM không ổn định: cùng một
    prompt, báo cáo FPT lặp disclaimer hai lần còn CII thì không. Đây là lớp
    chặn cuối, không phụ thuộc vào việc LLM có tuân lệnh hay không.
    """
    if not text:
        return ""
    cleaned = _SELF_INTRO_RE.sub("", text.strip(), count=1)
    cleaned = _PET_NAME_RE.sub("", cleaned)
    cleaned = _TASK_DONE_RE.sub("", cleaned)
    blocks = re.split(r"\n\s*\n", cleaned)
    result: list[str] = []
    seen_disclaimer = False
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        if _is_disclaimer(stripped):
            if seen_disclaimer:
                continue
            seen_disclaimer = True
        result.append(stripped)
    return "\n\n".join(result).strip()


def ensure_disclaimer(text: str) -> str:
    """LUÔN kết thúc báo cáo bằng đúng 1 dòng STANDARD_DISCLAIMER.

    Gọi SAU clean_analysis_output(). Các đoạn disclaimer THUẦN do LLM tự viết
    (xem _is_disclaimer - tiêu chí chặt, không bao giờ khớp đoạn phân tích)
    được thay bằng dòng chuẩn để không lặp; mọi đoạn nội dung khác giữ nguyên.
    Idempotent: gọi nhiều lần vẫn chỉ có 1 dòng chuẩn ở cuối.
    """
    if not text:
        return text
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    kept = [b for b in blocks if b != STANDARD_DISCLAIMER and not _is_disclaimer(b)]
    if not kept:
        return STANDARD_DISCLAIMER
    return "\n\n".join(kept) + "\n\n" + STANDARD_DISCLAIMER


# ─── Kiểm tra bản diễn giải LLM với Decision của policy ─────────────────────
# Hợp đồng "action và mọi con số do stock/policy.py chốt" trước đây chỉ là
# câu chữ trong prompt. Các hàm dưới đây là lớp chặn TRONG CODE: phát hiện
# nhãn hành động mâu thuẫn, ngôn ngữ kế hoạch giao dịch ở chế độ chỉ tham
# khảo, và mốc giá không có trong dữ liệu hệ thống đã cấp.

_ACTION_LABEL_PATTERNS: dict[str, tuple[str, ...]] = {
    # Nhãn VIẾT HOA (cách LLM nêu kết luận) + tên action tiếng Anh.
    "BUY": (r"(?<![\w])BUY(?![\w])", r"(?<![\w])MUA(?![\w])"),
    "SELL": (r"(?<![\w])SELL(?![\w])", r"(?<![\w])BÁN(?![\w])"),
    "HOLD": (r"(?<![\w])HOLD(?![\w])", r"(?<![\w])GIỮ(?![\w])"),
    "WATCH": (r"(?<![\w])WATCH(?![\w])", r"(?<![\w])THEO DÕI(?![\w])"),
    "NO_TRADE": (r"(?<![\w])NO_TRADE(?![\w])", r"(?<![\w])ĐỨNG NGOÀI(?![\w])"),
}
# Khuyến nghị bằng chữ thường - chỉ các cụm rõ nghĩa "khuyên làm".
_ADVICE_PATTERNS: dict[str, tuple[str, ...]] = {
    "BUY": (r"(?<!\w)nên (?:mua|gom|giải ngân|vào lệnh)(?!\w)", r"(?<!\w)canh mua(?!\w)"),
    # Không gồm "nên cắt lỗ/chốt lời": kế hoạch BUY/HOLD hợp lệ luôn có câu
    # "nếu thủng stop thì nên cắt lỗ", "chốt 1/2 tại T1".
    "SELL": (r"(?<!\w)nên (?:bán|thoát hàng|thoát vị thế)(?!\w)",),
}
_NEGATION_WORDS = ("không", "chưa", "đừng", "tránh", "chẳng", "not ", "no ", "chớ")
# Câu điều kiện ("nếu thủng 48.500 thì nên bán") là kịch bản, không phải đổi
# action - chỉ bỏ qua khi kiểm action của bản ĐẦY ĐỦ; bản tham khảo cho thành
# viên vẫn cấm cả câu điều kiện có vùng giá/khuyến nghị.
_CONDITIONAL_WORDS = ("nếu", "khi ", "trường hợp", "kịch bản")
_NEGATION_WINDOW = 18
_CONDITIONAL_WINDOW = 60
_PLAN_LANGUAGE_RE = re.compile(
    r"(?<!\w)(vùng mua|điểm mua|giá mua|cắt lỗ|chốt lời|stop[- ]?loss|stop|target|"
    r"take profit|r:r|tỷ trọng (?:đề xuất|tham khảo|vị thế|giải ngân|mua)|% ?nav|"
    r"nên mua|nên bán|nên giữ|nên gom|nên cắt|nên chốt)(?!\w)",
    re.IGNORECASE,
)
_PRICE_NUMBER_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d{5,7})(?:,\d+)?(?![\d.])")


def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - _NEGATION_WINDOW) : start].lower()
    return any(word in window for word in _NEGATION_WORDS)


def _conditional(text: str, start: int) -> bool:
    """Cụm nằm trong câu điều kiện/kịch bản (tính từ đầu câu hiện tại)."""
    window = text[max(0, start - _CONDITIONAL_WINDOW) : start]
    # Chỉ tách câu ở dấu câu + khoảng trắng: "48.500" là số, không phải hết câu.
    sentence = re.split(r"[.!?](?:\s|$)|\n", window)[-1].lower()
    return any(word in sentence for word in _CONDITIONAL_WORDS)


def find_action_conflicts(text: str, allowed_actions: set[str] | frozenset[str]) -> list[str]:
    """Các nhãn/cụm khuyến nghị hành động trong `text` KHÔNG thuộc
    `allowed_actions` (bỏ qua chỗ bị phủ định: "không MUA đuổi", "chưa nên mua").
    Trả list mô tả ngắn (rỗng = không mâu thuẫn)."""
    if not text:
        return []
    conflicts: list[str] = []
    for action, patterns in _ACTION_LABEL_PATTERNS.items():
        if action in allowed_actions:
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                if not _negated(text, match.start()):
                    conflicts.append(f"{action}:{match.group(0)}")
    for action, patterns in _ADVICE_PATTERNS.items():
        if action in allowed_actions:
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                if not _negated(text, match.start()) and not _conditional(text, match.start()):
                    conflicts.append(f"{action}:{match.group(0)}")
    return conflicts


def find_plan_language(text: str) -> list[str]:
    """Ngôn ngữ kế hoạch giao dịch (vùng mua, stop, target, R:R, tỷ trọng,
    "nên mua/bán"...) - bị cấm ở báo cáo chỉ tham khảo cho thành viên."""
    if not text:
        return []
    return [m.group(0) for m in _PLAN_LANGUAGE_RE.finditer(text) if not _negated(text, m.start())]


def _normalize_price_token(token: str) -> int | None:
    digits = token.split(",")[0].replace(".", "")
    return int(digits) if digits.isdigit() else None


def price_numbers(text: str) -> set[int]:
    """Mọi số dạng giá/khối lượng (>= 1.000 viết kiểu VN, hoặc 5-7 chữ số liền)."""
    out: set[int] = set()
    for match in _PRICE_NUMBER_RE.finditer(text or ""):
        value = _normalize_price_token(match.group(1))
        if value is not None and value >= 1000:
            out.add(value)
    return out


def find_unknown_prices(text: str, allowed_text: str) -> list[int]:
    """Số dạng giá trong `text` không xuất hiện ở `allowed_text` (prompt đã cấp)."""
    allowed = price_numbers(allowed_text)
    return sorted(value for value in price_numbers(text) if value not in allowed)
