"""Danh mục cổ phiếu có cấu trúc: vị thế, sổ giao dịch (lãi/lỗ đã chốt, phí,
thuế), báo cáo lãi/lỗ và mức cảnh báo.

SQL nằm ở core/repositories/portfolio.py (wrapper ``db.portfolio_*``). Mọi con
số ở đây tính bằng code, không qua LLM. Mức stop/target/ngưỡng giá là mức NGƯỜI
DÙNG tự nhập, không phải đầu ra của stock/policy.py.
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from core import database as db
from stock import providers

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Phí giao dịch CTCK (mỗi chiều) và thuế TNCN khi bán (0,1% giá trị bán - giữ
# nguyên theo luật thuế TNCN sửa đổi, hiệu lực 1/7/2026). Chỉnh qua env.
_DEFAULT_FEE_RATE = 0.0015
_DEFAULT_SELL_TAX_RATE = 0.001
_MAX_RATE = 0.02


@dataclass(frozen=True)
class Holding:
    user_id: int
    symbol: str
    quantity: float
    average_price: float
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    note: str = ""
    # Ngày (giờ VN, YYYY-MM-DD) mà số lượng/giá vốn/stop/target đang được tính
    # theo - sự kiện quyền có GDKHQ SAU ngày này chưa được quy đổi vào vị thế.
    cost_basis_as_of: Optional[str] = None


@dataclass(frozen=True)
class SaleResult:
    holding: Optional[Holding]  # vị thế còn lại; None = đã bán hết
    quantity: float
    price: Optional[float]
    fee: float
    tax: float
    average_cost: float
    realized_pnl: Optional[float]  # None khi chưa có giá bán

    @property
    def closed(self) -> bool:
        return self.holding is None


@dataclass(frozen=True)
class Transaction:
    id: int
    symbol: str
    side: str
    quantity: float
    price: Optional[float]
    fee: float
    tax: float
    average_cost: Optional[float]
    realized_pnl: Optional[float]
    note: str
    traded_at: datetime


def _env_rate(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip().replace(",", "."))
    except ValueError:
        return default
    return value if 0 <= value <= _MAX_RATE else default


def fee_rate() -> float:
    """Phí CTCK mỗi chiều mua/bán (STOCK_FEE_RATE, mặc định 0,15%)."""
    return _env_rate("STOCK_FEE_RATE", _DEFAULT_FEE_RATE)


def sell_tax_rate() -> float:
    """Thuế TNCN khi bán (STOCK_SELL_TAX_RATE, mặc định 0,1%)."""
    return _env_rate("STOCK_SELL_TAX_RATE", _DEFAULT_SELL_TAX_RATE)


def today_vn() -> str:
    return datetime.now(_VN_TZ).date().isoformat()


async def ensure_schema() -> None:
    await db.ensure_migrations()


def _opt_float(value) -> Optional[float]:
    return float(value) if value is not None else None


def _holding(row) -> Holding:
    basis = row["cost_basis_as_of"] if "cost_basis_as_of" in row.keys() else None
    return Holding(
        user_id=row["telegram_user_id"],
        symbol=row["symbol"],
        quantity=float(row["quantity"]),
        average_price=float(row["average_price"]),
        stop_price=_opt_float(row["stop_price"]),
        target_price=_opt_float(row["target_price"]),
        note=row["note"] or "",
        cost_basis_as_of=str(basis)[:10] if basis else None,
    )


def _transaction(row) -> Transaction:
    return Transaction(
        id=int(row["id"]),
        symbol=row["symbol"],
        side=row["side"],
        quantity=float(row["quantity"]),
        price=_opt_float(row["price"]),
        fee=float(row["fee"] or 0),
        tax=float(row["tax"] or 0),
        average_cost=_opt_float(row["average_cost"]),
        realized_pnl=_opt_float(row["realized_pnl"]),
        note=row["note"] or "",
        traded_at=row["traded_at"],
    )


async def list_holdings(user_id: int) -> list[Holding]:
    await ensure_schema()
    return [_holding(row) for row in await db.portfolio_list_holdings(user_id)]


async def get_holding(user_id: int, symbol: str) -> Holding | None:
    await ensure_schema()
    row = await db.portfolio_get_holding(user_id, symbol.strip().upper())
    return _holding(row) if row else None


async def list_all_holdings(limit: int = 300) -> list[Holding]:
    """Vị thế của mọi người dùng (job nền), có trần số dòng."""
    await ensure_schema()
    return [_holding(row) for row in await db.portfolio_list_all_holdings(limit)]


async def set_holding(
    user_id: int,
    symbol: str,
    quantity: float,
    average_price: float,
    *,
    stop_price: float | None = None,
    target_price: float | None = None,
    note: str = "",
) -> Holding:
    """Ghi đè vị thế (/capnhatcp) + dòng ADJUST; mốc giá vốn = hôm nay."""
    await ensure_schema()
    row = await db.portfolio_replace_holding(
        user_id,
        symbol.strip().upper(),
        quantity,
        average_price,
        stop_price=stop_price,
        target_price=target_price,
        note=note.strip()[:500],
        basis_date=today_vn(),
    )
    return _holding(row)


async def add_purchase(
    user_id: int,
    symbol: str,
    quantity: float,
    price: float,
    *,
    stop_price: float | None = None,
    target_price: float | None = None,
) -> Holding:
    """Ghi lệnh mua (bình quân giá trong 1 transaction) + dòng BUY kèm phí mua."""
    await ensure_schema()
    row = await db.portfolio_record_purchase(
        user_id,
        symbol.strip().upper(),
        quantity,
        price,
        stop_price=stop_price,
        target_price=target_price,
        fee=round(quantity * price * fee_rate()),
        basis_date=today_vn(),
    )
    return _holding(row)


async def update_alerts(
    user_id: int,
    symbol: str,
    *,
    stop_price: float | None,
    target_price: float | None,
) -> Holding | None:
    await ensure_schema()
    row = await db.portfolio_update_alerts(
        user_id, symbol.strip().upper(), stop_price=stop_price, target_price=target_price
    )
    return _holding(row) if row else None


async def record_sale(
    user_id: int, symbol: str, quantity: float | None = None, price: float | None = None
) -> SaleResult | None:
    """Bán (quantity None = bán hết; price None = chưa rõ giá). None nếu không
    có vị thế."""
    if quantity is not None and quantity <= 0:
        raise ValueError("quantity must be positive")
    if price is not None and price <= 0:
        raise ValueError("price must be positive")
    await ensure_schema()
    result = await db.portfolio_record_sale(
        user_id,
        symbol.strip().upper(),
        quantity,
        price,
        fee_rate=fee_rate(),
        tax_rate=sell_tax_rate(),
    )
    if result is None:
        return None
    row, detail = result
    return SaleResult(
        holding=_holding(row) if row else None,
        quantity=float(detail["quantity"]),
        price=detail["price"],
        fee=float(detail["fee"]),
        tax=float(detail["tax"]),
        average_cost=float(detail["average_cost"]),
        realized_pnl=detail["realized_pnl"],
    )


async def sell(
    user_id: int, symbol: str, quantity: float | None = None, price: float | None = None
) -> Holding | None:
    """Giảm vị thế; quantity=None là bán hết. Trả vị thế còn lại (None nếu hết
    hoặc không có vị thế)."""
    result = await record_sale(user_id, symbol, quantity, price)
    return result.holding if result else None


async def delete_holding(user_id: int, symbol: str) -> bool:
    await ensure_schema()
    return await db.portfolio_delete_holding(user_id, symbol.strip().upper())


async def was_closed(user_id: int, symbol: str) -> bool:
    await ensure_schema()
    return await db.portfolio_was_closed(user_id, symbol.strip().upper())


async def list_transactions(
    user_id: int, *, symbol: str | None = None, limit: int = 15
) -> list[Transaction]:
    await ensure_schema()
    rows = await db.portfolio_list_transactions(
        user_id, symbol=symbol.strip().upper() if symbol else None, limit=limit
    )
    return [_transaction(row) for row in rows]


async def fill_sale_price(user_id: int, symbol: str, price: float) -> Transaction | None:
    """Bổ sung giá cho lệnh bán gần nhất chưa có giá của 1 mã."""
    if price <= 0:
        raise ValueError("price must be positive")
    await ensure_schema()
    row = await db.portfolio_fill_sale_price(
        user_id,
        symbol.strip().upper(),
        price,
        fee_rate=fee_rate(),
        tax_rate=sell_tax_rate(),
    )
    return _transaction(row) if row else None


# ─── Lãi/lỗ đã chốt theo tháng ───────────────────────────────────────────────


@dataclass
class MonthRealized:
    month: str  # YYYY-MM
    priced_sells: int = 0
    wins: int = 0
    gross: float = 0.0  # Σ (giá bán - giá vốn) x số lượng
    sell_fee: float = 0.0
    buy_fee: float = 0.0
    tax: float = 0.0
    realized: float = 0.0  # Σ realized_pnl (đã trừ phí bán + thuế)
    unpriced: list[tuple[str, str]] | None = None  # (mã, dd/mm) bán chưa có giá

    @property
    def net_after_buy_fees(self) -> float:
        return self.realized - self.buy_fee


def _vn_time(value: datetime) -> datetime:
    return value.astimezone(_VN_TZ) if value.tzinfo else value


def summarize_realized(transactions: list[Transaction]) -> list[MonthRealized]:
    """Gom sổ giao dịch theo tháng (giờ VN), tháng mới nhất trước - hàm thuần."""
    months: dict[str, MonthRealized] = {}
    for tx in transactions:
        local = _vn_time(tx.traded_at)
        key = f"{local:%Y-%m}"
        month = months.setdefault(key, MonthRealized(key))
        if tx.side == "BUY":
            month.buy_fee += tx.fee
        elif tx.side == "SELL":
            if tx.price is None or tx.realized_pnl is None:
                month.unpriced = (month.unpriced or []) + [(tx.symbol, f"{local:%d/%m}")]
                continue
            month.priced_sells += 1
            month.wins += 1 if tx.realized_pnl > 0 else 0
            month.sell_fee += tx.fee
            month.tax += tx.tax
            month.realized += tx.realized_pnl
            if tx.average_cost is not None:
                month.gross += (tx.price - tx.average_cost) * tx.quantity
    return [months[key] for key in sorted(months, reverse=True)]


def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=_VN_TZ)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


async def realized_by_month(
    user_id: int, *, months: int = 6, month: str | None = None
) -> list[MonthRealized]:
    """Lãi/lỗ đã chốt của ``months`` tháng gần nhất, hoặc đúng 1 tháng YYYY-MM."""
    await ensure_schema()
    if month:
        year, mon = (int(part) for part in month.split("-", 1))
        start = _month_start(year, mon)
        end = _month_start(*_shift_month(year, mon, 1))
    else:
        now = datetime.now(_VN_TZ)
        start = _month_start(*_shift_month(now.year, now.month, -(max(1, months) - 1)))
        end = now + timedelta(minutes=1)
    rows = await db.portfolio_transactions_between(user_id, start, end)
    return summarize_realized([_transaction(row) for row in rows])


def _fmt_number(value: float, digits: int = 0) -> str:
    if digits:
        return f"{value:,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{value:,.0f}".replace(",", ".")


def _fmt_signed(value: float) -> str:
    return ("+" if value > 0 else "") + _fmt_number(value)


def _day_change_note(quote, quantity: float) -> str:
    """ " ▲ +1.25% (+250.000đ)" cho thay đổi trong ngày của 1 mã; rỗng nếu thiếu
    giá đóng cửa phiên trước."""
    if not quote.prev_close or quote.prev_close <= 0:
        return ""
    change = quote.price - quote.prev_close
    pct = change / quote.prev_close * 100
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "▬")
    return f" {arrow} {pct:+.2f}% ({_fmt_signed(change * quantity)}đ)"


def _session_label(quotes) -> str:
    """Nhãn dòng lãi/lỗ trong ngày: "Hôm nay" chỉ khi mọi giá là của phiên hôm
    nay; ngoài ra (digest 8h sáng, cuối tuần, ngày lễ) ghi rõ "Phiên dd/mm" theo
    ngày của giá mới nhất - trước đây digest gọi thay đổi phiên trước là "Hôm nay"."""
    today = datetime.now(_VN_TZ).date().isoformat()
    dates = sorted({str(getattr(q, "date", "") or "")[:10] for q in quotes} - {""})
    if not dates or dates == [today]:
        return "Hôm nay"
    try:
        return f"Phiên {date.fromisoformat(dates[-1]):%d/%m}"
    except ValueError:
        return "Phiên gần nhất"


async def build_report(user_id: int, *, digest: bool = False) -> str:
    holdings = await list_holdings(user_id)
    if not holdings:
        return "📭 Chưa có cổ phiếu nào trong danh mục đang nắm giữ."

    quotes = await asyncio.gather(
        *(providers.fetch_quote(item.symbol) for item in holdings),
        return_exceptions=True,
    )
    priced = []
    for holding, quote in zip(holdings, quotes, strict=False):
        valid_quote = None if isinstance(quote, BaseException) else quote
        market_value = holding.quantity * valid_quote.price if valid_quote else 0.0
        priced.append((holding, valid_quote, market_value))
    total_market_value = sum(item[2] for item in priced)
    total_cost = sum(
        holding.quantity * holding.average_price
        for holding, quote, _ in priced
        if quote is not None
    )
    total_pnl = total_market_value - total_cost if total_market_value else 0.0
    total_pnl_pct = total_pnl / total_cost * 100 if total_cost and total_market_value else 0.0
    # Lãi/lỗ TRONG NGÀY: (giá - giá đóng cửa phiên trước) x số lượng, chỉ tính mã
    # có prev_close hợp lệ; % so với giá trị danh mục theo giá phiên trước.
    day_pnl = sum(
        (quote.price - quote.prev_close) * holding.quantity
        for holding, quote, _ in priced
        if quote is not None and quote.prev_close and quote.prev_close > 0
    )
    prev_value = sum(
        quote.prev_close * holding.quantity
        for holding, quote, _ in priced
        if quote is not None and quote.prev_close and quote.prev_close > 0
    )
    day_pnl_pct = day_pnl / prev_value * 100 if prev_value else 0.0
    day_label = _session_label([quote for _, quote, _ in priced if quote is not None])

    title = "📊 *Digest danh mục đang nắm giữ:*" if digest else "📊 *Danh mục đang nắm giữ:*"
    lines = [title]
    for holding, quote, market_value in priced:
        quantity = _fmt_number(holding.quantity)
        average = _fmt_number(holding.average_price)
        if quote is None:
            lines.append(
                f"\n• *{holding.symbol}* — {quantity} cp | Giá vốn {average}đ"
                "\n  ⚠️ Chưa lấy được giá hiện tại; không tính vào tổng lãi/lỗ"
            )
            continue
        pnl = (quote.price - holding.average_price) * holding.quantity
        pnl_pct = (quote.price / holding.average_price - 1) * 100
        allocation = market_value / total_market_value * 100 if total_market_value else 0.0
        icon = "🟢" if pnl >= 0 else "🔴"
        day_note = _day_change_note(quote, holding.quantity)
        lines.append(
            f"\n• *{holding.symbol}* — {quantity} cp | Giá vốn {average}đ"
            f"\n  Giá {_fmt_number(quote.price)}đ{day_note} | {icon} {_fmt_number(pnl)}đ "
            f"({pnl_pct:+.2f}%) | Tỷ trọng {allocation:.1f}%"
        )
        alerts = []
        if holding.stop_price is not None:
            alerts.append(f"SL {_fmt_number(holding.stop_price)}đ")
            if quote.price <= holding.stop_price:
                alerts.append("🚨 đã chạm/cắt xuống stop")
        if holding.target_price is not None:
            alerts.append(f"TP {_fmt_number(holding.target_price)}đ")
            if quote.price >= holding.target_price:
                alerts.append("🎯 đã đạt/vượt mục tiêu")
        if alerts:
            lines.append("  " + " | ".join(alerts))

    if total_market_value:
        total_icon = "🟢" if total_pnl >= 0 else "🔴"
        lines.extend(
            [
                "",
                f"💰 Giá trị hiện tại: *{_fmt_number(total_market_value)}đ*",
                f"🧾 Tổng giá vốn đã định giá: {_fmt_number(total_cost)}đ",
                f"{total_icon} Lãi/lỗ tạm tính: "
                f"*{_fmt_number(total_pnl)}đ ({total_pnl_pct:+.2f}%)*",
            ]
        )
        if prev_value:
            day_icon = "🟢" if day_pnl >= 0 else "🔴"
            lines.append(
                f"{day_icon} {day_label}: *{_fmt_signed(day_pnl)}đ ({day_pnl_pct:+.2f}%)* "
                "so với phiên trước"
            )
    lines.append("\n⚠️ Số liệu chỉ để theo dõi, không phải khuyến nghị mua bán.")
    return "\n".join(lines)


# ─── Sổ giao dịch & lãi/lỗ đã chốt (văn bản) ─────────────────────────────────

_PRICE_HINT = "<GIÁ>"


def _tx_line(tx: Transaction) -> str:
    day = f"{_vn_time(tx.traded_at):%d/%m}"
    qty = _fmt_number(tx.quantity)
    if tx.side == "BUY":
        line = f"• {day} MUA {tx.symbol} {qty} cp @ {_fmt_number(tx.price or 0)}đ"
        if tx.fee:
            line += f" (phí {_fmt_number(tx.fee)}đ)"
        if tx.average_cost:
            line += f" → giá vốn {_fmt_number(tx.average_cost)}đ"
        return line
    if tx.side == "SELL":
        if tx.price is None or tx.realized_pnl is None:
            return f"• {day} BÁN {tx.symbol} {qty} cp (chưa có giá - /giaban {tx.symbol} {_PRICE_HINT})"
        icon = "🟢" if tx.realized_pnl > 0 else "🔴"
        return (
            f"• {day} BÁN {tx.symbol} {qty} cp @ {_fmt_number(tx.price)}đ → {icon} "
            f"{_fmt_signed(tx.realized_pnl)}đ (phí {_fmt_number(tx.fee)}đ, thuế {_fmt_number(tx.tax)}đ)"
        )
    if tx.side == "OPEN":
        return f"• {day} ĐẦU KỲ {tx.symbol} {qty} cp, giá vốn {_fmt_number(tx.price or 0)}đ"
    if tx.side == "ADJUST" and tx.quantity <= 0:
        return f"• {day} XOÁ {tx.symbol} khỏi danh mục (không ghi giá bán)"
    label = "QUY ĐỔI QUYỀN" if tx.side == "CORP" else "GHI ĐÈ"
    line = f"• {day} {label} {tx.symbol} → {qty} cp, giá vốn {_fmt_number(tx.average_cost or 0)}đ"
    if tx.side == "CORP" and tx.note:
        line += f" ({tx.note})"
    return line


async def build_history_text(user_id: int, *, symbol: str | None = None, limit: int = 15) -> str:
    transactions = await list_transactions(user_id, symbol=symbol, limit=limit)
    scope = f" {symbol.upper()}" if symbol else ""
    if not transactions:
        return f"📭 Sổ giao dịch{scope} chưa có dòng nào."
    lines = [f"📒 *Sổ giao dịch{scope}* ({len(transactions)} dòng gần nhất)"]
    lines.extend(_tx_line(tx) for tx in transactions)
    lines.append("\nDùng /laicp để xem lãi/lỗ đã chốt theo tháng.")
    return "\n".join(lines)


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%".replace(".", ",") if whole else "-"


def format_realized(months: list[MonthRealized], *, title_scope: str = "") -> str:
    """Báo cáo lãi/lỗ đã chốt (văn bản) - hàm thuần."""
    fee_pct = f"{fee_rate() * 100:.3g}%".replace(".", ",")
    tax_pct = f"{sell_tax_rate() * 100:.3g}%".replace(".", ",")
    if not months:
        return f"📭 Chưa có lệnh mua/bán nào trong sổ giao dịch{title_scope}."
    lines = [
        f"📈 *Lãi/lỗ đã chốt{title_scope}*",
        f"(giá vốn bình quân; phí CTCK {fee_pct}/chiều, thuế bán {tax_pct})",
    ]
    total_realized = total_buy_fee = 0.0
    total_sells = total_wins = 0
    for month in months:
        year, mon = month.month.split("-")
        lines.append(f"\n*Tháng {int(mon)}/{year}*")
        if month.priced_sells:
            icon = "🟢" if month.realized >= 0 else "🔴"
            lines.append(
                f"  {month.priced_sells} lệnh bán có giá, thắng {month.wins}/{month.priced_sells} "
                f"({_pct(month.wins, month.priced_sells)})"
            )
            lines.append(
                f"  Lãi/lỗ gộp {_fmt_signed(month.gross)}đ | phí bán -{_fmt_number(month.sell_fee)}đ"
                f" | thuế -{_fmt_number(month.tax)}đ"
            )
            lines.append(f"  {icon} Đã chốt ròng: *{_fmt_signed(month.realized)}đ*")
        else:
            lines.append("  Chưa có lệnh bán nào có giá.")
        if month.buy_fee:
            lines.append(f"  Phí mua trong tháng: -{_fmt_number(month.buy_fee)}đ")
        if month.unpriced:
            listed = ", ".join(f"{sym} {day}" for sym, day in month.unpriced[:5])
            first = month.unpriced[0][0]
            lines.append(
                f"  ⚠️ {len(month.unpriced)} lệnh bán chưa có giá ({listed}) - chưa tính; "
                f"gõ /giaban {first} {_PRICE_HINT} để bổ sung."
            )
        total_realized += month.realized
        total_buy_fee += month.buy_fee
        total_sells += month.priced_sells
        total_wins += month.wins
    if len(months) > 1:
        lines.append(
            f"\n💰 Cộng {len(months)} tháng: đã chốt ròng *{_fmt_signed(total_realized)}đ*, "
            f"thắng {total_wins}/{total_sells} ({_pct(total_wins, total_sells)}), "
            f"phí mua -{_fmt_number(total_buy_fee)}đ"
        )
    lines.append("\n⚠️ Số liệu tính từ lệnh anh tự ghi (/themcp, /bancp), không phải sao kê CTCK.")
    return "\n".join(lines)
