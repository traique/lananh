"""SQL danh mục cổ phiếu: vị thế, sổ giao dịch, theo dõi/cảnh báo giá, nhật ký
thông báo chủ động (migrations/001, 008, 022).

Hàm nhận pool asyncpg; wrapper @_with_reconnect ở core/database.py lấy pool và
quyết định retry. Mọi thao tác GHI vị thế (mua, bán, ghi đè, xoá, quy đổi quyền)
chạy trong 1 transaction cùng dòng sổ giao dịch nên sổ không bao giờ lệch với
stock_holdings; các hàm đó INSERT không idempotent -> wrapper dùng
``idempotent=False``.

Kiểu dữ liệu: cột DATE được đọc dưới dạng text ``YYYY-MM-DD`` và tham số ngày
truyền dạng text rồi ép ``::date`` trong SQL - asyncpg (production) và PGlite
(test SQL) trả/nhận kiểu ngày khác nhau, text thì giống nhau.
"""

from __future__ import annotations

import math
from typing import Any, NamedTuple

HOLDING_COLUMNS = (
    "telegram_user_id, symbol, quantity, average_price, stop_price, target_price, note, "
    "cost_basis_as_of::text AS cost_basis_as_of"
)
_TX_COLUMNS = (
    "id, symbol, side, quantity, price, fee, tax, average_cost, realized_pnl, note, traded_at"
)


class CorporateRebase(NamedTuple):
    """Quy đổi vị thế theo sự kiện quyền đã được giá xác nhận: số cp x factor
    (làm tròn xuống - cp lẻ bị huỷ), giữ nguyên TỔNG giá vốn, stop/target chia
    factor. as_of = ngày GDKHQ muộn nhất; chỉ áp khi mốc giá vốn còn trước đó."""

    factor: float
    as_of: str  # YYYY-MM-DD
    note: str


def rebased_values(
    quantity: float,
    average_price: float,
    stop_price: float | None,
    target_price: float | None,
    factor: float,
) -> tuple[float, float, float | None, float | None]:
    """(số cp, giá vốn, stop, target) sau quy đổi - hàm thuần."""
    new_quantity = float(math.floor(quantity * factor + 1e-9))
    if new_quantity <= 0 or factor <= 0:
        return quantity, average_price, stop_price, target_price
    new_average = round(average_price * quantity / new_quantity, 4)
    new_stop = round(stop_price / factor, 2) if stop_price is not None else None
    new_target = round(target_price / factor, 2) if target_price is not None else None
    return new_quantity, new_average, new_stop, new_target


def _num(value) -> float | None:
    return float(value) if value is not None else None


