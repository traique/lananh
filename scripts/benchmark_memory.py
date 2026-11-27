"""Đo RAM (RSS) của tiến trình web theo từng kịch bản tải - chạy offline, không mạng/DB.

Mỗi kịch bản chạy trong 1 tiến trình Python MỚI (RSS không lẫn nhau), in 1 dòng JSON:

- ``web``: chỉ ``import web`` (mức nền sau khởi động).
- ``nen_moi``: import web rồi chạy đường việc nền nhẹ như cảnh báo giá/danh mục
  (fetch_quote + fetch_ohlcv 90 phiên khi DNSE lỗi -> dự phòng VCI qua httpx, lịch
  sự kiện quyền qua REST VCI) với HTTP giả lập - đường sau bản sửa 27/10. (Kịch
  bản ``nen_cu`` nạp vnstock để so sánh đã bỏ cùng thư viện vnstock, 16/11/2026.)
- ``trim``: import web, mô phỏng 1 việc nặng (JSON vài chục MB + nhiều buffer cỡ
  vừa như PCM/ảnh/bài báo, xen object sống sót gây phân mảnh heap), giải phóng rồi đo
  RSS trước/sau ``core.memtrim.trim_now()`` (gc.collect + malloc_trim(0)).
- ``luot_chat``: import web rồi chạy ``CHAT_TURNS`` lượt chat chữ Zalo trọn đường
  (handle_channel_text: lệnh/lối tắt, trí nhớ, tra web, kho tài liệu, persona,
  ngân sách prompt, chuỗi provider, lưu lịch sử, trích nhớ nền) với provider giả
  (9Router trả SSE qua httpx.MockTransport) và DB giả (pool asyncpg trả rỗng) - không
  mạng (getaddrinfo bị chặn). Đo RSS sau lượt đầu/cuối, đỉnh, thời gian từng lượt.

``--importtime``: chạy ``python -X importtime -c "import web"`` và in 20 module nạp
chậm nhất (theo thời gian tích luỹ và thời gian riêng) - bắt hồi quy thời gian khởi
động (Render free ~0,1 CPU: mỗi giây ở máy dev là nhiều giây trên Render). Lưu ý:
venv dev có rich/pygments (kéo theo bởi pip-audit) nên ``import httpx`` nạp luôn CLI
của nó (httpx._main, ~75 ms, vài MB); image Render không cài 2 gói này nên không nạp
- số đo ở venv dev cao hơn production một chút.

Chạy: ``python scripts/benchmark_memory.py`` (hoặc truyền tên kịch bản),
``python scripts/benchmark_memory.py --importtime``.
Số đo phụ thuộc máy/phiên bản thư viện; ghi kết quả vào docs khi đổi phần liên quan.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("web", "nen_moi", "trim", "luot_chat")
CHAT_TURNS = 5
IMPORTTIME_TOP = 20


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
    import httpx

    from stock import corporate_actions, providers, vci_direct

    providers._client = _fake_http()
    # Lịch sự kiện quyền VCI (stock/vci_direct.py dùng 1 httpx.Client đồng bộ chung):
    # trả dữ liệu giả, không gọi mạng thật.
    vci_direct._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_FakeResponse().json())
        )
    )
    for symbol in ("FPT", "HPG", "VNM"):
        await providers.fetch_quote(symbol)
        series = await providers.fetch_ohlcv(symbol, days=90)
        await corporate_actions.adjust_with_corporate_actions(
            symbol, series.closes, series.highs, series.lows, series.dates
        )


class _NullAsync:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _Row(dict):
    """Dòng RETURNING giả: cột nào không có thì None."""

    def __missing__(self, key):
        return None


_ids = iter(range(1, 1_000_000))


class _FakeConn:
    """Kết nối asyncpg giả: SELECT trả rỗng, INSERT ... RETURNING trả id mới, lệnh
    ghi khác coi như thành công."""

    async def fetch(self, *args, **kwargs) -> list:
        return []

    async def fetchrow(self, query: str = "", *args, **kwargs):
        return _Row(id=next(_ids)) if "RETURNING" in query.upper() else None

    async def fetchval(self, query: str = "", *args, **kwargs):
        return next(_ids) if "RETURNING" in query.upper() else None

    async def execute(self, *args, **kwargs) -> str:
        return "OK"

    async def executemany(self, *args, **kwargs) -> None:
        return None

    def transaction(self) -> _NullAsync:
        return _NullAsync()


class _Acquire(_NullAsync):
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn


class _FakePool(_FakeConn):
    def acquire(self) -> _Acquire:
        return _Acquire(_FakeConn())

    async def close(self) -> None:
        return None


# Câu tán gẫu thường: kế hoạch tra web "auto" (không tra), không lệnh, không lối tắt.
_CHAT_TEXT = "Em ơi, anh đang buồn, nói chuyện với anh chút đi"
_ANSWER = (
    "Dạ anh ơi, em kể anh nghe chuyện này nha. Có anh chàng đi phỏng vấn, được hỏi điểm "
    "yếu lớn nhất là gì, anh ấy đáp: trả lời quá thật thà. Người phỏng vấn bảo: tôi "
    "không nghĩ đó là điểm yếu. Anh chàng nói: tôi chẳng quan tâm anh nghĩ gì. "
) * 4


def _fake_provider():
    """9Router giả: trả câu trả lời dạng SSE từng đoạn như gateway thật."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        parts = [_ANSWER[i : i + 120] for i in range(0, len(_ANSWER), 120)]
        events = [
            "data: " + json.dumps({"choices": [{"delta": {"content": part}}]}, ensure_ascii=False)
            for part in parts
        ]
        body = "\n\n".join([*events, "data: [DONE]"]) + "\n\n"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _block_network() -> None:
    """Không lượt nào được ra mạng thật: phân giải DNS ném lỗi ngay."""
    import socket

    def refuse(*args, **kwargs):
        raise OSError("benchmark_memory: không gọi mạng")

    socket.getaddrinfo = refuse  # type: ignore[assignment]


