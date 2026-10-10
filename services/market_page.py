"""Đăng tự động lên Facebook Page riêng (key MARKET), tách khỏi luồng Shopee.

Chuyển từ 2 workflow n8n:
- ``stock``: nến DNSE -> chỉ báo -> AI nhận định -> ảnh biểu đồ VN-INDEX + caption.
  Mặc định 15:20 (kết phiên), thứ Hai-thứ Sáu.
- ``news``: RSS CafeF -> AI tổng hợp thành bài đăng (kèm ảnh bài nổi bật nếu có),
  rồi comment bản viết lại của chính bài nổi bật đó. Mặc định 08:30 hằng ngày.

Giờ đăng đổi qua MARKET_STOCK_TIMES_VN / MARKET_NEWS_TIMES_VN. ``run_manual`` chạy
ngoài lịch cho lệnh /fb_market và trang admin (mặc định chỉ xem thử, không đăng).

Chống đăng sai/trùng: mỗi phiên giao dịch chỉ đăng 1 lần (kể cả đăng tay), ngày nghỉ
lễ không đăng lại phiên cũ, bản tin chỉ dùng tin 24 giờ qua và bỏ nếu không có tin mới.
Lịch chạy bù sau khi restart và thử lại khi AI/DNSE lỗi (MARKET_CATCHUP_MIN,
MARKET_RETRY_MIN, MARKET_MAX_ATTEMPTS). Ảnh đăng kèm có khung + logo như luồng Zalo.

Page cấu hình bằng FACEBOOK_PAGE_ID_MARKET / FACEBOOK_PAGE_ACCESS_TOKEN_MARKET và
không nằm trong configured_page_keys() mặc định nên /fb_ok không đăng lên đây.
"""
import asyncio
import json
import logging
import math
import os
import re
import statistics
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
import httpx
from bs4 import BeautifulSoup

from services import http_client

from ai import orchestrator
from core import config
from core import database as db
from services import web_reader
from services.facebook_image import brand_image
from services.facebook_page_service import (
    MARKET_PAGE_KEY,
    FacebookPublicationUncertain,
    configured_page_keys,
    post_comment,
    publish_page_post,
)

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_task: asyncio.Task | None = None

_DEFAULT_NEWS_TIMES = (time(8, 30),)
_DEFAULT_STOCK_TIMES = (time(15, 20),)


class MarketPageError(RuntimeError):
    """Lỗi vận hành báo được cho người gọi (page chưa cấu hình, đang có job chạy)."""


class MarketSkip(Exception):
    """Job cố ý không đăng (đã đăng phiên này, không có tin mới, chưa có dữ liệu...).

    ``retry=True``: có thể có dữ liệu sau ít phút (DNSE chưa cập nhật phiên hôm nay)
    nên lịch sẽ thử lại trong khung chạy bù; ``retry=False``: xong lượt này luôn.
    """

    def __init__(self, reason: str, *, retry: bool = False):
        super().__init__(reason)
        self.retry = retry


def _parse_times(raw: str, default: tuple[time, ...]) -> tuple[time, ...]:
    try:
        parsed = tuple(
            datetime.strptime(part.strip(), "%H:%M").time()
            for part in raw.split(",")
            if part.strip()
        )
    except ValueError:
        logger.warning("market_page: giờ đăng '%s' sai định dạng HH:MM, dùng mặc định.", raw)
        return default
    return parsed or default


def _schedule() -> tuple[tuple[str, time, range], ...]:
    """(job, giờ VN, các thứ chạy; Monday=0). Ngày nghỉ lễ trong tuần: job stock tự
    bỏ qua vì DNSE không có phiên hôm nay (xem _is_current_session)."""
    news = _parse_times(config.MARKET_NEWS_TIMES_VN, _DEFAULT_NEWS_TIMES)
    stock = _parse_times(config.MARKET_STOCK_TIMES_VN, _DEFAULT_STOCK_TIMES)
    return tuple(("news", at, range(7)) for at in news) + tuple(
        ("stock", at, range(5)) for at in stock
    )


# Một job chạy tại một thời điểm: tránh lệnh thủ công đè lên lượt theo lịch.
_run_lock = asyncio.Lock()

_MIN_POST_CHARS = 80
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ─── Tuân thủ: không khuyến nghị đầu tư, luôn dẫn nguồn + miễn trừ trách nhiệm ──────
# Phần nguồn/miễn trừ do code tự gắn (không nhờ AI) để bài nào cũng có, không phụ thuộc
# việc AI có nhớ viết hay không.

_DISCLAIMER = (
    "⚠️ Nội dung chỉ mang tính tham khảo, không phải khuyến nghị đầu tư. "
    "Nhà đầu tư tự chịu trách nhiệm với quyết định của mình."
)
_DISCLAIMER_SHORT = (
    "⚠️ Nội dung biên tập lại từ bài gốc của CafeF, chỉ mang tính tham khảo, "
    "không phải khuyến nghị đầu tư."
)
_STYLE_RULES = """PHONG CÁCH VIẾT:
- Văn phong báo chí tài chính: câu ngắn, chủ động, đi thẳng vào sự việc; mỗi ý đi kèm số liệu hoặc sự kiện cụ thể.
- Tránh giọng AI và giọng quảng cáo: không dùng các cụm sáo rỗng như "đáng chú ý", "cho thấy rằng", "phản ánh", "hàm ý", "bức tranh", "sôi động", "bùng nổ", "đột biến", "trong bối cảnh", "nhìn chung", "có thể nói"; không câu hỏi tu từ, không lời chào hay kêu gọi người đọc; không viết hoa toàn bộ câu; không lạm dụng emoji.
- Không rào đón dài dòng: chỉ dè dặt một lần khi thật sự cần, tuyệt đối không viết kiểu "chưa đủ cơ sở để kết luận" hay "chưa thể khẳng định hoàn toàn".
- Không nhắc tới việc mình được cung cấp dữ liệu: cấm viết "theo dữ liệu được cung cấp", "các tin được cung cấp", "trong phạm vi dữ liệu". Thiếu thông tin thì bỏ chi tiết đó, không thông báo là thiếu.
- Thay đổi độ dài câu và đoạn, không lặp một mẫu mở đầu ở nhiều đoạn.
- Số liệu theo kiểu Việt Nam (dấu chấm ngăn hàng nghìn, dấu phẩy thập phân), làm tròn gọn."""
# Cụm từ khuyến nghị giao dịch. Bắt câu có ý "khuyên làm gì" (nên/có thể/hãy/canh/ưu tiên
# + mua/bán/giải ngân/chốt lời...), không bắt mô tả thị trường: "khối ngoại bán ròng",
# "áp lực chốt lời", "lực mua vào cuối phiên", "ngân hàng đẩy mạnh giải ngân tín dụng".
_ADVICE_ACTION = (
    r"(mua|bán|nắm giữ|giữ|gom|tích lũy|tích luỹ|giải ngân|chốt lời|cắt lỗ|cơ cấu|"
    r"(gia tăng|tăng|giảm|hạ|nâng)\s+tỷ trọng|đứng ngoài|bắt đáy|lướt sóng|trading)"
)
_ADVICE_RE = re.compile(
    r"(nên|khuyến nghị|khuyến cáo|đề xuất|gợi ý|hãy|có thể|cân nhắc|ưu tiên|canh|chờ|"
    r"tranh thủ|thận trọng)\s+(cân nhắc\s+|tiếp tục\s+|từng bước\s+)?"
    + _ADVICE_ACTION
    + r"(?!\s+(ròng|tháo))"
    + r"|giá mục tiêu|tỷ trọng\s+(cổ phiếu|tiền mặt)|(hạ|giảm|tăng|nâng)\s+tỷ trọng"
    r"|vùng\s+(mua|bán|gom|giải ngân)|điểm\s+(mua|bán)|chốt lời\s+(một phần|từng phần)"
    r"|(mua|bán)\s+(thăm dò|trading|lướt)",
    re.IGNORECASE,
)
_ADVICE_RETRY_NOTE = (
    "\n\nLƯU Ý: bản trước bị loại vì có câu mang tính khuyến nghị giao dịch "
    "(ví dụ \"nên/có thể/canh mua\", \"ưu tiên giải ngân\", \"chốt lời một phần\", "
    "\"vùng mua\"). Viết lại, chỉ mô tả và phân tích, không có hành động nào cho người đọc."
)


