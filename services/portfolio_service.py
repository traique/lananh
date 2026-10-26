"""Commands and deterministic natural-language actions for stock holdings."""

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from stock import calendar as trading_calendar
from stock import portfolio, providers
from stock.report_format import fmt_price as _fmt
from stock.sector import ALL_KNOWN_SYMBOLS

_NUMBER_RE = r"[\d.,]+(?:k|nghìn|nghin|tr|triệu|trieu|m)?"
_SYMBOL_RE = r"[A-Za-z]{3,4}"
# Mã đứng riêng một từ: "Urban area" không được thành "ban AREA".
_SYMBOL_WORD = rf"(?<![\w])({_SYMBOL_RE})(?![\w])"
# Giá dưới ngưỡng này (không kèm k/nghìn/tr) được hiểu theo quy ước bảng giá
# VN là NGHÌN đồng: "giá 25.5" = 25.500đ, "giá 120" = 120.000đ.
_THOUSAND_VND_CUTOFF = 1_000
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def parse_number(raw: str) -> float:
    text = (raw or "").strip().lower().replace(" ", "")
    multiplier = 1.0
    for suffix, value in (
        ("nghìn", 1_000.0),
        ("nghin", 1_000.0),
        ("triệu", 1_000_000.0),
        ("trieu", 1_000_000.0),
        ("tr", 1_000_000.0),
        ("k", 1_000.0),
        ("m", 1_000_000.0),
    ):
        if text.endswith(suffix):
            multiplier = value
            text = text[: -len(suffix)]
            break
    if not text:
        raise ValueError("missing number")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif multiplier == 1 and re.fullmatch(r"\d{1,3}([.,]\d{3})+", text):
        text = text.replace(".", "").replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    return float(text) * multiplier


def parse_price(raw: str) -> float:
    """Giá cổ phiếu từ người dùng -> VND.

    Bảng giá VN niêm yết theo NGHÌN đồng nên người dùng hay gõ "giá 25.5",
    "giá 120": trước đây lưu thẳng 25,5đ/120đ làm lãi/lỗ sai hàng nghìn lần.
    Số < 1.000 không kèm hậu tố được nhân 1.000; có hậu tố (k, tr...) hoặc
    đã >= 1.000 thì giữ nguyên. Khối lượng dùng parse_number, không qua đây.
    """
    text = (raw or "").strip().lower().replace(" ", "")
    value = parse_number(text)
    has_suffix = bool(re.search(r"(k|nghìn|nghin|tr|triệu|trieu|m)$", text))
    if not has_suffix and 0 < value < _THOUSAND_VND_CUTOFF:
        value *= 1_000
    return value


async def _validate_symbol(symbol: str) -> str | None:
    normalized = symbol.strip().upper()
    if normalized in ALL_KNOWN_SYMBOLS:
        return normalized
    try:
        return normalized if await providers.verify_symbol_exists(normalized) else None
    except Exception:
        return None


def _extract_alerts(text: str) -> tuple[float | None, float | None]:
    stop_match = re.search(
        rf"(?:stop|sl|cắt lỗ|cat lo)\s*(?:ở|o|là|la|:)?\s*({_NUMBER_RE})",
        text,
        re.IGNORECASE,
    )
    target_match = re.search(
        rf"(?:target|tp|chốt lời|chot loi)\s*(?:ở|o|là|la|:)?\s*({_NUMBER_RE})",
        text,
        re.IGNORECASE,
    )
    stop_price = parse_price(stop_match.group(1)) if stop_match else None
    target_price = parse_price(target_match.group(1)) if target_match else None
    return stop_price, target_price