async def _insert_tx(
    conn: Any,
    user_id: int,
    symbol: str,
    side: str,
    quantity: float,
    price: float | None,
    *,
    fee: float = 0.0,
    tax: float = 0.0,
    average_cost: float | None = None,
    realized_pnl: float | None = None,
    note: str = "",
) -> None:
    await conn.execute(
        """
        INSERT INTO stock_transactions (
            telegram_user_id, symbol, side, quantity, price, fee, tax,
            average_cost, realized_pnl, note
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
        user_id,
        symbol,
        side,
        quantity,
        price,
        fee,
        tax,
        average_cost,
        realized_pnl,
        note[:300],
    )


async def _lock_holding(conn: Any, user_id: int, symbol: str):
    return await conn.fetchrow(
        f"""
        SELECT {HOLDING_COLUMNS}
        FROM stock_holdings
        WHERE telegram_user_id = $1 AND symbol = $2
        FOR UPDATE
        """,
        user_id,
        symbol,
    )


async def _apply_rebase(conn: Any, row, rebase: CorporateRebase | None):
    """Quy đổi dòng vị thế (đã FOR UPDATE) nếu mốc giá vốn còn trước sự kiện;
    ghi dòng CORP. Trả dòng vị thế hiện hành."""
    if rebase is None or row is None or str(row["cost_basis_as_of"]) >= rebase.as_of:
        return row
    quantity, average, stop, target = rebased_values(
        float(row["quantity"]),
        float(row["average_price"]),
        _num(row["stop_price"]),
        _num(row["target_price"]),
        rebase.factor,
    )
    updated = await conn.fetchrow(
        f"""
        UPDATE stock_holdings
        SET quantity = $3, average_price = $4, stop_price = $5, target_price = $6,
            cost_basis_as_of = ($7::text)::date, updated_at = now()
        WHERE telegram_user_id = $1 AND symbol = $2
        RETURNING {HOLDING_COLUMNS}
        """,
        row["telegram_user_id"],
        row["symbol"],
        quantity,
        average,
        stop,
        target,
        rebase.as_of,
    )
    await _insert_tx(
        conn,
        row["telegram_user_id"],
        row["symbol"],
        "CORP",
        quantity,
        None,
        average_cost=average,
        note=rebase.note,
    )
    return updated


# ─── Vị thế ──────────────────────────────────────────────────────────────────


async def list_holdings(pool: Any, user_id: int):
    return await pool.fetch(
        f"SELECT {HOLDING_COLUMNS} FROM stock_holdings WHERE telegram_user_id = $1 ORDER BY symbol",
        user_id,
    )


async def get_holding(pool: Any, user_id: int, symbol: str):
    return await pool.fetchrow(
        f"SELECT {HOLDING_COLUMNS} FROM stock_holdings WHERE telegram_user_id = $1 AND symbol = $2",
        user_id,
        symbol,
    )


async def list_all_holdings(pool: Any, limit: int):
    """Vị thế của MỌI người dùng (job nền hằng ngày), có trần số dòng."""
    return await pool.fetch(
        f"SELECT {HOLDING_COLUMNS} FROM stock_holdings "
        "ORDER BY (telegram_user_id > 0) DESC, telegram_user_id, symbol LIMIT $1",
        limit,
    )


async def replace_holding(
    pool: Any,
    user_id: int,
    symbol: str,
    quantity: float,
    average_price: float,
    *,
    stop_price: float | None,
    target_price: float | None,
    note: str,
    basis_date: str,
):
    """/capnhatcp: ghi đè vị thế (số liệu anh nhập là số hiện hành -> đặt lại mốc
    giá vốn) + dòng ADJUST."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                INSERT INTO stock_holdings (
                    telegram_user_id, symbol, quantity, average_price,
                    stop_price, target_price, note, cost_basis_as_of
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, ($8::text)::date)
                ON CONFLICT (telegram_user_id, symbol)
                DO UPDATE SET
                    quantity = EXCLUDED.quantity,
                    average_price = EXCLUDED.average_price,
                    stop_price = EXCLUDED.stop_price,
                    target_price = EXCLUDED.target_price,
                    note = EXCLUDED.note,
                    cost_basis_as_of = EXCLUDED.cost_basis_as_of,
                    updated_at = now()
                RETURNING {HOLDING_COLUMNS}
                """,
                user_id,
                symbol,
                quantity,
                average_price,
                stop_price,
                target_price,
                note,
                basis_date,
            )
            await _insert_tx(
                conn,
                user_id,
                symbol,
                "ADJUST",
                quantity,
                average_price,
                average_cost=average_price,
                note="Ghi đè vị thế (/capnhatcp)",
            )
            return row


async def record_purchase(
    pool: Any,
    user_id: int,
    symbol: str,
    quantity: float,
    price: float,
    *,
    stop_price: float | None,
    target_price: float | None,
    fee: float,
    basis_date: str,
    rebase: CorporateRebase | None = None,
):
    """Mua thêm: quy đổi sự kiện quyền còn treo TRƯỚC khi bình quân giá (không
    trộn giá vốn trước GDKHQ với giá sau GDKHQ), rồi ghi dòng BUY. Mốc giá vốn
    dời tới ``basis_date`` - caller (stock.portfolio.record_purchase) phải đã
    kiểm tra được lịch quyền, nếu không sự kiện bỏ sót không bao giờ được quy đổi."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await _apply_rebase(conn, await _lock_holding(conn, user_id, symbol), rebase)
            if current is None:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO stock_holdings (
                        telegram_user_id, symbol, quantity, average_price,
                        stop_price, target_price, cost_basis_as_of
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, ($7::text)::date)
                    RETURNING {HOLDING_COLUMNS}
                    """,
                    user_id,
                    symbol,
                    quantity,
                    price,
                    stop_price,
                    target_price,
                    basis_date,
                )
            else:
                old_quantity = float(current["quantity"])
                total = old_quantity + quantity
                average = (
                    old_quantity * float(current["average_price"]) + quantity * price
                ) / total
                row = await conn.fetchrow(
                    f"""
                    UPDATE stock_holdings
                    SET quantity = $3, average_price = $4,
                        stop_price = COALESCE($5, stop_price),
                        target_price = COALESCE($6, target_price),
                        cost_basis_as_of = GREATEST(cost_basis_as_of, ($7::text)::date),
                        updated_at = now()
                    WHERE telegram_user_id = $1 AND symbol = $2
                    RETURNING {HOLDING_COLUMNS}
                    """,
                    user_id,
                    symbol,
                    total,
                    average,
                    stop_price,
                    target_price,
                    basis_date,
                )
            await _insert_tx(
                conn,
                user_id,
                symbol,
                "BUY",
                quantity,
                price,
                fee=fee,
                average_cost=float(row["average_price"]),
            )
            return row


async def record_sale(
    pool: Any,
    user_id: int,
    symbol: str,
    quantity: float | None,
    price: float | None,
    *,
    fee_rate: float,
    tax_rate: float,
    rebase: CorporateRebase | None = None,
):
    """Bán (quantity None = bán hết). Trả None nếu không có vị thế, ngược lại
    (dòng vị thế còn lại hoặc None nếu đã bán hết, dict chi tiết lệnh bán).

    Lãi/lỗ đã chốt theo giá vốn bình quân (như stock_holdings.average_price và
    cách CTCK VN tính): (giá bán - giá vốn) x số lượng - phí bán - thuế. Không có
    giá bán thì ghi price NULL, không tính lãi/lỗ (bổ sung sau bằng /giaban)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _apply_rebase(conn, await _lock_holding(conn, user_id, symbol), rebase)
            if row is None:
                return None
            current = float(row["quantity"])
            average = float(row["average_price"])
            sold = current if quantity is None or quantity >= current else quantity
            fee = tax = 0.0
            realized = None
            if price is not None:
                value = sold * price
                fee = round(value * fee_rate)
                tax = round(value * tax_rate)
                realized = round((price - average) * sold - fee - tax, 2)
            await _insert_tx(
                conn,
                user_id,
                symbol,
                "SELL",
                sold,
                price,
                fee=fee,
                tax=tax,
                average_cost=average,
                realized_pnl=realized,
            )
            detail = {
                "quantity": sold,
                "price": price,
                "fee": fee,
                "tax": tax,
                "average_cost": average,
                "realized_pnl": realized,
                "closed": sold >= current,
            }
            if sold >= current:
                await conn.execute(
                    """INSERT INTO stock_closed_positions (telegram_user_id, symbol)
                    VALUES ($1, $2) ON CONFLICT (telegram_user_id, symbol)
                    DO UPDATE SET closed_at = now()""",
                    user_id,
                    symbol,
                )
                await conn.execute(
                    "DELETE FROM stock_holdings WHERE telegram_user_id = $1 AND symbol = $2",
                    user_id,
                    symbol,
                )
                return None, detail
            updated = await conn.fetchrow(
                f"""
                UPDATE stock_holdings
                SET quantity = quantity - $3, updated_at = now()
                WHERE telegram_user_id = $1 AND symbol = $2
                RETURNING {HOLDING_COLUMNS}
                """,
                user_id,
                symbol,
                sold,
            )
            return updated, detail


