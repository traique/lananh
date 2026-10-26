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
from core.repositories.portfolio import CorporateRebase, rebased_values
from stock import corporate_actions, providers

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


# ─── Sự kiện quyền: thưởng / cổ tức bằng cổ phiếu / chia tách ────────────────
# Ngày GDKHQ của sự kiện cổ phiếu tỷ lệ r, giá tham chiếu giảm còn 1/(1+r) (cổ
# tức CP 20% -> -16,7%): so giá thô với giá vốn/stop thô là báo "thủng stop", "lỗ"
# giả mỗi ngày tới khi anh /capnhatcp. Vị thế được QUY ĐỔI (số cp x (1+r) làm
# tròn xuống, giữ tổng giá vốn, stop/target chia (1+r)) CHỈ với sự kiện có GDKHQ
# sau mốc giá vốn VÀ được gap giá xác nhận (stock/corporate_actions.py). Sự kiện
# chưa xác nhận chỉ được GHI CHÚ, không bao giờ làm tắt cảnh báo stop thật.
_CORPORATE_LOOKUP_TIMEOUT_SEC = 8.0
_BACKGROUND_LOOKUP_TIMEOUT_SEC = 45.0
# Ghi chú GDKHQ cổ tức tiền chỉ trong vài ngày gần đây (giải thích nhịp giảm mới).
_CASH_DIVIDEND_NOTE_DAYS = 7


def _event_day(raw: str) -> str:
    return str(raw or "")[:10]


def _vn_day(raw: str) -> str:
    try:
        return f"{date.fromisoformat(_event_day(raw)):%d/%m}"
    except ValueError:
        return _event_day(raw)


def _ratio_pct(ratio: float) -> str:
    return f"{ratio * 100:.4g}%".replace(".", ",")


def stock_event_label(event) -> str:
    return f"cổ phiếu {_ratio_pct(event.ratio)} GDKHQ {_vn_day(event.ex_date)}"


def _factor(events) -> float:
    factor = 1.0
    for event in events:
        factor *= 1 + event.ratio
    return factor


def effective_holding(holding: Holding, events) -> Holding:
    """Vị thế sau khi quy đổi các sự kiện cổ phiếu ``events`` - hàm thuần."""
    if not events:
        return holding
    quantity, average, stop, target = rebased_values(
        holding.quantity,
        holding.average_price,
        holding.stop_price,
        holding.target_price,
        _factor(events),
    )
    latest = max(_event_day(event.ex_date) for event in events)
    return Holding(
        user_id=holding.user_id,
        symbol=holding.symbol,
        quantity=quantity,
        average_price=average,
        stop_price=stop,
        target_price=target,
        note=holding.note,
        cost_basis_as_of=max(holding.cost_basis_as_of or latest, latest),
    )


@dataclass(frozen=True)
class CorporateView:
    """Vị thế thô + vị thế đã quy đổi theo sự kiện quyền đã được giá xác nhận."""

    raw: Holding
    effective: Holding
    applied: tuple = ()  # StockEvent đã xác nhận, GDKHQ sau mốc giá vốn
    unconfirmed: tuple = ()  # StockEvent GDKHQ sau mốc, giá CHƯA xác nhận
    cash_dividends: tuple = ()  # DividendEvent GDKHQ gần đây (chỉ ghi chú)
    checked: bool = True  # False: chưa kiểm tra được lịch quyền (lỗi/timeout)

    def rebase(self) -> CorporateRebase | None:
        if not self.applied:
            return None
        return CorporateRebase(
            factor=_factor(self.applied),
            as_of=max(_event_day(event.ex_date) for event in self.applied),
            note="; ".join(stock_event_label(event) for event in self.applied)[:300],
        )

    def ex_date_factor(self, day: str) -> float:
        """Factor của sự kiện đã xác nhận có GDKHQ đúng ngày ``day`` (để quy đổi
        giá đóng cửa phiên trước khi tính lãi/lỗ trong ngày GDKHQ)."""
        return _factor(e for e in self.applied if _event_day(e.ex_date) == _event_day(day))

    def notes(self) -> list[str]:
        """Dòng ghi chú (đã quy đổi / sự kiện chưa xác nhận / cổ tức tiền)."""
        lines: list[str] = []
        raw, eff = self.raw, self.effective
        if self.applied:
            lines.append(
                f"🔁 Đã quy đổi theo {', '.join(stock_event_label(e) for e in self.applied)}: "
                f"{_fmt_number(raw.quantity)} → {_fmt_number(eff.quantity)} cp, giá vốn "
                f"{_fmt_number(raw.average_price)} → {_fmt_number(eff.average_price)}đ"
            )
        if self.unconfirmed:
            estimate = raw.average_price / _factor(self.applied + self.unconfirmed)
            labels = ", ".join(stock_event_label(e) for e in self.unconfirmed)
            lines.append(
                f"ℹ️ Có thể do GDKHQ {labels} (giá chưa xác nhận) - lãi/lỗ, stop có thể lệch; "
                f"nếu đúng, giá vốn quy đổi ≈ {_fmt_number(estimate)}đ, gõ /capnhatcp để lưu"
            )
        for dividend in self.cash_dividends:
            dps = corporate_actions.normalize_dps(dividend.cash_value)
            if dps:
                lines.append(
                    f"ℹ️ GDKHQ cổ tức tiền {_fmt_number(dps)}đ/cp ngày {_vn_day(dividend.ex_date)}"
                    " - giá giảm tương ứng, không phải lỗ thật"
                )
        return lines