async def add_holding(
    user_id: int,
    symbol: str,
    quantity: float,
    price: float,
    *,
    stop_price: float | None = None,
    target_price: float | None = None,
    replace: bool = False,
) -> str:
    valid_symbol = await _validate_symbol(symbol)
    if valid_symbol is None:
        return f"❌ Không xác nhận được mã {symbol.upper()} trên nguồn dữ liệu thị trường."
    if quantity <= 0 or price <= 0:
        return "❌ Số lượng và giá vốn phải lớn hơn 0."
    if replace:
        holding = await portfolio.set_holding(
            user_id,
            valid_symbol,
            quantity,
            price,
            stop_price=stop_price,
            target_price=target_price,
        )
        action = "Đã cập nhật"
    else:
        holding = await portfolio.add_purchase(
            user_id,
            valid_symbol,
            quantity,
            price,
            stop_price=stop_price,
            target_price=target_price,
        )
        action = "Đã ghi nhận mua"
    text = (
        f"✅ {action} {valid_symbol}: {_fmt(holding.quantity)} cp, "
        f"giá vốn bình quân {_fmt(holding.average_price)}đ."
    )
    if not replace:
        text += f"\n⏳ Cổ phiếu mua về tài khoản {settlement_note()}."
    return text


def settlement_note(today: date | None = None) -> str:
    """Ngày thanh toán T+2 (theo phiên giao dịch) cho lệnh khớp hôm nay."""
    today = today or datetime.now(_VN_TZ).date()
    session = (
        today
        if trading_calendar.is_trading_day(today)
        else trading_calendar.previous_trading_day(today)
    )
    settle = trading_calendar.next_trading_day(trading_calendar.next_trading_day(session))
    return f"ngày {settle:%d/%m} (T+2, nếu lệnh khớp phiên {session:%d/%m})"


def _signed(value: float) -> str:
    return ("+" if value > 0 else "") + _fmt(value)


def format_sale(symbol: str, result: portfolio.SaleResult) -> str:
    """Câu xác nhận lệnh bán; lãi/lỗ đã chốt tính trong code (stock.portfolio)."""
    if result.closed:
        text = f"✅ Đã bán hết và xóa {symbol} khỏi danh mục."
    else:
        text = f"✅ Đã giảm {symbol}, còn {_fmt(result.holding.quantity)} cp."
    if result.price is not None and result.realized_pnl is not None:
        icon = "🟢" if result.realized_pnl > 0 else "🔴"
        text += (
            f"\n{icon} Bán {_fmt(result.quantity)} cp @ {_fmt(result.price)}đ, giá vốn "
            f"{_fmt(result.average_cost)}đ: lãi/lỗ đã chốt {_signed(result.realized_pnl)}đ "
            f"(sau phí {_fmt(result.fee)}đ, thuế {_fmt(result.tax)}đ)."
        )
        text += f"\n⏳ Tiền bán về tài khoản {settlement_note()}."
    else:
        text += (
            f"\nℹ️ Chưa có giá bán nên em chưa tính lãi/lỗ đã chốt - gõ /giaban {symbol} <GIÁ> "
            "để bổ sung nhé."
        )
    return text


async def sell_holding(
    user_id: int, symbol: str, quantity: float | None, price: float | None = None
) -> str:
    if quantity is not None and quantity <= 0:
        return "❌ Số lượng bán phải lớn hơn 0."
    if price is not None and price <= 0:
        return "❌ Giá bán phải lớn hơn 0."
    current = await portfolio.get_holding(user_id, symbol)
    if current is None:
        return f"❌ {symbol.upper()} không có trong danh mục."
    result = await portfolio.record_sale(user_id, symbol, quantity, price)
    if result is None:
        return f"❌ {symbol.upper()} không có trong danh mục."
    return format_sale(symbol.upper(), result)


_MONTH_ARG_RE = re.compile(r"^(?:(\d{4})-(\d{1,2})|(\d{1,2})/(\d{4}))$")


def _parse_month_arg(raw: str) -> tuple[str | None, int | None]:
    """ "2026-09" / "9/2026" -> ("2026-09", None); "12" -> (None, 12 tháng)."""
    match = _MONTH_ARG_RE.match(raw.strip())
    if match:
        year = int(match.group(1) or match.group(4))
        month = int(match.group(2) or match.group(3))
        if 1 <= month <= 12 and 2000 <= year <= 2100:
            return f"{year:04d}-{month:02d}", None
        raise ValueError("bad month")
    months = int(raw)
    if not 1 <= months <= 24:
        raise ValueError("bad months")
    return None, months


