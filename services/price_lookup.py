"""Giá vàng, giá xăng dầu, tỷ giá dạng dữ liệu cho web công khai /gia-vang, /gia-xang
và GET /api/v1/gold, /api/v1/fuel, /api/v1/fx.

Dùng lại đúng hàm lấy dữ liệu + cache của chat (services/vn_market_data.get_gold_live,
get_gold, get_fx và services/fuel_prices.get_fuel): mỗi nguồn 1 bản trong RAM, cache
lỗi 60 giây, không gọi nguồn trùng, không parse lại, không gọi AI, không đụng DB. Ở
đây chỉ đổi bảng đã parse sang dict khoá camelCase (số thô, đơn vị ghi trong ``unit``)
để trang HTML và API dùng chung. Câu chữ trung tính (trang công khai).

Vàng: vang.today là nguồn chính (nhiều thương hiệu + XAU/USD, có tăng giảm so với hôm
trước); vang.today lỗi thì rơi về bảng SJC (``fallback: true``, không có tăng giảm).
Tỷ giá Vietcombank chỉ để quy đổi vàng thế giới và mục tỷ giá; tỷ giá lỗi thì bỏ phần
quy đổi chứ không làm hỏng bảng vàng. Cả hai nguồn vàng lỗi: raise MarketDataError.
"""

from __future__ import annotations

import asyncio

from services import fuel_prices
from services import vn_market_data as market

GOLD_SOURCE_LIVE = "vang.today"
GOLD_SOURCE_SJC = "sjc.com.vn"
FX_SOURCE = "Vietcombank"
_SJC_MAX_ROWS = 12
CONVERSION_NOTE = (
    "Quy đổi tham khảo theo giá bán USD Vietcombank (37,5 g/lượng, 31,1035 g/oz), "
    "chưa gồm thuế, phí gia công, vận chuyển."
)


# Tên tiếng Việt cho các ngoại tệ chính (nguồn chỉ ghi tên tiếng Anh viết hoa).
FX_NAMES_VI = {
    "USD": "Đô la Mỹ",
    "EUR": "Euro",
    "JPY": "Yên Nhật",
    "CNY": "Nhân dân tệ",
    "KRW": "Won Hàn Quốc",
    "GBP": "Bảng Anh",
    "AUD": "Đô la Úc",
    "SGD": "Đô la Singapore",
    "THB": "Baht Thái",
}


def _fx_rows(table: market.FxTable) -> list[dict]:
    return [
        {
            "code": r.code,
            "name": r.name,
            "nameVi": FX_NAMES_VI.get(r.code, ""),
            "cashBuy": r.buy,
            "transferBuy": r.transfer,
            "sell": r.sell,
        }
        for code in market.MAJOR_CURRENCIES
        if (r := table.rates.get(code)) is not None
    ]


def fx_json(table: market.FxTable) -> dict:
    return {
        "source": FX_SOURCE,
        "updated": table.updated,
        "unit": "VND",
        "rates": _fx_rows(table),
    }


def _world_json(live: market.LiveGoldTable, fx: market.FxTable | None) -> dict | None:
    row = live.get("XAUUSD")
    price = (row.buy or row.sell) if row is not None else None
    if row is None or not price:
        return None
    out: dict = {
        "code": row.code,
        "name": row.label,
        "priceUsdPerOunce": price,
        "change": row.change_buy if row.buy else row.change_sell,
        "unit": "USD/oz",
        "conversion": None,
    }
    rate = market._usd_rate(fx)
    if rate:
        converted = market.world_gold_vnd(price, rate)
        sjc = live.get("SJL1L10")
        out["conversion"] = {
            "vndPerTael": round(converted),
            "usdVnd": rate,
            "rateSource": FX_SOURCE,
            "rateUpdated": fx.updated if fx is not None else "",
            # Giá bán SJC miếng trừ giá quy đổi (dương = SJC cao hơn thế giới).
            "sjcSellGap": round(sjc.sell - converted) if sjc is not None and sjc.sell else None,
            "note": CONVERSION_NOTE,
        }
    return out


def gold_json(
    live: market.LiveGoldTable | None,
    sjc: market.GoldTable | None = None,
    fx: market.FxTable | None = None,
) -> dict:
    """Bảng vàng từ vang.today (``live``), hoặc bảng SJC khi vang.today lỗi."""
    if live is not None:
        domestic = [
            {
                "code": r.code,
                "name": r.label,
                "brand": r.brand,
                "branch": "",
                "buy": r.buy,
                "sell": r.sell,
                "changeBuy": r.change_buy,
                "changeSell": r.change_sell,
            }
            for r in live.rows
            if r.currency == "VND"
        ]
        return {
            "source": GOLD_SOURCE_LIVE,
            "fallback": False,
            "updated": live.updated,
            "unit": "VND/lượng",
            "domestic": domestic,
            "world": _world_json(live, fx),
            "fx": fx_json(fx) if fx is not None else None,
        }
    if sjc is None:
        raise market.MarketDataError("Không có bảng giá vàng nào")
    # Bảng SJC liệt kê lại cùng loại vàng theo từng chi nhánh: giữ chi nhánh đầu.
    branch = sjc.rows[0].branch
    rows = [r for r in sjc.rows if r.branch == branch] or list(sjc.rows)
    return {
        "source": GOLD_SOURCE_SJC,
        "fallback": True,
        "updated": sjc.updated,
        "unit": "VND/lượng",
        "domestic": [
            {
                "code": "",
                "name": r.name,
                "brand": "sjc",
                "branch": r.branch,
                "buy": r.buy,
                "sell": r.sell,
                "changeBuy": None,
                "changeSell": None,
            }
            for r in rows[:_SJC_MAX_ROWS]
        ],
        "world": None,
        "fx": fx_json(fx) if fx is not None else None,
    }


async def _optional_fx() -> market.FxTable | None:
    try:
        return await market.get_fx()
    except market.MarketDataError:
        return None  # thiếu tỷ giá: bỏ quy đổi, bảng vàng vẫn hiện


async def _gold_tables() -> tuple[market.LiveGoldTable | None, market.GoldTable | None]:
    try:
        return await market.get_gold_live(), None
    except market.MarketDataError:
        return None, await market.get_gold()  # lỗi nữa thì raise MarketDataError


async def gold() -> dict:
    """Giá vàng + tỷ giá (tải song song, mỗi nguồn qua cache sẵn có)."""
    (live, sjc), fx = await asyncio.gather(_gold_tables(), _optional_fx())
    return gold_json(live, sjc, fx)


async def fx() -> dict:
    return fx_json(await market.get_fx())


def _fuel_row(row: fuel_prices.FuelRow, *, zones: bool) -> dict:
    out: dict = {
        "name": row.name,
        "kind": fuel_prices.kind_of(row.name),
        "change": row.change,
        "prevChange": row.prev_change,
    }
    if zones:
        out["zone1"], out["zone2"] = row.zone1, row.zone2
    else:
        out["price"] = row.zone1
    return out


def fuel_json(table: fuel_prices.FuelTable) -> dict:
    return {
        "source": table.source,
        "updated": table.updated,
        "unit": "VND/lít",
        "petrolimex": [_fuel_row(r, zones=True) for r in table.petrolimex],
        "pvoil": [_fuel_row(r, zones=False) for r in table.pvoil],
    }


async def fuel() -> dict:
    return fuel_json(await fuel_prices.get_fuel())
