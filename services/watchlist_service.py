"""Danh sách theo dõi và ngưỡng giá do NGƯỜI DÙNG tự đặt: /theodoi, /botheodoi,
/canhbaogia, /xoacanhbao và câu "báo anh khi HPG về 25".

Parse tất định (không gọi AI). Ngưỡng là mức người dùng tự nhập - không phải
khuyến nghị; vòng quét trong phiên nằm ở services/price_alerts.py. Có trần số
mã theo dõi (WATCHLIST_MAX_SYMBOLS) và số ngưỡng mỗi người
(PRICE_ALERT_MAX_PER_USER). Mỗi người (chủ bot / từng thành viên Zalo) có danh
sách riêng.
"""

from __future__ import annotations

import asyncio
import re

from core import database as db
from core.envutil import env_int
from services import portfolio_push, portfolio_service, price_alerts
from stock import providers

COMMANDS = frozenset({"/theodoi", "/botheodoi", "/canhbaogia", "/xoacanhbao"})

_BELOW_WORDS = (
    "về",
    "ve",
    "xuống",
    "xuong",
    "dưới",
    "duoi",
    "giảm",
    "giam",
    "thủng",
    "thung",
)
_ABOVE_WORDS = ("lên", "len", "vượt", "vuot", "trên", "tren", "tăng", "tang", "qua")
# Không rõ chiều ("báo khi HPG chạm 30"): suy theo giá hiện tại.
_NEUTRAL_WORDS = ("chạm", "cham", "tới", "toi", "đến", "den")
_DIR_RE = "|".join(sorted(_BELOW_WORDS + _ABOVE_WORDS + _NEUTRAL_WORDS, key=len, reverse=True))
_ALERT_REQUEST_RE = re.compile(
    r"(?<![\w])(?:báo|bao|nhắc|nhac)\s+(?:cho\s+)?(?:anh|em|tôi|toi|mình|minh|chị|chi)?\s*"
    r"(?:khi|nếu|neu|lúc|luc)\s+(?:mã\s+|ma\s+|cổ phiếu\s+|co phieu\s+)?"
    rf"{portfolio_service._SYMBOL_WORD}\s+(?:giá\s+|gia\s+)?({_DIR_RE})(?:\s+(?:dưới|duoi|trên|tren))?"
    rf"\s+(?:mức\s+|muc\s+|giá\s+|gia\s+|ngưỡng\s+|nguong\s+)?({portfolio_service._NUMBER_RE})(?![\w])",
    re.IGNORECASE,
)
_NOT_ADVICE = "Đây là mức anh tự đặt, em chỉ báo khi giá chạm - không phải khuyến nghị mua bán."


def watchlist_max() -> int:
    return env_int("WATCHLIST_MAX_SYMBOLS", 20, 1, 100)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}".replace(",", ".")


def _direction_from_word(word: str | None) -> str | None:
    if not word:
        return None
    word = word.lower()
    if word in _BELOW_WORDS:
        return price_alerts.BELOW
    if word in _ABOVE_WORDS:
        return price_alerts.ABOVE
    return None


async def _quote(symbol: str):
    try:
        return await providers.fetch_quote(symbol)
    except Exception:
        return None


