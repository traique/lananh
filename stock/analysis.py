import asyncio
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jinja2

from stock import backtest
from stock import calendar as trading_calendar
from stock import corporate_actions
from stock import features as feat
from stock import fundamentals
from stock import policy
from stock import price_adjust
from stock import providers
from stock import report_format as rfmt
from stock import personas as stock_personas
from stock import sector
from stock import validation
import messages
from core import database as db
from stock.sector import ALL_KNOWN_SYMBOLS

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_CACHE_TTL_SEC = 15 * 60
_CACHE_MAX_ENTRIES = 200
# Khoá cache gồm cả full_access: bản đầy đủ (có tín hiệu/kế hoạch) của chủ bot
# KHÔNG được trả nhầm cho thành viên Zalo chỉ được xem bản tham khảo.
_cache: dict[tuple[str, bool, bool], tuple[float, str]] = {}

def _cache_get(symbol: str, holding: bool, full_access: bool = True) -> str | None:
    key = (symbol, holding, full_access)
    entry = _cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > _CACHE_TTL_SEC:
        _cache.pop(key, None)
        return None
    return value

def _cache_set(symbol: str, holding: bool, value: str, full_access: bool = True) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest_key, None)
    _cache[(symbol, holding, full_access)] = (time.time(), value)

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
# định - list này thuần túy là tối ưu, không phải rào chắn.
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


# URL trong tin nhắn ("phân tích bài này https://cafef.vn/hpg-lai-ky-luc-...") không
# được thành mã cổ phiếu: slug hpg/fpt từng chiếm cả lượt bằng pipeline phân tích.
_URL_IN_TEXT_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _strip_urls(text: str) -> str:
    return _URL_IN_TEXT_RE.sub(" ", text or "")