async def _realized_command(user_id: int, parts: list[str]) -> str:
    month, months = None, 6
    if len(parts) >= 2:
        try:
            month, parsed = _parse_month_arg(parts[1])
        except ValueError:
            return "Cú pháp: /laicp [THÁNG] - vd /laicp, /laicp 2026-09, /laicp 9/2026, /laicp 12 (12 tháng)"
        months = parsed or months
    report = await portfolio.realized_by_month(user_id, months=months, month=month)
    scope = f" tháng {int(month[5:])}/{month[:4]}" if month else f" {months} tháng gần nhất"
    return portfolio.format_realized(report, title_scope=scope)


async def _fill_price_command(user_id: int, parts: list[str]) -> str:
    if len(parts) < 3:
        return "Cú pháp: /giaban <MÃ> <GIÁ> - bổ sung giá cho lệnh bán gần nhất chưa có giá."
    try:
        price = parse_price(parts[2])
    except ValueError:
        return "❌ Giá bán không hợp lệ. Ví dụ: /giaban HPG 27.5 hoặc /giaban HPG 27500"
    symbol = parts[1].upper()
    tx = await portfolio.fill_sale_price(user_id, symbol, price)
    if tx is None:
        return f"❌ Không có lệnh bán {symbol} nào đang thiếu giá."
    return (
        f"✅ Đã bổ sung giá {_fmt(price)}đ cho lệnh bán {_fmt(tx.quantity)} {symbol}: "
        f"lãi/lỗ đã chốt {_signed(tx.realized_pnl or 0)}đ (phí {_fmt(tx.fee)}đ, thuế {_fmt(tx.tax)}đ)."
    )


async def handle_command(user_id: int, text: str) -> str | None:
    parts = text.strip().split()
    if not parts:
        return None
    command = parts[0].lower().split("@", 1)[0]
    if command == "/danhmuc":
        return await portfolio.build_report(user_id)
    if command in {"/themcp", "/capnhatcp"}:
        if len(parts) < 4:
            return (
                f"Cú pháp: {command} <MÃ> <SỐ_LƯỢNG> <GIÁ_VỐN> [STOP] [TARGET]\n"
                f"Ví dụ: {command} FPT 100 120k 110k 145k (giá dưới 1.000 hiểu là nghìn đồng: 120 = 120.000đ)"
            )
        try:
            values = [parse_number(parts[2])] + [parse_price(value) for value in parts[3:6]]
        except ValueError:
            return "❌ Số lượng/giá không hợp lệ. Ví dụ giá: 120000 hoặc 120k."
        stop_price = values[2] if len(values) >= 3 else None
        target_price = values[3] if len(values) >= 4 else None
        return await add_holding(
            user_id,
            parts[1],
            values[0],
            values[1],
            stop_price=stop_price,
            target_price=target_price,
            replace=command == "/capnhatcp",
        )
    if command == "/bancp":
        if len(parts) < 2:
            return (
                "Cú pháp: /bancp <MÃ> [SỐ_LƯỢNG|hết] [GIÁ_BÁN]\n"
                "Ví dụ: /bancp HPG 1000 27.5 (có giá bán thì em tính lãi/lỗ đã chốt sau phí, thuế)"
            )
        quantity = None
        if len(parts) >= 3 and parts[2].lower() not in {"hết", "het", "all"}:
            try:
                quantity = parse_number(parts[2])
            except ValueError:
                return "❌ Số lượng bán không hợp lệ."
        price = None
        if len(parts) >= 4:
            try:
                price = parse_price(parts[3])
            except ValueError:
                return "❌ Giá bán không hợp lệ. Ví dụ: 27.5 hoặc 27500"
        if price is None:
            return await sell_holding(user_id, parts[1], quantity)
        return await sell_holding(user_id, parts[1], quantity, price)
    if command == "/lichsucp":
        symbol = parts[1].upper() if len(parts) >= 2 else None
        return await portfolio.build_history_text(user_id, symbol=symbol)
    if command == "/laicp":
        return await _realized_command(user_id, parts)
    if command == "/giaban":
        return await _fill_price_command(user_id, parts)
    from services import watchlist_service  # import trễ: watchlist_service dùng module này

    if command in watchlist_service.COMMANDS:
        return await watchlist_service.handle_command(user_id, command, parts)
    if command == "/xoacp":
        if len(parts) < 2:
            return "Cú pháp: /xoacp <MÃ>"
        deleted = await portfolio.delete_holding(user_id, parts[1])
        return (
            f"✅ Đã xóa {parts[1].upper()} khỏi danh mục."
            if deleted
            else f"❌ {parts[1].upper()} không có trong danh mục."
        )
    return None