async def add_alert(user_id: int, symbol: str, level: float, word: str | None = None) -> str:
    if not portfolio_push.can_deliver(user_id):
        return (
            "⚠️ Kênh này đang tắt tin nhắn chủ động nên em chưa đặt được cảnh báo giá cho anh "
            "(chủ bot chỉnh PORTFOLIO_PUSH_ZALO_DAILY_CAP)."
        )
    if level <= 0:
        return "❌ Ngưỡng giá phải lớn hơn 0."
    valid = await portfolio_service._validate_symbol(symbol)
    if valid is None:
        return f"❌ Không xác nhận được mã {symbol.upper()} trên nguồn dữ liệu thị trường."
    quote = await _quote(valid)
    price = quote.price if quote is not None and quote.price > 0 else None
    direction = _direction_from_word(word)
    if direction is None:
        if price is None or price == level:
            return (
                f"❌ Em chưa lấy được giá {valid} để biết anh muốn báo khi giá lên hay về "
                f"{_fmt(level)}đ. Anh gõ /canhbaogia {valid} về {_fmt(level)} hoặc "
                f"/canhbaogia {valid} lên {_fmt(level)} nhé."
            )
        direction = price_alerts.BELOW if level < price else price_alerts.ABOVE
    if price is None:
        state = "unknown"
    else:
        state = "crossed" if price_alerts.is_hit(direction, level, price) else "armed"
    row, full = await db.portfolio_add_price_alert(
        user_id, valid, direction, level, state=state, max_per_user=price_alerts.max_per_user()
    )
    if full:
        return (
            f"❌ Anh đã đặt đủ {price_alerts.max_per_user()} ngưỡng giá. Gõ /theodoi để xem và "
            "/xoacanhbao <MÃ> [GIÁ] để bỏ bớt nhé."
        )
    word_text = price_alerts.direction_word(direction)
    if row is None:
        return f"ℹ️ Ngưỡng {valid} {word_text} {_fmt(level)}đ đã có rồi anh."
    await db.portfolio_add_watch(user_id, [valid], watchlist_max())
    now_text = f" (giá hiện {_fmt(price)}đ)" if price is not None else ""
    if state == "crossed":
        return (
            f"✅ Đã đặt ngưỡng {valid} {word_text} {_fmt(level)}đ{now_text}. Giá đang ở phía "
            "ngưỡng rồi nên em sẽ báo khi giá quay lại rồi chạm lại. " + _NOT_ADVICE
        )
    return (
        f"✅ Em sẽ báo anh khi {valid} {word_text} {_fmt(level)}đ{now_text}, kiểm tra mỗi "
        f"{price_alerts.poll_seconds() // 60} phút trong giờ giao dịch. " + _NOT_ADVICE
    )


async def maybe_handle_alert_request(user_id: int, text: str) -> str | None:
    """'báo anh khi HPG về 25' -> đặt ngưỡng. Caller đã loại câu hỏi/xin ý kiến."""
    match = _ALERT_REQUEST_RE.search(text)
    if not match:
        return None
    symbol, word, level_raw = match.group(1), match.group(2), match.group(3)
    if not portfolio_service._is_known_symbol(symbol):
        return None
    try:
        level = portfolio_service.parse_price(level_raw)
    except ValueError:
        return None
    return await add_alert(user_id, symbol.upper(), level, word)


_STATE_LABEL = {
    "unknown": "đang chờ",
    "armed": "đang chờ",
    "crossed": "đã chạm, chờ giá quay lại",
}


def _change_text(quote) -> str:
    if quote is None:
        return " (chưa lấy được giá)"
    arrow = "▲" if quote.change > 0 else ("▼" if quote.change < 0 else "▬")
    pct = f"{quote.change_pct:+.2f}%".replace(".", ",")
    return f" {_fmt(quote.price)}đ {arrow} {pct}"


async def watch_report(user_id: int) -> str:
    symbols = await db.portfolio_watchlist(user_id)
    alerts = await db.portfolio_list_price_alerts(user_id)
    if not symbols and not alerts:
        return (
            "📭 Anh chưa theo dõi mã nào. Gõ /theodoi FPT MWG để thêm, hoặc nhắn "
            '"báo anh khi HPG về 25" để em báo khi giá chạm ngưỡng.'
        )
    wanted = list(dict.fromkeys(symbols + [a["symbol"] for a in alerts]))
    quotes = dict(zip(wanted, await asyncio.gather(*(_quote(s) for s in wanted)), strict=True))
    lines: list[str] = []
    if symbols:
        lines.append(f"👀 *Danh sách theo dõi* ({len(symbols)}/{watchlist_max()} mã)")
        lines.extend(f"• {s}{_change_text(quotes.get(s))}" for s in symbols)
    if alerts:
        lines.append(f"\n🔔 *Ngưỡng giá anh đặt* ({len(alerts)}/{price_alerts.max_per_user()})")
        for alert in alerts:
            lines.append(
                f"• {alert['symbol']} {price_alerts.direction_word(alert['direction'])} "
                f"{_fmt(float(alert['level']))}đ ({_STATE_LABEL.get(alert['last_state'], '')})"
            )
        lines.append(
            f"\nEm kiểm tra ngưỡng mỗi {price_alerts.poll_seconds() // 60} phút trong giờ giao "
            "dịch. " + _NOT_ADVICE
        )
    return "\n".join(lines)


