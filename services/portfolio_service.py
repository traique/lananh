"""Commands and deterministic natural-language actions for stock holdings."""

import re

from stock import portfolio, providers
from stock.sector import ALL_KNOWN_SYMBOLS

_NUMBER_RE = r"[\d.,]+(?:k|nghìn|nghin|tr|triệu|trieu|m)?"
_SYMBOL_RE = r"[A-Za-z]{3,4}"


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


def _fmt(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")


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
    stop_price = parse_number(stop_match.group(1)) if stop_match else None
    target_price = parse_number(target_match.group(1)) if target_match else None
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
    return (
        f"✅ {action} {valid_symbol}: {_fmt(holding.quantity)} cp, "
        f"giá vốn bình quân {_fmt(holding.average_price)}đ."
    )


async def sell_holding(user_id: int, symbol: str, quantity: float | None) -> str:
    if quantity is not None and quantity <= 0:
        return "❌ Số lượng bán phải lớn hơn 0."
    current = await portfolio.get_holding(user_id, symbol)
    if current is None:
        return f"❌ {symbol.upper()} không có trong danh mục."
    updated = await portfolio.sell(user_id, symbol, quantity)
    if updated is None:
        return f"✅ Đã bán hết và xóa {symbol.upper()} khỏi danh mục."
    return f"✅ Đã giảm {symbol.upper()}, còn {_fmt(updated.quantity)} cp."


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
                f"Ví dụ: {command} FPT 100 120k 110k 145k"
            )
        try:
            values = [parse_number(value) for value in parts[2:6]]
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
            return "Cú pháp: /bancp <MÃ> [SỐ_LƯỢNG|hết]"
        quantity = None
        if len(parts) >= 3 and parts[2].lower() not in {"hết", "het", "all"}:
            try:
                quantity = parse_number(parts[2])
            except ValueError:
                return "❌ Số lượng bán không hợp lệ."
        return await sell_holding(user_id, parts[1], quantity)
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
        return await portfolio.build_report(user_id)

    buy_patterns = [
        rf"(?:mua|thêm|them)\s+({_SYMBOL_RE})\s+({_NUMBER_RE})\s*"
        rf"(?:cp|cổ phiếu|co phieu)?\s*(?:giá vốn|gia von|giá|gia|@)\s*({_NUMBER_RE})",
        rf"(?:đang giữ|dang giu|ghi nhận|ghi nhan)\s+({_NUMBER_RE})\s*"
        rf"(?:cp|cổ phiếu|co phieu)?\s+({_SYMBOL_RE})\s*"
        rf"(?:giá vốn|gia von|giá|gia|@)\s*({_NUMBER_RE})",
    ]
    for index, pattern in enumerate(buy_patterns):
        match = re.search(pattern, stripped, re.IGNORECASE)
        if not match:
            continue
        if index == 0:
            symbol, quantity_raw, price_raw = match.groups()
        else:
            quantity_raw, symbol, price_raw = match.groups()
        stop_price, target_price = _extract_alerts(stripped)
        return await add_holding(
            user_id,
            symbol,
            parse_number(quantity_raw),
            parse_number(price_raw),
            stop_price=stop_price,
            target_price=target_price,
        )

    alert_match = re.search(
        rf"(?:đặt|dat|cập nhật|cap nhat).*(?:stop|sl|cắt lỗ|cat lo|target|tp|chốt lời|chot loi).*\b({_SYMBOL_RE})\b",
        stripped,
        re.IGNORECASE,
    )
    if alert_match:
        symbol = alert_match.group(1).upper()
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

    sell_match = re.search(
        rf"(?:bán|ban|đã bán|da ban)\s+(?:mã\s+)?({_SYMBOL_RE})"
        rf"(?:\s+({_NUMBER_RE}|hết|het|all))?",
        stripped,
        re.IGNORECASE,
    )
    if sell_match:
        symbol, quantity_raw = sell_match.groups()
        quantity = None
        if quantity_raw and quantity_raw.lower() not in {"hết", "het", "all"}:
            quantity = parse_number(quantity_raw)
        return await sell_holding(user_id, symbol, quantity)
    return None
