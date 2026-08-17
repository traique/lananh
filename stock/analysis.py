import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jinja2

from stock import backtest
from stock import features as feat
from stock import fundamentals
from stock import policy
from stock import price_adjust
from stock import providers
from stock import report_format as rfmt
from stock import sector
from stock import validation
import messages
from core import database as db
from stock.sector import ALL_KNOWN_SYMBOLS

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_CACHE_TTL_SEC = 15 * 60
_CACHE_MAX_ENTRIES = 200
_cache: dict[tuple[str, bool], tuple[float, str]] = {}

def _cache_get(symbol: str, holding: bool) -> str | None:
    entry = _cache.get((symbol, holding))
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > _CACHE_TTL_SEC:
        _cache.pop((symbol, holding), None)
        return None
    return value

def _cache_set(symbol: str, holding: bool, value: str) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest_key, None)
    _cache[(symbol, holding)] = (time.time(), value)

_SYMBOL_TOKEN_RE = re.compile(r"\b[A-Za-z]{3,4}\b")
_UPPERCASE_TOKEN_RE = re.compile(r"\b[A-Z]{3,4}\b")
_INDEX_NAME_RE = re.compile(r"\b(vn[\s\-]?index|vn[\s\-]?30|hnx[\s\-]?index|hnx[\s\-]?30|upcom[\s\-]?index)\b", re.IGNORECASE)

def _normalize_index_name(raw: str) -> str:
    return re.sub(r"[\s\-]", "", raw).upper()

_COMMON_WORD_EXCLUDE = {
    "ANH", "EM", "OI", "GIA", "CHO", "KHI", "NAY", "ROI", "NHE", "NHA",
    "VOI", "LA", "VA", "DO", "CO", "KO", "MA", "THE", "SAO", "VAY", "NAO",
    "LAM", "XEM", "GIO", "DUOC", "MOT", "HAI", "BA", "NAM", "NGAY", "TUAN",
    "OK", "CEO", "CFO", "CTO", "ATM", "PR", "FYI", "ASAP", "VIP", "FAQ",
    "TV", "PC", "AI", "US", "UK", "EU", "OS", "ID", "URL", "PDF", "CV", "OMG",
}

# Chỉ áp dụng cho token VIẾT THƯỜNG (xem detect_symbol_candidates). Tiếng Việt
# không dấu sinh ra khá nhiều token 3-4 ký tự vô hại; loại sẵn nhóm hay gặp
# nhất để đỡ tốn request verify tới DNSE. Tính đúng đắn vẫn do DNSE quyết
# định - list này thuần tuú là tối ưu, không phải rào chắn.
_LOWERCASE_NOISE_EXCLUDE = {
    "BAO", "HOM", "MAI", "QUA", "DANG", "CHUA", "HAY", "TOI", "MINH",
    "VAN", "CON", "THI", "MOI", "BAY", "MUA", "BAN", "GIU", "SAN",
}
_MAX_UNVERIFIED_PER_MSG = 6

_AMBIGUOUS_KNOWN = {"GAS", "VND", "HAG", "OIL"}
_STOCK_CONTEXT_KEYWORDS = ("cổ phiếu", "co phieu", "mã", "ma ", "phân tích", "phan tich", "cp ", " cp")


def _has_stock_context(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _STOCK_CONTEXT_KEYWORDS)


_BARE_SYMBOL_MSG_RE = re.compile(r"^[A-Za-z]{3,4}$")


def is_bare_symbol_message(text: str) -> bool:
    """Tin nhắn chỉ gồm ĐÚNG một token 3-4 chữ cái, không gì khác.

    Đây là tín hiệu tra mã mạnh nhất mà không cần tới cách viết hoa. Lý do
    phải tách riêng khỏi rào "viết HOA nguyên bản": bàn phím di động tự viết
    hoa chữ đầu tin nhắn, nên người dùng gõ "gvr" thì cái Telegram gửi đi là
    "Gvr" - trượt _UPPERCASE_TOKEN_RE, trượt luôn cả ngữ cảnh chứng khoán
    (tin nhắn có mỗi 3 chữ cái thì lấy đâu ra ngữ cảnh). Kết quả là đường
    nhập liệu phổ biến NHẤT lại là đường duy nhất không nhận ra mã, và câu
    hỏi rơi xuống Gemini để bị trả lời bằng giá bịa.

    Trong bối cảnh bot này (một người dùng duy nhất, chơi chứng khoán), một
    tin nhắn trơ trọi 3-4 chữ cái gần như chắc chắn là mã. Chi phí trường hợp
    xấu nhất là 1 request verify tới DNSE (đã cache 24h).
    """
    return bool(_BARE_SYMBOL_MSG_RE.match(text.strip()))


