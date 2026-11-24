"""Danh sách mã/sàn, khối ngoại và lịch sự kiện lấy thẳng REST VCI - thay vnstock
(bản 4.x bị khoá từ 10/11/2026, kho vnstocks.com trả 410)."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import analysis as stock_analysis  # noqa: E402
from stock import fundamentals, providers, vci_direct  # noqa: E402

# Dạng dòng GET trading.vietcap.com.vn/api/price/symbols/getAll (16/11/2026).
_LISTING = [
    {"symbol": "FPT", "type": "STOCK", "board": "HSX"},
    {"symbol": "SHS", "type": "STOCK", "board": "HNX"},
    {"symbol": "ABI", "type": "STOCK", "board": "UPCOM"},
    {"symbol": "CFPT2401", "type": "CW", "board": "HSX"},
    {"symbol": "E1VFVN30", "type": "ETF", "board": "HSX"},
    {"symbol": "OLD", "type": "STOCK", "board": "DELISTED"},
    {"symbol": "VBB12345", "type": "BOND", "board": "BOND"},
    {"symbol": "bad sym!", "type": "STOCK", "board": "HSX"},
    {"symbol": None, "type": "STOCK", "board": "HSX"},
]


def test_san_va_danh_sach_ma_tu_vci():
    assert providers.exchange_map_from_listing(_LISTING) == {
        "FPT": "HOSE",
        "SHS": "HNX",
        "ABI": "UPCOM",
        "CFPT2401": "HOSE",
        "E1VFVN30": "HOSE",
    }
    # Universe cho backtest/huấn luyện: chỉ cổ phiếu đang niêm yết.
    assert providers.stock_symbols_from_listing(_LISTING) == ["ABI", "FPT", "SHS"]


@pytest.mark.asyncio
async def test_fetch_symbol_exchange_va_universe_qua_vci(monkeypatch):
    monkeypatch.setattr(vci_direct, "fetch_listing", lambda: _LISTING)
    monkeypatch.setattr(providers, "_exchange_map_cache", None)
    assert await providers.fetch_symbol_exchange("shs") == "HNX"
    assert await providers.fetch_symbol_universe() == ["ABI", "FPT", "SHS"]

    def down():
        raise RuntimeError("VCI lỗi")

    from stock.sector import ALL_KNOWN_SYMBOLS

    monkeypatch.setattr(vci_direct, "fetch_listing", down)
    assert await providers.fetch_symbol_universe() == sorted(ALL_KNOWN_SYMBOLS)


def test_khoi_ngoai_tu_bang_gia_vci():
    room = {"currentRoom": 425534401, "totalRoom": 924006547}
    flow = fundamentals.foreign_flow_from_match(
        {"foreignBuyVolume": 1200, "foreignSellVolume": 500, **room}
    )
    assert flow is not None
    assert (flow.foreign_buy_vol, flow.foreign_sell_vol, flow.foreign_net_vol) == (1200, 500, 700)
    assert flow.foreign_room_pct == 46.05  # room còn lại / room tối đa
    # Trước giờ mở cửa VCI trả 0 cả mua lẫn bán: không báo "mua 0, bán 0".
    early = fundamentals.foreign_flow_from_match(
        {"foreignBuyVolume": 0, "foreignSellVolume": 0, **room}
    )
    assert early is not None and early.foreign_buy_vol is None and early.foreign_net_vol is None
    assert early.foreign_room_pct == 46.05
    assert fundamentals.foreign_flow_from_match({}) is None
    assert fundamentals.foreign_flow_from_match({"currentRoom": 5, "totalRoom": 0}) is None


def test_lich_su_kien_phan_tich_qua_vci_direct(monkeypatch):
    ev = vci_direct.UpcomingEvent
    events = [
        ev("FPT", "AGME", "Họp ĐHCĐ bất thường", "", "", "2026-12-20", None),
        ev("FPT", "DIV", "Cổ tức tiền 10%", "2026-11-25", "2026-11-26", "", 1000.0),
        ev("FPT", "ISS", "", "2026-12-01", "", "", None),  # không tiêu đề: bỏ
    ]
    monkeypatch.setattr(vci_direct, "fetch_upcoming_events", lambda symbol: events)
    out = fundamentals._fetch_events_sync("FPT")
    assert out is not None
    assert [(e.title, e.date) for e in out] == [
        ("Cổ tức tiền 10%", "2026-11-25"),
        ("Họp ĐHCĐ bất thường", "2026-12-20"),
    ]
    monkeypatch.setattr(vci_direct, "fetch_upcoming_events", lambda symbol: [])
    assert fundamentals._fetch_events_sync("FPT") is None


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_goi_rest_bang_gia_va_danh_sach_ma(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append((url, json, headers.get("Origin")))
        return _Response([{"listingInfo": {"symbol": "FPT"}, "matchPrice": {"currentRoom": 5}}])

    def fake_get(url, headers, timeout):
        calls.append((url, None, headers.get("Origin")))
        return _Response([*_LISTING[:2], "rác"])

    monkeypatch.setattr(vci_direct.requests, "post", fake_post)
    monkeypatch.setattr(vci_direct.requests, "get", fake_get)
    assert vci_direct.fetch_match_price(" fpt ") == {"currentRoom": 5}
    assert vci_direct.fetch_listing() == _LISTING[:2]
    assert calls[0][0].endswith("/symbols/getList") and calls[0][1] == {"symbols": ["FPT"]}
    assert calls[1][0].endswith("/symbols/getAll")
    assert all(origin == "https://trading.vietcap.com.vn" for _, _, origin in calls)

    monkeypatch.setattr(vci_direct.requests, "post", lambda *a, **k: _Response([]))
    assert vci_direct.fetch_match_price("FPT") == {}


@pytest.mark.asyncio
async def test_xac_minh_ma_bang_danh_sach_niem_yet(monkeypatch):
    """Mã ngoài SECTOR_MAP (phần lớn HNX/UPCOM) từng chỉ được xác minh bằng 1 lần gọi
    giá DNSE: lỗi tạm thời bị nhớ 10 phút là "không tồn tại" - lúc tra được lúc không.
    Nay danh sách niêm yết (cache 24h) xác nhận trước, giá chỉ còn là dự phòng."""
    listing = [
        {"symbol": "HUT", "type": "STOCK", "board": "HNX"},
        {"symbol": "L14", "type": "STOCK", "board": "HNX"},
        {"symbol": "VC3", "type": "STOCK", "board": "HNX"},
    ]
    fetches, priced = [], []

    def fake_listing():
        fetches.append(1)
        return listing

    async def fake_price(symbol):
        priced.append(symbol)
        return 0.0

    monkeypatch.setattr(vci_direct, "fetch_listing", fake_listing)
    monkeypatch.setattr(providers, "fetch_current_price", fake_price)
    monkeypatch.setattr(providers, "_exchange_map_cache", None)
    monkeypatch.setattr(providers, "_exchange_map_lock", None)
    monkeypatch.setattr(providers, "_verify_cache", {})
    checks = await asyncio.gather(
        *(providers.verify_symbol_exists(s) for s in ("hut", "L14", "S24", "XYZ"))
    )
    assert checks == [True, True, False, False]
    assert fetches == [1]  # 4 lượt cùng lúc chỉ tải danh sách 1 lần
    assert priced == ["XYZ"]  # S24 có số mà không niêm yết: không tốn lượt gọi giá
    assert await stock_analysis.find_valid_symbols("phân tích L14 và VC3") == ["L14", "VC3"]
    assert await stock_analysis.find_valid_symbols("giá hut") == ["HUT"]
    assert await stock_analysis.find_valid_symbols("Samsung S24 có tốt không") == []
    assert priced == ["XYZ"]


@pytest.mark.asyncio
async def test_danh_sach_loi_thi_xac_minh_bang_gia_nhu_cu(monkeypatch):
    def down():
        raise RuntimeError("VCI lỗi")

    async def fake_price(symbol):
        return 15_000.0 if symbol in {"HUT", "L14"} else 0.0

    monkeypatch.setattr(vci_direct, "fetch_listing", down)
    monkeypatch.setattr(providers, "fetch_current_price", fake_price)
    monkeypatch.setattr(providers, "_exchange_map_cache", None)
    monkeypatch.setattr(providers, "_verify_cache", {})
    assert await providers.verify_symbol_exists("HUT") is True
    assert await providers.verify_symbol_exists("L14") is True
    assert await providers.verify_symbol_exists("ABC") is False


@pytest.mark.asyncio
async def test_ma_moi_len_san_tai_lai_danh_sach_toi_da_1_lan_moi_gio(monkeypatch):
    """Danh sách cache 24h có thể chưa có mã vừa lên sàn: tra trượt thì tải lại, nhưng tối
    đa 1 lần mỗi giờ (tên sản phẩm như S24 không làm tải liên tục); tải lỗi giữ bản cũ."""
    old = [{"symbol": "HUT", "type": "STOCK", "board": "HNX"}]
    new = old + [{"symbol": "F88", "type": "STOCK", "board": "UPCOM"}]
    listings = [old, new]
    fetches = []

    def fake_listing():
        fetches.append(1)
        if not listings:
            raise RuntimeError("VCI lỗi")
        return listings.pop(0)

    async def no_price(symbol):
        raise AssertionError(f"không được gọi giá cho {symbol}")

    hour = providers._EXCHANGE_MAP_MISS_REFRESH_SEC
    monkeypatch.setattr(vci_direct, "fetch_listing", fake_listing)
    monkeypatch.setattr(providers, "fetch_current_price", no_price)
    monkeypatch.setattr(providers, "_exchange_map_cache", None)
    monkeypatch.setattr(providers, "_exchange_map_lock", None)
    monkeypatch.setattr(providers, "_exchange_map_miss_refresh_at", None)
    monkeypatch.setattr(providers, "_verify_cache", {})

    assert await providers.verify_symbol_exists("HUT") is True
    assert await providers.verify_symbol_exists("F88") is False  # danh sách mới tải
    assert len(fetches) == 1

    def age_listing(seconds):
        ts, mapping = providers._exchange_map_cache
        providers._exchange_map_cache = (ts - seconds, mapping)
        providers._verify_cache.clear()

    age_listing(hour + 1)
    checks = await asyncio.gather(
        providers.verify_symbol_exists("F88"), providers.verify_symbol_exists("S24")
    )
    assert checks == [True, False] and len(fetches) == 2  # 2 lượt trượt chỉ tải lại 1 lần
    assert await providers.fetch_symbol_exchange("F88") == "UPCOM"

    # Tải lại lỗi: giữ bản đang có, 1 giờ sau mới thử lại.
    age_listing(hour + 1)
    providers._exchange_map_miss_refresh_at -= hour + 1
    assert await providers.verify_symbol_exists("S24") is False
    assert len(fetches) == 3
    assert await providers.verify_symbol_exists("F88") is True
    assert await providers.verify_symbol_exists("VF8") is False
    assert len(fetches) == 3
