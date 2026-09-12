"""Bản tin RSS cho chủ bot.

- Scheduler mỗi sáng tạo ``BẢN TIN SÁNG`` và gửi đồng thời Zalo + Zoom.
- ``/bangtinsang`` chạy ngay đúng luồng scheduler (vẫn gửi cả Zalo + Zoom).
- ``/tintuc`` chỉ tạo ``ĐIỂM TIN`` rồi trả về kênh đang gọi lệnh; hàm này
  tuyệt đối không tự broadcast sang kênh còn lại.

RSS được tổng hợp bằng AI nhưng danh sách link cuối bài KHÔNG để AI tự bịa URL:
mỗi item được gắn ID nội bộ, model chỉ chọn ID đáng chú ý, còn code lấy lại đúng
tiêu đề + URL gốc từ RSS để người dùng bấm trực tiếp.
"""
import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ai import orchestrator
from core import config
from core import database as db
from services import web_reader

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_LAST_SENT_KEY = "morning_news:last_sent_date"
_task: asyncio.Task | None = None


class RunResult(NamedTuple):
    """Kết quả 1 lượt ``run_once``.

    ``content`` khác None chỉ có nghĩa là tổng hợp được bản tin; xem
    ``sent_zalo``/``sent_zoom`` để biết đã gửi tới đâu.
    """

    content: str | None
    sent_zalo: bool
    sent_zoom: bool


@dataclass(frozen=True)
class _NewsItem:
    item_id: str
    title: str
    url: str
    source: str


_GROUNDING = (
    "Bên dưới LUÔN có ít nhất 1 nguồn tin RSS thật với tiêu đề cụ thể. PHẢI "
    "tổng hợp từ đúng dữ liệu được cung cấp; không bịa thêm số liệu, sự kiện, "
    "nhân vật hoặc kết luận không có trong nguồn."
)
_STYLE = (
    "Viết tiếng Việt, gọn và dễ đọc trên điện thoại. KHÔNG thêm tiêu đề tổng "
    "ở đầu vì hệ thống sẽ tự thêm. Không thêm lời chào hoặc lời kết.\n"
    "Chia nội dung thành các phần rõ ràng bằng đúng kiểu tiêu đề plain-text sau "
    "(chỉ giữ phần có tin, không cần cố điền phần rỗng):\n"
    "📈 KINH TẾ & THỊ TRƯỜNG\n"
    "🇻🇳 TRONG NƯỚC\n"
    "🌍 THẾ GIỚI\n"
    "💻 CÔNG NGHỆ & KHOA HỌC\n"
    "🧭 ĐÁNG CHÚ Ý KHÁC\n"
    "Mỗi phần gồm 1-3 đoạn ngắn; gom các tin cùng chủ đề, ưu tiên tin mới và có "
    "ảnh hưởng rộng. Phải giữ nội dung kinh tế/chứng khoán nếu nguồn có dữ liệu, "
    "đồng thời bổ sung thời sự, xã hội, quốc tế và công nghệ đáng chú ý.\n"
    "Không dùng markdown heading (#), không dùng bảng, không chèn tên nguồn kiểu "
    "[CafeF], (theo VnExpress), và KHÔNG tự viết URL/link trong phần nội dung.\n"
    "Mỗi tin trong dữ liệu có ID như F1I1. Ở DÒNG CUỐI response, bắt buộc ghi "
    "duy nhất theo mẫu: IMPORTANT_IDS: F1I1,F2I3,F4I1. Hãy chọn 5-8 tin đáng "
    "chú ý nhất và CHỈ dùng ID thật sự xuất hiện trong dữ liệu. Dòng này là dữ "
    "liệu máy, không giải thích thêm."
)
_MIN_VALID_DIGEST_CHARS = 80
_IMPORTANT_IDS_RE = re.compile(r"(?mi)^\s*IMPORTANT_IDS\s*:\s*([^\n]+)\s*$")
_ITEM_ID_RE = re.compile(r"F\d+I\d+", re.IGNORECASE)
_FEED_TITLE_RE = re.compile(r"^\[Feed:\s*(.*?)\]\s*$")
_ITEM_HEADER_RE = re.compile(r"^(\d+)\.\s+(.+)$")
_PUBLISHED_SUFFIX_RE = re.compile(
    r"\s+\((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+.*\)\s*$",
    re.IGNORECASE,
)
_SOURCE_LABELS = {
    "vnexpress.net": "VnExpress",
    "cafef.vn": "CafeF",
    "vietstock.vn": "Vietstock",
    "tuoitre.vn": "Tuổi Trẻ",
    "genk.vn": "GenK",
}