async def corporate_view(holding: Holding, *, series=None) -> CorporateView:
    """Quy đổi vị thế theo sự kiện quyền đã được giá xác nhận (không raise).

    Vị thế không có mốc giá vốn (dữ liệu test cũ) hoặc mốc là hôm nay thì không
    tra cứu gì. ``series``: OhlcvSeries thô 90 phiên nếu caller đã có sẵn."""
    plain = CorporateView(holding, holding)
    basis = holding.cost_basis_as_of
    today = today_vn()
    if not basis or basis >= today:
        return plain
    try:
        dividends, stock_events = await corporate_actions.fetch_corporate_events_cached(
            holding.symbol
        )
    except Exception:
        return CorporateView(holding, holding, checked=False)
    relevant = [e for e in stock_events if basis < _event_day(e.ex_date) <= today]
    recent_from = (date.fromisoformat(today) - timedelta(days=_CASH_DIVIDEND_NOTE_DAYS)).isoformat()
    cash = tuple(
        d
        for d in dividends
        if basis < _event_day(d.ex_date) <= today and _event_day(d.ex_date) >= recent_from
    )
    if not relevant:
        return CorporateView(holding, holding, cash_dividends=cash)
    # Chỉ khi có sự kiện cổ phiếu sau mốc giá vốn mới cần chuỗi giá để xác nhận gap.
    try:
        if series is None or not getattr(series, "closes", None):
            series = await providers.fetch_ohlcv(holding.symbol, days=90)
    except Exception:
        return CorporateView(holding, holding, (), tuple(relevant), cash, checked=False)
    applied: list = []
    closes = list(getattr(series, "closes", None) or [])
    if len(closes) >= 2:
        outcome = corporate_actions.apply_corporate_adjustment(
            closes,
            list(series.highs),
            list(series.lows),
            list(series.dates),
            [],
            relevant,
            list(series.volumes),
        )
        applied = list(outcome.applied_stock_events)
    unconfirmed = tuple(e for e in relevant if e not in applied)
    return CorporateView(
        holding,
        effective_holding(holding, applied),
        tuple(applied),
        unconfirmed,
        cash,
    )


async def corporate_views(
    holdings: list[Holding], series_list=None, *, limit_sec: float = _CORPORATE_LOOKUP_TIMEOUT_SEC
) -> list[CorporateView]:
    """corporate_view cho nhiều mã, có trần thời gian chung (lỗi/quá giờ -> vị thế
    thô). Lệnh tương tác giữ trần ngắn; job nền truyền ``limit_sec`` dài hơn."""
    series_list = series_list or [None] * len(holdings)
    try:
        return list(
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        corporate_view(h, series=s)
                        for h, s in zip(holdings, series_list, strict=False)
                    )
                ),
                timeout=limit_sec,
            )
        )
    except Exception:
        return [CorporateView(h, h, checked=False) for h in holdings]


async def _pending_rebase(user_id: int, symbol: str) -> CorporateRebase | None:
    """Quy đổi quyền còn treo của 1 vị thế, áp TRƯỚC mọi thao tác ghi (mua thêm,
    bán, đặt SL/TP) để số anh nhập không bị trộn với số trước GDKHQ."""
    try:
        holding = await get_holding(user_id, symbol)
        if holding is None:
            return None
        (view,) = await corporate_views([holding])
    except Exception:
        return None
    return view.rebase()