def _has_investment_advice(text: str) -> bool:
    return _ADVICE_RE.search(text) is not None


def _vn_date(iso_date: str) -> str:
    return date.fromisoformat(iso_date).strftime("%d/%m/%Y")


# Dấu trích dẫn kiểu "[3]", "[11][15]", "[2, 4]", "[1-3]", "【5】" mà model có tra web
# hay tự thêm. Bài Facebook đọc tự nhiên, nguồn đã ghi ở cuối bài.
_CITATION_RE = re.compile(r"\s*(?:\[\s*\d+(?:\s*[,;\-–]\s*\d+)*\s*\]|【[^】]*】)+")


def _strip_citations(text: str) -> str:
    text = _CITATION_RE.sub("", text or "")
    return re.sub(r"[ \t]+([.,;:])", r"\1", text)


def _vn_number(value: float, digits: int = 1) -> str:
    """Kiểu Việt Nam: chấm ngăn nghìn, phẩy thập phân (1234.5 -> "1.234,5")."""
    return f"{value:,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _today() -> date:
    return datetime.now(_VN_TZ).date()


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


async def _ask_clean(prompt: str, clean=lambda text: text) -> str | None:
    """Hỏi AI; nếu bài có câu khuyến nghị thì hỏi lại 1 lần kèm cảnh báo, vẫn có thì bỏ."""
    for attempt in range(2):
        response = await orchestrator.ask(prompt if attempt == 0 else prompt + _ADVICE_RETRY_NOTE)
        text = clean(getattr(response, "text", None) or "")
        if not _has_investment_advice(text):
            return text
        logger.warning("market_page: bài AI có câu khuyến nghị (lần %d).", attempt + 1)
    return None


# ─── Báo cáo chứng khoán ────────────────────────────────────────────────────

_DNSE_URL = "https://services.entrade.com.vn/chart-api/v2/ohlcs/{kind}"
_QUICKCHART_URL = "https://quickchart.io/chart"
_INDEX_SYMBOLS = frozenset({"VNINDEX", "VN30", "HNXINDEX", "HNX30", "UPCOMINDEX", "UPINDEX"})
# Chỉ tải mã thực sự đi vào bài: VNINDEX + cổ phiếu. HNXINDEX/UPCOMINDEX từng nằm trong
# danh sách n8n nhưng DNSE trả 400 và báo cáo không dùng tới chúng.
_REPORT_SYMBOLS = (
    "VNINDEX", "VN30",
    "VIC", "VHM", "VRE", "FPT", "MWG", "HPG",
    "VCB", "BID", "CTG", "MBB", "TCB",
    "SSI", "VND", "HCM", "VCI", "GVR", "IJC",
)
_HISTORY_DAYS = 180
_BARS_KEPT = 120
_CHART_BARS = 30
_DNSE_CONCURRENCY = 5