def _feed_source(feed_text: str, url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if host in _SOURCE_LABELS:
        return _SOURCE_LABELS[host]

    first = feed_text.splitlines()[0].strip() if feed_text.splitlines() else ""
    match = _FEED_TITLE_RE.match(first)
    return (match.group(1).strip() if match else host or url)[:80]


def _parse_feed_items(feed_text: str, feed_index: int, source: str) -> list[_NewsItem]:
    """Parse đúng format do ``web_reader.read_rss`` tạo ra.

    Parser này chỉ phục vụ việc nối lại URL gốc ở cuối bản tin. Nếu một feed có
    format lạ, phần tổng hợp AI vẫn chạy; chỉ những item parse được mới xuất hiện
    trong mục link cuối bài.
    """
    lines = feed_text.strip().splitlines()
    if lines and _FEED_TITLE_RE.match(lines[0].strip()):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    if not body:
        return []

    blocks = re.split(r"\n\s*\n(?=\d+\.\s+)", body)
    items: list[_NewsItem] = []
    for block in blocks:
        block_lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not block_lines:
            continue
        header = _ITEM_HEADER_RE.match(block_lines[0])
        if not header:
            continue
        ordinal = int(header.group(1))
        title = _PUBLISHED_SUFFIX_RE.sub("", header.group(2).strip()).strip()
        url = next((line for line in reversed(block_lines[1:]) if line.startswith(("https://", "http://"))), "")
        if not url:
            continue
        items.append(
            _NewsItem(
                item_id=f"F{feed_index}I{ordinal}",
                title=title or "Tin đáng chú ý",
                url=url,
                source=source,
            )
        )
    return items


def _annotate_feed(feed_text: str, feed_index: int) -> str:
    """Gắn ID trước mỗi item để model chọn tin quan trọng mà không tự tạo URL."""
    return re.sub(
        r"(?m)^(\d+)\.\s+",
        lambda match: f"[ID:F{feed_index}I{match.group(1)}] {match.group(1)}. ",
        feed_text,
    )


def _build_prompt(feed_texts: list[str]) -> str:
    annotated = [_annotate_feed(text, i) for i, text in enumerate(feed_texts, start=1)]
    combined = "\n\n--- NGUỒN KHÁC ---\n\n".join(annotated)
    return f"Tổng hợp các tin RSS dưới đây thành bản tin.\n{_GROUNDING}\n{_STYLE}\n\n{combined}"


def _extract_important_ids(body: str) -> tuple[str, list[str]]:
    matches = list(_IMPORTANT_IDS_RE.finditer(body))
    if not matches:
        return body.strip(), []
    raw_ids = matches[-1].group(1)
    cleaned = _IMPORTANT_IDS_RE.sub("", body).strip()
    ids: list[str] = []
    for item_id in _ITEM_ID_RE.findall(raw_ids.upper()):
        if item_id not in ids:
            ids.append(item_id)
    return cleaned, ids


def _select_important_items(all_items: list[_NewsItem], requested_ids: list[str]) -> list[_NewsItem]:
    by_id = {item.item_id.upper(): item for item in all_items}
    selected: list[_NewsItem] = []
    seen_urls: set[str] = set()

    def add(item: _NewsItem | None) -> None:
        if item is None or item.url in seen_urls or len(selected) >= 8:
            return
        seen_urls.add(item.url)
        selected.append(item)

    for item_id in requested_ids:
        add(by_id.get(item_id.upper()))

    # Nếu model quên/ghi sai ID, vẫn bảo đảm cuối bài có link thật. Ưu tiên lấy
    # ít nhất một tin từ mỗi nguồn trước, sau đó mới bù theo thứ tự RSS.
    if len(selected) < 5:
        seen_sources: set[str] = set()
        for item in all_items:
            if item.source in seen_sources:
                continue
            add(item)
            seen_sources.add(item.source)
            if len(selected) >= 5:
                break
    if len(selected) < 5:
        for item in all_items:
            add(item)
            if len(selected) >= 5:
                break
    return selected


def _format_sources(successful_sources: list[str]) -> str:
    deduped: list[str] = []
    for source in successful_sources:
        if source and source not in deduped:
            deduped.append(source)
    return "Nguồn RSS: " + ", ".join(deduped) if deduped else ""


def _format_important_links(items: list[_NewsItem]) -> str:
    if not items:
        return ""
    lines = ["🔗 TIN ĐÁNG CHÚ Ý"]
    for item in items:
        lines.append(f"• {item.title}\n  {item.url}")
    return "\n".join(lines)


def _header(mode: str, now: datetime) -> str:
    if mode == "on_demand":
        return f"📰 ĐIỂM TIN — {now.strftime('%d/%m/%Y %H:%M')}"
    return f"☀️ BẢN TIN SÁNG — {now.strftime('%d/%m/%Y')}"


async def build_digest(mode: str = "morning") -> str | None:
    """Đọc RSS và tổng hợp.

    ``mode='morning'`` dùng cho scheduler và ``/bangtinsang``.
    ``mode='on_demand'`` dùng cho ``/tintuc`` và chỉ khác tiêu đề; việc gửi
    đi đâu do caller quyết định.
    """
    feeds = config.MORNING_NEWS_RSS_FEEDS
    if not feeds:
        return None

    feed_texts: list[str] = []
    all_items: list[_NewsItem] = []
    successful_sources: list[str] = []
    for url in feeds:
        try:
            text = await web_reader.read_rss(url)
            feed_texts.append(text)
            feed_index = len(feed_texts)
            source = _feed_source(text, url)
            successful_sources.append(source)
            all_items.extend(_parse_feed_items(text, feed_index, source))
            logger.info("morning_news: đọc feed '%s' OK (%d ký tự).", url, len(text))
        except web_reader.WebReaderError as exc:
            logger.warning("morning_news: lỗi đọc feed '%s': %s", url, exc)

    if not feed_texts:
        logger.warning("morning_news: tất cả %d feed đều lỗi, không có gì để tổng hợp.", len(feeds))
        return None

    response = await orchestrator.ask(_build_prompt(feed_texts))
    raw_body = (getattr(response, "text", None) or "").strip()
    if not raw_body:
        return None

    body, important_ids = _extract_important_ids(raw_body)
    if len(body) < _MIN_VALID_DIGEST_CHARS:
        logger.warning(
            "morning_news: model trả lời bất thường (%d ký tự: %r) dù có %d feed đọc được - bỏ, không gửi.",
            len(body), body, len(feed_texts),
        )
        return None

    important_items = _select_important_items(all_items, important_ids)
    now = datetime.now(_VN_TZ)
    parts = [_header(mode, now), body]
    sources = _format_sources(successful_sources)
    if sources:
        parts.append(sources)
    links = _format_important_links(important_items)
    if links:
        parts.append(links)
    return "\n\n".join(parts)


async def _send_to_zalo(content: str) -> bool:
    from channels import zalo_repository, zalo_session

    account_id = os.getenv("ZALO_BOT_ACCOUNT_ID", "zalo-bot").strip() or "zalo-bot"
    recipient_id = await zalo_session.load_controller()
    if not recipient_id:
        logger.info("morning_news: chưa có Zalo controller, bỏ qua gửi Zalo.")
        return False
    try:
        await zalo_repository.enqueue_outbox(account_id, recipient_id, content)
        return True
    except Exception:
        logger.warning("morning_news: enqueue Zalo outbox lỗi.", exc_info=True)
        return False


async def _send_to_zoom(content: str) -> bool:
    from channels import zoom

    pairing = await db.zoom_get_pairing()
    if not pairing:
        logger.info("morning_news: chưa pair Zoom, bỏ qua gửi Zoom.")
        return False
    jid, _display_name = pairing
    try:
        await zoom.send_message(to_jid=jid, text=content)
        return True
    except Exception:
        logger.warning("morning_news: gửi Zoom lỗi.", exc_info=True)
        return False


async def _already_sent_today(now: datetime) -> bool:
    last_sent = await db.get_setting(_LAST_SENT_KEY)
    return last_sent == now.date().isoformat()


async def _mark_sent(now: datetime) -> None:
    await db.set_setting(_LAST_SENT_KEY, now.date().isoformat())


async def run_once(force: bool = False) -> RunResult:
    """Tạo + broadcast một lượt bản tin sáng sang Zalo và Zoom."""
    now = datetime.now(_VN_TZ)
    if not force and await _already_sent_today(now):
        return RunResult(content=None, sent_zalo=False, sent_zoom=False)

    content = await build_digest()
    if not content:
        logger.info("morning_news: không có nội dung để gửi (thiếu feed hoặc tất cả feed lỗi).")
        return RunResult(content=None, sent_zalo=False, sent_zoom=False)

    sent_zalo = await _send_to_zalo(content)
    sent_zoom = await _send_to_zoom(content)
    if sent_zalo or sent_zoom:
        await _mark_sent(now)
    else:
        logger.info("morning_news: có nội dung nhưng chưa pair Zalo lẫn Zoom, chưa gửi được đâu cả.")
    return RunResult(content=content, sent_zalo=sent_zalo, sent_zoom=sent_zoom)


def _seconds_until_next_hour(hour: int) -> float:
    now = datetime.now(_VN_TZ)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _loop() -> None:
    while True:
        await asyncio.sleep(_seconds_until_next_hour(config.MORNING_NEWS_HOUR_VN))
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("morning_news: lỗi khi tạo/gửi bản tin sáng.")


def start() -> None:
    global _task
    if not config.MORNING_NEWS_ENABLED:
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
