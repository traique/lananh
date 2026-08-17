"""Phân tích ngành (Sector Rotation) - port từ src/lib/sector-map.ts + sector-analyzer.ts."""
import asyncio
import time
from dataclasses import dataclass

from stock import providers

# SECTOR_MAP có HAI vai trò, đừng chỉ nghĩ nó là bản đồ ngành:
#   1. Dữ liệu cho phân tích luân chuyển ngành (build_sector_context).
#   2. Nguồn sinh ALL_KNOWN_SYMBOLS - danh sách mã mà stock_analysis coi là
#      "đã biết chắc", không cần gọi DNSE verify. Mã nằm ngoài bản đồ này bị
#      xếp vào nhóm chưa xác minh và phải qua thêm một vòng rào ở tầng nhận
#      diện, nên thiếu ngành không chỉ mất phần bình luận ngành mà còn làm
#      chính việc tra giá kém tin cậy đi.
#
# LƯU Ý khi thêm mã:
#   - _SECTOR_SAMPLE_SIZE chỉ lấy 8 mã ĐẦU của mỗi ngành để tính hiệu suất,
#     nên hãy xếp các mã vốn hoá lớn / tiêu biểu lên trước.
#   - Không thêm mã trùng từ thông dụng đang nằm trong _COMMON_WORD_EXCLUDE /
#     _LOWERCASE_NOISE_EXCLUDE của stock_analysis (vd CEO, SAN, MUA, BAN, HAI)
#     - token đó bị lọc bỏ trước khi tra bản đồ nên sẽ không bao giờ nhận ra.
#     Nếu buộc phải thêm, khai báo nó trong _AMBIGUOUS_KNOWN thay vì ở đây.
#     test/test_sector_map.py canh giúp điều kiện này.
#   - Một mã có thể thuộc nhiều ngành (vd REE ở cả electrical lẫn utilities).
#
# ĐỘ MỊN CỦA NGÀNH là vấn đề đúng/sai, không phải thẩm mỹ: hiệu suất ngành
# được đưa thẳng vào prompt như một luận cứ ("ngành X giảm 11,51%/1 tháng").
# Ngày 05/08/2026, CII (hạ tầng giao thông BOT) và GEX (thiết bị điện) cùng
# nằm trong ngành gộp "Khu công nghiệp & Xây dựng" nên hai báo cáo khác nhau
# nhận CÙNG một con số ngành - một luận cứ sai cho cả hai mã. Vì vậy nhóm này
# được tách thành industrial_park / construction / electrical.
SECTOR_MAP: dict[str, dict] = {
    "banking":    {"label": "Ngân hàng",                 "symbols": ["VCB", "BID", "CTG", "TCB", "MBB", "ACB", "VPB", "HDB", "STB", "EIB", "TPB", "SHB", "VIB", "LPB", "SSB", "MSB", "OCB", "NAB", "BAB", "ABB"]},
    "steel":      {"label": "Thép",                       "symbols": ["HPG", "HSG", "NKG", "TLH", "SMC", "VGS", "TVN"]},
    "realestate": {"label": "Bất động sản",               "symbols": ["VIC", "VHM", "NVL", "KDH", "DXG", "PDR", "NLG", "DIG", "VRE", "KBC", "BCM", "HDC", "IJC", "SCR", "TCH", "AGG", "NTL", "QCG", "LDG", "HQC", "ITA", "TDC"]},
    "oilgas":     {"label": "Dầu khí",                    "symbols": ["GAS", "PLX", "PVD", "PVT", "PVS", "BSR", "OIL", "PLC", "PVC", "PVB", "PGD", "CNG"]},
    "technology": {"label": "Công nghệ",                  "symbols": ["FPT", "CMG", "VGI", "CTR", "ELC", "ITD", "SGT", "FOX"]},
    "securities": {"label": "Chứng khoán",                "symbols": ["SSI", "VCI", "HCM", "VND", "VIX", "SHS", "MBS", "BVS", "FTS", "BSI", "CTS", "AGR", "VDS", "ORS", "DSC"]},
    "retail":     {"label": "Bán lẻ",                     "symbols": ["MWG", "FRT", "PNJ", "DGW", "PET", "HAX"]},
    "food":       {"label": "Thực phẩm & Đồ uống",        "symbols": ["VNM", "SAB", "MSN", "DBC", "HAG", "QNS", "MCH", "KDC", "SBT", "BAF", "HNG", "LSS"]},
    "seafood":    {"label": "Thuỷ sản",                   "symbols": ["VHC", "ANV", "FMC", "IDI", "MPC", "ASM", "ACL", "CMX"]},
    "rubber":     {"label": "Cao su & Săm lốp",           "symbols": ["GVR", "PHR", "DPR", "DRC", "CSM", "DRI", "TRC", "RTB"]},
    "chemicals":  {"label": "Hoá chất & Phân bón",        "symbols": ["DGC", "DPM", "DCM", "CSV", "LAS", "BFC", "DDV"]},
    "insurance":  {"label": "Bảo hiểm",                   "symbols": ["BVH", "BMI", "PVI", "MIG", "PTI", "BIC", "ABI"]},
    "aviation":   {"label": "Hàng không",                 "symbols": ["HVN", "VJC", "ACV", "SCS", "SAS", "AST", "NCT"]},
    "textile":    {"label": "Dệt may",                    "symbols": ["VGT", "TNG", "MSH", "TCM", "STK", "GIL"]},
    "pharma":     {"label": "Dược phẩm & Y tế",           "symbols": ["DHG", "IMP", "DBD", "DVN", "DHT"]},
    "materials":  {"label": "Vật liệu xây dựng",          "symbols": ["VGC", "HT1", "BMP", "NTP", "VCS", "PTB", "BCC", "CVT", "KSB", "DHA"]},
    "industrial_park": {"label": "Khu công nghiệp",       "symbols": ["KBC", "BCM", "IDC", "SIP"]},
    "construction": {"label": "Xây dựng & Hạ tầng",       "symbols": ["CTD", "VCG", "HHV", "CII", "LCG", "FCN", "TCD"]},
    "electrical": {"label": "Thiết bị điện & Công nghiệp", "symbols": ["GEX", "REE", "PC1"]},
    "utilities":  {"label": "Điện & Tiện ích",            "symbols": ["POW", "REE", "GAS", "PLC", "NT2", "PC1", "GEG", "VSH", "QTP", "HDG"]},
    "logistics":  {"label": "Vận tải & Logistics",        "symbols": ["GMD", "PVT", "ACV", "VJC", "VTP", "HAH", "VSC", "VOS", "TMS", "PHP"]},
}