def detect_symbol_candidates(text: str) -> tuple[list[str], list[str]]:
    tokens = _SYMBOL_TOKEN_RE.findall(text)
    uppercase_tokens = set(_UPPERCASE_TOKEN_RE.findall(text))
    known, unverified = [], []
    seen = set()
    stock_context = _has_stock_context(text)
    bare_symbol_msg = is_bare_symbol_message(text)
    # Tin nhắn có tín hiệu rõ ràng là đang nói chuyện cổ phiếu ("cổ phiếu",
    # "mã", "phân tích"), đang hỏi giá, hoặc chỉ gồm đúng 1 token dạng mã
    # -> chấp nhận cả token viết thường làm ứng viên mã. _PRICE_KEYWORDS_RE
    # khai báo phía dưới trong module, chỉ resolve lúc gọi hàm nên không lỗi
    # thứ tự.
    allow_lowercase = stock_context or bool(_PRICE_KEYWORDS_RE.search(text)) or bare_symbol_msg

    for m in _INDEX_NAME_RE.finditer(text):
        norm = _normalize_index_name(m.group(0))
        if norm not in seen:
            seen.add(norm)
            known.append(norm)

    for tok in tokens:
        upper = tok.upper()
        if upper in seen: continue
        seen.add(upper)
        if upper in ALL_KNOWN_SYMBOLS or upper == "VNINDEX":
            if upper in _AMBIGUOUS_KNOWN and not (tok in uppercase_tokens or stock_context or bare_symbol_msg):
                # mã trùng từ tiếng Việt/tiếng Anh thông dụng ("gas", "vnd"...)
                # - chỉ nhận khi viết HOA nguyên bản, tin nhắn có keyword
                # chứng khoán, hoặc tin nhắn chỉ gồm đúng token đó; tránh bot
                # trả nhầm giá cổ phiếu cho câu hỏi không liên quan.
                # bare_symbol_msg là bắt buộc ở đây: "Oil" trơ trọi từng bị
                # loại rồi rơi xuống Gemini và bị trả lời bằng giá dầu Brent/WTI
                # bịa hoàn toàn, trong khi "OIL" lại ra đúng giá cổ phiếu PVOIL.
                continue
            known.append(upper)
        else:
            # nhóm unverified (cần verify qua DNSE). TRƯỚC ĐÂY nhóm này chỉ
            # nhận token viết HOA NGUYÊN BẢN, nên mọi mã không nằm trong
            # ALL_KNOWN_SYMBOLS mà người dùng gõ thường (vd "gvr") bị bỏ rơi
            # hoàn toàn - find_valid_symbols trả rỗng, câu hỏi rơi xuống
            # Gemini không kèm grounding và bị trả lời bằng giá bịa.
            # Nay token viết thường cũng được nhận, nhưng chỉ khi tin nhắn có
            # ngữ cảnh chứng khoán/hỏi giá/là tin nhắn trơ trọi dạng mã
            # (allow_lowercase) để token thường từ tiếng Việt không dấu không
            # tràn vào đây.
            is_upper = tok in uppercase_tokens
            if not (is_upper or allow_lowercase):
                continue
            if upper in _COMMON_WORD_EXCLUDE:
                continue
            if not is_upper and upper in _LOWERCASE_NOISE_EXCLUDE:
                continue
            unverified.append(upper)
    return known, unverified

async def find_valid_symbols(text: str, limit: int = 3) -> list[str]:
    known, unverified = detect_symbol_candidates(text)
    result = list(known)
    if len(result) < limit and unverified:
        to_check = [s for s in unverified if s not in result][:_MAX_UNVERIFIED_PER_MSG]
        checks = await asyncio.gather(*[providers.verify_symbol_exists(s) for s in to_check], return_exceptions=True)
        for sym, ok in zip(to_check, checks):
            if ok is True and sym not in result:
                result.append(sym)
                if len(result) >= limit: break
    return result[:limit]

# Từ khoá thể hiện RÕ RÀNG ý muốn được phân tích/tư vấn. Nhóm này tự nó đã đủ
# để kích hoạt pipeline phân tích đầy đủ.
_STRONG_ANALYSIS_KEYWORDS = [
    "phân tích", "phan tich", "kỹ thuật", "ky thuat", "cơ bản", "co ban",
    "đánh giá", "danh gia", "nhận định", "nhan dinh", "khuyến nghị",
    "khuyen nghi", "tư vấn", "tu van", "nên mua", "nen mua", "nên bán",
    "nen ban", "có nên", "co nen", "triển vọng", "trien vong", "review",
    "so sánh", "so sanh", "dự báo", "du bao", "xu hướng", "xu huong",
    "định giá", "dinh gia", "dòng tiền", "dong tien",
    "xủ lý sao", "xu ly sao", "cắt lỗ", "cat lo", "chốt lời", "chot loi",
    "giữ hay bán", "giu hay ban", "nên giữ", "nen giu",
]

# Từ khoá YẾU: đứng một mình chúng là ngôn ngữ tâm sự đời thường ("kệt xe",
# "giờ sao anh", "làm sao bây giờ") nên chỉ được coi là yêu cầu phân tích khi
# tin nhắn có thêm ngữ cảnh chứng khoán hoặc đã phát hiện được mã cổ phiếu.
_WEAK_ANALYSIS_KEYWORDS = [
    "giờ sao", "gio sao", "làm sao", "lam sao",
    "kệt", "ket", "về bờ", "ve bo",
]

# Giữ tên cũ cho code/test còn tham chiếu.
ANALYSIS_KEYWORDS = _STRONG_ANALYSIS_KEYWORDS + _WEAK_ANALYSIS_KEYWORDS


def _build_keyword_re(keywords: list[str]) -> re.Pattern[str]:
    return re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(kw) for kw in keywords) + r")(?!\w)",
        re.IGNORECASE,
    )


_STRONG_ANALYSIS_RE = _build_keyword_re(_STRONG_ANALYSIS_KEYWORDS)
_WEAK_ANALYSIS_RE = _build_keyword_re(_WEAK_ANALYSIS_KEYWORDS)


def wants_full_analysis(text: str, symbols: list[str] | None = None) -> bool:
    """Người dùng có đang yêu cầu phân tích đầy đủ (thay vì chỉ hỏi giá)?

    So khớp theo BIÊN TỪ chứ không phải substring. Trước đây hàm dùng
    `kw in lower` nên "ket" khớp cả "kết quả", "kết nối", "market", "ticket",
    làm bot chạy nguyên pipeline phân tích (nhiều request mạng + 1 lượt gọi
    LLM) cho những câu chẳng liên quan.

    Nhóm từ khoá yếu ("kệt", "làm sao", "giờ sao", "về bờ") chỉ tính khi tin
    nhắn có ngữ cảnh chứng khoán hoặc caller đã phát hiện được mã - truyền
    `symbols` vào để dùng tín hiệu này.
    """
    if _STRONG_ANALYSIS_RE.search(text):
        return True
    if _WEAK_ANALYSIS_RE.search(text):
        return bool(symbols) or _has_stock_context(text)
    return False

PRICE_KEYWORDS = ["giá", "gia", "price", "bao nhiêu", "bao nhieu"]
_PRICE_KEYWORDS_RE = re.compile(r"\b(?:" + "|".join(re.escape(kw) for kw in PRICE_KEYWORDS) + r")\b", re.IGNORECASE)
_BARE_SYMBOLS_FILLER_RE = re.compile(r"[,\.\-/&+]+")

def wants_price_quote(text: str, symbols: list[str]) -> bool:
    remaining = text
    for sym in symbols:
        remaining = re.sub(re.escape(sym), "", remaining, flags=re.IGNORECASE)
    remaining = _PRICE_KEYWORDS_RE.sub(" ", remaining)
    remaining = _BARE_SYMBOLS_FILLER_RE.sub(" ", remaining)
    return remaining.strip() == ""


