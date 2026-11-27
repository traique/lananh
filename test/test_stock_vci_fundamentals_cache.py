"""Bộ nhớ của cache VCI + TTL bundle định giá rỗng.

- stock/vci_direct.py chỉ cache đúng các trường/quý mà stock/fundamentals.py đọc
  (hàng raw VCI 100-150 trường x 41+ quý x 128 mã từng chiếm hàng chục MB RAM) -
  đầu ra định giá/tăng trưởng phải giữ nguyên như khi đọc hàng raw.
- Bundle định giá rỗng hoàn toàn (VCI lỗi thoáng qua) chỉ cache 3 phút.
"""

import random
import time

import httpx
import pytest

from stock import fundamentals, vci_direct


def _raw_ratio_rows(count: int = 41) -> list[dict]:
    rows = []
    for i in range(count):
        year, quarter = 2016 + i // 4, 1 + i % 4
        row = {f"junk{k}": k * 1.5 for k in range(140)}
        row.update(
            {
                "yearReport": year,
                "quarter": quarter,
                "pe": 8.0 + (i % 7),
                "pb": 1.2 + i / 100,
                "roe": 0.15 + i / 1000,
                "dividendYield": 0.03,
                "debtToEquity": 0.8,
                "currentRatio": 1.4,
                "marketCap": 1e12,
            }
        )
        rows.append(row)
    random.Random(3).shuffle(rows)  # API trả CŨ->MỚI, thử cả thứ tự lộn xộn
    return rows


def _raw_income_rows(count: int = 41) -> list[dict]:
    rows = []
    for i in range(count):
        row = {f"isa{k}": float(k * 1000 + i) for k in range(1, 30)}
        row.update(
            {
                "yearReport": 2016 + i // 4,
                "lengthReport": 1 + i % 4,
                "isa3": 1000.0 + i * 37,
                "isa20": 100.0 + (i % 5) * 11,
            }
        )
        rows.append(row)
    return rows


