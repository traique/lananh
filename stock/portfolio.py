"""Structured stock holdings, profit/loss reporting, and alert levels."""

import asyncio
from dataclasses import dataclass
from typing import Optional

from core import database as db
from stock import providers

_schema_lock = asyncio.Lock()
_schema_ready = False


@dataclass(frozen=True)
class Holding:
    user_id: int
    symbol: str
    quantity: float
    average_price: float
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    note: str = ""


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    async with _schema_lock:
        if _schema_ready:
            return
        pool = await db.get_pool()
        await pool.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_holdings (
                telegram_user_id BIGINT NOT NULL,
                symbol TEXT NOT NULL,
                quantity NUMERIC NOT NULL CHECK (quantity > 0),
                average_price NUMERIC NOT NULL CHECK (average_price > 0),
                stop_price NUMERIC CHECK (stop_price > 0),
                target_price NUMERIC CHECK (target_price > 0),
                note TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (telegram_user_id, symbol)
            )
            """
        )
        _schema_ready = True


def _holding(row) -> Holding:
    return Holding(
        user_id=row["telegram_user_id"],
        symbol=row["symbol"],
        quantity=float(row["quantity"]),
        average_price=float(row["average_price"]),
        stop_price=float(row["stop_price"]) if row["stop_price"] is not None else None,
        target_price=(float(row["target_price"]) if row["target_price"] is not None else None),
        note=row["note"] or "",
    )


async def list_holdings(user_id: int) -> list[Holding]:
    await ensure_schema()
    rows = await (await db.get_pool()).fetch(
        """
        SELECT telegram_user_id, symbol, quantity, average_price,
               stop_price, target_price, note
        FROM stock_holdings
        WHERE telegram_user_id = $1
        ORDER BY symbol
        """,
        user_id,
    )
    return [_holding(row) for row in rows]


async def get_holding(user_id: int, symbol: str) -> Holding | None:
    await ensure_schema()
    row = await (await db.get_pool()).fetchrow(
        """
        SELECT telegram_user_id, symbol, quantity, average_price,
               stop_price, target_price, note
        FROM stock_holdings
        WHERE telegram_user_id = $1 AND symbol = $2
        """,
        user_id,
        symbol.strip().upper(),
    )
    return _holding(row) if row else None


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
    await ensure_schema()
    row = await (await db.get_pool()).fetchrow(
        """
        INSERT INTO stock_holdings (
            telegram_user_id, symbol, quantity, average_price,
            stop_price, target_price, note
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (telegram_user_id, symbol)
        DO UPDATE SET
            quantity = EXCLUDED.quantity,
            average_price = EXCLUDED.average_price,
            stop_price = EXCLUDED.stop_price,
            target_price = EXCLUDED.target_price,
            note = EXCLUDED.note,
            updated_at = now()
        RETURNING telegram_user_id, symbol, quantity, average_price,
                  stop_price, target_price, note
        """,
        user_id,
        symbol.strip().upper(),
        quantity,
        average_price,
        stop_price,
        target_price,
        note.strip()[:500],
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
    """Add a buy and calculate the weighted average price atomically."""
    await ensure_schema()
    row = await (await db.get_pool()).fetchrow(
        """
        INSERT INTO stock_holdings (
            telegram_user_id, symbol, quantity, average_price,
            stop_price, target_price
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (telegram_user_id, symbol)
        DO UPDATE SET
            average_price = (
                stock_holdings.quantity * stock_holdings.average_price
                + EXCLUDED.quantity * EXCLUDED.average_price
            ) / (stock_holdings.quantity + EXCLUDED.quantity),
            quantity = stock_holdings.quantity + EXCLUDED.quantity,
            stop_price = COALESCE(EXCLUDED.stop_price, stock_holdings.stop_price),
            target_price = COALESCE(EXCLUDED.target_price, stock_holdings.target_price),
            updated_at = now()
        RETURNING telegram_user_id, symbol, quantity, average_price,
                  stop_price, target_price, note
        """,
        user_id,
        symbol.strip().upper(),
        quantity,
        price,
        stop_price,
        target_price,
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
    row = await (await db.get_pool()).fetchrow(
        """
        UPDATE stock_holdings
        SET stop_price = $3, target_price = $4, updated_at = now()
        WHERE telegram_user_id = $1 AND symbol = $2
        RETURNING telegram_user_id, symbol, quantity, average_price,
                  stop_price, target_price, note
        """,
        user_id,
        symbol.strip().upper(),
        stop_price,
        target_price,
    )
    return _holding(row) if row else None


async def sell(user_id: int, symbol: str, quantity: float | None = None) -> Holding | None:
    """Reduce a holding; quantity=None means sell the full position."""
    if quantity is not None and quantity <= 0:
        raise ValueError("quantity must be positive")
    await ensure_schema()
    pool = await db.get_pool()
    symbol = symbol.strip().upper()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT telegram_user_id, symbol, quantity, average_price,
                       stop_price, target_price, note
                FROM stock_holdings
                WHERE telegram_user_id = $1 AND symbol = $2
                FOR UPDATE
                """,
                user_id,
                symbol,
            )
            if row is None:
                return None
            current = float(row["quantity"])
            if quantity is None or quantity >= current:
                await conn.execute(
                    "DELETE FROM stock_holdings WHERE telegram_user_id = $1 AND symbol = $2",
                    user_id,
                    symbol,
                )
                return None
            updated = await conn.fetchrow(
                """
                UPDATE stock_holdings
                SET quantity = quantity - $3, updated_at = now()
                WHERE telegram_user_id = $1 AND symbol = $2
                RETURNING telegram_user_id, symbol, quantity, average_price,
                          stop_price, target_price, note
                """,
                user_id,
                symbol,
                quantity,
            )
            return _holding(updated)


async def delete_holding(user_id: int, symbol: str) -> bool:
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        "DELETE FROM stock_holdings WHERE telegram_user_id = $1 AND symbol = $2",
        user_id,
        symbol.strip().upper(),
    )
    return result != "DELETE 0"


def _fmt_number(value: float, digits: int = 0) -> str:
    if digits:
        return f"{value:,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{value:,.0f}".replace(",", ".")


async def build_report(user_id: int, *, digest: bool = False) -> str:
    holdings = await list_holdings(user_id)
    if not holdings:
        return "📭 Chưa có cổ phiếu nào trong danh mục đang nắm giữ."

    quotes = await asyncio.gather(
        *(providers.fetch_quote(item.symbol) for item in holdings),
        return_exceptions=True,
    )
    priced = []
    for holding, quote in zip(holdings, quotes):
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
        lines.append(
            f"\n• *{holding.symbol}* — {quantity} cp | Giá vốn {average}đ"
            f"\n  Giá {_fmt_number(quote.price)}đ | {icon} {_fmt_number(pnl)}đ "
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
    lines.append("\n⚠️ Số liệu chỉ để theo dõi, không phải khuyến nghị mua bán.")
    return "\n".join(lines)