# Câu hỏi/xin ý kiến KHÔNG BAO GIỜ được ghi vào danh mục: "Có nên bán FPT
# không?" từng xoá sạch vị thế FPT vì regex bán chỉ cần thấy "bán FPT".
_QUESTION_RE = re.compile(
    r"\?|(?<![\w])(?:có nên|co nen|nên|nen|hay là|hay la|giữ hay|giu hay|hay bán|hay ban|hay mua|"
    r"hay giữ|hay giu|thế nào|the nao|ra sao|làm sao|lam sao|sao rồi|sao roi|được không|duoc khong|"
    r"có được|co duoc|có phải|co phai|khuyên|khuyen|tư vấn|tu van|phân tích|phan tich|đánh giá|danh gia|"
    r"nhận định|nhan dinh|định mua|dinh mua|định bán|dinh ban|muốn mua|muon mua|muốn bán|muon ban|"
    r"tính mua|tinh mua|tính bán|tinh ban|có nên không|liệu|lieu)(?![\w])"
    r"|(?<![\w])(?:không|khong|ko|chưa|chua|nhỉ|nhi|hả|ha|à)\s*[.!]*\s*$",
    re.IGNORECASE,
)
# Đánh dấu câu KỂ việc đã làm (đã/vừa/mới ...) - chỉ câu kể mới được ghi.
_DONE_PREFIX = r"(?:đã|da|vừa|vua|mới|moi|vừa mới|vua moi)\s+"
_DONE_SUFFIX_RE = re.compile(r"(?<![\w])(?:rồi|roi)(?![\w])\s*[.!]*\s*$", re.IGNORECASE)
_ALL_WORDS = {"hết", "het", "all", "toàn bộ", "toan bo"}


def _is_question_or_advice(text: str) -> bool:
    return bool(_QUESTION_RE.search(text))


def _is_known_symbol(symbol: str) -> bool:
    return symbol.upper() in ALL_KNOWN_SYMBOLS


async def _held_symbol(user_id: int, candidates: list[str]) -> str | None:
    """Mã đầu tiên trong danh sách ứng viên mà user ĐANG giữ."""
    for candidate in candidates:
        try:
            if await portfolio.get_holding(user_id, candidate.upper()) is not None:
                return candidate.upper()
        except Exception:
            return None
    return None


# Giá bán tuỳ chọn sau số lượng: "đã bán HPG 1000 giá 27", "bán HPG 1000 cp @27 rồi".
_SELL_PRICE = rf"(?:\s+(?:cp|cổ phiếu|co phieu))?(?:\s*(?:giá|gia|@)\s*({_NUMBER_RE}))?"
_SELL_DONE_RE = re.compile(
    rf"(?<![\w]){_DONE_PREFIX}(?:bán|ban|chốt|chot|cắt|cat)\s+(?:(hết|het|toàn bộ|toan bo)\s+)?"
    rf"(?:mã\s+|ma\s+)?{_SYMBOL_WORD}(?:\s+({_NUMBER_RE}|hết|het|all)(?![\w]))?{_SELL_PRICE}",
    re.IGNORECASE,
)
_SELL_ROI_RE = re.compile(
    rf"(?<![\w])(?:bán|ban)\s+(?:(hết|het)\s+)?(?:mã\s+|ma\s+)?{_SYMBOL_WORD}"
    rf"(?:\s+({_NUMBER_RE}|hết|het|all)(?![\w]))?{_SELL_PRICE}\s+(?:rồi|roi)(?![\w])",
    re.IGNORECASE,
)
_SELL_BARE_RE = re.compile(
    rf"^\s*(?:bán|ban)\s+(?:mã\s+|ma\s+)?{_SYMBOL_WORD}(?:\s+({_NUMBER_RE}|hết|het|all))?\s*[.!]*\s*$",
    re.IGNORECASE,
)