def looks_like_price_question(text: str) -> bool:
    """Tin nhắn RÕ RÀNG đang hỏi giá nhưng hệ thống không nhận ra mã nào.

    Dùng để CHẮN, không đẩy câu hỏi xuống Gemini: khi không có khối
    "[DỮ LIỆU GIÁ THỰC TẾ ...]" đi kèm, LLM gần như chắc chắn dựng ra một con
    số nghe hợp lý rồi trình bày như số liệu thật. Thà trả lời "không tra
    được mã" còn hơn trả lời sai số.

    Hai trường hợp được chặn:
    1. Tin nhắn trơ trọi dạng mã (vd "Xyz") mà DNSE không xác nhận - người
       dùng rõ ràng đang tra mã, gõ nhầm hoặc mã không tồn tại.
    2. Có keyword giá VÀ còn ít nhất một token 3-4 ký tự trông giống mã
       (không nằm trong các list từ thông dụng) - để "giá vàng hôm nay bao
       nhiêu" vẫn đi tiếp xuống chat bình thường.
    """
    if is_bare_symbol_message(text):
        upper = text.strip().upper()
        if upper not in _COMMON_WORD_EXCLUDE and upper not in _LOWERCASE_NOISE_EXCLUDE:
            return True
    if not _PRICE_KEYWORDS_RE.search(text):
        return False
    for tok in _SYMBOL_TOKEN_RE.findall(text):
        upper = tok.upper()
        if upper in _COMMON_WORD_EXCLUDE or upper in _LOWERCASE_NOISE_EXCLUDE:
            continue
        return True
    return False


# Dữ liệu thị trường KHÔNG thuộc sàn chứng khoán VN: bot không có provider nào
# cho nhóm này (stock_providers.py chỉ nói chuyện với DNSE), nên nếu để câu hỏi
# đi thẳng vào chat thường, LLM sẽ tự dựng số liệu và cả sự kiện thời sự kèm
# theo. Phát hiện được thì ép nhánh có Google Search thật + chỉ thị cấm bịa
# (xem ai/orchestrator.chat(require_real_search=True)).
_EXTERNAL_MARKET_KEYWORDS = [
    "dầu", "dau tho", "dầu thô", "brent", "wti", "opec",
    "vàng", "vang sjc", "sjc", "gold", "kim loại quý",
    "tỷ giá", "ty gia", "usd", "eur", "jpy", "yên nhật", "nhân dân tệ",
    "bitcoin", "btc", "eth", "ethereum", "crypto", "tiền số", "tien so",
    "dow jones", "nasdaq", "s&p", "sp500", "nikkei", "hang seng",
    "chứng khoán mỹ", "chung khoan my", "phố wall", "pho wall",
    "fed", "lãi suất", "lai suat", "lạm phát", "lam phat",
]
_EXTERNAL_MARKET_RE = _build_keyword_re(_EXTERNAL_MARKET_KEYWORDS)


def wants_external_market_data(text: str) -> bool:
    """Câu hỏi có đang cần số liệu/sự kiện thị trường ngoài sàn VN không?"""
    return bool(_EXTERNAL_MARKET_RE.search(text))


# Giờ giao dịch HOSE/HNX/UPCOM: T2-T6, 9h00-15h00 (gộp cả nghỉ trưa cho đơn
# giản - mục đích duy nhất ở đây là gắn nhãn hiển thị, không phải khớp lệnh).
# Không xử lý ngày lễ: hôm lễ sẽ không có tick nào của "hôm nay" nên
# providers.fetch_quote() trả is_realtime=False sẵn.
_MARKET_OPEN_MINUTE = 9 * 60
_MARKET_CLOSE_MINUTE = 15 * 60


def is_market_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(_VN_TZ)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return _MARKET_OPEN_MINUTE <= minutes <= _MARKET_CLOSE_MINUTE


def _quote_time_note(q: providers.Quote, *, verbose: bool) -> str:
    """Nhãn thời điểm cho một Quote.

    providers.Quote.is_realtime chỉ có nghĩa "giá này lấy từ tick API của hôm
    nay", KHÔNG có nghĩa "thị trường đang khớp lệnh lúc này". Tick cuối phiên
    vẫn nằm đó suốt đêm, nên nếu gắn cứng nhãn "khớp lệnh realtime" theo cờ
    này thì lúc 23h35 bot vẫn báo "khớp lệnh realtime lúc 23:35" trong khi sàn
    đóng cửa từ 15h - vừa sai, vừa mâu thuẫn với chính phần văn bản do LLM
    sinh ra ngay bên dưới ("thị trường đã đóng cửa rồi anh ha").
    """
    if q.is_realtime and is_market_hours():
        if verbose:
            return f"khớp lệnh realtime lúc {datetime.now(_VN_TZ).strftime('%H:%M ngày %d/%m/%Y')} giờ VN"
        return "khớp lệnh realtime"
    if q.is_realtime:
        return f"giá khớp cuối phiên {q.date}" if q.date else "giá khớp cuối phiên gần nhất"
    if q.date:
        return f"giá đóng cửa phiên gần nhất ({q.date})" if verbose else f"đóng cửa phiên {q.date}"
    return "giá đóng cửa phiên gần nhất" if verbose else "đóng cửa phiên gần nhất"


def format_quote_message(q: providers.Quote) -> str:
    arrow, sign = ("🟢▲", "+") if q.change > 0 else ("🔴▼", "") if q.change < 0 else ("⚪", "")
    time_note = _quote_time_note(q, verbose=True)
    # Số theo chuẩn VN ở CẢ giá và %: trước đây dòng này in "24.900 VND" (dấu
    # chấm nghìn) ngay cạnh "+0.2%" (dấu chấm thập phân) - hai chuẩn trái
    # ngược nhau trong cùng một dòng, rất dễ đọc sai độ lớn.
    return f"📊 **{q.symbol}**: **{_fmt_price(q.price)} VND** ({time_note})\n{arrow} {sign}{_fmt_price(q.change)} ({sign}{rfmt.fmt_number(q.change_pct)}%) so với phiên trước ({_fmt_price(q.prev_close)} VND)"