async def _chat_turns(result: dict) -> None:
    """CHAT_TURNS lượt chat Zalo trọn đường như channel_dispatch (khoá lượt, hạn
    chót, mã lượt) với provider + DB giả."""
    from ai import router9_client
    from ai.timeouts import turn_deadline
    from core import config, trace
    from core import database as db
    from services import channel_chat_service, concurrency

    async def no_migrations() -> None:
        return None

    _block_network()
    # Pool thật nằm ở core/db/pool.py (core.database chỉ re-export): gán pool giả ở đó.
    from core.db import pool as db_pool

    db_pool._pool = _FakePool()  # type: ignore[assignment]
    db.ensure_migrations = no_migrations  # type: ignore[assignment]
    config.ROUTER9_API_KEY = "benchmark-key"
    config.ROUTER9_BASE_URL = "https://router9.benchmark.invalid/v1"
    config.PROVIDER_ORDER = ["router9"]
    router9_client._pool._client = _fake_provider()
    timings: list[int] = []
    replies: list[str] = []
    for index in range(CHAT_TURNS):
        started = time.perf_counter()
        with trace.turn(f"benchmark:{index}", channel="zalo", user_kind="member"):
            async with concurrency.assistant_turn("zalo:-1"):
                with turn_deadline():
                    reply = await channel_chat_service.handle_channel_text(
                        -1, _CHAT_TEXT, is_admin=False, channel="zalo"
                    )
        timings.append(round((time.perf_counter() - started) * 1000))
        replies.append(" ".join(reply.messages))
        if index == 0:
            result["after_first_turn_mib"] = _status("VmRSS")
    await channel_chat_service.stop_background_tasks()
    await trace.drain()
    result["turn_ms"] = timings
    result["provider_ok"] = all("phỏng vấn" in text for text in replies)
    result["reply_chars"] = len(replies[-1])


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
    if name == "nen_moi":
        asyncio.run(_background_path())
        result["rss_mib"] = _status("VmRSS")
    elif name == "luot_chat":
        asyncio.run(_chat_turns(result))
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


def parse_importtime(stderr: str) -> list[tuple[str, int, int]]:
    """Dòng ``import time: self [us] | cumulative | imported package`` của -X importtime
    -> [(module, self_us, cumulative_us)] (tên bỏ thụt lề lồng nhau)."""
    rows = []
    for line in stderr.splitlines():
        if not line.startswith("import time:"):
            continue
        parts = line.split(":", 1)[1].split("|")
        if len(parts) != 3:
            continue
        try:
            self_us, cumulative_us = int(parts[0]), int(parts[1])
        except ValueError:
            continue  # dòng tiêu đề
        rows.append((parts[2].strip(), self_us, cumulative_us))
    return rows


def importtime_report(top: int = IMPORTTIME_TOP) -> str:
    """``import web`` dưới -X importtime trong tiến trình MỚI: tổng thời gian + top
    module theo thời gian tích luỹ (gồm module con) và thời gian riêng."""
    env = {**os.environ}
    env.setdefault("SETTINGS_ENC_KEY", __import__("base64").urlsafe_b64encode(b"0" * 32).decode())
    env.setdefault("MEMORY_TRIM_AFTER_WORK", "0")
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import web"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        check=False,
    )
    wall = time.perf_counter() - started
    rows = parse_importtime(proc.stderr)
    if proc.returncode != 0 or not rows:
        return json.dumps({"importtime": "error", "stderr": proc.stderr[-400:]}, ensure_ascii=False)
    total_us = sum(row[1] for row in rows)
    lines = [
        json.dumps(
            {"importtime_total_ms": round(total_us / 1000), "wall_ms": round(wall * 1000)},
            ensure_ascii=False,
        ),
        f"Top {top} theo thời gian tích luỹ (ms, gồm module con):",
    ]
    for name, _, cumulative in sorted(rows, key=lambda r: r[2], reverse=True)[:top]:
        lines.append(f"  {cumulative / 1000:8.1f}  {name}")
    lines.append(f"Top {top} theo thời gian riêng (ms):")
    for name, own, _ in sorted(rows, key=lambda r: r[1], reverse=True)[:top]:
        lines.append(f"  {own / 1000:8.1f}  {name}")
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    if len(argv) == 2 and argv[1].startswith("--run="):
        print(json.dumps(run_scenario(argv[1].split("=", 1)[1]), ensure_ascii=False))
        return
    if "--importtime" in argv[1:]:
        print(importtime_report())
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