def _num(values: list, index: int) -> float | None:
    try:
        value = float(values[index])
    except (IndexError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse_bars(symbol: str, body: dict) -> list[dict]:
    """DNSE trả giá cổ phiếu theo nghìn đồng, chỉ số theo điểm."""
    scale = 1 if symbol in _INDEX_SYMBOLS else 1000
    t, o, h, l, c, v = (body.get(key) or [] for key in "tohlcv")
    bars = []
    for i in range(len(t)):
        close = _num(c, i)
        ts = _num(t, i)
        if close is None or ts is None or close <= 0:
            continue
        bars.append({
            "ts": ts,
            "date": datetime.fromtimestamp(ts, _VN_TZ).date().isoformat(),
            "open": (_num(o, i) or close) * scale,
            "high": (_num(h, i) or close) * scale,
            "low": (_num(l, i) or close) * scale,
            "close": close * scale,
            "volume": _num(v, i) or 0.0,
        })
    bars.sort(key=lambda bar: bar["ts"])
    return bars[-_BARS_KEPT:]


def _sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def _ema(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    ema = sum(values[:n]) / n
    k = 2 / (n + 1)
    for value in values[n:]:
        ema = value * k + ema * (1 - k)
    return ema


def _rsi(values: list[float], n: int = 14) -> float | None:
    """RSI theo Wilder (cách TradingView/app chứng khoán tính) để số liệu khớp
    với những gì người đọc tự kiểm tra."""
    if len(values) < n + 1:
        return None
    deltas = [cur - prev for prev, cur in zip(values[:-1], values[1:])]
    gain = sum(d for d in deltas[:n] if d > 0) / n
    loss = sum(-d for d in deltas[:n] if d < 0) / n
    for delta in deltas[n:]:
        gain = (gain * (n - 1) + max(delta, 0.0)) / n
        loss = (loss * (n - 1) + max(-delta, 0.0)) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def _streak(closes: list[float]) -> int:
    """Số phiên tăng (+) hoặc giảm (-) liên tiếp tính tới phiên mới nhất."""
    streak = 0
    for prev, cur in zip(reversed(closes[:-1]), reversed(closes)):
        step = (cur > prev) - (cur < prev)
        if step == 0 or (streak and (streak > 0) != (step > 0)):
            break
        streak += step
    return streak


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _indicators(symbol: str, bars: list[dict]) -> dict:
    closes = [bar["close"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    latest = bars[-1]
    prev = bars[-2] if len(bars) > 1 else None

    ma20, ma50 = _sma(closes, 20), _sma(closes, 50)
    sd20 = statistics.pstdev(closes[-20:]) if len(closes) >= 20 else None
    bb_upper = ma20 + 2 * sd20 if ma20 is not None and sd20 is not None else None
    bb_lower = ma20 - 2 * sd20 if ma20 is not None and sd20 is not None else None
    bb_width = (bb_upper - bb_lower) / ma20 * 100 if bb_upper is not None and ma20 else None

    vol_ma20 = _sma(volumes, 20)
    vol_ratio = latest["volume"] / vol_ma20 if vol_ma20 and latest["volume"] else None
    spread = latest["high"] - latest["low"]
    close_position = (latest["close"] - latest["low"]) / spread if spread > 0 else 0.5

    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    return {
        "symbol": symbol,
        "date": latest["date"],
        "close": latest["close"],
        "change_pct": (
            round((latest["close"] - prev["close"]) / prev["close"] * 100, 2) if prev else None
        ),
        "change_points": _round(latest["close"] - prev["close"]) if prev else None,
        "open": _round(latest.get("open")),
        "prev_close": _round(prev["close"]) if prev else None,
        "high": _round(latest["high"]),
        "low": _round(latest["low"]),
        "streak": _streak(closes),
        "volume": latest["volume"],
        "vol_ratio": _round(vol_ratio),
        "close_position": round(close_position, 2),
        "ma20": _round(ma20),
        "ma50": _round(ma50),
        "above_ma20": None if ma20 is None else latest["close"] > ma20,
        "rsi14": _round(_rsi(closes)),
        "macd": _round(ema12 - ema26) if ema12 is not None and ema26 is not None else None,
        "bb_upper": _round(bb_upper),
        "bb_lower": _round(bb_lower),
        "bb_width": _round(bb_width),
    }


def _build_report(rows: dict[str, list[dict]]) -> dict | None:
    """None khi thiếu dữ liệu VNINDEX: không có gì đáng đăng."""
    if not rows.get("VNINDEX"):
        return None
    metrics = [_indicators(symbol, bars) for symbol, bars in rows.items() if bars]
    stocks = [m for m in metrics if m["symbol"] not in _INDEX_SYMBOLS]
    moves = [m for m in stocks if m["change_pct"] is not None]
    return {
        "report_date": rows["VNINDEX"][-1]["date"],
        "vnindex": next(m for m in metrics if m["symbol"] == "VNINDEX"),
        "tracked": len(moves),
        "advancers": sum(m["change_pct"] > 0 for m in moves),
        "decliners": sum(m["change_pct"] < 0 for m in moves),
        "unchanged": sum(m["change_pct"] == 0 for m in moves),
        "pct_above_ma20": (
            round(sum(bool(m["above_ma20"]) for m in stocks) / len(stocks) * 100, 1)
            if stocks else 0
        ),
        "gainers": sorted(
            (m for m in moves if m["change_pct"] > 0), key=lambda m: m["change_pct"], reverse=True
        )[:5],
        "losers": sorted((m for m in moves if m["change_pct"] < 0), key=lambda m: m["change_pct"])[:5],
    }


def _fmt(value, default="N/A"):
    return default if value is None else value


def _signed(pct: float) -> str:
    return f"+{pct}" if pct > 0 else str(pct)


def _movers(rows: list[dict]) -> str:
    return ", ".join(
        f"{m['symbol']} ({_signed(m['change_pct'])}%, Vol {_fmt(m['vol_ratio'], 1)}x TB20)"
        for m in rows
    ) or "Không có mã đáng chú ý"


def _ma_zone_note(vn: dict) -> str:
    """MA20 và MA50 gần trùng nhau thì gợi ý gọi là một vùng, tránh nhắc 2 mốc như 2 ngưỡng."""
    ma20, ma50 = vn.get("ma20"), vn.get("ma50")
    if ma20 and ma50 and abs(ma20 - ma50) / ma20 < 0.005:
        low, high = sorted((ma20, ma50))
        return (
            f"\n- MA20 và MA50 gần trùng nhau: gọi chung là vùng {_vn_number(low, 2)}-"
            f"{_vn_number(high, 2)} điểm, không tách thành hai mốc."
        )
    return ""


_LEVEL_MERGE_PCT = 0.003  # mốc cách nhau < 0,3% coi là một mốc


def _key_levels(vn: dict) -> tuple[tuple[str, float, float] | None, tuple[str, float, float] | None]:
    """(mốc gần nhất phía trên, phía dưới) giá đóng cửa: (tên, giá thấp, giá cao).

    Các mốc (đỉnh/đáy phiên, MA20/MA50, biên Bollinger) cách nhau < 0,3% được gộp
    thành một vùng để AI không viết hai kịch bản cho cùng một chỗ.
    """
    close = vn.get("close")
    if not close:
        return None, None
    named = [
        ("đỉnh phiên", vn.get("high")),
        ("đáy phiên", vn.get("low")),
        ("MA20", vn.get("ma20")),
        ("MA50", vn.get("ma50")),
        ("biên trên Bollinger", vn.get("bb_upper")),
        ("biên dưới Bollinger", vn.get("bb_lower")),
    ]
    levels = sorted((price, name) for name, price in named if price)
    zones: list[list] = []  # [giá thấp, giá cao, [tên]]
    for price, name in levels:
        if zones and (price - zones[-1][1]) / zones[-1][1] < _LEVEL_MERGE_PCT:
            zones[-1][1] = price
            zones[-1][2].append(name)
        else:
            zones.append([price, price, [name]])
    above = [z for z in zones if z[0] > close * (1 + _LEVEL_MERGE_PCT / 2)]
    below = [z for z in zones if z[1] < close * (1 - _LEVEL_MERGE_PCT / 2)]

    def pack(zone):
        return (" + ".join(zone[2]), zone[0], zone[1]) if zone else None

    return pack(above[0] if above else None), pack(below[-1] if below else None)


def _level_text(level: tuple[str, float, float] | None) -> str:
    if level is None:
        return "không có"
    name, low, high = level
    if high - low < 0.005:
        return f"{_vn_number(low, 2)} điểm ({name})"
    return f"vùng {_vn_number(low, 2)}-{_vn_number(high, 2)} điểm ({name})"


def _streak_text(streak: int) -> str:
    if streak <= -2:
        return f"phiên giảm thứ {-streak} liên tiếp"
    if streak >= 2:
        return f"phiên tăng thứ {streak} liên tiếp"
    return "không có chuỗi tăng/giảm liên tiếp đáng kể"


def _stock_prompt(report: dict) -> str:
    vn = report["vnindex"]
    volume = _vn_number(vn["volume"] / 1e6) + " triệu" if vn["volume"] else "N/A"
    session = _vn_date(report["report_date"])
    above, below = _key_levels(vn)
    points = vn.get("change_points")
    points_text = f"{_signed(points)} điểm, " if points is not None else ""
    return f"""Bạn là chuyên viên phân tích của một công ty chứng khoán, viết bản nhận định cuối phiên cho Fanpage đầu tư. Người đọc là nhà đầu tư cá nhân, đọc trên điện thoại.

DỮ LIỆU PHIÊN {session} (đây là TOÀN BỘ dữ liệu được dùng):
- VN-Index đóng cửa {_fmt(vn['close'])} điểm ({points_text}{_signed(vn['change_pct'] or 0)}%), {_streak_text(vn.get('streak', 0))}.
- Mở cửa {_fmt(vn.get('open'))}, cao nhất phiên {_fmt(vn.get('high'))}, thấp nhất phiên {_fmt(vn.get('low'))} điểm; đóng cửa phiên trước {_fmt(vn.get('prev_close'))} điểm.
- Mốc gần nhất PHÍA TRÊN giá đóng cửa: {_level_text(above)}.
- Mốc gần nhất PHÍA DƯỚI giá đóng cửa: {_level_text(below)}.
- Khối lượng khớp {volume} cổ phiếu, bằng {_fmt(vn['vol_ratio'], 1)}x trung bình 20 phiên.
- Vị trí đóng cửa trong biên độ phiên: {vn['close_position']} (1.0 = sát đỉnh phiên, 0.0 = sát đáy phiên).
- MA20 = {_fmt(vn['ma20'])}, MA50 = {_fmt(vn['ma50'])}, RSI(14) = {_fmt(vn['rsi14'])}, MACD = {_fmt(vn['macd'])}, dải Bollinger {_fmt(vn['bb_lower'])} - {_fmt(vn['bb_upper'])}.{_ma_zone_note(vn)}
- Trong nhóm {report['tracked']} cổ phiếu hệ thống theo dõi (KHÔNG phải toàn thị trường): {report['advancers']} mã tăng, {report['decliners']} mã giảm, {report['unchanged']} mã đứng giá. {report['pct_above_ma20']}% số mã nằm trên MA20.
- Mã tăng mạnh nhất: {_movers(report['gainers'])}
- Mã giảm mạnh nhất: {_movers(report['losers'])}

CẤU TRÚC BÀI:
- Dòng đầu là tiêu đề, bắt đầu bằng 📌: một câu ngắn (tối đa 15 từ) nêu kết quả phiên kèm một con số chính, viết như tiêu đề báo (không viết hoa toàn bộ).
- Tiếp theo là 4 phần, mỗi phần mở đầu bằng một dòng nhãn ngắn (không đánh số): "Diễn biến phiên", "Dòng tiền và độ rộng", "Kỹ thuật", "Cần theo dõi". Mỗi phần 2-4 câu hoặc vài gạch đầu dòng ngắn. Không lặp lại cùng một ý (ví dụ vị trí so với MA) ở hai phần.
- "Cần theo dõi" gồm ĐÚNG 2 kịch bản dạng "nếu... thì...": một kịch bản với mốc gần nhất phía trên và một với mốc gần nhất phía dưới (đã cho ở phần dữ liệu, dùng đúng tên và giá). Mỗi kịch bản nói rõ điều đó có nghĩa gì bằng lời thường (ví dụ "xu hướng ngắn hạn cải thiện", "áp lực giảm còn kéo dài"), không lặp lại vế "nếu" ở vế "thì". Không dùng thuật ngữ rỗng như "khu vực kiểm định cân bằng", không viết câu không có thông tin như "vùng thấp hơn tiếp tục được theo dõi". Chỉ mô tả, không đưa ra hành động giao dịch.
- Dài khoảng 250-350 từ.

CÁCH DIỄN ĐẠT SỐ LIỆU:
- Khối lượng quy ra triệu cổ phiếu; điểm số lấy 2 chữ số thập phân; RSI, MACD lấy 1 chữ số.
- Vị trí đóng cửa diễn đạt bằng lời, MỘT lần (ví dụ "đóng cửa ở nửa dưới biên độ phiên"), không nêu con số x/1.0.
- RSI và MACD là chỉ báo, KHÔNG ghi đơn vị "điểm" (viết "RSI(14) ở 37,2", "MACD ở -14,5").
- Dữ liệu chỉ có giá mở cửa, cao, thấp, đóng cửa: KHÔNG mô tả diễn biến theo thời gian trong phiên ("đầu phiên", "cuối phiên", "phiên chiều", "lực bán tăng dần", "về cuối phiên") vì không biết các mức giá xảy ra lúc nào.
- CHỈ dùng số liệu ở trên, không tra cứu hay thêm số liệu nào khác. Không suy diễn nguyên nhân từ tin tức, khối ngoại hay yếu tố vĩ mô vì không có trong dữ liệu.
- Không ghi chú thích hay số trích dẫn kiểu [1], [3].

{_STYLE_RULES}

QUY ĐỊNH BẮT BUỘC (tuân thủ pháp lý):
- TUYỆT ĐỐI KHÔNG khuyến nghị mua/bán/nắm giữ, KHÔNG đề xuất tỷ trọng cổ phiếu/tiền mặt, KHÔNG nêu giá mục tiêu, KHÔNG gợi ý mã cụ thể nên mua hay nên bán. Chỉ phân tích và mô tả.
- KHÔNG tự viết phần "Nguồn", lời miễn trừ trách nhiệm hay hashtag (hệ thống sẽ tự thêm)."""


async def _fetch_bars(
    client: httpx.AsyncClient, gate: asyncio.Semaphore, symbol: str, now: int
) -> list[dict]:
    kind = "index" if symbol in _INDEX_SYMBOLS else "stock"
    params = {
        "from": now - _HISTORY_DAYS * 86400,
        "to": now,
        "symbol": symbol,
        "resolution": "1D",
    }
    async with gate:
        try:
            response = await client.get(_DNSE_URL.format(kind=kind), params=params)
            response.raise_for_status()
            return _parse_bars(symbol, response.json())
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("market_page: DNSE lỗi mã %s (%s).", symbol, exc)
            return []


async def _fetch_all_bars() -> dict[str, list[dict]]:
    now = int(datetime.now(timezone.utc).timestamp())
    gate = asyncio.Semaphore(_DNSE_CONCURRENCY)
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    async with http_client.scoped(timeout=30, headers=headers) as client:
        results = await asyncio.gather(
            *(_fetch_bars(client, gate, symbol, now) for symbol in _REPORT_SYMBOLS)
        )
    return dict(zip(_REPORT_SYMBOLS, results))


async def _market_snapshot() -> str | None:
    """Một dòng số liệu phiên gần nhất của VN-Index (DNSE) cho bản tin sáng.

    Tin CafeF trong feed thường không ghi điểm số; có dòng này thì mục "Thị trường"
    của bản tin có số cụ thể. Lỗi DNSE thì bản tin vẫn chạy, chỉ không có dòng này.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    async with http_client.scoped(timeout=30, headers=headers) as client:
        bars = await _fetch_bars(client, asyncio.Semaphore(1), "VNINDEX", now)
    if len(bars) < 2:
        return None
    vn = _indicators("VNINDEX", bars)
    volume = f", khối lượng khớp {_vn_number(vn['volume'] / 1e6)} triệu cổ phiếu" if vn["volume"] else ""
    return (
        f"Phiên {_vn_date(vn['date'])}: VN-Index đóng cửa {_vn_number(vn['close'], 2)} điểm, "
        f"{'tăng' if vn['change_points'] > 0 else 'giảm' if vn['change_points'] < 0 else 'đứng giá'} "
        f"{_vn_number(abs(vn['change_points']), 2)} điểm ({_vn_number(abs(vn['change_pct']), 2)}%)"
        f"{volume}."
    )


def _chart_config(bars: list[dict], report_date: str) -> dict:
    last = bars[-_CHART_BARS:]
    prices = [round(bar["close"], 2) for bar in last]
    return {
        "type": "line",
        "data": {
            "labels": [f"{bar['date'][8:10]}/{bar['date'][5:7]}" for bar in last],
            "datasets": [{
                "label": "Điểm số VN-INDEX",
                "data": prices,
                "borderColor": "#2563eb",
                "backgroundColor": "rgba(37, 99, 235, 0.15)",
                "fill": True,
                "tension": 0.25,
                "pointRadius": 3,
                "pointBackgroundColor": "#2563eb",
            }],
        },
        "options": {
            "responsive": True,
            "scales": {
                "y": {
                    "min": math.floor(min(prices) * 0.995),
                    "max": math.ceil(max(prices) * 1.005),
                    "ticks": {"precision": 1},
                },
            },
            "plugins": {
                "legend": {"display": False},
                "title": {
                    "display": True,
                    "text": f"DIỄN BIẾN VN-INDEX ĐẾN {_vn_date(report_date)}",
                    "font": {"size": 16},
                },
            },
        },
    }


async def _render_chart(bars: list[dict], report_date: str) -> bytes | None:
    """Biểu đồ chỉ là phần kèm theo: lỗi thì bài vẫn đăng dạng văn bản."""
    payload = {
        "version": "4", "backgroundColor": "#ffffff", "width": 800, "height": 450,
        "format": "png", "chart": _chart_config(bars, report_date),
    }
    try:
        async with http_client.scoped(timeout=30) as client:
            response = await client.post(_QUICKCHART_URL, json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("market_page: không tạo được biểu đồ (%s), đăng không kèm ảnh.", exc)
        return None
    if not response.headers.get("content-type", "").startswith("image/"):
        return None
    return response.content


def _stock_footer(report: dict) -> str:
    return (
        f"📊 Nguồn: dữ liệu giá và khối lượng từ DNSE (Entrade), phiên {_vn_date(report['report_date'])}; "
        "các chỉ báo (MA, RSI, MACD, Bollinger) do hệ thống tự tính từ dữ liệu lịch sử.\n\n"
        f"{_DISCLAIMER}\n\n"
        "#chungkhoan #vnindex #nhandinhthitruong #chungkhoanvietnam"
    )


_STOCK_SESSION_KEY = "market_page:stock:session:{date}"


def _is_current_session(report_date: str) -> bool:
    """Dữ liệu mới nhất có phải phiên hôm nay không. Sai khi nghỉ lễ (DNSE chỉ có
    phiên trước) hoặc DNSE chưa cập nhật xong phiên vừa đóng cửa."""
    return report_date == _today().isoformat()


_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z(\d])\d{1,3}(?:\.\d{3})+(?:,\d+)?|(?<![A-Za-z(\d.,])\d+(?:[.,]\d+)?")


def _parse_vn_number(token: str) -> tuple[float, int]:
    """(giá trị, số chữ số thập phân) của số kiểu VN "1.735,09" hoặc kiểu "1735.09"."""
    if "," in token:
        whole, frac = token.replace(".", "").split(",", 1)
        return float(f"{whole}.{frac}"), len(frac)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", token):
        return float(token.replace(".", "")), 0
    frac = token.split(".", 1)[1] if "." in token else ""
    return float(token), len(frac)


def _report_numbers(report: dict) -> list[float]:
    values: list[float] = []

    def walk(value):
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)) and math.isfinite(value):
            values.extend((float(value), abs(float(value))))
            if abs(value) >= 1e5:  # khối lượng: bài viết theo đơn vị triệu
                values.append(abs(value) / 1e6)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(report)
    return values


def unknown_numbers(text: str, report: dict) -> list[str]:
    """Số liệu trong bài không khớp dữ liệu đã tính (dấu hiệu AI tự tra web hoặc bịa).

    Chỉ kiểm tra số có phần thập phân hoặc từ 100 trở lên (điểm số, khối lượng, %
    lẻ); bỏ qua ngày tháng, "MA20", "RSI(14)", số đếm nhỏ. Cho phép sai số làm tròn
    theo đúng số chữ số thập phân AI viết.
    """
    allowed = _report_numbers(report)
    unknown = []
    for token in _NUMBER_TOKEN_RE.findall(_DATE_RE.sub(" ", text)):
        value, decimals = _parse_vn_number(token)
        if decimals == 0 and value < 100:
            continue
        tolerance = 0.5 * 10 ** -decimals + 1e-9 if decimals else 0.5 + 0.0006 * value
        if not any(abs(value - a) <= tolerance for a in allowed):
            unknown.append(token)
    return unknown


_INTRADAY_RE = re.compile(
    r"\b(đầu phiên|cuối phiên|giữa phiên|phiên sáng|phiên chiều|về cuối|cuối ngày|"
    r"tăng dần|giảm dần|lực bán tăng|lực mua tăng|nửa cuối phiên|nửa đầu phiên)\b",
    re.IGNORECASE,
)
_INDICATOR_UNIT_RE = re.compile(r"\b(RSI|MACD)\b[^.\n;]{0,25}?\d[\d.,]*\s*điểm", re.IGNORECASE)


def stock_issues(text: str, report: dict) -> list[str]:
    """Lỗi cần AI viết lại: số liệu ngoài dữ liệu, mô tả diễn biến trong phiên mà
    dữ liệu không có, RSI/MACD ghi đơn vị "điểm"."""
    issues = []
    numbers = unknown_numbers(text, report)
    if numbers:
        issues.append("số liệu không có trong dữ liệu: " + ", ".join(numbers[:10]))
    intraday = sorted({m.group(0).lower() for m in _INTRADAY_RE.finditer(text)})
    if intraday:
        issues.append("mô tả diễn biến trong phiên mà dữ liệu không có: " + ", ".join(intraday))
    if _INDICATOR_UNIT_RE.search(text):
        issues.append('ghi RSI/MACD kèm đơn vị "điểm"')
    return issues


_ISSUES_NOTE = (
    "\n\nLƯU Ý: bản trước bị loại vì {issues}. Viết lại, chỉ dùng đúng dữ liệu ở phần "
    "DỮ LIỆU PHIÊN, không tra cứu hay tự tính thêm."
)


def _clean_stock_text(text: str) -> str:
    return _strip_citations(text).replace("*", "").strip()


async def _post_stock_report(dry_run: bool = False) -> str | None:
    """Trả nội dung bài (đã đăng, hoặc chỉ tạo khi dry_run); None nếu AI lỗi/không đạt.

    Raise MarketSkip khi không nên đăng: chưa có dữ liệu phiên hôm nay (nghỉ lễ hoặc
    DNSE chậm - lịch sẽ thử lại), hoặc phiên này đã được đăng (kể cả đăng tay).
    Xem thử (dry_run) bỏ qua hai kiểm tra này để vẫn xem được nội dung.
    """
    rows = await _fetch_all_bars()
    report = _build_report(rows)
    if report is None:
        logger.warning("market_page: DNSE không trả dữ liệu VNINDEX, bỏ báo cáo.")
        return None
    session_key = _STOCK_SESSION_KEY.format(date=report["report_date"])
    if not dry_run:
        if not _is_current_session(report["report_date"]):
            raise MarketSkip(
                f"chưa có dữ liệu phiên hôm nay (mới nhất là phiên {_vn_date(report['report_date'])}"
                " - nghỉ lễ hoặc DNSE chưa cập nhật)",
                retry=True,
            )
        if await db.get_setting(session_key):
            raise MarketSkip(f"phiên {_vn_date(report['report_date'])} đã được đăng")

    prompt = _stock_prompt(report)
    text = await _ask_clean(prompt, _clean_stock_text)
    if text is None:
        logger.warning("market_page: nhận định vẫn chứa khuyến nghị mua/bán, không đăng.")
        return None
    issues = stock_issues(text, report)
    if issues:
        logger.warning("market_page: nhận định chưa đạt (%s), hỏi lại AI.", "; ".join(issues))
        text = await _ask_clean(prompt + _ISSUES_NOTE.format(issues="; ".join(issues)), _clean_stock_text)
        remaining = stock_issues(text, report) if text is not None else ["khuyến nghị"]
        if remaining:
            logger.warning(
                "market_page: AI vẫn chưa đạt (%s), không đăng lượt này.", "; ".join(remaining)
            )
            return None
    if len(text) < _MIN_POST_CHARS:
        logger.warning("market_page: AI trả nhận định bất thường (%d ký tự), bỏ.", len(text))
        return None
    text = f"{text}\n\n{_stock_footer(report)}"
    if dry_run:
        return text

    chart = await _render_chart(rows["VNINDEX"], report["report_date"])
    media = [await asyncio.to_thread(brand_image, "image/png", chart)] if chart else []
    try:
        await publish_page_post(text, media, MARKET_PAGE_KEY)
    except FacebookPublicationUncertain:
        # Có thể bài đã lên: coi như phiên đã đăng để không tạo bài thứ hai.
        await db.set_setting(session_key, "uncertain")
        raise
    await db.set_setting(session_key, "1")
    return text


# ─── Tin CafeF ──────────────────────────────────────────────────────────────

_NEWS_RSS = "https://cafef.vn/thi-truong-chung-khoan.rss"
_NEWS_DIGEST_ITEMS = 15
_ARTICLE_MAX_CHARS = 8000
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_AD_IMAGE_MARKERS = (
    "/ads/", "/quangcao/", "_300x250", "_728x90", "/banner/", "googleads", "doubleclick.net",
)
_NEWS_KEYWORDS = (
    "vn-index", "hnx-index", "upcom-index",
    "tăng mạnh", "giảm sâu", "biến động", "đảo chiều", "bùng nổ", "tích cực", "tiêu cực",
    "thanh khoản", "tỷ đô",
    "chính sách", "nghị định", "thông tư", "lãi suất", "tỷ giá", "ngân hàng nhà nước", "ubcknn",
    "khối ngoại", "bán ròng", "mua ròng",
    "kqkd", "kết quả kinh doanh", "lợi nhuận", "doanh thu",
    "cổ tức", "phát hành", "niêm yết", "hose", "hnx",
    "vic", "vhm", "vre", "hpg", "fpt", "mwg", "gvr",
)
_LEAD_IN_RE = re.compile(r"^Dưới đây là.*?\n+", re.IGNORECASE)
_SOFT_BREAK_RE = re.compile(r"(?<=[^\n])\n(?=[a-zA-Z0-9à-ỹÀ-Ỹ])")


class _Entry(NamedTuple):
    title: str
    summary: str
    link: str
    published: datetime | None


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _parse_entries(feed_bytes: bytes) -> list[_Entry]:
    entries = []
    for item in feedparser.parse(feed_bytes).entries:
        stamp = item.get("published_parsed")
        entries.append(_Entry(
            title=_collapse(item.get("title") or "Không có tiêu đề"),
            summary=_collapse(BeautifulSoup(item.get("summary") or "", "lxml").get_text(" ")),
            link=item.get("link") or "",
            published=datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else None,
        ))
    return entries


def _news_score(entry: _Entry) -> int:
    haystack = f"{entry.title} {entry.summary}".lower()
    return sum(keyword in haystack for keyword in _NEWS_KEYWORDS)


def _pick_entry(entries: list[_Entry], today) -> _Entry | None:
    """Bài nhiều từ khóa nhất trong ngày; không có bài hôm nay thì lấy bài đầu feed."""
    todays = [
        e for e in entries
        if e.published and e.published.astimezone(_VN_TZ).date() == today
    ]
    pool = todays or entries[:1]
    return max(pool, key=_news_score) if pool else None


_NEWS_GROUPS = (
    "Thị trường", "Khối ngoại", "Cổ phiếu nổi bật", "Cổ đông và lãnh đạo", "Doanh nghiệp",
    "Quy định và sàn",
)
_NEWS_GROUP_LIST = ", ".join(_NEWS_GROUPS)


def _digest_prompt(entries: list[_Entry], snapshot: str | None = None) -> str:
    blocks = "".join(
        f"Tin {i}:\nTiêu đề: {e.title}\nTóm tắt: {e.summary or 'Không có tóm tắt'}\n---\n"
        for i, e in enumerate(entries, start=1)
    )
    return f"""Bạn là biên tập viên mục thị trường chứng khoán của một trang tin tài chính. Từ các tin của CafeF bên dưới, hãy viết MỘT bản tin tổng hợp để đăng Fanpage, khoảng 250-400 từ, đọc trên điện thoại.

CẤU TRÚC:
- Dòng đầu là tiêu đề: tối đa 16 từ, nêu sự việc chính của thị trường, viết như tiêu đề báo (không viết hoa toàn bộ), không ghi ngày.
- Đoạn mở 2-3 câu nêu diễn biến chính của thị trường. Ghi "theo CafeF" đúng một lần trong đoạn này.
- Thân bài gồm 3-5 mục, mỗi mục mở đầu bằng "- <Tên nhóm>: ", 1-3 câu. Tên nhóm CHỈ chọn trong danh sách sau, mỗi nhóm dùng tối đa một lần, nhóm không có tin thì bỏ: {_NEWS_GROUP_LIST}. "Quy định và sàn" dành cho tin của UBCKNN, HOSE, HNX, VSDC (danh sách ký quỹ, quy định giao dịch...); "Doanh nghiệp" gồm kết quả kinh doanh, cổ tức, phát hành trái phiếu, thông báo của doanh nghiệp. Chỉ chọn tin quan trọng, bỏ tin vụn.
- Không lặp ý: sự việc đã nêu ở đoạn mở thì không kể lại ở các mục (hoặc ngược lại, chỉ nêu ngắn ở đoạn mở và để chi tiết ở mục).
- Không có đoạn kết, không lời kêu gọi.

NGUYÊN TẮC NỘI DUNG:
- Chỉ dùng thông tin trong các tin bên dưới, không tự thêm số liệu, dự đoán hay tin ngoài. Mâu thuẫn giữa các tin thì chọn thông tin đáng tin hơn hoặc bỏ qua.
- Chi tiết nào không có tên cụ thể (ví dụ "một cổ phiếu", "một doanh nghiệp") thì bỏ chi tiết đó thay vì viết mơ hồ.
- Dự báo số liệu của tổ chức phân tích thì nêu rõ tên tổ chức và ghi đó là dự báo.
- Viết như một bản tin đọc liền mạch: KHÔNG ghi số thứ tự tin hay chú thích kiểu [1], [11][15], không viết "tin số...". Không ghép hai sự việc không liên quan bằng "dù", "nhờ", "do" khi tin không nói chúng liên quan.
- Mỗi mục chỉ giữ tin có ý nghĩa với nhà đầu tư (diễn biến thị trường, khối ngoại, cổ phiếu lớn biến động, kết quả kinh doanh, cổ tức, giao dịch lớn của cổ đông/lãnh đạo). Bỏ tin thủ tục nhỏ (xử phạt chậm công bố thông tin, miễn nhiệm ở công ty nhỏ) trừ khi không còn tin nào khác.

{_STYLE_RULES}

QUY ĐỊNH BẮT BUỘC (tuân thủ pháp lý):
- TUYỆT ĐỐI KHÔNG đưa ra khuyến nghị mua/bán/nắm giữ hay giá mục tiêu. Tin có nhắc khuyến nghị của tổ chức/cá nhân thì bỏ qua phần đó.
- Facebook không hỗ trợ Markdown: không dùng dấu * hay **.
- KHÔNG tự viết phần "Nguồn" hay lời miễn trừ trách nhiệm ở cuối bài (hệ thống sẽ tự thêm).

{_snapshot_block(snapshot)}CÁC TIN CẦN TỔNG HỢP:

{blocks}"""


def _snapshot_block(snapshot: str | None) -> str:
    if not snapshot:
        return ""
    return (
        "SỐ LIỆU PHIÊN GẦN NHẤT (từ DNSE, KHÔNG phải từ CafeF - không viết \"theo CafeF\" "
        "cho các số này; dùng cho mục \"Thị trường\", chép đúng số):\n"
        f"{snapshot}\n\n"
    )


def _rewrite_prompt(article: str) -> str:
    return f"""Bạn là biên tập viên báo tài chính. Hãy tóm lược bài báo dưới đây thành một đoạn ngắn khoảng 80-120 từ để đăng làm bình luận Facebook, kèm link đọc bài gốc (hệ thống tự thêm link). Chỉ nêu các ý chính, không chép lại câu chữ của bài gốc.

QUY TẮC BẮT BUỘC:
1. Xuất nội dung trực tiếp, bắt đầu ngay bằng một dòng tiêu đề ngắn (không viết hoa toàn bộ). Không có lời dẫn kiểu "Dưới đây là...".
2. Không dùng dấu * hay ** (Facebook không hỗ trợ Markdown), không hashtag.
3. Mỗi đoạn viết liền mạch, chỉ xuống 2 dòng khi sang ý mới. Không tự ngắt dòng giữa câu.
4. Giữ nguyên số liệu chính xác của bài gốc, không thêm thông tin ngoài bài gốc.
5. Nếu bài gốc có khuyến nghị mua/bán/nắm giữ hoặc giá mục tiêu của tổ chức/cá nhân nào, hãy bỏ phần đó.
6. KHÔNG tự viết phần "Nguồn" hay lời miễn trừ trách nhiệm (hệ thống sẽ tự thêm).

{_STYLE_RULES}

Nội dung bài báo:
{article}"""


def _clean_news_text(text: str) -> str:
    text = _LEAD_IN_RE.sub("", _strip_citations(text)).replace("*", "")
    return _SOFT_BREAK_RE.sub(" ", text).strip()


def _first_content_image(body, page_url: str) -> str | None:
    urls = []
    for img in body.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        url = urljoin(page_url, src)
        if url.startswith(("http://", "https://")):
            urls.append(url)
    clean = [u for u in urls if not any(marker in u.lower() for marker in _AD_IMAGE_MARKERS)]
    return (clean or urls or [None])[0]


def _parse_article(html: str, page_url: str) -> tuple[str, str | None]:
    """(nội dung text, URL ảnh đại diện) của trang bài CafeF."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one(".detail-content")
    if body is None:
        return "", None
    image = _first_content_image(body, page_url)
    if image is None:
        og = soup.find("meta", property="og:image")
        image = urljoin(page_url, og["content"]) if og and og.get("content") else None
    for tag in body.find_all(["script", "style", "figcaption"]):
        tag.decompose()
    return _collapse(body.get_text(" "))[:_ARTICLE_MAX_CHARS], image


async def _get(url: str) -> httpx.Response:
    response = await web_reader._get_public(url, headers={"User-Agent": _USER_AGENT})
    if response.status_code != 200:
        raise web_reader.WebReaderError(f"HTTP {response.status_code} khi đọc {url}")
    return response


async def _download_image(url: str) -> tuple[str, bytes] | None:
    try:
        response = await _get(url)
    except (httpx.HTTPError, web_reader.WebReaderError) as exc:
        logger.warning("market_page: không tải được ảnh bài viết (%s).", exc)
        return None
    mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime not in _IMAGE_MIMES or len(response.content) > _MAX_IMAGE_BYTES:
        return None
    return mime, response.content


async def _load_article(entry: _Entry) -> tuple[str, tuple[str, bytes] | None]:
    """(nội dung bài, ảnh đại diện); thiếu phần nào trả rỗng/None phần đó."""
    try:
        page = await _get(entry.link)
    except (httpx.HTTPError, web_reader.WebReaderError) as exc:
        logger.warning("market_page: không đọc được bài '%s' (%s).", entry.link, exc)
        return "", None
    content, image_url = _parse_article(page.text, entry.link)
    return content, await _download_image(image_url) if image_url else None


_NEWS_MAX_AGE_HOURS = 24
_NEWS_MIN_FRESH_ITEMS = 3
_NEWS_LAST_LINKS_KEY = "market_page:news:last_links"
# Tỷ lệ tin trùng với bản tin trước mà vẫn coi là "không có tin mới".
_NEWS_REPEAT_RATIO = 0.7


def _fresh_entries(entries: list[_Entry], now: datetime) -> list[_Entry]:
    """Chỉ tin đăng trong 24 giờ qua (tin không có ngày đăng bị bỏ)."""
    cutoff = now - timedelta(hours=_NEWS_MAX_AGE_HOURS)
    return [e for e in entries if e.published and e.published >= cutoff]


def _news_footer(today: date, with_index_data: bool = False) -> str:
    source = "📰 Nguồn: CafeF (cafef.vn)"
    if with_index_data:
        source += "; số liệu VN-Index: DNSE"
    return f"{source}\n\n{_DISCLAIMER}"


def _news_image_mode() -> str:
    """branded (mặc định): ảnh bài nổi bật + khung trắng + logo như luồng Zalo;
    none: đăng chữ không ảnh (an toàn nhất về bản quyền ảnh)."""
    mode = os.getenv("MARKET_NEWS_IMAGE", "branded").strip().lower()
    return mode if mode in {"branded", "none"} else "branded"


async def _comment_rewrite(post_id: str, entry: _Entry, article: str) -> None:
    # Bài chính đã lên; AI hay Facebook lỗi ở bước comment không được làm job thất bại.
    try:
        comment = await _ask_clean(_rewrite_prompt(article), _clean_news_text)
        if comment is None:
            logger.warning("market_page: bản viết lại chứa khuyến nghị mua/bán, không comment.")
            return
        if comment:
            source = f"📰 Đọc bài gốc trên CafeF: {entry.title}\n{entry.link}"
            await post_comment(
                post_id, f"{comment}\n\n{source}\n{_DISCLAIMER_SHORT}", MARKET_PAGE_KEY
            )
    except Exception:
        logger.warning("market_page: đăng comment bài nổi bật lỗi.", exc_info=True)


async def _post_news(dry_run: bool = False) -> str | None:
    feed = await _get(_NEWS_RSS)
    entries = _parse_entries(feed.content)
    if not entries:
        logger.warning("market_page: feed CafeF rỗng, bỏ bản tin.")
        return None
    now = datetime.now(timezone.utc)
    fresh = _fresh_entries(entries, now)[:_NEWS_DIGEST_ITEMS]
    if len(fresh) < _NEWS_MIN_FRESH_ITEMS:
        raise MarketSkip(
            f"chỉ có {len(fresh)} tin trong 24 giờ qua (cần ít nhất {_NEWS_MIN_FRESH_ITEMS})",
            retry=True,
        )
    links = sorted({e.link for e in fresh if e.link})
    if not dry_run:
        try:
            previous = set(json.loads(await db.get_setting(_NEWS_LAST_LINKS_KEY) or "[]"))
        except ValueError:
            previous = set()
        if links and len(previous & set(links)) >= _NEWS_REPEAT_RATIO * len(links):
            raise MarketSkip("không có đủ tin mới so với bản tin trước")

    try:
        snapshot = await _market_snapshot()
    except Exception:
        logger.warning("market_page: không lấy được số liệu VN-Index cho bản tin.", exc_info=True)
        snapshot = None
    text = await _ask_clean(_digest_prompt(fresh, snapshot), _clean_news_text)
    if text is None:
        logger.warning("market_page: bản tin vẫn chứa khuyến nghị mua/bán, không đăng.")
        return None
    if len(text) < _MIN_POST_CHARS:
        logger.warning("market_page: AI trả bản tin bất thường (%d ký tự), bỏ.", len(text))
        return None
    today = datetime.now(_VN_TZ).date()
    text = f"{text}\n\n{_news_footer(today, with_index_data=bool(snapshot))}"
    if dry_run:
        return text

    featured = _pick_entry(fresh, today)
    article, image = await _load_article(featured) if featured else ("", None)
    media = []
    if image and _news_image_mode() == "branded":
        media = [await asyncio.to_thread(brand_image, *image)]
    try:
        published = await publish_page_post(text, media, MARKET_PAGE_KEY)
    except FacebookPublicationUncertain:
        # Có thể bài đã lên: ghi lại bộ tin để không đăng lại cùng nội dung.
        await db.set_setting(_NEWS_LAST_LINKS_KEY, json.dumps(links))
        raise
    await db.set_setting(_NEWS_LAST_LINKS_KEY, json.dumps(links))
    if article:
        await _comment_rewrite(published.post_id, featured, article)
    return text


# ─── Lịch chạy ──────────────────────────────────────────────────────────────

_JOBS = {"stock": _post_stock_report, "news": _post_news}
_LAST_RUN_KEY = "market_page:last:{job}"


def _next_slot(now: datetime) -> tuple[datetime, str]:
    candidates = []
    for job, at, weekdays in _schedule():
        for offset in range(8):
            day = (now + timedelta(days=offset)).date()
            when = datetime.combine(day, at, _VN_TZ)
            if day.weekday() in weekdays and when > now:
                candidates.append((when, job))
                break
    return min(candidates)


async def _record(job: str, outcome: str) -> None:
    """Ghi kết quả lượt chạy gần nhất cho trang admin; lỗi ghi không được cản việc đăng."""
    try:
        stamp = datetime.now(_VN_TZ).strftime("%d/%m %H:%M")
        await db.set_setting(_LAST_RUN_KEY.format(job=job), f"{stamp} — {outcome}")
    except Exception:
        logger.warning("market_page: không ghi được kết quả lượt chạy %s.", job, exc_info=True)


async def _execute(job: str, *, dry_run: bool = False) -> str | None:
    async with _run_lock:
        try:
            text = await _JOBS[job](dry_run=dry_run)
        except MarketSkip as skip:
            if not dry_run:
                await _record(job, f"bỏ qua: {skip}")
            raise
        except Exception as exc:
            if not dry_run:
                await _record(job, f"lỗi {type(exc).__name__}")
            raise
        if not dry_run:
            await _record(job, "đã đăng" if text else "chưa đăng được (AI lỗi/bài không đạt), sẽ thử lại")
        return text


def _slot_key(job: str, when: datetime) -> str:
    return f"market_page:{job}:{when:%Y-%m-%dT%H%M}"


async def run_once(job: str, when: datetime, *, force: bool = False) -> bool:
    """Chạy 1 lượt theo lịch; mỗi (job, giờ) chỉ đăng một lần dù loop bị đánh thức lại.

    Trả True khi slot đã xong (đăng được, không rõ kết quả, hoặc cố ý bỏ qua không
    cần thử lại); False khi nên thử lại sau (AI lỗi, chưa có dữ liệu phiên...).
    """
    key = _slot_key(job, when)
    if not force and await db.get_setting(key):
        return False
    try:
        done = bool(await _execute(job))
    except FacebookPublicationUncertain:
        logger.error("market_page: %s chưa rõ Facebook đã tạo bài chưa, không tự đăng lại.", job)
        done = True
    except MarketSkip as skip:
        logger.info("market_page: %s bỏ qua - %s.", job, skip)
        done = not skip.retry
    if done:
        await db.set_setting(key, "1")
    return done


async def run_manual(job: str, *, publish: bool) -> str | None:
    """Chạy ngay ngoài lịch (lệnh /fb_market, trang admin); không đụng cờ chống đăng trùng
    của lịch. publish=False chỉ tạo nội dung để xem, không đăng. Trả nội dung bài."""
    if job not in _JOBS:
        raise MarketPageError(f"Job '{job}' không tồn tại (stock hoặc news).")
    if publish and MARKET_PAGE_KEY not in configured_page_keys(include_dedicated=True):
        raise MarketPageError(
            f"Chưa cấu hình FACEBOOK_PAGE_ID_{MARKET_PAGE_KEY} và "
            f"FACEBOOK_PAGE_ACCESS_TOKEN_{MARKET_PAGE_KEY}."
        )
    if _run_lock.locked():
        raise MarketPageError("Đang có job chứng khoán chạy, thử lại sau ít phút.")
    try:
        return await _execute(job, dry_run=not publish)
    except MarketSkip as skip:
        raise MarketPageError(f"Không đăng: {skip}.") from skip


async def status() -> dict:
    """Ảnh chụp trạng thái cho trang admin."""
    when, job = _next_slot(datetime.now(_VN_TZ))
    return {
        "configured": MARKET_PAGE_KEY in configured_page_keys(include_dedicated=True),
        "busy": _run_lock.locked(),
        "schedule": [
            {"job": j, "time": f"{at:%H:%M}", "days": "T2-T6" if len(days) == 5 else "Hằng ngày"}
            for j, at, days in sorted(_schedule(), key=lambda row: row[1])
        ],
        "next": {"job": job, "at": when.isoformat()},
        "last": {j: await db.get_setting(_LAST_RUN_KEY.format(job=j)) for j in _JOBS},
    }


# Chạy bù / thử lại: Render restart (deploy, hết RAM) đúng giờ đăng, hoặc AI/DNSE lỗi
# lúc chạy, không còn làm mất bài cả ngày. Slot được thử lại mỗi MARKET_RETRY_MIN phút,
# tối đa MARKET_MAX_ATTEMPTS lần, trong MARKET_CATCHUP_MIN phút kể từ giờ đăng.
_TICK_SEC = 60
_attempts: dict[str, tuple[int, datetime]] = {}


def _catchup() -> timedelta:
    return timedelta(minutes=_env_int("MARKET_CATCHUP_MIN", 120, 0, 720))


def _retry_gap() -> timedelta:
    return timedelta(minutes=_env_int("MARKET_RETRY_MIN", 10, 1, 120))


def _max_attempts() -> int:
    return _env_int("MARKET_MAX_ATTEMPTS", 4, 1, 20)


def _due_slots(now: datetime) -> list[tuple[str, datetime]]:
    """Các slot hôm nay đã tới giờ và còn trong khung chạy bù."""
    due = []
    for job, at, weekdays in _schedule():
        when = datetime.combine(now.date(), at, _VN_TZ)
        if now.weekday() in weekdays and when <= now < when + _catchup():
            due.append((job, when))
    return sorted(due, key=lambda item: item[1])


async def _tick(now: datetime) -> None:
    for job, when in _due_slots(now):
        key = _slot_key(job, when)
        count, last = _attempts.get(key, (0, None))
        if count >= _max_attempts() or (last is not None and now - last < _retry_gap()):
            continue
        if await db.get_setting(key):
            continue
        _attempts[key] = (count + 1, now)
        if count:
            logger.info("market_page: thử lại %s lần %d.", job, count + 1)
        try:
            await run_once(job, when)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("market_page: lỗi khi chạy job %s.", job)
    # Bỏ bộ đếm của các slot đã qua khung để dict không phình.
    for key in [k for k, (_, last) in _attempts.items() if now - last > timedelta(days=1)]:
        del _attempts[key]


async def _loop() -> None:
    while True:
        try:
            await _tick(datetime.now(_VN_TZ))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("market_page: vòng lịch lỗi.")
        await asyncio.sleep(_TICK_SEC)


def start() -> None:
    global _task
    if MARKET_PAGE_KEY not in configured_page_keys(include_dedicated=True):
        logger.info(
            "market_page: chưa có FACEBOOK_PAGE_ID_%s/FACEBOOK_PAGE_ACCESS_TOKEN_%s, không bật.",
            MARKET_PAGE_KEY, MARKET_PAGE_KEY,
        )
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