async def update_alerts(
    pool: Any,
    user_id: int,
    symbol: str,
    *,
    stop_price: float | None,
    target_price: float | None,
    rebase: CorporateRebase | None = None,
):
    """Đặt SL/TP; None = giữ mức đang có. Quy đổi quyền còn treo trước: mức anh
    nhập là giá hiện hành, không được bị chia thêm factor của sự kiện cũ, còn mức
    giữ nguyên là mức ĐÃ quy đổi (COALESCE chạy sau _apply_rebase trong cùng
    transaction - caller không được truyền lại mức thô đọc trước khi quy đổi).
    Không đổi mốc giá vốn."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _apply_rebase(conn, await _lock_holding(conn, user_id, symbol), rebase)
            if row is None:
                return None
            return await conn.fetchrow(
                f"""
                UPDATE stock_holdings
                SET stop_price = COALESCE($3, stop_price),
                    target_price = COALESCE($4, target_price),
                    updated_at = now()
                WHERE telegram_user_id = $1 AND symbol = $2
                RETURNING {HOLDING_COLUMNS}
                """,
                user_id,
                symbol,
                stop_price,
                target_price,
            )


async def rebase_holding(pool: Any, user_id: int, symbol: str, rebase: CorporateRebase):
    """Lưu hẳn quy đổi quyền (job nền). Trả (trước, sau) nếu có quy đổi, None nếu
    không có vị thế hoặc mốc giá vốn đã qua sự kiện (chạy lại an toàn)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _lock_holding(conn, user_id, symbol)
            if row is None or str(row["cost_basis_as_of"]) >= rebase.as_of:
                return None
            return row, await _apply_rebase(conn, row, rebase)