ALL_KNOWN_SYMBOLS: set[str] = {s for meta in SECTOR_MAP.values() for s in meta["symbols"]}


def get_symbol_sectors(symbol: str) -> list[str]:
    s = symbol.strip().upper()
    return [key for key, meta in SECTOR_MAP.items() if s in meta["symbols"]]


def get_primary_sector_label(symbol: str) -> str:
    keys = get_symbol_sectors(symbol)
    return SECTOR_MAP[keys[0]]["label"] if keys else "Khác"


@dataclass
class SectorPerformance:
    key: str
    label: str
    trend_1m: float
    trend_3m: float
    vs_vnindex_1m: float
    momentum: str  # hot | warm | cold | dump
    top_movers: list[str]


def _trend_pct(closes: list[float], lookback: int) -> float | None:
    if len(closes) < lookback + 1:
        return None
    past = closes[-1 - min(lookback, len(closes) - 1)]
    curr = closes[-1]
    return round(((curr - past) / past) * 100, 2) if past > 0 else None


_SECTOR_SAMPLE_SIZE = 8


async def _analyze_sector(key: str, meta: dict, vnindex_1m: float) -> SectorPerformance | None:
    sample = meta["symbols"][:_SECTOR_SAMPLE_SIZE]
    results = await asyncio.gather(*[providers.fetch_ohlcv(sym, days=90) for sym in sample], return_exceptions=True)

    valid = []
    for sym, series in zip(sample, results):
        if isinstance(series, Exception) or not series.closes:
            continue
        t1m = _trend_pct(series.closes, 22)
        t3m = _trend_pct(series.closes, 65)
        if t1m is not None:
            valid.append((sym, t1m, t3m if t3m is not None else 0.0))

    if not valid:
        return None

    avg_1m = sum(v[1] for v in valid) / len(valid)
    avg_3m = sum(v[2] for v in valid) / len(valid)
    vs_vnindex = round(avg_1m - vnindex_1m, 2)

    if avg_1m > 5:
        momentum = "hot"
    elif avg_1m > 1:
        momentum = "warm"
    elif avg_1m > -3:
        momentum = "cold"
    else:
        momentum = "dump"

    # top_movers sort theo |t1m| nên có thể gồm cả mã GIẢM mạnh nhất, không
    # chỉ mã tiêu biểu tăng - label ở build_sector_prompt_section phản ánh
    # đúng "biến động mạnh nhất" thay vì ngụ ý toàn mã tăng tốt.
    top_movers = [v[0] for v in sorted(valid, key=lambda x: abs(x[1]), reverse=True)[:2]]

    return SectorPerformance(key, meta["label"], round(avg_1m, 2), round(avg_3m, 2), vs_vnindex, momentum, top_movers)