async def handle_command(user_id: int, command: str, parts: list[str]) -> str:
    if command == "/theodoi":
        if len(parts) < 2:
            return await watch_report(user_id)
        raw = [p.strip(",;").upper() for p in parts[1:] if p.strip(",;")]
        valid, invalid = [], []
        for symbol in dict.fromkeys(raw):
            checked = await portfolio_service._validate_symbol(symbol)
            (valid if checked else invalid).append(checked or symbol)
        added = await db.portfolio_add_watch(user_id, valid, watchlist_max()) if valid else []
        parts_text = []
        if added:
            parts_text.append(f"✅ Đã thêm vào danh sách theo dõi: {', '.join(added)}.")
        skipped = [s for s in valid if s not in added]
        if skipped:
            parts_text.append(
                f"ℹ️ {', '.join(skipped)} đã có sẵn hoặc danh sách đã đủ {watchlist_max()} mã."
            )
        if invalid:
            parts_text.append(f"❌ Không xác nhận được mã: {', '.join(invalid)}.")
        parts_text.append("Gõ /theodoi để xem giá, /canhbaogia <MÃ> <GIÁ> để đặt ngưỡng báo.")
        return "\n".join(parts_text)
    if command == "/botheodoi":
        if len(parts) < 2:
            return "Cú pháp: /botheodoi <MÃ> (bỏ theo dõi kèm mọi ngưỡng giá của mã đó)"
        symbol = parts[1].upper()
        removed, alerts = await db.portfolio_remove_watch(user_id, symbol)
        if not removed and not alerts:
            return f"❌ {symbol} không có trong danh sách theo dõi."
        extra = f" và {alerts} ngưỡng giá" if alerts else ""
        return f"✅ Đã bỏ theo dõi {symbol}{extra}."
    if command == "/canhbaogia":
        usage = (
            "Cú pháp: /canhbaogia <MÃ> [về|lên] <GIÁ> - vd /canhbaogia HPG về 25 "
            "(giá dưới 1.000 hiểu là nghìn đồng). Không ghi về/lên thì em tự suy theo giá hiện tại."
        )
        if len(parts) < 3:
            return usage
        word = parts[2] if len(parts) >= 4 else None
        try:
            level = portfolio_service.parse_price(parts[-1])
        except ValueError:
            return usage
        if word is not None and word.lower() not in _BELOW_WORDS + _ABOVE_WORDS + _NEUTRAL_WORDS:
            return usage
        return await add_alert(user_id, parts[1].upper(), level, word)
    if command == "/xoacanhbao":
        if len(parts) < 2:
            return "Cú pháp: /xoacanhbao <MÃ> [GIÁ] - không ghi giá thì xoá mọi ngưỡng của mã đó."
        level = None
        if len(parts) >= 3:
            try:
                level = portfolio_service.parse_price(parts[2])
            except ValueError:
                return "❌ Giá không hợp lệ."
        symbol = parts[1].upper()
        count = await db.portfolio_delete_price_alerts(user_id, symbol, level)
        if not count:
            return f"❌ Không có ngưỡng giá nào của {symbol} khớp để xoá."
        return f"✅ Đã xoá {count} ngưỡng giá của {symbol}."
    return ""