async def quick_quote(symbol: str) -> str:
    q = await providers.fetch_quote(symbol.strip().upper())
    if q is None:
        return f"Em không lấy được giá {symbol} lúc này, anh thử lại sau ít phút nhé."
    return format_quote_message(q)

_GROUNDING_MAX_SYMBOLS = 5
async def build_price_grounding(symbols: list[str]) -> str:
    subset = symbols[:_GROUNDING_MAX_SYMBOLS]
    results = await asyncio.gather(*[providers.fetch_quote(s) for s in subset], return_exceptions=True)
    lines = []
    for sym, res in zip(subset, results):
        if isinstance(res, BaseException) or res is None: continue
        time_note = _quote_time_note(res, verbose=False)
        sign = "+" if res.change > 0 else ""
        lines.append(f"- {sym}: {_fmt_price(res.price)} VND ({time_note}), {sign}{_fmt_price(res.change)} ({sign}{rfmt.fmt_number(res.change_pct)}%) so với phiên trước ({_fmt_price(res.prev_close)} VND)")
    if not lines: return ""
    now = datetime.now(_VN_TZ)
    return f"[DỮ LIỆU GIÁ THỰC TẾ lúc {now:%H:%M %d/%m/%Y} giờ VN, lấy trực tiếp từ DNSE - đây là số liệu ĐÚNG duy nhất được phép dùng cho các mã dưới đây. TUYỆT ĐỐI KHÔNG tự suy diễn/bịa thêm số liệu nào khác ngoài danh sách này:\n" + "\n".join(lines) + "]"

@dataclass
class StockContext:
    symbol: str
    price: float
    fetched_at_vn: str
    stats: feat.SignalStats
    decision: policy.Decision
    enhanced: feat.EnhancedIndicators | None
    indicator_summary: str
    support_resistance: feat.SupportResistance | None
    key_levels: feat.KeyLevels | None
    ma_alignment: feat.MAAlignment | None
    sector_prompt: str
    fundamentals_prompt: str
    news: list[providers.NewsHeadline]
    relative_strength: float
    liquidity: feat.Liquidity | None
    quality: validation.DataQuality
    realtime_quote_line: str | None = None
    # Ngày của NẾN ĐÓNG CỬA được dùng để tính toàn bộ chỉ báo. Thiếu mốc
    # này, báo cáo mô tả chỉ báo ở THỬ HIỆN TẠI ("đang loay hoay", "lực cầu
    # chưa hào hứng") trong khi giá realtime đã +2,51% - mâu thuẫn ngay trong
    # cùng một tin nhắn (ca CII ngày 05/08/2026).
    last_bar_date: str = ""
    # Cảnh báo chuỗi giá có thể chưa điều chỉnh sau chia tách/cổ tức/thưởng.
    # Rỗng = không phát hiện gap bất thường nào (xem stock/price_adjust.py).
    adjustment_note: str = ""

async def _safe_sector_prompt(symbol: str) -> str:
    try:
        sector_keys = sector.get_symbol_sectors(symbol)
        if not sector_keys: return ""
        ctx = await sector.build_sector_context(sector_keys)
        return sector.build_sector_prompt_section(ctx, symbol)
    except Exception:
        return ""

async def _safe_fundamentals_prompt(symbol: str) -> str:
    try:
        bundle = await fundamentals.fetch_fundamentals(symbol)
        return fundamentals.build_fundamentals_prompt_section(bundle.valuation, bundle.foreign, symbol, foreign_trend=bundle.foreign_trend, growth=bundle.growth, events=bundle.events, sector_pe_avg=bundle.sector_pe_avg, sector_pe_sample=bundle.sector_pe_sample, sector_pe_label=bundle.sector_pe_label, sector_profile=bundle.sector_profile, sector_benchmark=bundle.sector_benchmark)
    except Exception:
        return ""

def _trend_pct(closes: list[float]) -> float:
    return ((closes[-1] - closes[0]) / closes[0]) * 100 if closes and closes[0] else 0.0

# NGUỒN DUY NHẤT cho khái niệm "fact nào được coi là thuộc danh mục đầu tư".
# services/tools.py._tool_get_portfolio và scheduler.py._build_portfolio_digest
# import lại từ đây thay vì tự khai báo bản copy riêng - trước đây có 3 bản
# giống nhau, sửa 1 chỗ là 2 chỗ kia lệch ngay.
PORTFOLIO_FACT_KEYWORDS = ("danh_muc", "portfolio", "co_phieu")

# Alias giữ tương thích ngược cho code/test cũ còn tham chiếu tên private.
_PORTFOLIO_FACT_KEYWORDS = PORTFOLIO_FACT_KEYWORDS


def is_portfolio_fact(key: str) -> bool:
    """Key của fact trong trí nhớ dài hạn có thuộc nhóm danh mục đầu tư không."""
    return any(kw in key for kw in PORTFOLIO_FACT_KEYWORDS)


async def _is_holding_symbol(user_id: int | None, symbol: str) -> bool:
    """Đoán user có đang giữ `symbol` không, dựa trên fact danh mục đã lưu
    trong trí nhớ dài hạn - dùng để stock_policy.evaluate_policy() phân biệt
    HOLD (đang giữ, tín hiệu chưa đủ rõ thì giữ nguyên) với NO_TRADE/SELL
    (đang cân nhắc mở mới, không có gì để bán). Suy đoán có thể sai (chưa
    từng nhắc trong chat, hoặc đã bán nhưng chưa cập nhật trí nhớ) - chấp
    nhận được vì chỉ ảnh hưởng action/label hiển thị, không đổi ngưỡng
    confidence hay dữ liệu đầu vào."""
    if user_id is None:
        return False
    try:
        facts = await db.get_facts(user_id)
    except Exception:
        return False
    symbol_re = re.compile(rf"\b{re.escape(symbol)}\b", re.IGNORECASE)
    return any(
        symbol_re.search(value)
        for key, value in facts
        if is_portfolio_fact(key)
    )

