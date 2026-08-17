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


def relevant_news_impact(items: list[tuple[str, float]], symbol: str) -> float:
    """news_impact chỉ được tính trên tin CÓ NHẮC ĐÚNG MÃ.

    providers.fetch_news tìm Google News bằng chuỗi "<mã> cổ phiếu" nhưng
    không kiểm tra mã có trong tiêu đề, nên tin thị trường chung hoặc tin
    của mã khác vẫn lọt vào. Nếu lấy trung bình sentiment MỌI tin, cảm xúc
    của tin không liên quan chảy thẳng vào PolicyInputs.news_impact, tức là
    ảnh hưởng trực tiếp tới khuyến nghị mua/bán bằng tiền thật.
    """
    relevant = [score for title, score in items if title_mentions_symbol(title, symbol)]
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

_DISCLAIMER_HINTS = (
    "chỉ là tham khảo",
    "chỉ mang tính tham khảo",
    "không phải khuyến nghị",
    "không phải là khuyến nghị",
    "tự chịu trách nhiệm",
    "quyết định cuối cùng",
    "cân nhắc kỹ trước khi",
    "khuyến nghị đầu tư",
)

# Đoạn dài hơn ngưỡng này được coi là nội dung phân tích thật, không phải
# disclaimer - tránh xoá oan một đoạn lập luận chỉ vì có chữ "tham khảo".
_DISCLAIMER_MAX_LEN = 400


def _is_disclaimer(block: str) -> bool:
    if len(block) > _DISCLAIMER_MAX_LEN:
        return False
    lowered = block.lower()
    return any(hint in lowered for hint in _DISCLAIMER_HINTS)


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