@pytest.fixture
def vci(monkeypatch):
    ratios, income = _raw_ratio_rows(), _raw_income_rows()
    details = {"currentPrice": 52_000, "organName": "x" * 500, "marketCap": 1e12}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        path = request.url.path
        if path.endswith("/statistics-financial"):
            return httpx.Response(200, json={"data": ratios})
        if path.endswith("/financial-statement"):
            return httpx.Response(200, json={"data": {"quarters": income}})
        if path.endswith("/details"):
            return httpx.Response(200, json={"data": details})
        return httpx.Response(404)

    monkeypatch.setattr(vci_direct, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    for name in ("_ratio_cache", "_income_cache", "_details_cache"):
        monkeypatch.setattr(vci_direct, name, {})
    return {"ratios": ratios, "income": income, "calls": calls}


def test_ratio_chi_cache_truong_va_so_quy_can_dung(vci):
    rows = vci_direct.fetch_statistical_ratios("FPT")
    assert len(rows) == vci_direct.RATIO_HISTORY_QUARTERS == fundamentals._PE_HISTORY_QUARTERS
    assert all(set(row) <= set(vci_direct._RATIO_KEYS) for row in rows)
    periods = [(row["yearReport"], row["quarter"]) for row in rows]
    assert periods == sorted(periods, reverse=True) and periods[0] == (2026, 1)
    cached = vci_direct._ratio_cache["FPT"][1]
    assert cached is rows or cached == rows
    vci_direct.fetch_statistical_ratios("fpt")
    assert vci["calls"].count("/api/iq-insight-service/v1/company/FPT/statistics-financial") == 1


def test_dinh_gia_giu_nguyen_so_voi_doc_hang_raw(vci, monkeypatch):
    projected = fundamentals._fetch_valuation_sync("FPT")
    raw_sorted = vci_direct._sorted_desc(vci["ratios"], "yearReport", "quarter")
    monkeypatch.setattr(vci_direct, "fetch_statistical_ratios", lambda symbol: raw_sorted)
    from_raw = fundamentals._fetch_valuation_sync("FPT")
    assert projected == from_raw
    assert projected.pe is not None and projected.eps == round(52_000 / projected.pe, 1)
    assert projected.pe_history_quarters == 20


def test_kqkd_chi_cache_5_quy_va_tang_truong_giu_nguyen(vci, monkeypatch):
    rows = vci_direct.fetch_income_statement("FPT")
    assert len(rows) == vci_direct.INCOME_QUARTERS == 5
    assert all(set(row) <= set(vci_direct._INCOME_KEYS) for row in rows)
    projected = fundamentals._fetch_growth_sync("FPT")
    raw_sorted = vci_direct._sorted_desc(vci["income"], "yearReport", "lengthReport")
    monkeypatch.setattr(vci_direct, "fetch_income_statement", lambda symbol: raw_sorted)
    from_raw = fundamentals._fetch_growth_sync("FPT")
    for field in ("revenue_qoq_pct", "revenue_yoy_pct", "profit_qoq_pct", "profit_yoy_pct"):
        assert getattr(projected, field) == getattr(from_raw, field)
    assert projected.revenue_yoy_pct is not None and projected.quarters_available == 5


def test_details_chi_giu_gia_hien_tai(vci):
    assert vci_direct.fetch_details("FPT") == {"currentPrice": 52_000}


# ─── Bundle rỗng: TTL ngắn ───────────────────────────────────────────────────


@pytest.fixture
def fake_sources(monkeypatch):
    state = {"calls": 0, "valuation": None}

    def valuation(symbol):
        state["calls"] += 1
        return state["valuation"]

    def nothing(symbol):
        return None

    async def benchmark(symbol, sample_size=8):
        return fundamentals.SectorBenchmark("pe", None, 0, None)

    monkeypatch.setattr(fundamentals, "_fetch_valuation_sync", valuation)
    monkeypatch.setattr(fundamentals, "_fetch_foreign_sync", nothing)
    monkeypatch.setattr(fundamentals, "_fetch_growth_sync", nothing)
    monkeypatch.setattr(fundamentals, "_fetch_events_sync", nothing)
    monkeypatch.setattr(fundamentals, "fetch_sector_benchmark", benchmark)
    monkeypatch.setattr(fundamentals, "_fundamentals_cache", {})
    return state


def _age(symbol: str, seconds: float) -> None:
    ts, ttl, bundle = fundamentals._fundamentals_cache[symbol]
    fundamentals._fundamentals_cache[symbol] = (ts - seconds, ttl, bundle)


@pytest.mark.asyncio
async def test_bundle_rong_chi_cache_3_phut(fake_sources):
    bundle = await fundamentals.fetch_fundamentals("ZZZ")
    assert fundamentals._bundle_is_empty(bundle)
    assert fundamentals._fundamentals_cache["ZZZ"][1] == fundamentals._FUNDAMENTALS_EMPTY_TTL
    await fundamentals.fetch_fundamentals("ZZZ")
    assert fake_sources["calls"] == 1  # vẫn dùng cache trong 3 phút
    _age("ZZZ", fundamentals._FUNDAMENTALS_EMPTY_TTL + 1)
    fake_sources["valuation"] = fundamentals.Valuation(pe=10.0)
    bundle = await fundamentals.fetch_fundamentals("ZZZ")
    assert fake_sources["calls"] == 2 and bundle.valuation.pe == 10.0
    # Có số liệu -> cache 30 phút như cũ.
    assert fundamentals._fundamentals_cache["ZZZ"][1] == fundamentals._FUNDAMENTALS_CACHE_TTL
    _age("ZZZ", fundamentals._FUNDAMENTALS_EMPTY_TTL + 1)
    await fundamentals.fetch_fundamentals("ZZZ")
    assert fake_sources["calls"] == 2


def test_cache_bundle_co_tran():
    now = time.monotonic()
    cache = {str(i): (now, 1800.0, None) for i in range(200)}
    original = fundamentals._fundamentals_cache
    try:
        fundamentals._fundamentals_cache = cache
        fundamentals._evict_fundamentals_cache(now)
        assert len(cache) < 128
    finally:
        fundamentals._fundamentals_cache = original