async def build_context(symbol: str, *, user_id: int | None = None, is_holding: bool | None = None) -> StockContext | None:
    results = await asyncio.gather(
        providers.fetch_ohlcv(symbol, days=90), providers.fetch_ohlcv("VNINDEX", days=90),
        providers.fetch_quote(symbol), providers.fetch_news(symbol),
        _safe_sector_prompt(symbol), _safe_fundamentals_prompt(symbol),
        return_exceptions=True
    )
    for r in results:
        if isinstance(r, BaseException): raise r
    symbol_series, vnindex_series, quote, news, sector_prompt, fundamentals_prompt = results
    if not symbol_series.closes: return None

    quality = validation.validate_ohlcv(symbol_series.closes, symbol_series.highs, symbol_series.lows, symbol_series.volumes, symbol_series.dates)
    # Kiểm tra chuỗi giá đã điều chỉnh sau chia tách/cổ tức chưa. OhlcvSeries có
    # sẵn trường is_adjusted từ đầu nhưng chưa provider nào gán giá trị, nên
    # trước đây hệ thống hoàn toàn không biết SMA50/Donchian/ATR14/trend 3
    # tháng có đang tính trên một gap giả của ngày GDKHQ hay không.
    audit = price_adjust.audit_series(symbol, symbol_series.source, symbol_series.closes, symbol_series.dates)
    symbol_series.is_adjusted = audit.is_adjusted
    if audit.gaps:
        logger.warning("Chuỗi giá %s (nguồn %s) có %d gap nghi điều chỉnh giá", symbol, symbol_series.source, len(audit.gaps))
    # analysis_price = close của phiên gần nhất trong CHUỖI OHLCV - toàn bộ
    # feature/policy (Donchian, Bollinger, S/R, session...) phải nhìn CÙNG
    # một thời điểm để không tự mâu thuẫn nhau (P0-3). quote.price là tick
    # realtime, có thể lệch pha với closes[-1] (vd trước giờ mở cửa, hoặc
    # cuối tuần) - chỉ dùng để HIỂN THỊ "giá khớp hiện tại", không đưa vào
    # tính stop/target/R:R.
    analysis_price = symbol_series.price
    last_bar_date = symbol_series.dates[-1] if symbol_series.dates else ""
    realtime_quote_line = None
    if quote is not None:
        time_note = _quote_time_note(quote, verbose=False)
        realtime_quote_line = f"Giá khớp hiện tại: {_fmt_price(quote.price)} VND ({time_note}) - CHỈ tham khảo hiển thị, KHÔNG dùng để tính stop/target/R:R bên dưới."
    # news_impact CHỈ được tính trên tin CÓ NHẮC ĐÚNG MÃ. providers.fetch_news
    # truy vấn Google News bằng chuỗi "<mã> cổ phiếu" nhưng không kiểm tra mã
    # có trong tiêu đề, nên tin thị trường chung/tin của mã khác vẫn lọt vào.
    # Dùng providers.calc_news_impact (trung bình sentiment MỌI tin) thì
    # sentiment của tin không liên quan chảy thẳng vào PolicyInputs.news_impact,
    # tức là ảnh hưởng trực tiếp tới khuyến nghị mua/bán.
    news_impact = rfmt.relevant_news_impact([(n.title, n.sentiment) for n in news], symbol)
    stats = feat.calc_signal_stats(symbol_series.closes, symbol_series.volumes, analysis_price)
    relative_strength = round(_trend_pct(symbol_series.closes) - _trend_pct(vnindex_series.closes), 2)

    enhanced, indicator_summary = None, ""
    if quality.usable and len(symbol_series.closes) >= 20:
        enhanced = feat.build_enhanced_indicators(symbol_series.closes, analysis_price, symbol_series.highs, symbol_series.lows)
        indicator_summary = feat.build_indicator_summary(enhanced, symbol)

    ma_alignment = feat.calc_ma_alignment(symbol_series.closes) if len(symbol_series.closes) >= 20 else None
    support_resistance = feat.calc_support_resistance(symbol_series.highs, symbol_series.lows, analysis_price, 30) if symbol_series.highs else None
    key_levels = feat.find_key_levels(symbol_series.highs, symbol_series.lows, symbol_series.closes) if symbol_series.highs else feat.KeyLevels([], [])
    liquidity = feat.calc_liquidity(symbol_series.volumes)
    session = feat.calc_session_metrics(symbol_series.closes, symbol_series.highs, symbol_series.lows, symbol_series.volumes)

    trend_score = feat.calc_trend_score(ma_alignment, stats.rsi14, enhanced.macd.histogram) if ma_alignment and ma_alignment.alignment != "unknown" and enhanced else None
    vnindex_multi_tf = feat.calc_multi_timeframe(vnindex_series.closes) if vnindex_series.closes else None
    vnindex_adx = feat.calc_adx(vnindex_series.closes, vnindex_series.highs, vnindex_series.lows) if vnindex_series.closes else None
    vnindex_distribution_days = feat.calc_distribution_days(vnindex_series.closes, vnindex_series.volumes)

    holding = is_holding if is_holding is not None else await _is_holding_symbol(user_id, symbol)
    decision = policy.evaluate_policy(policy.PolicyInputs(price=analysis_price, stats=stats, enhanced=enhanced, ma_alignment=ma_alignment, support_resistance=support_resistance, liquidity=liquidity, session=session, relative_strength=relative_strength, trend_score=trend_score, news_impact=news_impact, quality=quality, vnindex_multi_tf=vnindex_multi_tf, vnindex_adx=vnindex_adx, vnindex_distribution_days=vnindex_distribution_days, key_levels=key_levels, is_holding=holding))
    fetched_at_vn = datetime.now(_VN_TZ).strftime("%H:%M ngày %d/%m/%Y")

    return StockContext(symbol, analysis_price, fetched_at_vn, stats, decision, enhanced, indicator_summary, support_resistance, key_levels, ma_alignment, sector_prompt, fundamentals_prompt, news, relative_strength, liquidity, quality, realtime_quote_line, last_bar_date, audit.note)

def _fmt_price(v: float | None) -> str:
    # Nguồn duy nhất cho định dạng giá: stock/report_format.fmt_price (chuẩn VN,
    # dấu chấm phân cách nghìn). Giữ alias này để code/test cũ không vỡ.
    return rfmt.fmt_price(v)