def detect_symbol_candidates(text: str) -> tuple[list[str], list[str]]:
    text = _strip_urls(text)
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
        for sym, ok in zip(to_check, checks, strict=False):
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
# Câu hỏi LÝ GIẢI định tính (vd "tại sao giá X tăng") - không phải yêu cầu
# đọc giá hiện tại của một mã, dùng để loại khỏi looks_like_price_question.
_WHY_QUESTION_RE = re.compile(
    r"\b(?:tại sao|tai sao|vì sao|vi sao|do đâu|do dau|why)\b", re.IGNORECASE
)
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

    Ngoại lệ: câu hỏi LÝ GIẢI ("tại sao/vì sao giá...tăng") không phải yêu
    cầu đọc giá hiện tại, không chặn. Trước đây câu kiểu "tại sao giá điện
    thoại lại tăng" vẫn bị chặn vì token tiếng Việt có dấu bị regex ASCII
    cắt thành mảnh giả mã ("liên quan" -> QUAN, "điện thoại" -> THO) trông
    giống mã không xác định; loại câu này để xuống chat thường có Tavily
    search tiếp sức mới đúng.
    """
    text = _strip_urls(text)
    if is_bare_symbol_message(text):
        upper = text.strip().upper()
        if upper not in _COMMON_WORD_EXCLUDE and upper not in _LOWERCASE_NOISE_EXCLUDE:
            return True
    if _WHY_QUESTION_RE.search(text):
        return False
    if not _PRICE_KEYWORDS_RE.search(text):
        return False
    # Sửa 27/10 ("giá samsung s24 plus cũ" bị báo "chưa tra ra mã" vì token PLUS):
    # chỉ chặn khi câu RÕ RÀNG là hỏi cổ phiếu - có chữ "cổ phiếu/mã/CK/chứng khoán"
    # kèm một từ còn lại để tra, hoặc ngoài từ hỏi giá/thời gian ra CHỈ còn token
    # dạng mã ("giá xyz bao nhiêu", "giá ABCD"). Còn tên sản phẩm/hàng hoá khác
    # (samsung, iphone 15, sầu riêng...) thì để lượt đi tiếp xuống chat có tra web
    # thật (wants_product_price -> require_real_search), không chặn.
    leftover = _price_leftover_words(text)
    if _has_explicit_stock_word(text):
        return bool(leftover)
    candidates = _symbol_like_tokens(text)
    return bool(candidates) and all(word.upper() in candidates for word in leftover)


# Từ "cổ phiếu/mã/CK/chứng khoán" (có dấu, biên từ): "mã" không dấu trùng "mà/ma"
# nên chỉ nhận dạng có dấu; "ck" chữ thường là "chuyển khoản" nên chỉ nhận "CK".
_EXPLICIT_STOCK_RE = re.compile(
    r"(?<!\w)(?:cổ phiếu|co phieu|chứng khoán|chung khoan|mã|cp)(?!\w)|(?<!\w)CK(?!\w)",
    re.IGNORECASE,
)
# Từ đệm của câu hỏi giá (đã bỏ dấu) - không tính là "tên sản phẩm".
_PRICE_FILLER_WORDS = frozenset(
    """gia price bao nhieu bn bnhieu hom nay hien tai bay gio gio luc nay sao the nao
    the nao a ah nhi nhe nha vay oi anh em chi ban minh cua cho xem tra giup voi di
    duoc ko khong k hok la bi dang tang giam phien ma co phieu chung khoan ck cp cai
    con hon may mot""".split()
)
_WORD_RE = re.compile(r"[^\W_]+")


def _has_explicit_stock_word(text: str) -> bool:
    # "ck" chữ thường = chuyển khoản ("giá ck bao nhiêu" không phải cổ phiếu).
    return any(m.group(0) != "ck" for m in _EXPLICIT_STOCK_RE.finditer(text))


def _price_leftover_words(text: str) -> list[str]:
    """Các từ còn lại (bỏ dấu, giữ hoa/thường) sau khi bỏ từ hỏi giá/thời gian/xưng hô."""
    from core.text_normalize import fold

    words = []
    for raw in _WORD_RE.findall(text):
        folded = fold(raw)
        if folded in _PRICE_FILLER_WORDS:
            continue
        words.append(raw)
    return words


def _symbol_like_tokens(text: str) -> set[str]:
    """Token có thể là mã: 3-4 chữ ASCII không thuộc từ thông dụng, hoặc chữ in HOA
    nguyên bản 3-6 ký tự ("ABCXYZ")."""
    found = set()
    for tok in _SYMBOL_TOKEN_RE.findall(text):
        upper = tok.upper()
        if upper in _COMMON_WORD_EXCLUDE or upper in _LOWERCASE_NOISE_EXCLUDE:
            continue
        found.add(upper)
    found.update(re.findall(r"\b[A-Z]{3,6}\b", text))
    return found


def wants_product_price(text: str) -> bool:
    """Câu hỏi giá sản phẩm/hàng hoá (không phải cổ phiếu, không phải vàng/xăng/tỷ
    giá đã có đường riêng): lượt chat phải TRA WEB THẬT (require_real_search) thay vì
    để LLM tự nghĩ ra giá. Gọi khi nhánh cổ phiếu không nhận ra mã nào."""
    text = _strip_urls(text)
    if not _PRICE_KEYWORDS_RE.search(text) or _WHY_QUESTION_RE.search(text):
        return False
    if _NON_PRICE_GIA_RE.search(text) and not _PRICE_ASK_RE.search(text):
        return False
    if _has_explicit_stock_word(text):
        return False
    return bool(_price_leftover_words(text))


# "giá trị", "đánh giá", "giá như"... không phải hỏi giá; trừ khi câu vẫn hỏi
# "giá ... bao nhiêu".
_NON_PRICE_GIA_RE = re.compile(
    r"(?<!\w)(?:giá trị|gia tri|đánh giá|danh gia|định giá|dinh gia|giá như|gia nhu|giá mà|"
    r"gia ma|tăng giá trị|vô giá|vo gia|trị giá|tri gia|giá đỡ|gia do)(?!\w)",
    re.IGNORECASE,
)
_PRICE_ASK_RE = re.compile(r"(?<!\w)(?:bao nhiêu|bao nhieu|bn|mấy tiền|may tien)(?!\w)", re.IGNORECASE)


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
    text = _strip_urls(text)
    return bool(_EXTERNAL_MARKET_RE.search(text))


# Bộ nhận diện cho persona (chat_needs_stock_rules). Khác _EXTERNAL_MARKET_RE (ép
# tra web thật): "dầu gội", "hoa vàng" không được kéo phần phân tích cổ phiếu vào
# câu chuyện đời thường, nên tài sản đầu tư chỉ tính khi đi kèm ý mua/bán/giá.
# Từ không dấu chỉ giữ dạng ít trùng nghĩa ("du dinh" = dự định, "ban" = bạn).
_INVESTMENT_TALK_RE = _build_keyword_re([
    "đầu tư", "dau tu", "danh mục", "danh muc", "cắt lỗ", "chốt lời", "chot loi",
    "bắt đáy", "đu đỉnh", "lướt sóng", "luot song", "margin", "cổ tức", "co tuc",
    "khối ngoại", "khoi ngoai", "về bờ", "trái phiếu", "trai phieu", "chứng chỉ quỹ",
    "etf", "vn-index", "vn30", "hnx-index", "gia vang",
])
_INVESTMENT_ASSET_RE = _build_keyword_re([
    "vàng", "sjc", "bitcoin", "btc", "eth", "ethereum", "crypto",
    "tiền số", "tien so", "tiền ảo", "tien ao", "usd", "đô la", "ngoại tệ", "ngoai te",
    "tỷ giá", "ty gia", "chứng khoán mỹ", "nasdaq", "dow jones", "s&p", "sp500",
    "dầu thô", "dau tho", "brent", "wti", "lãi suất", "lai suat",
    "bất động sản", "bat dong san", "ngân hàng", "ngan hang", "thép",
])
_INVESTMENT_INTENT_RE = _build_keyword_re([
    "mua", "bán", "giữ", "nắm giữ", "giá", "tăng", "giảm", "lên không", "còn lên",
    "xuống", "tích trữ", "tích luỹ", "tích lũy", "lãi", "lỗ", "lời", "nên", "đáng",
    "hold", "all in",
])


def chat_needs_stock_rules(text: str) -> bool:
    """Lượt chat có cần phần phân tích cổ phiếu của persona (core/config.
    load_chat_skill(stock=True)) không: có từ chứng khoán rõ ràng, mã/chỉ số đã
    biết, từ ngữ đầu tư, hoặc hỏi mua/bán/giá một tài sản đầu tư (vàng, ngoại tệ,
    crypto, nhóm ngành...). Hàm thuần, không gọi mạng (chỉ xét mã có sẵn trong
    danh sách, không verify qua DNSE). Bắt nhầm chỉ là gửi persona đầy đủ như
    trước khi tách, nên khi lưỡng lự thì nghiêng về bật."""
    text = _strip_urls(text)
    if not text.strip():
        return False
    if _has_explicit_stock_word(text) or _INVESTMENT_TALK_RE.search(text):
        return True
    if detect_symbol_candidates(text)[0]:
        return True
    return bool(_INVESTMENT_ASSET_RE.search(text) and _INVESTMENT_INTENT_RE.search(text))


# Giờ giao dịch HOSE/HNX/UPCOM (T2-T6 không phải ngày lễ, 9h00-15h00) - xem
# stock/calendar.py. Giữ tên hàm ở đây vì nhiều nơi (và test) monkeypatch
# stock.analysis.is_market_hours.
def is_market_hours(now: datetime | None = None) -> bool:
    return trading_calendar.is_market_hours(now)


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

_PERSONA_UNKNOWN_PRICE_NOTE = (
    "\n\n⚠️ Các mức giá {prices} trong phần diễn giải KHÔNG có trong dữ liệu hệ thống - "
    "chỉ là ước lượng định tính của persona, không dùng làm entry/stop/target."
)


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
    for sym, res in zip(subset, results, strict=False):
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
    # Xác suất tăng sau 5 phiên theo model thống kê walk-forward
    # (stock/trend_model.py). CHỈ tham khảo trong prompt, KHÔNG phải gate;
    # None khi chưa train model / sklearn không có.
    ml_prob_up: float | None = None
    # Giá khớp realtime (chỉ để tính lãi/lỗ vị thế hiển thị) - None khi không có.
    realtime_price: float | None = None
    # True khi nến HÔM NAY chưa đóng cửa (đang trong giờ giao dịch) đã bị loại
    # khỏi chuỗi tính chỉ báo: khối lượng của nến dở dang làm sai volume
    # ratio/xác nhận breakout/distribution day.
    intraday_bar_excluded: bool = False
    # True khi chuỗi còn gap quyền chưa điều chỉnh trong cửa sổ chỉ báo policy.
    unadjusted_gap: bool = False

async def _safe_sector_prompt(symbol: str) -> str:
    try:
        sector_keys = sector.get_symbol_sectors(symbol)
        if not sector_keys: return ""
        ctx = await sector.build_sector_context(sector_keys)
        return sector.build_sector_prompt_section(ctx, symbol)
    except Exception:
        # Nuốt exception để không sập pipeline, nhưng PHẢI log - nếu sector
        # chết hẳn (đổi format nguồn...), prompt thiếu ngành vĩnh viễn mà log
        # không có dấu vết thì không bao giờ phát hiện được.
        logger.warning("_safe_sector_prompt lỗi cho %s", symbol, exc_info=True)
        return ""

async def _safe_fundamentals_prompt(symbol: str) -> str:
    try:
        bundle = await fundamentals.fetch_fundamentals(symbol)
        return fundamentals.build_fundamentals_prompt_section(bundle.valuation, bundle.foreign, symbol, growth=bundle.growth, events=bundle.events, sector_pe_avg=bundle.sector_pe_avg, sector_pe_sample=bundle.sector_pe_sample, sector_pe_label=bundle.sector_pe_label, sector_profile=bundle.sector_profile, sector_benchmark=bundle.sector_benchmark)
    except Exception:
        logger.warning("_safe_fundamentals_prompt lỗi cho %s", symbol, exc_info=True)
        return ""

# NGUỒN DUY NHẤT cho khái niệm "fact nào được coi là thuộc danh mục đầu tư".
# services/tools.py._tool_get_portfolio và scheduler.py._build_portfolio_digest
# import lại từ đây thay vì tự khai báo bản copy riêng - trước đây có 3 bản
# giống nhau, sửa 1 chỗ là 2 chỗ kia lệch ngay.
PORTFOLIO_FACT_KEYWORDS = ("danh_muc", "portfolio", "co_phieu")


def is_portfolio_fact(key: str) -> bool:
    """Key của fact trong trí nhớ dài hạn có thuộc nhóm danh mục đầu tư không."""
    return any(kw in key for kw in PORTFOLIO_FACT_KEYWORDS)


async def _is_holding_symbol(user_id: int | None, symbol: str) -> bool:
    """Prefer recorded positions; old memory only applies to unmanaged symbols."""
    if user_id is None:
        return False
    try:
        from stock import portfolio
        holding = await portfolio.get_holding(user_id, symbol)
        if holding is not None:
            return holding.quantity > 0
        if await portfolio.was_closed(user_id, symbol):
            return False
        facts = await db.get_facts(user_id)
    except Exception:
        return False
    symbol_re = re.compile(rf"\b{re.escape(symbol)}\b", re.IGNORECASE)
    return any(
        symbol_re.search(value)
        for key, value in facts
        if is_portfolio_fact(key)
    )

def _drop_unfinished_bar(series: providers.OhlcvSeries, now: datetime | None = None) -> tuple[providers.OhlcvSeries, bool]:
    """Loại nến HÔM NAY khỏi chuỗi khi đang trong giờ giao dịch (nến chưa đóng
    cửa). DNSE chart-api trả cả nến đang hình thành; giữ nó thì volume ratio,
    xác nhận breakout bằng volume và distribution day chạy trên KHỐI LƯỢNG DỞ
    DANG (lúc 10h mới ~20% cả phiên). Trả series MỚI, không mutate object
    đang nằm trong cache của providers."""
    now = now or datetime.now(_VN_TZ)
    if not series.dates or not series.closes or not is_market_hours(now):
        return series, False
    if series.dates[-1] != now.strftime("%Y-%m-%d") or len(series.closes) < 2:
        return series, False
    return replace(
        series,
        closes=series.closes[:-1], highs=series.highs[:-1], lows=series.lows[:-1],
        volumes=series.volumes[:-1], dates=series.dates[:-1],
    ), True


async def build_context(symbol: str, *, user_id: int | None = None, is_holding: bool | None = None) -> StockContext | None:
    results = await asyncio.gather(
        providers.fetch_ohlcv(symbol, days=260), providers.fetch_ohlcv("VNINDEX", days=260),
        providers.fetch_quote(symbol), providers.fetch_news(symbol),
        _safe_sector_prompt(symbol), _safe_fundamentals_prompt(symbol),
        fundamentals.fetch_company_news(symbol), providers.fetch_symbol_exchange(symbol),
        return_exceptions=True
    )
    # Chỉ exception của OHLCV mã chính (task 0) mới làm chết pipeline - đây
    # là dữ liệu bắt buộc duy nhất. VNINDEX/quote/news/company_news là dữ liệu
    # TÙY CHỌN (downstream có guard cho thiếu): trước đây 1 timeout của Google
    # News là toàn bộ /phantich trả lỗi dù mọi chỉ báo chính vẫn tính được.
    _OPTIONAL_FALLBACKS = {
        1: providers.OhlcvSeries(symbol="VNINDEX"),
        2: None,          # quote -> realtime_quote_line = None
        3: [],            # news -> không có tin
        5: "",            # fundamentals prompt -> rỗng
        6: [],            # company news -> rỗng
        7: None,          # exchange -> policy dùng bước giá 100đ an toàn
    }
    for idx, r in enumerate(results):
        if isinstance(r, BaseException):
            if idx == 0:
                raise r
            logger.warning("Task dữ liệu phụ #%s lỗi cho %s - bỏ qua: %s", idx, symbol, r)
            results[idx] = _OPTIONAL_FALLBACKS.get(idx, [] if idx in (3, 6) else "")
    symbol_series, vnindex_series, quote, news, sector_prompt, fundamentals_prompt, company_news, exchange = results
    # Tin công ty CHÍNH CHỦ từ VCI (đã confirmed=True) + tin cào Google News,
    # loại trùng theo tiêu đề (không phân biệt hoa/thường).
    news = list(news or [])
    seen_titles = {n.title.strip().lower() for n in news}
    news = news + [n for n in (company_news or []) if n.title.strip().lower() not in seen_titles]
    if not symbol_series.closes: return None

    # Nến hôm nay chưa đóng cửa -> loại khỏi CẢ chuỗi mã lẫn VNINDEX (cùng mốc
    # thời gian, distribution day của VNINDEX cũng dùng volume).
    now_vn = datetime.now(_VN_TZ)
    symbol_series, intraday_excluded = _drop_unfinished_bar(symbol_series, now_vn)
    vnindex_series, _ = _drop_unfinished_bar(vnindex_series, now_vn)

    # Kiểm tra chuỗi giá đã điều chỉnh sau chia tách/cổ tức chưa. Mọi bước
    # dưới đây tạo series MỚI (dataclasses.replace) - object gốc đang nằm
    # trong cache 90s của providers, mutate sẽ làm các lượt phân tích kế
    # tiếp nhận luôn chuỗi/cờ đã đổi mà không kiểm lại.
    audit = price_adjust.audit_series(symbol, symbol_series.source, symbol_series.closes, symbol_series.dates)
    if audit.gaps:
        logger.warning("Chuỗi giá %s (nguồn %s) có %d gap nghi điều chỉnh giá", symbol, symbol_series.source, len(audit.gaps))
    adjustment_note = audit.note
    events_applied = 0
    # Điều chỉnh cổ tức tiền/sự kiện cổ phiếu cho MỌI chuỗi (không chỉ khi có
    # gap >= 12%): cổ tức 3-6% là trường hợp phổ biến nhất và trước đây bị bỏ qua.
    if symbol_series.closes:
        outcome = await corporate_actions.adjust_with_corporate_actions(
            symbol, symbol_series.closes, symbol_series.highs, symbol_series.lows, symbol_series.dates,
            symbol_series.volumes,
        )
        if outcome.events_applied:
            events_applied = outcome.events_applied
            logger.info("Đã điều chỉnh %d sự kiện quyền cho %s (nguồn %s)", outcome.events_applied, symbol, symbol_series.source)
            symbol_series = replace(
                symbol_series, closes=outcome.closes, highs=outcome.highs, lows=outcome.lows,
                volumes=outcome.volumes or symbol_series.volumes,
            )
            adjustment_note = outcome.note or adjustment_note
    # Gap còn lại SAU điều chỉnh = gap chưa giải thích được. Chỉ coi là "đã
    # điều chỉnh" khi đã áp sự kiện VÀ không còn gap nào (trước đây chỉ cần
    # note bắt đầu bằng "ℹ️" là gắn is_adjusted=True dù vẫn còn gap).
    remaining_gaps = price_adjust.detect_price_gaps(symbol_series.closes, symbol_series.dates)
    if events_applied and not remaining_gaps:
        is_adjusted = True
    elif any(gap.level == "certain" for gap in remaining_gaps):
        is_adjusted = False
    else:
        is_adjusted = None
    symbol_series = replace(symbol_series, is_adjusted=is_adjusted)
    window_gap = price_adjust.last_gap_in_window(remaining_gaps, len(symbol_series.closes))
    # Policy/indicator phải dùng quality của chính chuỗi sau cùng. Nếu vừa
    # điều chỉnh corporate action, quality raw trước đó không còn mô tả dữ
    # liệu thực tế đang được dùng nữa.
    quality = validation.validate_ohlcv(
        symbol_series.closes, symbol_series.highs, symbol_series.lows, symbol_series.volumes, symbol_series.dates
    )
    if window_gap is not None and quality.usable:
        gap_reason = (
            f"chuỗi giá có gap {rfmt.fmt_signed_pct(window_gap.move_pct)} ngày {window_gap.date or 'không rõ'}"
            + (f" (khớp {window_gap.ratio_hint})" if window_gap.ratio_hint else "")
            + " nghi ngày không hưởng quyền CHƯA điều chỉnh"
        )
        quality = replace(quality, status="degraded", reasons=[*quality.reasons, gap_reason])
    # analysis_price = close của phiên gần nhất trong CHUỖI OHLCV - toàn bộ
    # feature/policy (Donchian, Bollinger, S/R, session...) phải nhìn CÙNG
    # một thời điểm để không tự mâu thuẫn nhau (P0-3). quote.price là tick
    # realtime, có thể lệch pha với closes[-1] (vd trước giờ mở cửa, hoặc
    # cuối tuần) - chỉ dùng để HIỂN THỊ "giá khớp hiện tại", không đưa vào
    # tính stop/target/R:R.
    analysis_price = symbol_series.price
    last_bar_date = symbol_series.dates[-1] if symbol_series.dates else ""
    realtime_quote_line = None
    realtime_price = None
    if quote is not None:
        realtime_price = quote.price
        time_note = _quote_time_note(quote, verbose=False)
        diff = quote.price - analysis_price
        diff_pct = diff / analysis_price * 100 if analysis_price else 0.0
        sign = "+" if diff > 0 else ""
        realtime_quote_line = (
            f"Giá khớp hiện tại: {_fmt_price(quote.price)} VND ({time_note}), chênh {sign}{_fmt_price(diff)} VND "
            f"({rfmt.fmt_signed_pct(diff_pct)}) so với giá dùng để tính toán - CHỈ tham khảo hiển thị, KHÔNG dùng để tính stop/target/R:R bên dưới."
        )
    # news_impact CHỈ được tính trên tin CÓ NHẮC ĐÚNG MÃ. providers.fetch_news
    # truy vấn Google News bằng chuỗi "<mã> cổ phiếu" nhưng không kiểm tra mã
    # có trong tiêu đề, nên tin thị trường chung/tin của mã khác vẫn lọt vào.
    news_impact = rfmt.relevant_news_impact([(n.title, n.sentiment, n.confirmed) for n in news], symbol)
    stats = feat.calc_signal_stats(symbol_series.closes, symbol_series.volumes, analysis_price)
    relative_strength = feat.calc_relative_strength(symbol_series.closes, vnindex_series.closes, lookback=65)

    enhanced, indicator_summary = None, ""
    if quality.usable and len(symbol_series.closes) >= 20:
        enhanced = feat.build_enhanced_indicators(symbol_series.closes, analysis_price, symbol_series.highs, symbol_series.lows)
        indicator_summary = feat.build_indicator_summary(enhanced, symbol)

    # Gap quyền chưa điều chỉnh trong cửa sổ: hỗ trợ/kháng cự CHỈ lấy từ các
    # phiên SAU gap - đỉnh/đáy trước gap khác hệ quy chiếu giá, không phải
    # kháng cự thật (đỉnh cũ chưa chia thưởng từng làm target ảo, R:R ảo).
    level_start = window_gap.index if window_gap is not None else 0
    level_highs = symbol_series.highs[level_start:]
    level_lows = symbol_series.lows[level_start:]
    level_closes = symbol_series.closes[level_start:]
    ma_alignment = feat.calc_ma_alignment(symbol_series.closes) if len(symbol_series.closes) >= 20 else None
    support_resistance = feat.calc_support_resistance(level_highs, level_lows, analysis_price, 30) if level_highs else None
    key_levels = feat.find_key_levels(level_highs, level_lows, level_closes) if level_highs else feat.KeyLevels([], [])
    liquidity = feat.calc_liquidity(symbol_series.volumes)
    session = feat.calc_session_metrics(symbol_series.closes, symbol_series.highs, symbol_series.lows, symbol_series.volumes)

    trend_score = feat.calc_trend_score(ma_alignment, stats.rsi14, enhanced.macd.histogram) if ma_alignment and ma_alignment.alignment != "unknown" and enhanced else None
    vnindex_multi_tf = feat.calc_multi_timeframe(vnindex_series.closes) if vnindex_series.closes else None
    vnindex_adx = feat.calc_adx(vnindex_series.closes, vnindex_series.highs, vnindex_series.lows) if vnindex_series.closes else None
    vnindex_distribution_days = feat.calc_distribution_days(vnindex_series.closes, vnindex_series.volumes)

    holding = is_holding if is_holding is not None else await _is_holding_symbol(user_id, symbol)
    decision = policy.evaluate_policy(policy.PolicyInputs(price=analysis_price, stats=stats, enhanced=enhanced, ma_alignment=ma_alignment, support_resistance=support_resistance, liquidity=liquidity, session=session, relative_strength=relative_strength, trend_score=trend_score, news_impact=news_impact, quality=quality, vnindex_multi_tf=vnindex_multi_tf, vnindex_adx=vnindex_adx, vnindex_distribution_days=vnindex_distribution_days, key_levels=key_levels, exchange=exchange, is_holding=holding, unadjusted_gap=window_gap is not None))

    # Model thống kê (chỉ tham khảo, không phải gate). Tự vô hiệu khi chưa
    # train model, sklearn không có, hoặc stats không qua gate chất lượng.
    ml_prob_up = None
    try:
        from stock import trend_model
        ml_prob_up = trend_model.prob_up_for_series(symbol_series, vnindex_series)
    except Exception:
        logger.debug("trend_model không khả dụng cho %s", symbol, exc_info=True)

    fetched_at_vn = now_vn.strftime("%H:%M ngày %d/%m/%Y")

    return StockContext(
        symbol=symbol, price=analysis_price, fetched_at_vn=fetched_at_vn, stats=stats, decision=decision,
        enhanced=enhanced, indicator_summary=indicator_summary, support_resistance=support_resistance,
        key_levels=key_levels, ma_alignment=ma_alignment, sector_prompt=sector_prompt,
        fundamentals_prompt=fundamentals_prompt, news=news, relative_strength=relative_strength,
        liquidity=liquidity, quality=quality, realtime_quote_line=realtime_quote_line,
        last_bar_date=last_bar_date, adjustment_note=adjustment_note, ml_prob_up=ml_prob_up,
        realtime_price=realtime_price, intraday_bar_excluded=intraday_excluded,
        unadjusted_gap=window_gap is not None,
    )

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


def _data_as_of_line(ctx: StockContext) -> str | None:
    if not ctx.last_bar_date:
        return None
    line = (
        f"Toàn bộ chỉ báo kỹ thuật bên dưới được tính trên NẾN ĐÓNG CỬA ngày "
        f"{ctx.last_bar_date}, KHÔNG phải giá đang khớp - khi mô tả chỉ báo phải "
        f"gắn mốc thời gian này."
    )
    if getattr(ctx, "intraday_bar_excluded", False):
        line += " Phiên hôm nay CHƯA đóng cửa nên nến hôm nay (khối lượng dở dang) không được đưa vào chỉ báo."
    return line


def build_prompt(
    ctx: StockContext,
    *,
    news_analysis=None,
    bull_case=None,
    bear_case=None,
    final_decision=None,
    reference_only: bool = False,
    position_line: str | None = None,
) -> str:
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

    # Tin có nhắc đúng mã (hoặc confirmed=True từ nguồn chính chủ VCI) được
    # đẩy lên trước để không bị tin thị trường chung chiếm hết 5 suất.
    ranked_news = sorted(
        ctx.news,
        key=lambda n: not rfmt.is_news_relevant(n.title, ctx.symbol, n.confirmed),
    )
    news = [
        {
            "tag": "🟢" if n.sentiment > 0.2 else ("🔴" if n.sentiment < -0.2 else "⚪"),
            "title": n.title,
            "source": n.source,
            "date": rfmt.fmt_news_date(n.pub_date),
            "confirmed": rfmt.is_news_relevant(n.title, ctx.symbol, n.confirmed),
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
        if reference_only:
            # Bản tham khảo: mốc gần nhất chỉ là dữ kiện kỹ thuật, không phải
            # điểm vào/ra - bỏ chỉ dẫn "dùng mốc này khi nói về điểm vào/ra".
            nearest_levels_line = nearest_levels_line.replace(
                "MỐC GẦN NHẤT (dùng mốc này khi nói về điểm vào/ra): ", "MỐC GẦN NHẤT: "
            ).replace(", KHÔNG dùng làm điểm vào/ra ngắn hạn", "")

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
            + (f", SMA200 {rfmt.format_level(price, e.sma200)} (xu hướng dài hạn)" if e.sma200 is not None else "")
        )

    data_as_of_line = _data_as_of_line(ctx)

    # Xác suất từ model thống kê - chỉ hiện khi model qua gate chất lượng.
    # Ghi rõ AUC out-of-sample và "không phải gate" để LLM không trình bày
    # như một khuyến nghị định lượng của hệ thống.
    ml_prob_line = None
    if ctx.ml_prob_up is not None and not reference_only:
        from stock import trend_model as _trend_model
        stats_meta = _trend_model.model_stats()
        auc_note = f", AUC out-of-sample {stats_meta['auc']}" if stats_meta.get("auc") else ""
        pct = f"{ctx.ml_prob_up * 100:.0f}%"
        ml_prob_line = (
            f"[MODEL THỐNG KÊ — CHỈ THAM KHẢO]: xác suất model dự báo close TĂNG sau 5 phiên: {pct}"
            f" (gradient boosting, một lần split theo ngày{auc_note}; không phải gate định lượng, KHÔNG được dùng"
            " như lý do chính để đổi action hệ thống; nêu nếu hữu ích, bỏ qua nếu xung đột với dữ liệu thật)."
        )

    trade_plan = None
    entry_zone_text = None
    scenarios = []
    backtest_stats_line = None
    if not reference_only:
        if d.trade_plan:
            tp = d.trade_plan
            trade_plan = {
                "entry_low": _fmt_price(tp.entry_low), "entry_high": _fmt_price(tp.entry_high),
                "stop": _fmt_price(tp.stop), "target1": _fmt_price(tp.target1),
                "target2": _fmt_price(tp.target2) if tp.target2 is not None else None,
                "position_size_pct": tp.position_size_pct, "plan_note": tp.plan_note,
            }
            entry_zone_text = f"{_fmt_price(tp.entry_low)} - {_fmt_price(tp.entry_high)}"
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
        realtime_quote_line=_reference_quote_line(ctx) if reference_only else ctx.realtime_quote_line,
        key_levels_line=key_levels_line,
        nearest_levels_line=nearest_levels_line, momentum_detail_line=momentum_detail_line,
        ma_distance_line=ma_distance_line, data_as_of_line=data_as_of_line,
        adjustment_note=ctx.adjustment_note, ml_prob_line=ml_prob_line,
        trade_plan=trade_plan, entry_zone_text=entry_zone_text, scenarios=scenarios,
        backtest_stats_line=backtest_stats_line,
        news_analysis=news_analysis, bull_case=bull_case, bear_case=bear_case,
        final_decision=final_decision, reference_only=reference_only, position_line=position_line,
    )


def _reference_quote_line(ctx: StockContext) -> str | None:
    """Dòng giá khớp cho bản tham khảo: bỏ đoạn nhắc stop/target/R:R."""
    line = ctx.realtime_quote_line
    return line.split(" - CHỈ tham khảo")[0] if line else line


def _adjustment_warning(ctx: StockContext) -> str | None:
    """Dòng cảnh báo điều chỉnh giá cho bản rút gọn - phân biệt chuỗi ĐÃ điều
    chỉnh (note "ℹ️") với chuỗi còn gap chưa giải thích được."""
    note = ctx.adjustment_note or ""
    if not note:
        return None
    if note.startswith("ℹ️") and not getattr(ctx, "unadjusted_gap", False) and "Vẫn còn" not in note:
        return "ℹ️ Chuỗi giá đã được điều chỉnh theo dữ liệu quyền (cổ tức/cổ phiếu thưởng)."
    return "⚠️ Chuỗi giá có gap nghi ngày giao dịch không hưởng quyền chưa được điều chỉnh - SMA50/Donchian/ATR/trend 3 tháng kém tin cậy."


def _fallback_text(ctx: StockContext, *, fallback_note: bool = True) -> str:
    """Bản số liệu rule-based ĐẦY ĐỦ (tín hiệu + kế hoạch) - chỉ cho người có
    quyền xem đầy đủ (chủ bot, admin). Đây là nguồn số chuẩn của báo cáo."""
    d = ctx.decision
    action_label = _ACTION_LABEL_VI.get(d.action, d.action)
    tp = getattr(d, "trade_plan", None)
    if d.action == "BUY" and tp is not None:
        price_line = (
            f"Vùng mua {_fmt_price(tp.entry_low)}-{_fmt_price(tp.entry_high)} | Tỷ trọng tham khảo {rfmt.fmt_number(tp.position_size_pct, 1)}% NAV"
            f" | SL {_fmt_price(tp.stop)} | T1 {_fmt_price(tp.target1)}"
            + (f" | T2 {_fmt_price(tp.target2)}" if tp.target2 is not None else "")
            + f" | R:R ~{rfmt.fmt_number(d.rr_ratio)} (tính tại giá mua cao nhất của vùng)"
        )
    elif d.action == "BUY":
        price_line = f"Giá {_fmt_price(ctx.price)} | TP {_fmt_price(d.target_price)} | SL {_fmt_price(d.stop_price)} | R:R ~{d.rr_ratio}"
    elif d.action == "SELL":
        support_part = (
            f" | Mốc hỗ trợ tham chiếu nếu giảm tiếp: {rfmt.format_level(ctx.price, d.target_price)} (không phải mức chờ để bán)"
            if d.target_price is not None else ""
        )
        price_line = (
            f"KHÔNG phải vùng mua | Giá {_fmt_price(ctx.price)} | Nếu đang giữ: tín hiệu nghiêng về giảm vị thế/thoát hàng theo kỷ luật rủi ro"
            f"{support_part} | Tín hiệu SELL vô hiệu nếu giá đóng cửa vượt {_fmt_price(d.stop_price)}"
        )
    elif d.action == "HOLD" and d.target_price is not None and d.stop_price is not None:
        price_line = f"Đang giữ, tín hiệu vẫn thuận lợi | Giá {_fmt_price(ctx.price)} | Tham khảo chốt lời {_fmt_price(d.target_price)} | Cân nhắc cắt lỗ dưới {_fmt_price(d.stop_price)} | R:R ~{d.rr_ratio}"
    elif d.action == "NO_TRADE": price_line = "Hệ thống chưa đủ edge để đề xuất vùng giá - ưu tiên đứng ngoài quan sát."
    else: price_line = f"Giá {_fmt_price(ctx.price)} | Chưa đủ rõ xu hướng để đề xuất vùng giá cụ thể"

    lines = [
        f"📊 **{ctx.symbol}** — **{_fmt_price(ctx.price)} VND** ({ctx.fetched_at_vn})",
        f"Tín hiệu hệ thống: **{action_label}** (confidence {d.confidence}, setup {d.setup_type}, regime {d.market_regime})",
        price_line,
        f"RSI14 {rfmt.fmt_number(ctx.stats.rsi14, 1) if ctx.stats.rsi14 is not None else 'chưa đủ dữ liệu'} | Trend ~3M {rfmt.fmt_number(ctx.stats.trend_3m)}% | Risk: {d.risk_level}",
    ]
    as_of = _data_as_of_line(ctx)
    if as_of: lines.append(f"Chỉ báo tính trên nến đóng cửa ngày {ctx.last_bar_date}." + (" Nến hôm nay chưa đóng cửa nên chưa tính." if getattr(ctx, "intraday_bar_excluded", False) else ""))
    if ctx.ml_prob_up is not None: lines.append(f"Model thống kê (tham khảo): xác suất tăng sau 5 phiên ~{ctx.ml_prob_up * 100:.0f}%.")
    if d.reasons: lines.append("Lý do: " + "; ".join(d.reasons[:4]))
    if d.invalidation_reason: lines.append(f"Lưu ý: {d.invalidation_reason}")
    if ctx.realtime_quote_line: lines.append(ctx.realtime_quote_line)
    if ctx.liquidity and ctx.liquidity.is_thin: lines.append("⚠️ Thanh khoản TB20 quá thấp.")
    adjustment = _adjustment_warning(ctx)
    if adjustment: lines.append(adjustment)
    if ctx.quality.status != "ok": lines.append(f"⚠️ Chất lượng dữ liệu: {ctx.quality.status}")
    if fallback_note: lines.append("⚠️ API dự phòng không phản hồi nên đây là bản rút gọn.")
    return "\n".join(lines)


def _reference_text(ctx: StockContext, *, fallback_note: bool = False) -> str:
    """Bản số liệu CHỈ THAM KHẢO cho thành viên không có quyền xem đầy đủ:
    giá, chỉ báo, bối cảnh - KHÔNG nhãn hành động, vùng mua, stop, target,
    R:R hay tỷ trọng."""
    d = ctx.decision
    lines = [
        f"📊 **{ctx.symbol}** — **{_fmt_price(ctx.price)} VND** ({ctx.fetched_at_vn})",
        "Thông tin thị trường tham khảo (không phải tín hiệu giao dịch):",
        f"RSI14 {rfmt.fmt_number(ctx.stats.rsi14, 1) if ctx.stats.rsi14 is not None else 'chưa đủ dữ liệu'}"
        f" | Trend ~3M {rfmt.fmt_number(ctx.stats.trend_3m)}% | Sức mạnh tương đối vs VNINDEX {rfmt.fmt_number(ctx.relative_strength)}%",
    ]
    if ctx.ma_alignment and ctx.ma_alignment.alignment != "unknown":
        lines.append(f"Đường trung bình MA5/MA10/MA20: {ctx.ma_alignment.alignment}")
    sr = ctx.support_resistance
    if sr and sr.support:
        lines.append(
            f"Biên giá 30 phiên: thấp nhất {rfmt.format_level(ctx.price, sr.support)}, "
            f"cao nhất {rfmt.format_level(ctx.price, sr.resistance)}"
        )
    if ctx.liquidity:
        lines.append(
            f"Thanh khoản: KL phiên gần nhất {rfmt.fmt_price(ctx.liquidity.current_volume)} "
            f"({rfmt.fmt_number(ctx.liquidity.liquidity_ratio_pct, 1)}% TB20)"
            + (" - thanh khoản TB20 thấp" if ctx.liquidity.is_thin else "")
        )
    lines.append(f"Bối cảnh VNINDEX: {d.market_regime}")
    if ctx.last_bar_date:
        lines.append(f"Chỉ báo tính trên nến đóng cửa ngày {ctx.last_bar_date}." + (" Nến hôm nay chưa đóng cửa nên chưa tính." if getattr(ctx, "intraday_bar_excluded", False) else ""))
    quote_line = _reference_quote_line(ctx)
    if quote_line:
        lines.append(quote_line)
    adjustment = _adjustment_warning(ctx)
    if adjustment: lines.append(adjustment)
    if ctx.quality.status != "ok": lines.append(f"⚠️ Chất lượng dữ liệu: {ctx.quality.status}")
    if fallback_note: lines.append("⚠️ API dự phòng không phản hồi nên đây là bản rút gọn.")
    return "\n".join(lines)


# Nhóm action "tương thích" khi kiểm tra bản diễn giải: WATCH và NO_TRADE đều
# là "chưa mở mới", HOLD thường đi kèm "theo dõi" - không coi là mâu thuẫn.
_COMPATIBLE_ACTIONS = {
    "BUY": frozenset({"BUY"}),
    "SELL": frozenset({"SELL"}),
    "HOLD": frozenset({"HOLD", "WATCH"}),
    "WATCH": frozenset({"WATCH", "NO_TRADE"}),
    "NO_TRADE": frozenset({"NO_TRADE", "WATCH"}),
}
_CONFLICT_DROPPED_NOTE = "⚠️ Bản diễn giải AI lần này nêu tín hiệu/kế hoạch khác với hệ thống nên đã bị lược bỏ - chỉ dùng bản số liệu hệ thống."
_REFERENCE_DROPPED_NOTE = "⚠️ Bản diễn giải tự động có nội dung ngoài phạm vi thông tin tham khảo nên đã được thay bằng bản số liệu."
_UNKNOWN_PRICE_NOTE = "⚠️ Phần diễn giải dưới đây có mốc giá không nằm trong số liệu hệ thống - chỉ dùng các mốc ở bản số liệu hệ thống."


def _enforce_policy_contract(
    text: str, ctx: StockContext, prompt: str, *, full_access: bool, rule_report_sent: bool,
) -> str:
    """Lớp chặn TRONG CODE cho hợp đồng "action/số do policy chốt, LLM chỉ diễn
    giải": nhãn hành động lệch Decision (hoặc ngôn ngữ kế hoạch ở bản tham
    khảo) -> bỏ bản diễn giải, trả bản số liệu; mốc giá không có trong dữ liệu
    đã cấp -> đặt cảnh báo + bản số liệu lên TRƯỚC phần diễn giải."""
    if not text:
        return text
    deterministic = _fallback_text(ctx, fallback_note=False) if full_access else _reference_text(ctx)
    if full_access:
        allowed = _COMPATIBLE_ACTIONS.get(ctx.decision.action, frozenset({ctx.decision.action}))
        conflicts = rfmt.find_action_conflicts(text, allowed)
        dropped_note = _CONFLICT_DROPPED_NOTE
    else:
        conflicts = rfmt.find_action_conflicts(text, frozenset()) + rfmt.find_plan_language(text)
        dropped_note = _REFERENCE_DROPPED_NOTE
    if conflicts:
        logger.warning("Bản diễn giải LLM cho %s vi phạm hợp đồng policy: %s", ctx.symbol, conflicts[:5])
        return dropped_note if rule_report_sent else f"{deterministic}\n\n{dropped_note}"
    unknown = rfmt.find_unknown_prices(text, prompt)
    if unknown:
        logger.warning("Bản diễn giải LLM cho %s có mốc giá ngoài dữ liệu: %s", ctx.symbol, unknown[:5])
        head = _UNKNOWN_PRICE_NOTE if rule_report_sent else f"{deterministic}\n\n{_UNKNOWN_PRICE_NOTE}"
        return f"{head}\n\n{text}"
    return text


async def _position_line(user_id: int | None, symbol: str, price: float | None) -> str | None:
    """Lãi/lỗ vị thế đang ghi nhận, TÍNH TRONG CODE (LLM không được tự tính)."""
    if user_id is None or not price:
        return None
    try:
        from stock import portfolio
        raw = await portfolio.get_holding(user_id, symbol)
        # Vị thế ĐÃ quy đổi theo sự kiện quyền được giá xác nhận (cổ tức CP, thưởng,
        # chia tách) - không thì ngày GDKHQ dòng này báo lỗ giả ~1/(1+r).
        view = (await portfolio.corporate_views([raw]))[0] if raw is not None else None
    except Exception:
        return None
    holding = view.effective if view is not None else None
    if holding is None or holding.quantity <= 0 or holding.average_price <= 0:
        return None
    pnl = (price - holding.average_price) * holding.quantity
    pnl_pct = (price / holding.average_price - 1) * 100
    sign = "+" if pnl > 0 else ""
    line = (
        f"Vị thế đang ghi nhận (hệ thống đã tính sẵn): {_fmt_price(holding.quantity)} cp, giá vốn {_fmt_price(holding.average_price)} VND"
        f" -> lãi/lỗ tạm tính {sign}{_fmt_price(pnl)} VND ({rfmt.fmt_signed_pct(pnl_pct)}) theo giá {_fmt_price(price)} VND."
    )
    if view.applied:
        line += " (Đã quy đổi theo " + ", ".join(portfolio.stock_event_label(e) for e in view.applied) + ".)"
    if view.unconfirmed:
        line += " Lưu ý: lịch quyền có sự kiện GDKHQ chưa được giá xác nhận nên lãi/lỗ này có thể lệch."
    return line


_STALE_NOTE = "\n\n⏱️ _Lưu ý: dữ liệu/thời điểm bên trên là của lần phân tích gần nhất_"

PORTFOLIO_KEYWORDS = ["cơ cấu", "co cau", "danh mục", "danh muc", "tỷ trọng", "ty trong", "giữ hay bán", "nên giữ mã nào"]

def wants_portfolio_analysis(text: str, symbols: list[str]) -> bool:
    if len(symbols) < 2: return False
    lower = text.lower()
    return any(kw in lower for kw in PORTFOLIO_KEYWORDS)


async def _portfolio_row(ctx: StockContext, user_id: int | None, *, full_access: bool) -> str:
    """1 dòng số liệu cố định cho từng mã trong danh mục (tính trong code)."""
    rsi_text = rfmt.fmt_number(ctx.stats.rsi14, 1) if ctx.stats.rsi14 is not None else "chưa đủ dữ liệu"
    parts = [f"• **{ctx.symbol}**: giá {_fmt_price(ctx.price)} VND"]
    if ctx.last_bar_date:
        parts[0] += f" (nến {ctx.last_bar_date})"
    if full_access:
        d = ctx.decision
        signal = f"tín hiệu hệ thống {_ACTION_LABEL_VI.get(d.action, d.action)} (confidence {d.confidence})"
        if d.action == "SELL" and d.stop_price is not None:
            signal += f", vô hiệu nếu đóng cửa vượt {_fmt_price(d.stop_price)}"
        elif d.stop_price is not None and d.target_price is not None and d.action in ("BUY", "HOLD"):
            signal += f", stop {_fmt_price(d.stop_price)} / target {_fmt_price(d.target_price)}"
        parts.append(signal)
    parts.append(f"RSI {rsi_text} | trend 3M {rfmt.fmt_number(ctx.stats.trend_3m)}%")
    if ctx.support_resistance and ctx.support_resistance.support:
        sr = ctx.support_resistance
        parts.append(
            f"biên 30 phiên {rfmt.format_level(ctx.price, sr.support)} / {rfmt.format_level(ctx.price, sr.resistance)}"
        )
    position = await _position_line(user_id, ctx.symbol, ctx.realtime_price or ctx.price)
    if position:
        parts.append(position.replace("Vị thế đang ghi nhận (hệ thống đã tính sẵn): ", "vị thế: "))
    adjustment = _adjustment_warning(ctx)
    if adjustment and adjustment.startswith("⚠️"):
        parts.append("⚠️ giá lịch sử nghi chưa điều chỉnh, so sánh kém tin cậy")
    return " | ".join(parts)


async def analyze_portfolio(symbols: list[str], user_text: str, *, user_id: int | None = None, full_access: bool = True) -> str:
    # Dùng chung _is_holding_symbol với analyze_symbol thay vì gắn cứng
    # is_holding=True cho mọi mã - tránh 2 đường (phân tích đơn lẻ vs danh
    # mục) cho action khác nhau với cùng 1 mã khi user hỏi cả 2 kiểu.
    holdings = await asyncio.gather(*[_is_holding_symbol(user_id, sym) for sym in symbols])
    tasks = [build_context(sym, user_id=user_id, is_holding=holding) for sym, holding in zip(symbols, holdings, strict=False)]
    contexts = await asyncio.gather(*tasks, return_exceptions=True)
    valid_contexts = [ctx for ctx in contexts if not isinstance(ctx, BaseException) and ctx is not None]
    failed_symbols = [sym for sym, ctx in zip(symbols, contexts, strict=False) if isinstance(ctx, BaseException) or ctx is None]
    if failed_symbols:
        # Không âm thầm bỏ mã lỗi: user hỏi cơ cấu 5 mã mà báo cáo chỉ còn 3
        # mã mà không một chữ nào giải thích là thiếu dữ liệu sai lệch nhận định.
        logger.warning("analyze_portfolio: không lấy được dữ liệu cho %s", ", ".join(failed_symbols))
    if not valid_contexts:
        return "Em không lấy được dữ liệu của các mã này lúc này, anh thử lại sau xíu nha."

    # Bảng số liệu CỐ ĐỊNH do code tính (tín hiệu policy + lãi/lỗ vị thế):
    # luôn đứng đầu tin nhắn - LLM chỉ được bình luận bên dưới, KHÔNG quyết
    # định giữ/cắt/hạ tỷ trọng và KHÔNG tự tính lãi/lỗ.
    rows = [await _portfolio_row(ctx, user_id, full_access=full_access) for ctx in valid_contexts]
    title = "📋 Số liệu danh mục (hệ thống tính):" if full_access else "📋 Số liệu tham khảo các mã (không phải tín hiệu giao dịch):"
    table = title + "\n" + "\n".join(rows)
    if failed_symbols:
        table += f"\n⚠️ Chưa lấy được dữ liệu: {', '.join(failed_symbols)} - chưa được phân tích."

    if full_access:
        scope_rule = (
            "Tín hiệu của từng mã do hệ thống quy tắc chốt ở bảng trên - chỉ được nhắc lại ĐÚNG tín hiệu đó; "
            "TUYỆT ĐỐI không tự quyết định giữ/cắt/hạ tỷ trọng hay đổi tín hiệu, không tự tính lãi/lỗ hay mốc giá mới. "
        )
    else:
        scope_rule = (
            "Đây là bản THÔNG TIN THAM KHẢO: TUYỆT ĐỐI không nêu nên mua/bán/giữ/cắt/hạ tỷ trọng, không nhãn tín hiệu, "
            "không vùng mua, stop, target, R:R hay tỷ trọng; không tự tính lãi/lỗ hay mốc giá mới. "
        )
    prompt = (
        f"[SỐ LIỆU DANH MỤC LÚC NÀY - do hệ thống tính]:\n{table}\n\n"
        f"[CÂU HỎI TỪ NGƯỜI DÙNG]:\n\"{user_text}\"\n\n"
        f"Lan Anh hãy TỔNG HỢP THÔNG TIN THAM KHẢO về các mã trên: so sánh dữ liệu kỹ thuật/bối cảnh giữa các mã một cách trung tính. "
        f"{scope_rule}"
        f"Mọi mốc giá nhắc tới phải kèm khoảng cách % đã cho ở trên, không tự tính lại. "
        f"Mã nào có cảnh báo giá chưa điều chỉnh thì phải nói rõ hạn chế đó khi so sánh. "
        f"Mã chưa lấy được dữ liệu thì nêu rõ là chưa được phân tích. "
        f"Số viết theo chuẩn Việt Nam (dấu chấm nghìn, dấu phẩy thập phân). "
        f"Văn phong: xưng em/anh tự nhiên, rõ ràng, không tự giới thiệu, không dùng danh xưng thân mật quá đà. "
        f"KHÔNG tự viết câu disclaimer - hệ thống tự thêm ở cuối."
    )

    from ai import orchestrator
    try:
        response = await orchestrator.ask(prompt)
        commentary = rfmt.clean_analysis_output((response.text or "").strip())
    except Exception:
        logger.exception("Lỗi khi tổng hợp danh mục")
        commentary = ""
    if commentary:
        if full_access:
            allowed = frozenset().union(*(_COMPATIBLE_ACTIONS.get(c.decision.action, frozenset()) for c in valid_contexts))
            conflicts = rfmt.find_action_conflicts(commentary, allowed)
        else:
            conflicts = rfmt.find_action_conflicts(commentary, frozenset()) + rfmt.find_plan_language(commentary)
        if conflicts:
            logger.warning("Bình luận danh mục vi phạm hợp đồng policy: %s", conflicts[:5])
            commentary = _CONFLICT_DROPPED_NOTE if full_access else _REFERENCE_DROPPED_NOTE
        elif rfmt.find_unknown_prices(commentary, prompt):
            commentary = f"{_UNKNOWN_PRICE_NOTE}\n\n{commentary}"
        elif getattr(response, "used_fallback", False):
            commentary += "\n\n⚙️ _Báo cáo do API dự phòng trả về, ngôn ngữ có thể thô hơn bình thường._"
    result = table + (f"\n\n{commentary}" if commentary else "")
    # Disclaimer phải là dòng cuối - ensure_disclaimer luôn đặt dòng chuẩn ở cuối.
    return rfmt.ensure_disclaimer(result)

async def analyze_symbol(
    symbol: str, user_text: str = "", *, force_refresh: bool = False, user_id: int | None = None,
    on_rule_report=None, full_access: bool = True,
) -> str:
    """full_access=False (thành viên Zalo không phải admin): bản THAM KHẢO -
    không nhãn hành động, vùng mua, stop, target, R:R, tỷ trọng."""
    symbol = symbol.strip().upper()
    holding = await _is_holding_symbol(user_id, symbol)
    if not force_refresh and not user_text:
        cached = _cache_get(symbol, holding, full_access)
        if cached: return cached + _STALE_NOTE

    try:
        ctx = await build_context(symbol, user_id=user_id, is_holding=holding)
    except Exception:
        logger.exception("Lỗi lấy dữ liệu phân tích %s", symbol)
        ctx = None
    if ctx is None: return messages.STOCK_FETCH_ERROR.format(symbol=symbol)

    deterministic_text = _fallback_text if full_access else _reference_text
    # Gửi ngay bản số liệu rule-based thay vì bắt người dùng chờ cả pipeline
    # debate nhiều lượt LLM mới thấy tin đầu tiên. Đây là nguồn số chuẩn; bản
    # diễn giải phía sau bị _enforce_policy_contract kiểm lại.
    rule_report_sent = False
    if on_rule_report is not None:
        await on_rule_report(rfmt.ensure_disclaimer(
            deterministic_text(ctx, fallback_note=False)
            + ("\n\n⏳ Bản số liệu nhanh theo quy tắc hệ thống; bản diễn giải đầy đủ sẽ gửi tiếp ngay sau đây." if full_access
               else "\n\n⏳ Bản số liệu nhanh; phần tổng hợp thông tin tham khảo sẽ gửi tiếp ngay sau đây.")
        ))
        rule_report_sent = True

    position_line = await _position_line(user_id, symbol, ctx.realtime_price or ctx.price) if holding else None

    # Debate tuần tự (KHÔNG song song - bear cần thấy bull_case để phản biện
    # trực tiếp). Bản tham khảo chỉ chạy bước tóm tắt tin tức: bull/bear/
    # manager đều xoay quanh action hệ thống - nội dung thành viên không được xem.
    from stock import debate as stock_debate
    news_analysis = bull_case = bear_case = final_decision = None
    try:
        if full_access:
            news_analysis, bull_case, bear_case, final_decision = await stock_debate.run_debate(ctx)
        else:
            news_analysis = await stock_debate.run_news_step(ctx)
    except Exception:
        logger.exception("Lỗi pipeline debate cho %s - bỏ qua, dùng prompt không có debate", symbol)
        news_analysis = bull_case = bear_case = final_decision = None

    prompt = build_prompt(
        ctx, news_analysis=news_analysis, bull_case=bull_case, bear_case=bear_case, final_decision=final_decision,
        reference_only=not full_access, position_line=position_line,
    )
    if user_text:
        if full_access:
            answer_rule = (
                "trả lời trực tiếp vào câu hỏi bằng cách diễn giải ĐÚNG action hệ thống đã chốt ở trên. "
                "Nếu có dòng lãi/lỗ vị thế do hệ thống tính sẵn thì dùng đúng con số đó; KHÔNG tự tính lãi/lỗ, "
                "KHÔNG đặt thêm mốc giá hay hành động nào ngoài dữ liệu đã cho."
            )
        else:
            answer_rule = (
                "trả lời trong phạm vi THÔNG TIN THAM KHẢO. Nếu câu hỏi xin lời khuyên mua/bán/giữ, nói rõ em chỉ "
                "tổng hợp thông tin tham khảo, không đưa khuyến nghị hay vùng giá giao dịch. KHÔNG tự tính lãi/lỗ."
            )
        prompt += (
            f"\n\n[LƯU Ý QUAN TRỌNG TỪ HỆ THỐNG]:\n"
            f"Người dùng vừa hỏi: \"{user_text}\"\n"
            f"Lan Anh hãy dựa trên dữ liệu ở trên, {answer_rule} TUYỆT ĐỐI không tự giới thiệu, "
            f"không đoán cảm xúc của anh ấy, không dùng danh xưng thân mật quá đà."
        )

    from ai import orchestrator
    from core import config
    # Nhịp giãn tải trước bước tổng hợp cuối - chỉ cần khi debate đã chạy ít
    # nhất 1 bước; nếu cả 4 bước đều None thì không có lệnh LLM nào vừa gọi,
    # ngủ 3s là chết thời gian thuần.
    if config.ROUTER9_STEP_DELAY_SEC and any([news_analysis, bull_case, bear_case, final_decision]):
        await asyncio.sleep(config.ROUTER9_STEP_DELAY_SEC)
    try:
        response = await orchestrator.ask(prompt)
        # Làm sạch trước khi trả về: bỏ câu tự giới thiệu, bỏ danh xưng thân
        # mật, gộp disclaimer LLM tự viết (ensure_disclaimer đặt dòng chuẩn).
        text = rfmt.clean_analysis_output((response.text or "").strip())
        text = _enforce_policy_contract(text, ctx, prompt, full_access=full_access, rule_report_sent=rule_report_sent)
        result = text or None
        if result and getattr(response, "used_fallback", False):
            result = result + "\n\n⚙️ _Báo cáo do API dự phòng trả về, ngôn ngữ có thể thô hơn bình thường._"
    except Exception:
        logger.exception("Gemini lỗi khi phân tích %s", symbol)
        result = None

    if result is None:
        # LLM không trả được báo cáo: nếu bản rule-based đã gửi trước đó thì
        # hết content mới, còn không thì trả bản rút gọn thay thế.
        if rule_report_sent:
            return ""
        result = deterministic_text(ctx)

    result = rfmt.ensure_disclaimer(result)

    if not user_text:
        _cache_set(symbol, holding, result, full_access)

    return result

async def analyze_persona(
    symbol: str, persona_ids: list[str], user_text: str = "", *, user_id: int | None = None, full_access: bool = True,
) -> str:
    """Góc nhìn persona trên số liệu rule-based - tầng chat giải trí, KHÔNG là
    một bước phân tích: không chấm action, không sinh vùng giá (ràng buộc nằm
    trong prompt, xem stock/personas.py). Không cache - câu hỏi persona mang
    tính đối thoại, trả lời cũ khi giá đã đổi còn tệ hơn chờ vài giây.
    full_access=False: dữ liệu đưa persona là bản tham khảo (không tín hiệu/
    vùng giá) và output bị kiểm tra không chứa khuyến nghị/kế hoạch."""
    symbol = symbol.strip().upper()
    try:
        ctx = await build_context(symbol, user_id=user_id, is_holding=await _is_holding_symbol(user_id, symbol))
    except Exception:
        logger.exception("Lỗi lấy dữ liệu persona %s", symbol)
        ctx = None
    if ctx is None:
        return messages.STOCK_FETCH_ERROR.format(symbol=symbol)

    data_text = _fallback_text(ctx, fallback_note=False) if full_access else _reference_text(ctx)
    fundamentals_prompt = await _safe_fundamentals_prompt(symbol)
    if fundamentals_prompt:
        data_text += "\n\n" + fundamentals_prompt
    prompt = stock_personas.build_persona_prompt(symbol, persona_ids, data_text, user_text, reference_only=not full_access)

    from ai import orchestrator
    try:
        response = await orchestrator.ask(prompt)
        text = rfmt.clean_analysis_output((response.text or "").strip())
        if not text:
            return "Chưa diễn giải được góc nhìn này lúc này, anh thử lại sau nhé."
    except Exception:
        logger.exception("LLM lỗi khi diễn giải persona cho %s", symbol)
        return messages.STOCK_ANALYZE_FAILED.format(symbol=symbol)
    if not full_access:
        conflicts = rfmt.find_action_conflicts(text, frozenset()) + rfmt.find_plan_language(text)
        if conflicts or rfmt.find_unknown_prices(text, prompt):
            logger.warning("Persona cho thành viên vi phạm phạm vi tham khảo (%s): %s", symbol, conflicts[:5])
            text = f"{_reference_text(ctx)}\n\n{_REFERENCE_DROPPED_NOTE}"
    else:
        # Chủ bot cũng không được nhận số giá bịa: persona chỉ diễn giải định
        # tính, mọi mức giá phải có trong dữ liệu đã cấp (giống _enforce_policy_contract).
        unknown = rfmt.find_unknown_prices(text, prompt)
        if unknown:
            logger.warning("Persona (%s) nêu mức giá không có trong dữ liệu: %s", symbol, unknown[:5])
            text += _PERSONA_UNKNOWN_PRICE_NOTE.format(
                prices=", ".join(f"{p:,.0f}".replace(",", ".") for p in unknown[:5])
            )
    return rfmt.ensure_disclaimer(text)
