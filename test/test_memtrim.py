"""Trả heap rảnh về hệ điều hành (core/memtrim.py) và đường giá nhẹ không nạp
pandas/vnstock (stock/providers.py VCI httpx, stock/corporate_actions.py REST VCI) -
sửa sau khi chạy thật 27/10 (Render free 512 MB)."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from core import memtrim
from services import concurrency, memory_stats
from stock import corporate_actions, providers, vci_direct

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean():
    memtrim.reset_for_tests()
    yield
    memtrim.reset_for_tests()


def test_trim_now_ghi_so_do_va_co_malloc_trim_tren_linux():
    stats = memtrim.trim_now("test")
    assert stats.runs == 1 and stats.last_reason == "test"
    if sys.platform.startswith("linux"):
        assert stats.malloc_trim is True and stats.last_after_mib is not None
    snap = memory_stats.snapshot()["trim"]
    assert snap["runs"] == 1 and snap["interval_sec"] == 600


@pytest.mark.asyncio
async def test_request_gop_nhieu_yeu_cau_va_tat_duoc(monkeypatch):
    monkeypatch.setenv("MEMORY_TRIM_AFTER_WORK", "1")
    monkeypatch.setenv("MEMORY_TRIM_MIN_GAP_SEC", "0")
    assert memtrim.request("a", delay=0.01) is True
    assert memtrim.request("b", delay=0.01) is False  # đang có lần hẹn
    await asyncio.sleep(0.1)
    assert memtrim.stats.runs == 1 and memtrim.stats.last_reason == "a"
    monkeypatch.setenv("MEMORY_TRIM_AFTER_WORK", "0")
    assert memtrim.request("c", delay=0.01) is False


def test_dinh_ky_theo_moc_va_tat_bang_0(monkeypatch):
    monkeypatch.setenv("MEMORY_TRIM_INTERVAL_SEC", "600")
    assert memtrim.maybe_periodic(now=10_000.0) is True
    last = memtrim._last_at
    assert memtrim.maybe_periodic(now=last + 10) is False
    assert memtrim.maybe_periodic(now=last + 601) is True
    monkeypatch.setenv("MEMORY_TRIM_INTERVAL_SEC", "0")
    assert memtrim.maybe_periodic(now=last + 10_000) is False


@pytest.mark.asyncio
async def test_het_luot_tra_loi_va_viec_nen_thi_hen_don_ram(monkeypatch):
    reasons = []
    monkeypatch.setattr(memtrim, "request", lambda reason, **kw: reasons.append(reason))
    async with concurrency.assistant_turn("owner"):
        assert reasons == []
    async with concurrency.background_slot("owner"):
        pass
    assert reasons == ["lượt trả lời", "việc nền"]


# ─── Đường giá nhẹ: không vnstock/pandas ────────────────────────────────────


def _vci_handler(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if "entrade" in request.url.host:
            return httpx.Response(500)
        assert request.url.path.endswith("/api/chart/OHLCChart/gap-chart")
        body = json.loads(request.content)
        assert body["timeFrame"] == "ONE_DAY" and body["symbols"] == ["FPT"]
        base = 1_760_000_000
        return httpx.Response(
            200,
            json=[
                {
                    "t": [str(base + i * 86400) for i in range(5)],
                    "o": [120000] * 5,
                    "h": [121000 + i for i in range(5)],
                    "l": [119000] * 5,
                    "c": [120000 + i * 100 for i in range(5)],
                    "v": [1000] * 5,
                }
            ],
        )

    return handler


@pytest.mark.asyncio
async def test_dnse_loi_thi_du_phong_vci_qua_httpx(monkeypatch):
    calls = []
    monkeypatch.setattr(
        providers, "_client", httpx.AsyncClient(transport=httpx.MockTransport(_vci_handler(calls)))
    )
    providers._ohlcv_cache.clear()
    series = await providers.fetch_ohlcv("FPT", days=5)
    assert series.source == "vci" and series.closes == [120000, 120100, 120200, 120300, 120400]
    assert len(series.dates) == 5 and calls[-1] == "trading.vietcap.com.vn"
    await providers.close_http_client()
    providers._ohlcv_cache.clear()


def test_parse_vci_chart_chi_so_va_gia_nghin_dong():
    index = providers.parse_vci_chart(
        "VNINDEX", [{"t": [1_760_000_000], "h": [1700.5], "l": [1690], "c": [1695.2], "v": [1]}], 5
    )
    assert index.closes == [1695] and index.source == "vci"
    # Giá kiểu nghìn đồng (25.5) vẫn quy ra đồng như đường vnstock cũ.
    thousands = providers.parse_vci_chart(
        "AAA", {"data": [{"t": [1_760_000_000], "h": [26], "l": [25], "c": [25.5], "v": [1]}]}, 5
    )
    assert thousands.closes == [25500]
    assert providers.parse_vci_chart("AAA", {"data": {}}, 5).closes == []


@pytest.mark.asyncio
async def test_lich_su_kien_quyen_qua_rest_vci(monkeypatch):
    rows = vci_direct.normalize_event_rows(
        [
            {"eventCode": "DIV", "exrightDate": 1_718_730_000_000, "valuePerShare": 1500},
            {
                "eventCode": "ISS",
                "exrightDate": 1_720_000_000_000,
                "ratio": "100:20",
                "eventTitleVi": "Phát hành cổ phiếu thưởng",
            },
            "rác",
        ]
    )
    assert rows[0]["exright_date"] == "2024-06-19" and rows[0]["event_code"] == "DIV"
    monkeypatch.setattr(vci_direct, "fetch_event_history", lambda symbol: rows)
    dividends, stock_events = await corporate_actions.fetch_corporate_events("FPT")
    assert [(d.ex_date, d.cash_value) for d in dividends] == [("2024-06-19", 1500.0)]
    assert [round(e.ratio, 2) for e in stock_events] == [0.2]

    def broken(symbol):
        raise RuntimeError("mạng lỗi")

    monkeypatch.setattr(vci_direct, "fetch_event_history", broken)
    assert await corporate_actions.fetch_corporate_events("FPT") == ([], [])


def test_viec_nen_gia_khong_nap_pandas_vnstock():
    """Tiến trình mới: import web + đường cảnh báo giá/danh mục (DNSE lỗi -> VCI,
    lịch quyền) với HTTP giả - pandas và vnstock KHÔNG được nạp (~50 MB RSS)."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "benchmark_memory.py"), "--run=nen_moi"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["pandas_loaded"] is False and result["vnstock_loaded"] is False