def _confidence_label(c: float) -> str:
    if c >= policy.CONFIDENCE_BUY_MIN: return "CAO"
    if c >= policy.CONFIDENCE_WATCH_MIN: return "TRUNG BÌNH"
    return "THẤP"

_ACTION_LABEL_VI = {"BUY": "🟢 MUA", "HOLD": "🟡 GIỮ", "WATCH": "🟡 THEO DÕI", "SELL": "🔴 BÁN/TRÁNH", "NO_TRADE": "⚪ ĐỨNG NGOÀI (NO_TRADE)"}
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_jinja_env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES_DIR), trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=False)
_STOCK_PROMPT_TEMPLATE = _jinja_env.get_template("stock_analysis_prompt.j2")

def build_prompt(ctx: StockContext) -> str:
    d = ctx.decision
    price = ctx.price
    atr_pct = ctx.enhanced.atr_pct if ctx.enhanced else None
    sr = ctx.support_resistance
    # MỌI mốc giá đưa vào prompt đều kèm khoảng cách % so với giá hiện tại:
    # báo cáo FPT/GEX ngày 05/08/2026 nêu mốc 70.370 / 31.900 như vùng giao
    # dịch mà không cho người đọc biết mốc đó cách giá 1,6% hay 28%.
    support_resistance_line = None
    if sr and sr.support:
        support_resistance_line = (
            f"Hỗ trợ/kháng cự (biên 30 phiên): hỗ trợ {rfmt.format_level(price, sr.support)}, "
            f"kháng cự {rfmt.format_level(price, sr.resistance)}"
        )
    ma = ctx.ma_alignment
    ma_alignment_line = f"MA alignment: {ma.alignment} (MA5={_fmt_price(ma.ma5)}, MA10={_fmt_price(ma.ma10)}, MA20={_fmt_price(ma.ma20)})" if ma and ma.alignment != "unknown" else None
    liq = ctx.liquidity
    # Không dùng .replace(",", ".") cho cả dòng nữa: cách đó sẽ biến luôn dấu
    # phẩy thập phân của tỷ lệ % thành dấu chấm.
    liquidity_line = None
    if liq:
        liquidity_line = (
            f"Thanh khoản: KL phiên gần nhất {rfmt.fmt_price(liq.current_volume)} "
            f"so với TB 20 phiên {rfmt.fmt_price(liq.avg_volume_20)} "
            f"({rfmt.fmt_number(liq.liquidity_ratio_pct, 1)}% TB20)"
        )

    # Tin có nhắc đúng mã được đẩy lên trước để không bị tin thị trường chung
    # chiếm hết 5 suất, và từng tin được gắn cờ confirmed + ngày đăng.
    ranked_news = sorted(ctx.news, key=lambda n: not rfmt.title_mentions_symbol(n.title, ctx.symbol))
    news = [
        {
            "tag": "🟢" if n.sentiment > 0.2 else ("🔴" if n.sentiment < -0.2 else "⚪"),
            "title": n.title,
            "source": n.source,
            "date": rfmt.fmt_news_date(n.pub_date),
            "confirmed": rfmt.title_mentions_symbol(n.title, ctx.symbol),
        }
        for n in ranked_news[:5]
    ]

    kl = ctx.key_levels
    key_levels_line = None
    nearest_levels_line = None
    if kl and (kl.supports or kl.resistances):
        parts = []
        if kl.supports:
            parts.append("Hỗ trợ: " + ", ".join(rfmt.format_level(price, lv.price, lv.touches) for lv in kl.supports[:3]))
        if kl.resistances:
            parts.append("Kháng cự: " + ", ".join(rfmt.format_level(price, lv.price, lv.touches) for lv in kl.resistances[:3]))
        key_levels_line = "Vùng giá quan trọng (swing pivot, 60 phiên): " + " | ".join(parts)
        nearest_levels_line = rfmt.nearest_levels_line(
            price,
            [(lv.price, lv.touches) for lv in kl.supports],
            [(lv.price, lv.touches) for lv in kl.resistances],
            atr_pct=atr_pct,
        )

    # MACD quy theo % giá + ADX kèm hướng +DI/-DI. build_indicator_summary đã in
    # đủ số, nhưng dạng số trơ khiến phần diễn giải đánh giá sai độ mạnh (MACD
    # +24.44 trên cổ phiếu 14.000đ = 0,17% giá) và bỏ qua hướng (ADX 43,7 của
    # một mã đang giảm bị gọi là "xu hướng mạnh" theo nghĩa tích cực).
    momentum_detail_line = None
    ma_distance_line = None
    if ctx.enhanced:
        e = ctx.enhanced
        momentum_detail_line = "\n".join([
            rfmt.macd_strength_line(e.macd.macd_line, e.macd.histogram, price),
            rfmt.adx_direction_line(e.adx.adx, e.adx.di_plus, e.adx.di_minus, e.adx.trending, available=e.adx.available),
        ])
        ma_distance_line = (
            f"Giá vs đường trung bình: SMA20 {rfmt.format_level(price, e.sma20)}, "
            f"SMA50 {rfmt.format_level(price, e.sma50)}"
        )

    data_as_of_line = None
    if ctx.last_bar_date:
        data_as_of_line = (
            f"Toàn bộ chỉ báo kỹ thuật bên dưới được tính trên NẾN ĐÓNG CỬA ngày "
            f"{ctx.last_bar_date}, KHÔNG phải giá đang khớp - khi mô tả chỉ báo phải "
            f"gắn mốc thời gian này."
        )

    trade_plan = None
    if d.trade_plan:
        tp = d.trade_plan
        trade_plan = {
            "entry_low": _fmt_price(tp.entry_low), "entry_high": _fmt_price(tp.entry_high),
            "stop": _fmt_price(tp.stop), "target1": _fmt_price(tp.target1),
            "target2": _fmt_price(tp.target2) if tp.target2 is not None else None,
            "position_size_pct": tp.position_size_pct, "plan_note": tp.plan_note,
        }
    scenarios = [{"name": s.name, "trigger": s.trigger, "action": s.action} for s in d.scenarios]
    backtest_stats_line = backtest.format_setup_stats_line(d.setup_type)

    return _STOCK_PROMPT_TEMPLATE.render(
        symbol=ctx.symbol, fetched_at_vn=ctx.fetched_at_vn, price=_fmt_price(ctx.price), action=d.action,
        target_price=_fmt_price(d.target_price) if d.target_price is not None else "",
        stop_price=_fmt_price(d.stop_price) if d.stop_price is not None else "", rr_ratio=d.rr_ratio,
        confidence=d.confidence, confidence_label=_confidence_label(d.confidence), setup_type=d.setup_type,
        market_regime=d.market_regime, risk_level=d.risk_level, data_quality=d.data_quality,
        reasons_text="; ".join(d.reasons[:6]) if d.reasons else "", invalidation_reason=d.invalidation_reason,
        trend_3m=rfmt.fmt_number(ctx.stats.trend_3m), momentum=rfmt.fmt_number(ctx.stats.momentum),
        volume_trend=rfmt.fmt_number(ctx.stats.volume_trend), volatility=rfmt.fmt_number(ctx.stats.volatility),
        rsi14=rfmt.fmt_number(ctx.stats.rsi14, 1) if ctx.stats.rsi14 is not None else "chưa đủ dữ liệu",
        relative_strength=rfmt.fmt_number(ctx.relative_strength), indicator_summary=ctx.indicator_summary,
        support_resistance_line=support_resistance_line, ma_alignment_line=ma_alignment_line,
        liquidity_line=liquidity_line, liquidity_thin_warning=liq.is_thin if liq else False,
        sector_prompt=ctx.sector_prompt, fundamentals_prompt=ctx.fundamentals_prompt, news=news,
        realtime_quote_line=ctx.realtime_quote_line, key_levels_line=key_levels_line,
        nearest_levels_line=nearest_levels_line, momentum_detail_line=momentum_detail_line,
        ma_distance_line=ma_distance_line, data_as_of_line=data_as_of_line,
        adjustment_note=ctx.adjustment_note,
        trade_plan=trade_plan, scenarios=scenarios, backtest_stats_line=backtest_stats_line,
    )

