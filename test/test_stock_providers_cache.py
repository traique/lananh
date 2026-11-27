"""Cache/gộp request của stock/providers.py: OHLCV theo mã (chuỗi dài nhất, cắt
cho yêu cầu ngắn hơn), gộp lượt gọi đang bay, thử lại DNSE 1 lần khi lỗi thoáng
qua trước khi rơi sang VCI, cache tin Google News 10 phút. HTTP giả lập bằng
httpx.MockTransport - không mạng thật."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from stock import providers

_BASE_TS = int(datetime(2026, 1, 5, 2, tzinfo=timezone.utc).timestamp())


def _dnse_payload(count: int) -> dict:
    ts = [_BASE_TS + i * 86400 for i in range(count)]
    closes = [20.0 + i * 0.01 for i in range(count)]
    return {
        "t": ts,
        "h": [c + 0.2 for c in closes],
        "l": [c - 0.2 for c in closes],
        "c": closes,
        "v": [1_000_000 + i for i in range(count)],
    }


def _vci_payload(count: int) -> list:
    return [
        {
            "t": [str(_BASE_TS + i * 86400) for i in range(count)],
            "h": [30_500] * count,
            "l": [29_500] * count,
            "c": [30_000 + i for i in range(count)],
            "v": [1_000] * count,
        }
    ]


class _Recorder:
    def __init__(self, dnse_statuses=(), dnse_errors=(), delay=0.0):
        self.calls: list[str] = []
        self.dnse_statuses = list(dnse_statuses)
        self.dnse_errors = list(dnse_errors)
        self.delay = delay

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append(host)
        if self.delay:
            await asyncio.sleep(self.delay)
        if "entrade" in host:
            if self.dnse_errors:
                raise self.dnse_errors.pop(0)
            if self.dnse_statuses:
                return httpx.Response(self.dnse_statuses.pop(0))
            days = (int(request.url.params["to"]) - int(request.url.params["from"])) // 86400
            return httpx.Response(200, json=_dnse_payload(min(days, 400)))
        if "vietcap" in host:
            body = json.loads(request.content)
            return httpx.Response(200, json=_vci_payload(body["countBack"]))
        if "news.google" in host:
            return httpx.Response(200, text=_RSS)
        return httpx.Response(404)

    def count(self, fragment: str) -> int:
        return sum(1 for host in self.calls if fragment in host)


@pytest.fixture
def http(monkeypatch):
    """Cài client httpx giả cho providers (MockTransport, không socket) + cache sạch."""

    def install(recorder: _Recorder) -> _Recorder:
        client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
        monkeypatch.setattr(providers, "_client", client)
        return recorder

    monkeypatch.setattr(providers, "_ohlcv_cache", {})
    monkeypatch.setattr(providers, "_ohlcv_inflight", {})
    monkeypatch.setattr(providers, "_news_cache", {})
    monkeypatch.setattr(providers, "_DNSE_RETRY_DELAY_SEC", 0)
    return install


@pytest.mark.asyncio
async def test_cung_ma_260_90_5_phien_chi_mot_luot_dnse(http):
    rec = http(_Recorder())
    long = await providers.fetch_ohlcv("FPT", days=260)
    mid = await providers.fetch_ohlcv("fpt", days=90)
    short = await providers.fetch_ohlcv("FPT", days=5)
    assert rec.count("entrade") == 1
    assert len(long.closes) == 260 and long.source == "dnse"
    assert mid.closes == long.closes[-90:] and mid.dates == long.dates[-90:]
    assert short.volumes == long.volumes[-5:] and short.highs == long.highs[-5:]
    # Cắt đuôi tạo object mới, không đụng chuỗi đang nằm trong cache.
    assert len(providers._ohlcv_cache["FPT"][2].closes) == 260


@pytest.mark.asyncio
async def test_chuoi_ngan_truoc_thi_chuoi_dai_sau_van_lay_moi_roi_dung_chung(http):
    rec = http(_Recorder())
    await providers.fetch_ohlcv("HPG", days=5)
    await providers.fetch_ohlcv("HPG", days=260)
    await providers.fetch_ohlcv("HPG", days=90)
    await providers.fetch_ohlcv("HPG", days=5)
    assert rec.count("entrade") == 2
    assert providers._ohlcv_cache["HPG"][1] == 260  # giữ chuỗi dài nhất


@pytest.mark.asyncio
async def test_cache_het_han_thi_lay_lai(http, monkeypatch):
    rec = http(_Recorder())
    await providers.fetch_ohlcv("VNM", days=90)
    ts, days, series = providers._ohlcv_cache["VNM"]
    providers._ohlcv_cache["VNM"] = (ts - providers._OHLCV_CACHE_TTL - 1, days, series)
    await providers.fetch_ohlcv("VNM", days=90)
    assert rec.count("entrade") == 2


@pytest.mark.asyncio
async def test_goi_cung_luc_gop_vao_mot_request_dang_bay(http):
    rec = http(_Recorder(delay=0.05))
    results = await asyncio.gather(
        providers.fetch_ohlcv("VNINDEX", days=260),
        providers.fetch_ohlcv("VNINDEX", days=260),
        providers.fetch_ohlcv("VNINDEX", days=90),
        providers.fetch_ohlcv("VNINDEX", days=5),
    )
    assert rec.count("entrade") == 1
    assert [len(r.closes) for r in results] == [260, 260, 90, 5]
    assert results[2].closes == results[0].closes[-90:]
    assert providers._ohlcv_inflight == {}


@pytest.mark.asyncio
async def test_request_dang_bay_loi_thi_moi_nguoi_cho_deu_nhan_loi_va_don_sach(http, monkeypatch):
    started = asyncio.Event()

    async def boom(sym, days):
        started.set()
        await asyncio.sleep(0.02)
        raise RuntimeError("hỏng")

    monkeypatch.setattr(providers, "_fetch_ohlcv_fresh", boom)
    results = await asyncio.gather(
        providers.fetch_ohlcv("SSI", days=90),
        providers.fetch_ohlcv("SSI", days=90),
        return_exceptions=True,
    )
    assert started.is_set()
    assert all(isinstance(r, RuntimeError) for r in results)
    assert providers._ohlcv_inflight == {}


@pytest.mark.asyncio
async def test_nguoi_cho_bi_huy_khong_huy_request_cua_nguoi_khac(http):
    rec = http(_Recorder(delay=0.05))
    first = asyncio.ensure_future(providers.fetch_ohlcv("MWG", days=90))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(providers.fetch_ohlcv("MWG", days=90))
    await asyncio.sleep(0.01)
    first.cancel()
    series = await second
    assert len(series.closes) == 90 and rec.count("entrade") == 1
    with pytest.raises(asyncio.CancelledError):
        await first


@pytest.mark.asyncio
async def test_dnse_loi_thoang_qua_thu_lai_mot_lan_truoc_khi_sang_vci(http):
    rec = http(_Recorder(dnse_statuses=[503]))
    series = await providers.fetch_ohlcv("ACB", days=30)
    assert series.source == "dnse" and rec.count("entrade") == 2
    assert rec.count("vietcap") == 0

    rec = http(_Recorder(dnse_errors=[httpx.ConnectError("reset")]))
    providers._ohlcv_cache.clear()
    series = await providers.fetch_ohlcv("ACB", days=30)
    assert series.source == "dnse" and rec.count("entrade") == 2


@pytest.mark.asyncio
async def test_dnse_loi_hai_lan_hoac_qua_gio_thi_sang_vci(http):
    rec = http(_Recorder(dnse_statuses=[502, 502]))
    series = await providers.fetch_ohlcv("TCB", days=30)
    assert series.source == "vci" and rec.count("entrade") == 2

    # Quá giờ: đã tốn trọn timeout -> sang VCI ngay, không thử lại DNSE.
    rec = http(_Recorder(dnse_errors=[httpx.ReadTimeout("chậm")]))
    providers._ohlcv_cache.clear()
    series = await providers.fetch_ohlcv("TCB", days=30)
    assert series.source == "vci" and rec.count("entrade") == 1

    # 404 (mã không có trên DNSE) không phải lỗi thoáng qua.
    rec = http(_Recorder(dnse_statuses=[404]))
    providers._ohlcv_cache.clear()
    await providers.fetch_ohlcv("TCB", days=30)
    assert rec.count("entrade") == 1


_RSS = """<rss><channel>
<item><title>FPT lãi kỷ lục quý 3 - CafeF</title><source>CafeF</source>
<pubDate>{date}</pubDate><link>https://example.vn/1</link></item>
</channel></rss>""".format(
    date=(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
)


@pytest.mark.asyncio
async def test_tin_google_news_cache_10_phut_theo_ma(http):
    rec = http(_Recorder())
    first = await providers.fetch_news("FPT")
    again = await providers.fetch_news("fpt")
    assert rec.count("news.google") == 1
    assert [n.title for n in first] == [n.title for n in again] == ["FPT lãi kỷ lục quý 3"]
    again.clear()  # caller sửa list trả về không làm hỏng cache
    assert len(await providers.fetch_news("FPT")) == 1
    ts, items = providers._news_cache["FPT"]
    providers._news_cache["FPT"] = (ts - providers._NEWS_CACHE_TTL - 1, items)
    await providers.fetch_news("FPT")
    assert rec.count("news.google") == 2


@pytest.mark.asyncio
async def test_tin_loi_http_khong_bi_cache(http):
    class _Down(_Recorder):
        async def __call__(self, request):
            self.calls.append(request.url.host)
            return httpx.Response(503)

    rec = http(_Down())
    assert await providers.fetch_news("HPG") == []
    assert await providers.fetch_news("HPG") == []
    assert rec.count("news.google") == 2 and providers._news_cache == {}


def test_cache_tin_co_tran():
    cache = {str(i): (0.0 + i, []) for i in range(100)}
    providers._evict_expired(cache, 10**9, providers._NEWS_CACHE_MAX)
    assert len(cache) < providers._NEWS_CACHE_MAX