async def _handle_sell_statement(user_id: int, text: str) -> str | None:
    """Ghi nhận bán CHỈ với câu kể đã làm ("đã/vừa/mới bán ...", "bán ... rồi").
    Không có số lượng rõ ràng và không có "hết/all" -> KHÔNG xoá vị thế, hỏi
    xác nhận. Câu lệnh trần "bán FPT 50" (không phải câu kể) -> chỉ hướng dẫn."""
    match = _SELL_DONE_RE.search(text) or _SELL_ROI_RE.search(text)
    if match:
        all_before, symbol, quantity_raw = match.group(1), match.group(2), match.group(3)
        price_raw = match.group(4)
        symbol = symbol.upper()
        # Câu kể chung chung ("đã bán cho anh ấy" -> "CHO") không được bị bắt
        # làm lệnh danh mục: token ngoài danh sách mã đã biết chỉ được xử lý
        # khi user thật sự đang giữ mã đó. Mã đã biết thì sell_holding tự báo
        # "không có trong danh mục".
        if not _is_known_symbol(symbol) and await _held_symbol(user_id, [symbol]) is None:
            return None
        price = None
        if price_raw:
            try:
                price = parse_price(price_raw)
            except ValueError:
                price = None
        sell_all = bool(all_before) or (quantity_raw or "").lower() in _ALL_WORDS
        quantity = None
        if not sell_all and quantity_raw:
            try:
                quantity = parse_number(quantity_raw)
            except ValueError:
                quantity = None
        if sell_all or quantity:
            if price is None:
                return await sell_holding(user_id, symbol, quantity)
            return await sell_holding(user_id, symbol, quantity, price)
        return (
            f"⚠️ Em chưa ghi nhận bán {symbol} vì chưa rõ số lượng. Anh gõ /bancp {symbol} <SỐ_LƯỢNG>, "
            f'hoặc "đã bán hết {symbol}" nếu đã bán toàn bộ nhé.'
        )
    bare = _SELL_BARE_RE.search(text)
    if bare:
        symbol = bare.group(1).upper()
        if await _held_symbol(user_id, [symbol]) is None:
            return None
        quantity_hint = f" {bare.group(2)}" if bare.group(2) else " <SỐ_LƯỢNG>"
        return (
            f'ℹ️ Em chưa ghi nhận gì cho {symbol}. Nếu anh ĐÃ bán, gõ "đã bán {symbol}{quantity_hint}" '
            f'hoặc /bancp {symbol}{quantity_hint}; muốn phân tích thì hỏi "phân tích {symbol}" nhé.'
        )
    return None


_BUY_PATTERNS = [
    re.compile(
        rf"(?<![\w])(?:(?:{_DONE_PREFIX})?(?:mua|thêm|them))\s+{_SYMBOL_WORD}\s+({_NUMBER_RE})\s*"
        rf"(?:cp|cổ phiếu|co phieu)?\s*(?:giá vốn|gia von|giá|gia|@)\s*({_NUMBER_RE})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<![\w])(?:đang giữ|dang giu|ghi nhận|ghi nhan)\s+({_NUMBER_RE})\s*"
        rf"(?:cp|cổ phiếu|co phieu)?\s+{_SYMBOL_WORD}\s*"
        rf"(?:giá vốn|gia von|giá|gia|@)\s*({_NUMBER_RE})",
        re.IGNORECASE,
    ),
]
_BUY_DECLARATIVE_RE = re.compile(
    rf"(?<![\w]){_DONE_PREFIX}(?:mua|thêm|them)(?![\w])|(?<![\w])(?:giá vốn|gia von)(?![\w])|@"
    r"|(?<![\w])(?:đang giữ|dang giu|ghi nhận|ghi nhan)(?![\w])",
    re.IGNORECASE,
)