async def apply_confirmed_corporate_actions(
    holding: Holding,
) -> tuple[Holding, Holding, CorporateRebase] | None:
    """Lưu hẳn quy đổi quyền đã xác nhận (job nền hằng ngày) - chuỗi 90 phiên chỉ
    còn xác nhận được sự kiện trong ~4 tháng, sau đó quy đổi tạm tính sẽ mất.
    Trả (trước, sau, quy đổi) hoặc None. Chạy lại an toàn (mốc giá vốn đã qua)."""
    (view,) = await corporate_views([holding], limit_sec=_BACKGROUND_LOOKUP_TIMEOUT_SEC)
    rebase = view.rebase()
    if rebase is None:
        return None
    await ensure_schema()
    result = await db.portfolio_rebase_holding(holding.user_id, holding.symbol, rebase)
    if result is None:
        return None
    before, after = result
    return _holding(before), _holding(after), rebase


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
    symbol = symbol.strip().upper()
    row = await db.portfolio_record_purchase(
        user_id,
        symbol,
        quantity,
        price,
        stop_price=stop_price,
        target_price=target_price,
        fee=round(quantity * price * fee_rate()),
        basis_date=today_vn(),
        rebase=await _pending_rebase(user_id, symbol),
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
    symbol = symbol.strip().upper()
    row = await db.portfolio_update_alerts(
        user_id,
        symbol,
        stop_price=stop_price,
        target_price=target_price,
        rebase=await _pending_rebase(user_id, symbol),
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
    symbol = symbol.strip().upper()
    result = await db.portfolio_record_sale(
        user_id,
        symbol,
        quantity,
        price,
        fee_rate=fee_rate(),
        tax_rate=sell_tax_rate(),
        rebase=await _pending_rebase(user_id, symbol),
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


def _effective_quote(quote, view: CorporateView | None):
    """Quote với giá đóng cửa phiên trước đã quy đổi nếu hôm nay là ngày GDKHQ của
    sự kiện đã xác nhận (không thì "giảm 16,7% trong ngày" giả)."""
    if quote is None or view is None or not quote.prev_close:
        return quote
    factor = view.ex_date_factor(str(getattr(quote, "date", "") or ""))
    if factor == 1.0:
        return quote
    prev_close = quote.prev_close / factor
    change = quote.price - prev_close
    return providers.Quote(
        symbol=quote.symbol,
        price=quote.price,
        prev_close=prev_close,
        change=round(change, 2),
        change_pct=round(change / prev_close * 100, 2) if prev_close else 0.0,
        date=quote.date,
        is_realtime=quote.is_realtime,
    )


async def build_report(user_id: int, *, digest: bool = False) -> str:
    raw_holdings = await list_holdings(user_id)
    if not raw_holdings:
        return "📭 Chưa có cổ phiếu nào trong danh mục đang nắm giữ."

    quotes, views = await asyncio.gather(
        asyncio.gather(
            *(providers.fetch_quote(item.symbol) for item in raw_holdings),
            return_exceptions=True,
        ),
        corporate_views(raw_holdings),
    )
    priced = []
    for view, quote in zip(views, quotes, strict=False):
        holding = view.effective
        valid_quote = None if isinstance(quote, BaseException) else _effective_quote(quote, view)
        market_value = holding.quantity * valid_quote.price if valid_quote else 0.0
        priced.append((holding, valid_quote, market_value, view))
    total_market_value = sum(item[2] for item in priced)
    total_cost = sum(
        holding.quantity * holding.average_price
        for holding, quote, _, _ in priced
        if quote is not None
    )
    total_pnl = total_market_value - total_cost if total_market_value else 0.0
    total_pnl_pct = total_pnl / total_cost * 100 if total_cost and total_market_value else 0.0
    # Lãi/lỗ TRONG NGÀY: (giá - giá đóng cửa phiên trước) x số lượng, chỉ tính mã
    # có prev_close hợp lệ; % so với giá trị danh mục theo giá phiên trước.
    day_pnl = sum(
        (quote.price - quote.prev_close) * holding.quantity
        for holding, quote, _, _ in priced
        if quote is not None and quote.prev_close and quote.prev_close > 0
    )
    prev_value = sum(
        quote.prev_close * holding.quantity
        for holding, quote, _, _ in priced
        if quote is not None and quote.prev_close and quote.prev_close > 0
    )
    day_pnl_pct = day_pnl / prev_value * 100 if prev_value else 0.0
    day_label = _session_label([quote for _, quote, _, _ in priced if quote is not None])

    title = "📊 *Digest danh mục đang nắm giữ:*" if digest else "📊 *Danh mục đang nắm giữ:*"
    lines = [title]
    for holding, quote, market_value, view in priced:
        quantity = _fmt_number(holding.quantity)
        average = _fmt_number(holding.average_price)
        if quote is None:
            lines.append(
                f"\n• *{holding.symbol}* — {quantity} cp | Giá vốn {average}đ"
                "\n  ⚠️ Chưa lấy được giá hiện tại; không tính vào tổng lãi/lỗ"
            )
            lines.extend(f"  {note}" for note in view.notes())
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
        lines.extend(f"  {note}" for note in view.notes())

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