async def delete_holding(pool: Any, user_id: int, symbol: str) -> bool:
    """/xoacp: xoá vị thế không kèm giá bán - ghi ADJUST về 0 (không tính lãi/lỗ)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            removed = await conn.fetchval(
                """WITH removed AS (
                    DELETE FROM stock_holdings WHERE telegram_user_id = $1 AND symbol = $2
                    RETURNING telegram_user_id, symbol
                )
                INSERT INTO stock_closed_positions (telegram_user_id, symbol)
                SELECT telegram_user_id, symbol FROM removed
                ON CONFLICT (telegram_user_id, symbol) DO UPDATE SET closed_at = now()
                RETURNING symbol""",
                user_id,
                symbol,
            )
            if removed is None:
                return False
            await _insert_tx(
                conn, user_id, symbol, "ADJUST", 0, None, note="Xoá khỏi danh mục (/xoacp)"
            )
            return True


async def was_closed(pool: Any, user_id: int, symbol: str) -> bool:
    return bool(
        await pool.fetchval(
            "SELECT 1 FROM stock_closed_positions WHERE telegram_user_id = $1 AND symbol = $2",
            user_id,
            symbol,
        )
    )


# ─── Sổ giao dịch ────────────────────────────────────────────────────────────


async def list_transactions(pool: Any, user_id: int, *, symbol: str | None, limit: int):
    return await pool.fetch(
        f"""
        SELECT {_TX_COLUMNS}
        FROM stock_transactions
        WHERE telegram_user_id = $1 AND ($2::text IS NULL OR symbol = $2)
        ORDER BY traded_at DESC, id DESC
        LIMIT $3
        """,
        user_id,
        symbol,
        limit,
    )


async def transactions_between(pool: Any, user_id: int, start, end, *, limit: int):
    """Dòng sổ trong [start, end) (timestamptz), cũ trước - để tổng hợp báo cáo."""
    return await pool.fetch(
        f"""
        SELECT {_TX_COLUMNS}
        FROM stock_transactions
        WHERE telegram_user_id = $1 AND traded_at >= $2 AND traded_at < $3
        ORDER BY traded_at, id
        LIMIT $4
        """,
        user_id,
        start,
        end,
        limit,
    )


async def fill_sale_price(
    pool: Any, user_id: int, symbol: str, price: float, *, fee_rate: float, tax_rate: float
):
    """Bổ sung giá cho lệnh bán GẦN NHẤT chưa có giá của 1 mã; trả dòng đã cập
    nhật hoặc None. Chạy lại không đổi gì (chỉ đụng dòng price IS NULL)."""
    return await pool.fetchrow(
        f"""
        UPDATE stock_transactions AS t
        SET price = $3,
            fee = round(t.quantity * $3 * $4),
            tax = round(t.quantity * $3 * $5),
            realized_pnl = round(
                ($3 - t.average_cost) * t.quantity
                - round(t.quantity * $3 * $4) - round(t.quantity * $3 * $5), 2
            )
        WHERE t.id = (
            SELECT id FROM stock_transactions
            WHERE telegram_user_id = $1 AND symbol = $2 AND side = 'SELL' AND price IS NULL
              AND average_cost IS NOT NULL
            ORDER BY traded_at DESC, id DESC
            LIMIT 1
        )
        RETURNING {_TX_COLUMNS}
        """,
        user_id,
        symbol,
        price,
        fee_rate,
        tax_rate,
    )


# ─── Theo dõi & cảnh báo giá ─────────────────────────────────────────────────


async def watchlist(pool: Any, user_id: int) -> list[str]:
    rows = await pool.fetch(
        "SELECT symbol FROM stock_watchlist WHERE telegram_user_id = $1 ORDER BY created_at, symbol",
        user_id,
    )
    return [row["symbol"] for row in rows]


async def add_watch(pool: Any, user_id: int, symbols: list[str], max_symbols: int) -> list[str]:
    """Thêm mã (bỏ qua mã đã có), không vượt trần; trả các mã thêm được."""
    added: list[str] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = {
                row["symbol"]
                for row in await conn.fetch(
                    "SELECT symbol FROM stock_watchlist WHERE telegram_user_id = $1", user_id
                )
            }
            for symbol in symbols:
                if symbol in current or len(current) >= max_symbols:
                    continue
                await conn.execute(
                    "INSERT INTO stock_watchlist (telegram_user_id, symbol) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    user_id,
                    symbol,
                )
                current.add(symbol)
                added.append(symbol)
    return added


async def remove_watch(pool: Any, user_id: int, symbol: str) -> tuple[bool, int]:
    """Bỏ theo dõi 1 mã kèm mọi cảnh báo giá của mã đó: (đã có trong danh sách,
    số cảnh báo đã xoá)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            removed = await conn.fetchval(
                "DELETE FROM stock_watchlist WHERE telegram_user_id = $1 AND symbol = $2 "
                "RETURNING symbol",
                user_id,
                symbol,
            )
            result = await conn.execute(
                "DELETE FROM stock_price_alerts WHERE telegram_user_id = $1 AND symbol = $2",
                user_id,
                symbol,
            )
    return removed is not None, _affected(result)


