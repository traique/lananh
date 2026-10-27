"""Đo RAM (RSS) của tiến trình web theo từng kịch bản tải - chạy offline, không mạng/DB.

Mỗi kịch bản chạy trong 1 tiến trình Python MỚI (RSS không lẫn nhau), in 1 dòng JSON:

- ``web``: chỉ ``import web`` (mức nền sau khởi động).
- ``nen_moi``: import web rồi chạy đường việc nền nhẹ như cảnh báo giá/danh mục
  (fetch_quote + fetch_ohlcv 90 phiên khi DNSE lỗi -> dự phòng VCI qua httpx, lịch
  sự kiện quyền qua REST VCI) với HTTP giả lập - đường sau bản sửa 27/10.
- ``nen_cu``: như trên nhưng nạp thêm đúng các module vnstock mà đường cũ nạp
  (Quote cho dự phòng OHLCV, Company cho lịch quyền) - kéo theo pandas.
- ``trim``: import web, mô phỏng 1 việc nặng (JSON vài chục MB + nhiều buffer cỡ
  vừa như PCM/ảnh/bài báo, xen object sống sót gây phân mảnh heap), giải phóng rồi đo
  RSS trước/sau ``core.memtrim.trim_now()`` (gc.collect + malloc_trim(0)).

Chạy: ``python scripts/benchmark_memory.py`` (hoặc truyền tên kịch bản).
Số đo phụ thuộc máy/phiên bản thư viện; ghi kết quả vào docs khi đổi phần liên quan.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("web", "nen_moi", "nen_cu", "trim")


def _status(field: str) -> float | None:
    try:
        with open("/proc/self/status", encoding="ascii") as file:
            for line in file:
                if line.startswith(f"{field}:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def _env() -> None:
    os.environ.setdefault(
        "SETTINGS_ENC_KEY", __import__("base64").urlsafe_b64encode(b"0" * 32).decode()
    )
    os.environ.setdefault("MEMORY_TRIM_AFTER_WORK", "0")
    sys.path.insert(0, str(ROOT))


def _fake_http():
    """httpx giả: DNSE lỗi 500 (ép dự phòng VCI), VCI trả 300 phiên, events trả 3 dòng."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if "entrade" in request.url.host or "dnse" in request.url.host:
            return httpx.Response(500)
        if request.url.path.endswith("gap-chart"):
            base = 1_700_000_000
            n = 300
            bars = {
                "t": [base + i * 86400 for i in range(n)],
                "o": [25000 + i for i in range(n)],
                "h": [25500 + i for i in range(n)],
                "l": [24500 + i for i in range(n)],
                "c": [25000 + i for i in range(n)],
                "v": [1_000_000 + i for i in range(n)],
            }
            return httpx.Response(200, json=[bars])
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        rows = [
            {"eventCode": "DIV", "exrightDate": 1_718_730_000_000, "valuePerShare": 1500},
            {"eventCode": "ISS", "exrightDate": 1_720_000_000_000, "ratio": "100:20"},
            {"eventCode": "DIV", "exrightDate": 1_690_000_000_000, "valuePerShare": 0.1},
        ]
        return {"data": {"content": rows}}


async def _background_path() -> None:
    import requests

    from stock import corporate_actions, providers

    providers._client = _fake_http()
    requests.get = lambda *a, **k: _FakeResponse()  # type: ignore[assignment]
    for symbol in ("FPT", "HPG", "VNM"):
        await providers.fetch_quote(symbol)
        series = await providers.fetch_ohlcv(symbol, days=90)
        await corporate_actions.adjust_with_corporate_actions(
            symbol, series.closes, series.highs, series.lows, series.dates
        )


def _heavy_work() -> None:
    """Việc nặng giả lập: payload JSON lớn + buffer cỡ vừa xen object sống sót."""
    keep: list = []
    blob = json.dumps([{"t": i, "c": 25000.5 + i, "note": "x" * 40} for i in range(250_000)])
    data = json.loads(blob)
    buffers = []
    for i in range(1500):
        buffers.append(bytes(48 * 1024))  # dưới ngưỡng mmap -> nằm trong heap glibc
        if i % 50 == 0:
            keep.append(bytearray(256))  # object nhỏ sống sót giữa heap
    del blob, data, buffers
    globals()["_KEEP"] = keep


def run_scenario(name: str) -> dict:
    _env()
    import asyncio

    import web  # noqa: F401 - đo mức nền sau import app

    result: dict = {"scenario": name, "after_import_mib": _status("VmRSS")}
    if name in ("nen_moi", "nen_cu"):
        if name == "nen_cu":
            from vnstock.explorer.vci import Company, Quote  # noqa: F401
        asyncio.run(_background_path())
        result["rss_mib"] = _status("VmRSS")
    elif name == "trim":
        from core import memtrim

        _heavy_work()
        result["peak_mib"] = _status("VmHWM")
        result["before_trim_mib"] = _status("VmRSS")
        memtrim.trim_now("benchmark")
        result["after_trim_mib"] = _status("VmRSS")
        result["malloc_trim"] = memtrim.stats.malloc_trim
        result["trim_ms"] = memtrim.stats.last_ms
    result["peak_mib"] = result.get("peak_mib") or _status("VmHWM")
    result["pandas_loaded"] = "pandas" in sys.modules
    result["vnstock_loaded"] = "vnstock" in sys.modules
    return result


def main(argv: list[str]) -> None:
    if len(argv) == 2 and argv[1].startswith("--run="):
        print(json.dumps(run_scenario(argv[1].split("=", 1)[1]), ensure_ascii=False))
        return
    names = [a for a in argv[1:] if a in SCENARIOS] or list(SCENARIOS)
    for name in names:
        proc = subprocess.run(
            [sys.executable, __file__, f"--run={name}"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        line = (proc.stdout.strip().splitlines() or [""])[-1]
        print(line or f'{{"scenario": "{name}", "error": {json.dumps(proc.stderr[-400:])}}}')


if __name__ == "__main__":
    main(sys.argv)