def _fallback_text(ctx: StockContext) -> str:
    d = ctx.decision
    action_label = _ACTION_LABEL_VI.get(d.action, d.action)
    if d.action == "BUY": price_line = f"Vùng mua {_fmt_price(ctx.price)} | TP {_fmt_price(d.target_price)} | SL {_fmt_price(d.stop_price)} | R:R ~{d.rr_ratio}"
    elif d.action == "SELL": price_line = f"KHÔNG phải vùng mua | Giá {_fmt_price(ctx.price)} | Nếu đang giữ: cân nhắc chốt/cắt lỗ quanh {_fmt_price(d.target_price)} | Tín hiệu SELL vô hiệu nếu giá vượt {_fmt_price(d.stop_price)}"
    elif d.action == "HOLD" and d.target_price is not None and d.stop_price is not None:
        price_line = f"Đang giữ, tín hiệu vẫn thuận lợi | Giá {_fmt_price(ctx.price)} | Tham khảo chốt lời {_fmt_price(d.target_price)} | Cân nhắc cắt lỗ dưới {_fmt_price(d.stop_price)} | R:R ~{d.rr_ratio}"
    elif d.action == "NO_TRADE": price_line = "Hệ thống chưa đủ edge để đề xuất vùng giá - ưu tiên đứng ngoài quan sát."
    else: price_line = f"Giá {_fmt_price(ctx.price)} | Chưa đủ rõ xu hướng để đề xuất vùng giá cụ thể"

    lines = [
        f"📊 **{ctx.symbol}** — **{_fmt_price(ctx.price)} VND** ({ctx.fetched_at_vn})",
        f"Tín hiệu: **{action_label}** (confidence {d.confidence}, setup {d.setup_type}, regime {d.market_regime})",
        price_line,
        f"RSI14 {rfmt.fmt_number(ctx.stats.rsi14, 1) if ctx.stats.rsi14 is not None else 'chưa đủ dữ liệu'} | Trend ~3M {rfmt.fmt_number(ctx.stats.trend_3m)}% | Risk: {d.risk_level}",
    ]
    if ctx.last_bar_date: lines.append(f"Chỉ báo tính trên nến đóng cửa ngày {ctx.last_bar_date}.")
    if d.reasons: lines.append("Lý do: " + "; ".join(d.reasons[:4]))
    if d.invalidation_reason: lines.append(f"Lưu ý: {d.invalidation_reason}")
    if ctx.realtime_quote_line: lines.append(ctx.realtime_quote_line)
    if ctx.liquidity and ctx.liquidity.is_thin: lines.append("⚠️ Thanh khoản TB20 quá thấp.")
    if ctx.adjustment_note: lines.append("⚠️ Chuỗi giá có gap nghi ngày giao dịch không hưởng quyền chưa được điều chỉnh - SMA50/Donchian/ATR/trend 3 tháng kém tin cậy.")
    if ctx.quality.status != "ok": lines.append(f"⚠️ Chất lượng dữ liệu: {ctx.quality.status}")
    lines.append("⚠️ API dự phòng không phản hồi nên đây là bản rút gọn.")
    return "\n".join(lines)

_STALE_NOTE = "\n\n⏱️ _Lưu ý: dữ liệu/thời điểm bên trên là của lần phân tích gần nhất_"

PORTFOLIO_KEYWORDS = ["cơ cấu", "co cau", "danh mục", "danh muc", "tỷ trọng", "ty trong", "giữ hay bán", "nên giữ mã nào"]

def wants_portfolio_analysis(text: str, symbols: list[str]) -> bool:
    if len(symbols) < 2: return False
    lower = text.lower()
    return any(kw in lower for kw in PORTFOLIO_KEYWORDS)