async def maybe_handle_natural_language(user_id: int, text: str) -> str | None:
    stripped = text.strip()
    lower = stripped.lower()
    portfolio_queries = (
        "danh mục",
        "danh muc",
        "đang giữ mã",
        "dang giu ma",
        "đang nắm",
        "dang nam",
        "cổ phiếu đang giữ",
        "co phieu dang giu",
    )
    if any(phrase in lower for phrase in portfolio_queries) and any(
        word in lower
        for word in (
            "xem",
            "đang giữ",
            "dang giu",
            "đang nắm",
            "dang nam",
            "lãi lỗ",
            "lai lo",
            "của anh",
            "cua anh",
            "mã nào",
            "ma nao",
        )
    ):
        # Chỉ ĐỌC danh mục - an toàn kể cả khi là câu hỏi.
        return await portfolio.build_report(user_id)

    # Mọi thao tác GHI (mua/bán/đặt cảnh báo) bị chặn với câu hỏi/xin ý kiến:
    # để nhánh phân tích cổ phiếu xử lý thay vì ghi nhầm vào danh mục.
    if _is_question_or_advice(stripped):
        return None

    # "báo anh khi HPG về 25": ngưỡng giá NGƯỜI DÙNG tự đặt (không đụng danh mục).
    from services import watchlist_service

    alert_reply = await watchlist_service.maybe_handle_alert_request(user_id, stripped)
    if alert_reply is not None:
        return alert_reply

    for index, pattern in enumerate(_BUY_PATTERNS):
        match = pattern.search(stripped)
        if not match:
            continue
        if index == 0:
            symbol, quantity_raw, price_raw = match.groups()
        else:
            quantity_raw, symbol, price_raw = match.groups()
        if not _BUY_DECLARATIVE_RE.search(stripped) and not _DONE_SUFFIX_RE.search(stripped):
            # "mua FPT 100 giá 25" trơ trọi: lệnh hay ý định, chưa chắc đã
            # mua - chỉ hướng dẫn, không ghi.
            return (
                f'ℹ️ Em chưa ghi nhận gì. Nếu anh ĐÃ mua, gõ "đã mua {symbol.upper()} {quantity_raw} giá {price_raw}" '
                f"hoặc /themcp {symbol.upper()} {quantity_raw} {price_raw} nhé."
            )
        stop_price, target_price = _extract_alerts(stripped)
        return await add_holding(
            user_id,
            symbol,
            parse_number(quantity_raw),
            parse_price(price_raw),
            stop_price=stop_price,
            target_price=target_price,
        )

    alert_match = re.search(
        r"(?<![\w])(?:đặt|dat|cập nhật|cap nhat)(?![\w]).*(?<![\w])(?:stop|sl|cắt lỗ|cat lo|target|tp|chốt lời|chot loi)(?![\w])",
        stripped,
        re.IGNORECASE,
    )
    if alert_match:
        candidates = re.findall(_SYMBOL_WORD, stripped)
        symbol = await _held_symbol(user_id, candidates)
        if symbol is None:
            known = next((c.upper() for c in candidates if _is_known_symbol(c)), None)
            return f"❌ {known} không có trong danh mục." if known else None
        stop_price, target_price = _extract_alerts(stripped)
        if stop_price is None and target_price is None:
            return None
        current = await portfolio.get_holding(user_id, symbol)
        if current is None:
            return f"❌ {symbol} không có trong danh mục."
        updated = await portfolio.update_alerts(
            user_id,
            symbol,
            stop_price=stop_price if stop_price is not None else current.stop_price,
            target_price=(target_price if target_price is not None else current.target_price),
        )
        return (
            f"✅ Đã cập nhật mức theo dõi {symbol}: "
            f"SL {_fmt(updated.stop_price) if updated and updated.stop_price else '-'} | "
            f"TP {_fmt(updated.target_price) if updated and updated.target_price else '-'}"
        )

    return await _handle_sell_statement(user_id, stripped)
