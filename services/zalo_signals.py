"""Tín hiệu mua/bán cổ phiếu từ nhóm Zalo -> báo chủ bot qua Zoom và Zalo.

Luồng (mỗi ``interval`` phút, mặc định 2 - đổi bằng /tinhieu nhip <phút>):

1. Đọc tin nhóm MỚI (``zalo_group_messages`` có id > con trỏ) của các nhóm đang bật
   tín hiệu (/tinhieu on <nhóm> [admin|tatca]). Lần chạy đầu bắt đầu từ tin mới nhất
   (không quét lại lịch sử); tin cũ hơn ``ZALO_SIGNAL_MAX_AGE_MIN`` phút bị bỏ.
2. Lọc người gửi: chế độ ``admin`` chỉ nhận trưởng/phó nhóm (vai trò gateway lưu kèm
   tin TẠI LÚC NHẮN) và người thêm tay (/tinhieu nguoi); ``all`` nhận mọi người.
3. Lớp 1 - code, không tốn AI: tin phải có từ khoá mua/bán VÀ mã đang niêm yết (danh
   sách VCI cache 24h; mã lấy từ tin, tin không có mã thì lấy từ tin được trả lời).
4. Lớp 2 - AI JSON (``orchestrator.utility_json``: Groq trước, Gemini dự phòng; qua
   ``bounded()``), gom theo nhóm tối đa ``_CHUNK`` tin/lượt, trần
   ``ZALO_SIGNAL_MAX_AI_PER_HOUR`` lượt/giờ. Hết ngân sách hoặc AI lỗi: con trỏ dừng ở
   tin chưa xử lý, lượt sau làm tiếp.
5. Lớp 3 - code kiểm lại (chống AI bịa): mã phải là mã ứng viên của CHÍNH tin đó;
   hướng/loại thuộc tập cố định; giá chỉ giữ khi mọi con số có trong tin; câu trích
   phải là đoạn nguyên văn (không thì dùng đầu tin gốc). Người nêu, nhóm, giờ lấy từ
   DB, không từ AI. Lan Anh KHÔNG đánh giá tín hiệu (stock/policy.py không liên quan).
6. Lưu ``zalo_stock_signals`` (1 người/mã/hướng/loại 1 dòng mỗi ngày) rồi gửi gộp
   qua ``notify_hub`` nguồn ``zalo_signal`` (URGENT: gửi ngay, giờ yên lặng thì giữ):
   Zoom (tắt/chưa ghép đôi thì hub chuyển về Telegram) và Zalo của chủ bot (controller).
   Tắt từng kênh bằng ``ZALO_SIGNAL_CHANNELS``. Gửi lỗi thì thử lại ở lượt sau trong
   ``_NOTIFY_MAX_AGE_HOURS`` giờ.

Bộ nhớ: mỗi lượt đọc tối đa ``_PAGE * _MAX_PAGES`` tin, không cache gì ngoài vài số.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from collections.abc import Container
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ai import orchestrator
from ai.timeouts import TurnTimeoutError, bounded
from channels import zalo_repository, zalo_session
from channels import zalo_signal_repository as repo
from channels.zalo_signal_repository import CHANNELS, GroupMessage, Signal
from core import config, trace
from core import database as db
from core.envutil import env_int
from core.text_normalize import fold
from core.untrusted import wrap_untrusted

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
SOURCE = "zalo_signal"

INTERVAL_KEY = "zalo:signal_interval_min"
DEFAULT_INTERVAL_MIN = 2
MIN_INTERVAL_MIN = 1
MAX_INTERVAL_MIN = 60

_PAGE = 200
_MAX_PAGES = 10
_CHUNK = 8
_MAX_SYMBOLS_PER_MESSAGE = 5
_MESSAGE_CHARS = 1200
_QUOTE_CHARS = 300
_EXCERPT_CHARS = 280
_AI_TIMEOUT_SEC = 45.0
_MAX_PER_NOTICE = 8
_NOTIFY_MAX_AGE_HOURS = 6
_POLL_SEC = 20
_INTERVAL_CACHE_SEC = 60
_CLEANUP_EVERY_SEC = 24 * 3600
_MAX_CHUNK_FAILURES = 3

ACTIONS = ("buy", "sell")
KINDS = ("recommend", "self")
ADMIN_ROLES = ("owner", "admin")
ROLE_LABELS = {"owner": "trưởng nhóm", "admin": "phó nhóm", "member": "thành viên"}

# Lớp 1: từ khoá mua/bán/giá (so trên chữ đã bỏ dấu). Bắt rộng có chủ đích - AI lọc lại;
# "ban" trùng "bạn" chỉ tốn thêm lượt AI khi tin có cả mã niêm yết.
_KEYWORD_RE = re.compile(
    r"\b(?:mua|gom|muc|xuc|giai ngan|vao hang|vao lenh|bat day|tich luy|canh mua|"
    r"diem mua|vung mua|mo vi the|all ?in|buy|nap them|"
    r"ban|chot|cat lo|cat|cut ?loss|thoat hang|ha ty trong|ha ti trong|xa hang|"
    r"short|sell|stop ?loss|dung lo|take profit|"
    r"target|muc tieu|tp|sl)\b"
)
# Không bao giờ là mã: tên chỉ số và từ lóng giao dịch viết HOA. Chữ thường hay trùng
# mã thật (VIP, NAV...) KHÔNG đưa vào đây - để AI phân biệt theo ngữ cảnh.
_NOT_SYMBOLS = frozenset(
    {
        "VNINDEX", "VN30", "HNX", "HNX30", "HNXINDEX", "UPCOM", "UPCOMINDEX",
        "MUA", "BAN", "GOM", "MUC", "XUC", "CAT", "CHOT", "BUY", "SELL", "HOLD",
        "LONG", "SHORT", "STOP", "ATO", "ATC", "ETF", "CTCK", "USD", "ROOM", "ZALO", "LINK",
    }
)  # fmt: skip
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")

_ai_calls: deque[float] = deque()
_interval_cache: tuple[float, int] | None = None
# None = chưa dọn lần nào trong process này (monotonic tính từ lúc máy khởi động: so với
# 0.0 thì container mới bật chưa đủ 24h sẽ không bao giờ dọn).
_last_cleanup: float | None = None
# Lô AI lỗi liên tiếp: (row_id tin đầu lô, số lần). Lô lỗi _MAX_CHUNK_FAILURES lần liền
# thì bỏ qua để 1 lô "độc" (provider luôn từ chối) không chặn mọi tin phía sau.
_stuck: tuple[int, int] | None = None
_task: asyncio.Task | None = None


# ─── Cấu hình ───────────────────────────────────────────────────────────────


def _zalo_enabled() -> bool:
    on = os.getenv("ZALO_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    return on and bool(os.getenv("ZALO_BRIDGE_SECRET", "").strip())


def enabled() -> bool:
    """Vòng quét chạy khi gateway Zalo bật và chưa tắt bằng ZALO_SIGNALS=0."""
    return _zalo_enabled() and os.getenv("ZALO_SIGNALS", "1").strip() not in {"0", "false"}


def max_ai_per_hour() -> int:
    return env_int("ZALO_SIGNAL_MAX_AI_PER_HOUR", 30, 1, 600)


def max_age_minutes() -> int:
    return env_int("ZALO_SIGNAL_MAX_AGE_MIN", 120, 5, 24 * 60)


def channels() -> tuple[str, ...]:
    raw = os.getenv("ZALO_SIGNAL_CHANNELS", "zoom,zalo")
    picked = {part.strip().lower() for part in raw.split(",")}
    return tuple(channel for channel in CHANNELS if channel in picked)


def _default_interval() -> int:
    return env_int(
        "ZALO_SIGNAL_INTERVAL_MIN", DEFAULT_INTERVAL_MIN, MIN_INTERVAL_MIN, MAX_INTERVAL_MIN
    )


async def interval_minutes() -> int:
    """Nhịp quét (phút): setting do /tinhieu nhip đặt, không có thì env/mặc định."""
    global _interval_cache
    now = time.monotonic()
    if _interval_cache and now - _interval_cache[0] < _INTERVAL_CACHE_SEC:
        return _interval_cache[1]
    value = _default_interval()
    try:
        raw = await db.get_setting(INTERVAL_KEY)
        if raw and raw.strip().isdigit():
            value = max(MIN_INTERVAL_MIN, min(MAX_INTERVAL_MIN, int(raw)))
    except Exception:
        logger.warning("zalo_signals: không đọc được nhịp quét, dùng mặc định.", exc_info=True)
    _interval_cache = (now, value)
    return value


async def set_interval_minutes(minutes: int) -> int:
    global _interval_cache
    value = max(MIN_INTERVAL_MIN, min(MAX_INTERVAL_MIN, int(minutes)))
    await db.set_setting(INTERVAL_KEY, str(value))
    _interval_cache = (time.monotonic(), value)
    return value


def _take_ai_budget(now: float | None = None) -> bool:
    """Trần lượt AI theo cửa sổ trượt 1 giờ (trong RAM, tối đa max_ai_per_hour mốc)."""
    now = time.monotonic() if now is None else now
    while _ai_calls and now - _ai_calls[0] >= 3600:
        _ai_calls.popleft()
    if len(_ai_calls) >= max_ai_per_hour():
        return False
    _ai_calls.append(now)
    return True


def ai_calls_last_hour() -> int:
    now = time.monotonic()
    return sum(1 for moment in _ai_calls if now - moment < 3600)


# ─── Lớp 1: lọc bằng code ───────────────────────────────────────────────────


def has_signal_keyword(text: str) -> bool:
    return bool(_KEYWORD_RE.search(fold(text)))


def candidate_symbols(text: str, listed: Container[str]) -> list[str]:
    """Mã đang niêm yết được nhắc trong tin (dùng bộ nhận diện mã của chat, bỏ tên chỉ
    số và từ lóng giao dịch), tối đa _MAX_SYMBOLS_PER_MESSAGE mã."""
    from stock.analysis import detect_symbol_candidates

    if not text:
        return []
    known, unverified = detect_symbol_candidates(text)
    found: list[str] = []
    for symbol in [*known, *unverified]:
        if symbol in _NOT_SYMBOLS or symbol in found or symbol not in listed:
            continue
        found.append(symbol)
        if len(found) >= _MAX_SYMBOLS_PER_MESSAGE:
            break
    return found


async def listed_symbols() -> Container[str]:
    """Mã đang niêm yết (danh sách VCI cache 24h); VCI lỗi thì ~200 mã quen thuộc."""
    from stock import providers
    from stock.sector import ALL_KNOWN_SYMBOLS

    try:
        listed = await providers.exchange_map()
    except Exception:
        logger.warning("zalo_signals: không lấy được danh sách niêm yết.", exc_info=True)
        listed = {}
    return listed or ALL_KNOWN_SYMBOLS


def sender_allowed(message: GroupMessage, people: dict[str, dict[str, str]]) -> bool:
    if message.mode == "all":
        return True
    if message.sender_id in people.get(message.group_id, {}):
        return True
    return message.sender_role in ADMIN_ROLES


@dataclass(frozen=True)
class Candidate:
    message: GroupMessage
    symbols: tuple[str, ...]
    from_quote: bool


def pick_candidates(
    messages: list[GroupMessage],
    people: dict[str, dict[str, str]],
    listed: Container[str],
    now: datetime,
) -> list[Candidate]:
    oldest = now - timedelta(minutes=max_age_minutes())
    picked: list[Candidate] = []
    for message in messages:
        if message.sent_at < oldest or not sender_allowed(message, people):
            continue
        if not has_signal_keyword(message.content):
            continue
        symbols = candidate_symbols(message.content, listed)
        from_quote = False
        if not symbols and message.quote_text:
            symbols = candidate_symbols(message.quote_text, listed)
            from_quote = bool(symbols)
        if symbols:
            picked.append(Candidate(message, tuple(symbols), from_quote))
    return picked


# ─── Lớp 2: AI phân loại ────────────────────────────────────────────────────

_INSTRUCTIONS = """Bạn đọc tin nhắn trong room chứng khoán Việt Nam "{alias}" để tìm TÍN HIỆU MUA/BÁN cổ phiếu.
Mỗi tin có số thứ tự [n], người gửi và "mã ứng viên". Với từng tin, ghi các tín hiệu có thật trong tin đó:
- symbol: CHỈ chọn trong mã ứng viên của chính tin đó.
- action: "buy" (mua, gom, giải ngân, canh mua, bắt đáy, múc, vào hàng) hoặc "sell" (bán, chốt lời, cắt lỗ, hạ tỷ trọng, thoát hàng).
- kind: "recommend" nếu người viết khuyên/gợi ý người khác hoặc cả nhóm làm; "self" nếu người viết kể việc CHÍNH MÌNH đã/đang mua bán.
- entry, target, stop: chép NGUYÊN VĂN vùng giá mua/bán, giá mục tiêu, giá cắt lỗ có trong tin (vd "26.5-27"); không có thì "".
- quote: đoạn ngắn (tối đa 200 ký tự) chép NGUYÊN VĂN từ tin, chứa ý mua/bán.
KHÔNG ghi: câu hỏi ("HPG mua được không?"), tin tức hay số liệu doanh nghiệp, nhận định chung không có hành động, giả định ("nếu về 20 thì..."), quảng cáo môi giới/margin/mở tài khoản, lời nhắc riêng giữa các thành viên.
Tin "trả lời" một tin khác: mã ứng viên có thể lấy từ tin được trả lời ("múc được" trả lời "HPG thế nào?").
Chỉ trả JSON dạng {{"signals": [{{"n": 1, "symbol": "HPG", "action": "buy", "kind": "recommend", "entry": "", "target": "", "stop": "", "quote": "..."}}]}}. Không có tín hiệu nào: {{"signals": []}}."""


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _sender_label(message: GroupMessage) -> str:
    name = message.sender_name.strip() or message.sender_id
    role = ROLE_LABELS.get(message.sender_role)
    return f"{name} ({role})" if role else name


def build_prompt(alias: str, chunk: list[Candidate]) -> str:
    blocks = []
    for index, candidate in enumerate(chunk, 1):
        message = candidate.message
        moment = message.sent_at.astimezone(VN_TZ).strftime("%H:%M %d/%m")
        lines = [
            f"[{index}] {moment} · {_sender_label(message)} · mã ứng viên: "
            + ", ".join(candidate.symbols)
        ]
        if message.quote_text:
            lines.append(f"↪ trả lời tin: {_clip(message.quote_text, _QUOTE_CHARS)}")
        lines.append(f"Nội dung: {_clip(message.content, _MESSAGE_CHARS)}")
        blocks.append("\n".join(lines))
    return (
        _INSTRUCTIONS.format(alias=alias)
        + "\n\n"
        + wrap_untrusted("\n\n".join(blocks), "tin nhắn nhóm Zalo")
    )


async def _ask_ai(prompt: str) -> list | None:
    """Danh sách tín hiệu thô; None khi AI lỗi/quá hạn (lượt sau thử lại)."""
    try:
        data = await bounded(lambda: orchestrator.utility_json(prompt), _AI_TIMEOUT_SEC)
    except (TurnTimeoutError, TimeoutError):
        logger.info("zalo_signals: AI quá hạn, để lượt sau.")
        return None
    except Exception:
        logger.warning("zalo_signals: AI lỗi, để lượt sau.", exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("signals")
    if not isinstance(items, list):
        logger.warning("zalo_signals: AI trả JSON không có danh sách signals, coi như rỗng.")
        return []
    return items


# ─── Lớp 3: kiểm lại kết quả AI ─────────────────────────────────────────────


@dataclass(frozen=True)
class Found:
    candidate: Candidate
    symbol: str
    action: str
    kind: str
    entry: str
    target: str
    stop: str
    quote: str


def _numbers(text: str) -> set[str]:
    return {number.replace(",", ".") for number in _NUMBER_RE.findall(text or "")}


def verified_price(value: object, source: str) -> str:
    """Giữ giá AI chép ra chỉ khi MỌI con số trong đó có trong tin gốc."""
    text = _clip(str(value or ""), 40)
    numbers = _numbers(text)
    if not numbers or not numbers <= _numbers(source):
        return ""
    return text


def verified_quote(value: object, source: str) -> str:
    """Câu trích AI chép ra nếu đúng là đoạn nguyên văn của tin; không thì đầu tin gốc."""
    quote = _clip(str(value or ""), 400).strip(" \"'“”")
    plain = " ".join((source or "").split())
    if len(quote) >= 8 and quote.casefold() in plain.casefold():
        return _clip(quote, _EXCERPT_CHARS)
    return _clip(plain, _EXCERPT_CHARS)


def validate(items: list, chunk: list[Candidate]) -> list[Found]:
    found: list[Found] = []
    seen: set[tuple[int, str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_index = item.get("n")
        if isinstance(raw_index, bool) or not isinstance(raw_index, (int, str)):
            continue
        try:
            index = int(raw_index)
        except ValueError:
            continue
        if not 1 <= index <= len(chunk):
            continue
        candidate = chunk[index - 1]
        symbol = str(item.get("symbol") or "").strip().upper()
        action = str(item.get("action") or "").strip().lower()
        kind = str(item.get("kind") or "").strip().lower()
        if symbol not in candidate.symbols or action not in ACTIONS or kind not in KINDS:
            continue
        key = (index, symbol, action, kind)
        if key in seen:
            continue
        seen.add(key)
        message = candidate.message
        price_source = f"{message.content}\n{message.quote_text}"
        found.append(
            Found(
                candidate=candidate,
                symbol=symbol,
                action=action,
                kind=kind,
                entry=verified_price(item.get("entry"), price_source),
                target=verified_price(item.get("target"), price_source),
                stop=verified_price(item.get("stop"), price_source),
                quote=verified_quote(item.get("quote"), message.content),
            )
        )
    return found


# ─── Một lượt quét ──────────────────────────────────────────────────────────


def _vn_date(moment: datetime) -> date:
    return moment.astimezone(VN_TZ).date()


def _chunks(candidates: list[Candidate]) -> list[list[Candidate]]:
    """Gom theo nhóm (tối đa _CHUNK tin/lượt AI), xếp theo tin đầu tiên của từng lô."""
    by_group: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_group.setdefault(candidate.message.group_id, []).append(candidate)
    chunks = [
        items[start : start + _CHUNK]
        for items in by_group.values()
        for start in range(0, len(items), _CHUNK)
    ]
    return sorted(chunks, key=lambda chunk: chunk[0].message.row_id)


def _chunk_failed(row_id: int) -> bool:
    """Ghi 1 lần AI lỗi cho lô bắt đầu ở ``row_id``; True = đã lỗi đủ số lần, bỏ lô."""
    global _stuck
    count = _stuck[1] + 1 if _stuck and _stuck[0] == row_id else 1
    if count >= _MAX_CHUNK_FAILURES:
        _stuck = None
        return True
    _stuck = (row_id, count)
    return False


async def _process(account_id: str, candidates: list[Candidate]) -> tuple[int, int | None]:
    """(số tín hiệu mới, row_id tin đầu tiên CHƯA xử lý được nếu phải dừng)."""
    global _stuck
    saved = 0
    for chunk in _chunks(candidates):
        first = chunk[0].message.row_id
        if not _take_ai_budget():
            logger.info("zalo_signals: hết %d lượt AI/giờ, để lượt sau.", max_ai_per_hour())
            return saved, first
        items = await _ask_ai(build_prompt(chunk[0].message.alias, chunk))
        if items is None:
            if not _chunk_failed(first):
                return saved, first
            logger.warning(
                "zalo_signals: AI lỗi %d lần liền với lô %d tin từ tin #%d, bỏ qua lô này.",
                _MAX_CHUNK_FAILURES,
                len(chunk),
                first,
            )
            continue
        if _stuck and _stuck[0] == first:
            _stuck = None
        for item in validate(items, chunk):
            message = item.candidate.message
            new_id = await repo.save_signal(
                account_id,
                message,
                symbol=item.symbol,
                action=item.action,
                kind=item.kind,
                entry=item.entry,
                target=item.target,
                stop=item.stop,
                quote=item.quote,
                signal_date=_vn_date(message.sent_at),
            )
            if new_id is not None:
                saved += 1
    return saved, None


async def scan(account_id: str, now: datetime | None = None) -> int:
    """Quét tin mới của các nhóm đang bật tín hiệu; trả số tín hiệu mới đã lưu."""
    now = now or datetime.now(VN_TZ)
    upto = await repo.settled_max_id(account_id)
    cursor = await repo.load_cursor(account_id)
    if cursor is None or cursor > upto:
        # Lần đầu (hoặc bảng tin vừa bị dọn sạch): bắt đầu từ tin mới nhất.
        await repo.save_cursor(account_id, upto)
        return 0
    if cursor == upto:
        return 0
    people = await repo.list_people(account_id)
    listed = await listed_symbols()
    saved = 0
    for _ in range(_MAX_PAGES):
        if cursor >= upto:
            break
        messages = await repo.messages_between(account_id, cursor, upto, _PAGE)
        page_end = messages[-1].row_id if len(messages) == _PAGE else upto
        candidates = pick_candidates(messages, people, listed, now)
        new, stopped_at = await _process(account_id, candidates)
        saved += new
        cursor = stopped_at - 1 if stopped_at is not None else page_end
        await repo.save_cursor(account_id, cursor)
        if stopped_at is not None:
            break
    return saved


# ─── Gửi Zoom / Zalo ────────────────────────────────────────────────────────

_HEADLINES = {
    ("buy", "recommend"): "🟢 MUA {symbol} · gợi ý",
    ("buy", "self"): "🟢 MUA {symbol} · tự báo đã mua",
    ("sell", "recommend"): "🔴 BÁN {symbol} · gợi ý bán/chốt",
    ("sell", "self"): "🔴 BÁN {symbol} · tự báo đã bán",
}
DISCLAIMER = "ℹ️ Ý kiến của thành viên nhóm, không phải khuyến nghị của Lan Anh."


def format_signal(
    signal: Signal,
    *,
    crowd: tuple[int, int] | None = None,
    held: bool = False,
    with_date: bool = True,
) -> str:
    moment = signal.sent_at.astimezone(VN_TZ).strftime("%H:%M %d/%m" if with_date else "%H:%M")
    name = signal.sender_name.strip() or signal.sender_id
    role = ROLE_LABELS.get(signal.sender_role)
    who = f"{name} ({role})" if role else name
    lines = [
        _HEADLINES[(signal.action, signal.kind)].format(symbol=signal.symbol),
        f"👤 {who} · 💬 nhóm {signal.alias} · 🕘 {moment}",
    ]
    prices = []
    if signal.entry:
        prices.append(("💰 Vùng mua " if signal.action == "buy" else "💰 Giá ") + signal.entry)
    if signal.target:
        prices.append(f"🎯 Mục tiêu {signal.target}")
    if signal.stop:
        prices.append(f"🛑 Cắt lỗ {signal.stop}")
    if prices:
        lines.append(" · ".join(prices))
    lines.append(f"📝 “{signal.quote}”")
    if crowd and crowd[0] >= 2:
        verb = "mua" if signal.action == "buy" else "bán"
        lines.append(
            f"🔥 Hôm nay {crowd[0]} người ở {crowd[1]} nhóm cùng nói {verb} {signal.symbol}"
        )
    if held:
        lines.append(f"⭐ {signal.symbol} đang có trong danh mục")
    return "\n".join(lines)


async def _holdings() -> set[str]:
    if not config.ALLOWED_USER_ID:
        return set()
    from stock import portfolio

    try:
        return {h.symbol for h in await portfolio.list_holdings(config.ALLOWED_USER_ID)}
    except Exception:
        logger.warning("zalo_signals: không đọc được danh mục.", exc_info=True)
        return set()


async def compose(account_id: str, signals: list[Signal]) -> str:
    crowd: dict[tuple[str, str, date], tuple[int, int]] = {}
    for day in {_vn_date(signal.sent_at) for signal in signals}:
        symbols = [s.symbol for s in signals if _vn_date(s.sent_at) == day]
        for (symbol, action), value in (await repo.consensus(account_id, day, symbols)).items():
            crowd[(symbol, action, day)] = value
    held = await _holdings()
    title = "📈 TÍN HIỆU CỔ PHIẾU TỪ NHÓM ZALO"
    if len(signals) > 1:
        title += f" ({len(signals)})"
    blocks = [
        format_signal(
            signal,
            crowd=crowd.get((signal.symbol, signal.action, _vn_date(signal.sent_at))),
            held=signal.symbol in held,
        )
        for signal in signals
    ]
    symbol = signals[0].symbol
    footer = f"{DISCLAIMER}\nXem ai đã nói về mã: /tinhieu {symbol} · Tắt: /tinhieu off <nhóm>"
    return "\n\n".join([title, *blocks, footer])


async def zalo_controller() -> str:
    env = os.getenv("ZALO_CONTROLLER_ID", "").strip()
    if env:
        return env
    try:
        return await zalo_session.load_controller() or ""
    except Exception:
        logger.warning("zalo_signals: không đọc được Zalo controller.", exc_info=True)
        return ""


async def _send(channel: str, account_id: str, text: str, dedup: str) -> bool | None:
    """True = đã giao/xếp hàng; False = lỗi (thử lại); None = kênh không có người nhận."""
    from services import notify_hub

    if channel == "zoom":
        if not config.ALLOWED_USER_ID:
            return None
        return await notify_hub.send(
            "zoom", config.ALLOWED_USER_ID, text, source=SOURCE, dedup=dedup
        )
    controller = await zalo_controller()
    if not controller:
        return None
    from channels import zalo_users

    user = await zalo_users.resolve(controller)
    if user is not None and user.is_active:
        # Qua hub: giờ yên lặng riêng của Zalo chủ bot (/yenlang) được tôn trọng.
        return await notify_hub.send(
            "zalo", user.internal_user_id, text, source=SOURCE, dedup=dedup
        )
    from channels.zalo_text import to_plain_text

    await zalo_repository.enqueue_outbox(account_id, controller, to_plain_text(text))
    return True


async def deliver(account_id: str) -> int:
    """Gửi tín hiệu chưa giao tới từng kênh (mỗi kênh tối đa 1 tin gộp/lượt)."""
    sent = 0
    for channel in channels():
        signals = await repo.claim_pending(
            account_id, channel, _NOTIFY_MAX_AGE_HOURS, _MAX_PER_NOTICE
        )
        if not signals:
            continue
        ids = [signal.id for signal in signals]
        try:
            text = await compose(account_id, signals)
            ok = await _send(channel, account_id, text, f"zsig:{channel}:{ids[0]}:{ids[-1]}")
        except Exception:
            logger.warning("zalo_signals: gửi %s lỗi.", channel, exc_info=True)
            ok = False
        if ok is False:
            await repo.release(ids, channel)
            continue
        if ok is None:
            logger.info("zalo_signals: kênh %s chưa có người nhận, bỏ %d tin.", channel, len(ids))
            continue
        sent += len(ids)
    return sent


async def run_tick(now: datetime | None = None) -> tuple[int, int]:
    """1 lượt: quét rồi gửi. Trả (tín hiệu mới, tín hiệu đã gửi theo các kênh)."""
    global _last_cleanup
    account_id = await zalo_repository.resolve_active_account_id()
    saved = await scan(account_id, now)
    sent = await deliver(account_id)
    if _last_cleanup is None or time.monotonic() - _last_cleanup >= _CLEANUP_EVERY_SEC:
        _last_cleanup = time.monotonic()
        await repo.cleanup(account_id)
    if saved or sent:
        logger.info("zalo_signals: %d tín hiệu mới, %d lượt giao.", saved, sent)
    return saved, sent


# ─── Vòng lặp nền ───────────────────────────────────────────────────────────


async def _loop() -> None:
    trace.detach()
    last_run = float("-inf")
    while True:
        try:
            interval = await interval_minutes()
            if time.monotonic() - last_run >= interval * 60 - _POLL_SEC / 2:
                last_run = time.monotonic()
                with trace.job("zalo_signals"):
                    await run_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Vòng quét tín hiệu Zalo lỗi")
        await asyncio.sleep(_POLL_SEC)


def start() -> None:
    global _task
    if not enabled():
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