async def analyze_portfolio(symbols: list[str], user_text: str, *, user_id: int | None = None) -> str:
    # Dùng chung _is_holding_symbol với analyze_symbol thay vì gắn cứng
    # is_holding=True cho mọi mã - tránh 2 đường (phân tích đơn lẻ vs danh
    # mục) cho action khác nhau với cùng 1 mã khi user hỏi cả 2 kiểu.
    holdings = await asyncio.gather(*[_is_holding_symbol(user_id, sym) for sym in symbols])
    tasks = [build_context(sym, user_id=user_id, is_holding=holding) for sym, holding in zip(symbols, holdings)]
    contexts = await asyncio.gather(*tasks, return_exceptions=True)
    valid_contexts = [ctx for ctx in contexts if not isinstance(ctx, BaseException) and ctx is not None]
    if not valid_contexts:
        return "Em không lấy được dữ liệu của các mã này lúc này, anh thử lại sau xíu nha."

    combined_data = []
    for ctx in valid_contexts:
        d = ctx.decision
        # Mốc hỗ trợ/kháng cự kèm khoảng cách % giống nhánh phân tích đơn lẻ.
        sr_line = (
            f"Hỗ trợ: {rfmt.format_level(ctx.price, ctx.support_resistance.support)} | "
            f"Kháng cự: {rfmt.format_level(ctx.price, ctx.support_resistance.resistance)}"
        ) if ctx.support_resistance else "Không rõ"
        rsi_text = rfmt.fmt_number(ctx.stats.rsi14, 1) if ctx.stats.rsi14 is not None else "chưa đủ dữ liệu"
        trend_line = f"RSI: {rsi_text} | Trend 3M: {rfmt.fmt_number(ctx.stats.trend_3m)}%"
        bar_note = f" | Chỉ báo tính trên nến đóng cửa {ctx.last_bar_date}" if ctx.last_bar_date else ""
        # Cảnh báo điều chỉnh giá phải đi theo TẮNG MÃ trong danh mục, không
        # được âm thầm bỏ: mã vừa chia tách sẽ có trend 3M và hỗ trợ/kháng cự
        # sai lệch hẳn so với các mã còn lại, rất dễ bị xếp hạng oan.
        adjust_note = " | ⚠️ giá lịch sử nghi chưa điều chỉnh sau chia tách/cổ tức, trend/hỗ trợ/kháng cự của mã này kém tin cậy" if ctx.adjustment_note else ""
        combined_data.append(f"Mã {ctx.symbol}: Giá {_fmt_price(ctx.price)} | Tín hiệu hệ thống: {d.action} (Độ tin cậy: {d.confidence}) | {trend_line} | {sr_line}{bar_note}{adjust_note}")

    data_text = "\n".join(combined_data)
    prompt = (
        f"[DỮ LIỆU KỸ THUẬT DANH MỤC LÚC NÀY]:\n{data_text}\n\n"
        f"[CÂU HỎI TỪ NGƯỌI DÙNG]:\n\"{user_text}\"\n\n"
        f"Lan Anh hãy đóng vai broker chuyên nghiệp tư vấn Cơ CẤU DANH MỤC. "
        f"So sánh sức mạnh các mã, khuyên mã nào nên giữ/gồng lãi, mã nào vi phạm kỹ thuật cần hạ tỷ trọng/cắt lỗ. "
        f"Mọi mốc giá nhắc tới phải kèm khoảng cách % đã cho ở trên, không tự tính lại. "
        f"Mã nào có cảnh báo giá chưa điều chỉnh thì phải nói rõ hạn chế đó khi so sánh, không xếp hạng như số liệu sạch. "
        f"Số viết theo chuẩn Việt Nam (dấu chấm nghìn, dấu phẩy thập phân). "
        f"Văn phong: xưng em/anh tự nhiên, rõ ràng, không tự giới thiệu, không dùng danh xưng thân mật quá đà. "
        f"Chỉ MỘT câu nhắc đây là thông tin tham khảo ở cuối tin nhắn."
    )

    from ai import orchestrator
    try:
        response = await orchestrator.ask(prompt)
        result = rfmt.clean_analysis_output((response.text or "").strip())
        if result and getattr(response, "used_fallback", False): result += "\n\n⚙️ API"
        return result
    except Exception:
        logger.exception("Lỗi khi tổng hợp danh mục")
        return "Em đang gặp chút sự cố khi phân tích danh mục, anh chờ chút thử lại nha."

async def analyze_symbol(symbol: str, user_text: str = "", *, force_refresh: bool = False, user_id: int | None = None) -> str:
    symbol = symbol.strip().upper()
    holding = await _is_holding_symbol(user_id, symbol)
    if not force_refresh and not user_text:
        cached = _cache_get(symbol, holding)
        if cached: return cached + _STALE_NOTE

    try:
        ctx = await build_context(symbol, user_id=user_id, is_holding=holding)
    except Exception:
        logger.exception("Lỗi lấy dữ liệu phân tích %s", symbol)
        ctx = None
    if ctx is None: return messages.STOCK_FETCH_ERROR.format(symbol=symbol)

    prompt = build_prompt(ctx)
    if user_text:
        prompt += (
            f"\n\n[LƯU Ý QUAN TRỌNG TỪ HỆ THỐNG]:\n"
            f"Người dùng vừa hỏi: \"{user_text}\"\n"
            f"Lan Anh hãy phân tích kỹ thuật ở trên, ĐỒNG THỮI phải trả lời trực tiếp "
            f"vào tình huống này của anh ấy: tính mức lời/lỗ BẮNG SỐ dựa trên giá đã cho, "
            f"nêu hướng xử lý cụ thể dựa trên Action đã chốt. TUYỆT ĐỐI không tự giới thiệu, "
            f"không đoán cảm xúc của anh ấy, không dùng danh xưng thân mật quá đà."
        )

    from ai import orchestrator
    try:
        response = await orchestrator.ask(prompt)
        # Làm sạch trước khi trả về: bỏ câu tự giới thiệu, bỏ danh xưng thân
        # mật, giữ đúng MỘT đoạn disclaimer. Prompt đã yêu cầu điều này nhưng
        # LLM không ổn định: cùng một prompt, báo cáo FPT bị lặp disclaimer hai
        # lần còn CII thì không.
        text = rfmt.clean_analysis_output((response.text or "").strip())
        result = text or _fallback_text(ctx)
        if text and getattr(response, "used_fallback", False): result += "\n\n⚙️ API"
    except Exception:
        logger.exception("Gemini lỗi khi phân tích %s", symbol)
        result = _fallback_text(ctx)

    if not user_text:
        _cache_set(symbol, holding, result)

    return result