@dataclass
class SectorContext:
    sectors: list[SectorPerformance]
    strong_sectors: list[str]
    risky_sectors: list[str]
    rotation_signal: str


_SECTOR_CACHE_TTL = 180  # giây
_sector_cache: dict[tuple, tuple[float, "SectorContext | None"]] = {}


async def build_sector_context(sector_keys: list[str]) -> SectorContext | None:
    if not sector_keys:
        return None
    cache_key = tuple(sorted(sector_keys))
    cached = _sector_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _SECTOR_CACHE_TTL:
        return cached[1]
    ctx = await _build_sector_context_uncached(sector_keys)
    _sector_cache[cache_key] = (time.monotonic(), ctx)
    return ctx


async def _build_sector_context_uncached(sector_keys: list[str]) -> SectorContext | None:
    vn_series = await providers.fetch_ohlcv("VNINDEX", days=90)
    vnindex_1m = _trend_pct(vn_series.closes, 22) if vn_series.closes else None
    vnindex_1m = vnindex_1m if vnindex_1m is not None else 0.0

    results = await asyncio.gather(
        *[_analyze_sector(key, SECTOR_MAP[key], vnindex_1m) for key in sector_keys if key in SECTOR_MAP]
    )
    sectors = [s for s in results if s is not None]
    if not sectors:
        return None

    strong = [s.label for s in sectors if s.momentum == "hot" or s.vs_vnindex_1m > 3]
    risky = [s.label for s in sectors if s.momentum == "dump" or s.vs_vnindex_1m < -3]

    if strong:
        rotation = f"Dòng tiền đang vào: {', '.join(strong)}"
    elif risky:
        rotation = f"Dòng tiền đang rút khỏi: {', '.join(risky)}"
    else:
        rotation = "Dòng tiền chưa rõ xu hướng luân chuyển ngành rõ rệt"

    return SectorContext(sectors, strong, risky, rotation)


def build_sector_prompt_section(ctx: SectorContext | None, symbol: str) -> str:
    if not ctx:
        return ""
    sectors_of_symbol = get_symbol_sectors(symbol)
    if not sectors_of_symbol:
        return ""
    lines = [f"[NGÀNH — {symbol}]"]
    for key in sectors_of_symbol:
        sp = next((s for s in ctx.sectors if s.key == key), None)
        if not sp:
            continue
        emoji = {"hot": "🔥", "warm": "🟢", "cold": "🟡", "dump": "🔴"}[sp.momentum]
        lines.append(
            f"{emoji} Ngành {sp.label}: {'+' if sp.trend_1m > 0 else ''}{sp.trend_1m}% (1M), "
            f"{'outperform' if sp.vs_vnindex_1m > 0 else 'underperform'} VNINDEX {'+' if sp.vs_vnindex_1m > 0 else ''}{sp.vs_vnindex_1m}%. "
            f"Biến động mạnh nhất: {', '.join(sp.top_movers)}."
        )
    lines.append(f"Tín hiệu luân chuyển: {ctx.rotation_signal}")
    return "\n".join(lines)