def _affected(status: str) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


_ALERT_COLUMNS = (
    "id, telegram_user_id, symbol, direction, level, last_state, notified_at, created_at"
)


async def list_price_alerts(pool: Any, user_id: int):
    return await pool.fetch(
        f"SELECT {_ALERT_COLUMNS} FROM stock_price_alerts WHERE telegram_user_id = $1 "
        "ORDER BY symbol, level",
        user_id,
    )


async def add_price_alert(
    pool: Any,
    user_id: int,
    symbol: str,
    direction: str,
    level: float,
    *,
    state: str,
    max_per_user: int,
):
    """Thêm ngưỡng; trả (dòng mới | None nếu đã có, đã chạm trần?)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM stock_price_alerts WHERE telegram_user_id = $1", user_id
            )
            if int(count or 0) >= max_per_user:
                return None, True
            row = await conn.fetchrow(
                f"""
                INSERT INTO stock_price_alerts (
                    telegram_user_id, symbol, direction, level, last_state
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (telegram_user_id, symbol, direction, level) DO NOTHING
                RETURNING {_ALERT_COLUMNS}
                """,
                user_id,
                symbol,
                direction,
                level,
                state,
            )
            return row, False


async def delete_price_alerts(pool: Any, user_id: int, symbol: str, level: float | None) -> int:
    result = await pool.execute(
        "DELETE FROM stock_price_alerts WHERE telegram_user_id = $1 AND symbol = $2 "
        "AND ($3::numeric IS NULL OR level = $3)",
        user_id,
        symbol,
        level,
    )
    return _affected(result)


async def active_price_alerts(pool: Any, limit: int):
    """Mọi ngưỡng đang theo dõi (vòng quét trong phiên), cũ trước, có trần."""
    return await pool.fetch(
        f"SELECT {_ALERT_COLUMNS} FROM stock_price_alerts ORDER BY created_at, id LIMIT $1",
        limit,
    )


async def set_price_alert_state(pool: Any, alert_id: int, state: str, *, notified: bool) -> None:
    await pool.execute(
        "UPDATE stock_price_alerts SET last_state = $2, "
        "notified_at = CASE WHEN $3 THEN now() ELSE notified_at END WHERE id = $1",
        alert_id,
        state,
        notified,
    )


# ─── Nhật ký thông báo chủ động ──────────────────────────────────────────────


async def alert_logged(
    pool: Any, user_id: int, kind: str, ref_key: str, sent_on: str | None
) -> bool:
    """Đã gửi (kind, ref_key) cho user chưa - trong ngày sent_on, hoặc bất kỳ
    ngày nào nếu sent_on None."""
    return bool(
        await pool.fetchval(
            "SELECT 1 FROM stock_alert_log WHERE telegram_user_id = $1 AND kind = $2 "
            "AND ref_key = $3 AND ($4::text IS NULL OR sent_on = ($4::text)::date) LIMIT 1",
            user_id,
            kind,
            ref_key,
            sent_on,
        )
    )


async def log_alert(pool: Any, user_id: int, kind: str, ref_key: str, sent_on: str) -> None:
    await pool.execute(
        "INSERT INTO stock_alert_log (telegram_user_id, kind, ref_key, sent_on) "
        "VALUES ($1, $2, $3, ($4::text)::date) ON CONFLICT DO NOTHING",
        user_id,
        kind,
        ref_key,
        sent_on,
    )


# Mục nằm trong 1 tin gộp đã gửi (dòng nhật ký thêm, không phải tin riêng).
GROUPED_EVENT_KIND = "event_grouped"


async def count_alerts_on(pool: Any, user_id: int, sent_on: str) -> int:
    """Số TIN đã gửi cho user trong ngày (trần tin Zalo/ngày) - không đếm mục gộp."""
    return int(
        await pool.fetchval(
            "SELECT COUNT(*) FROM stock_alert_log WHERE telegram_user_id = $1 "
            "AND sent_on = ($2::text)::date AND kind <> $3",
            user_id,
            sent_on,
            GROUPED_EVENT_KIND,
        )
        or 0
    )


async def prune_alert_log(pool: Any, keep_days: int, batch: int) -> int:
    result = await pool.execute(
        "DELETE FROM stock_alert_log WHERE ctid IN (SELECT ctid FROM stock_alert_log "
        "WHERE created_at < now() - ($1::integer * INTERVAL '1 day') LIMIT $2)",
        keep_days,
        batch,
    )
    return _affected(result)
