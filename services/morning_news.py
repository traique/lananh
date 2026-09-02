"""Bản tin buổi sáng: đọc các feed RSS trong config.MORNING_NEWS_RSS_FEEDS,
tổng hợp bằng AI thành 1 bản tin gọn, gửi cho chủ bot qua Zalo + Zoom lúc
MORNING_NEWS_HOUR_VN (mặc định 8h) giờ VN.

Kiến trúc lặp lại đúng pattern scheduler.py::_daily_digest_loop (portfolio
digest cho Telegram) và channels/zalo_scheduler.py (digest nhóm Zalo) -
1 vòng lặp asyncio.sleep tới giờ mục tiêu, có guard idempotent qua
core.database.get_setting/set_setting để tránh gửi trùng nếu Render restart
đúng lúc quanh giờ chạy (free tier có thể spin down/up hoặc redeploy).
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ai import orchestrator
from core import config
from core import database as db
from services import web_reader

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_LAST_SENT_KEY = "morning_news:last_sent_date"
_task: asyncio.Task | None = None

_GROUNDING = (
    "Chỉ dùng đúng các tin đã cho bên dưới, không bịa thêm số liệu/sự kiện "
    "không có trong nguồn. Đây là tiêu đề + tóm tắt ngắn lấy từ RSS (không phải "
    "bài đầy đủ) - không suy diễn chi tiết ngoài những gì đã cho."
)
_STYLE = (
    "Viết tiếng Việt, giọng bản tin buổi sáng ngắn gọn dễ đọc trên điện thoại. "
    "Gom các tin trùng chủ đề lại thay vì liệt kê riêng lẻ. Mỗi tin 1-2 dòng. "
    "Ưu tiên tin kinh tế/chứng khoán/thời sự quan trọng lên đầu. Không dùng "
    "markdown (**, #, bảng). Không thêm lời chào mở đầu/kết thúc."
)


def _build_prompt(feed_texts: list[str]) -> str:
    combined = "\n\n--- NGUỒN KHÁC ---\n\n".join(feed_texts)
    return f"Tổng hợp các tin RSS dưới đây thành 1 bản tin buổi sáng.\n{_GROUNDING}\n{_STYLE}\n\n{combined}"


async def build_digest() -> str | None:
    """Đọc các feed cấu hình, tổng hợp bằng AI. Trả None nếu chưa cấu hình
    feed nào hoặc TẤT CẢ feed đều lỗi (không phải lỗi từng phần - đọc được
    ít nhất 1 feed là vẫn tổng hợp bình thường)."""
    feeds = config.MORNING_NEWS_RSS_FEEDS
    if not feeds:
        return None

    feed_texts: list[str] = []
    for url in feeds:
        try:
            feed_texts.append(await web_reader.read_rss(url))
        except web_reader.WebReaderError as exc:
            logger.warning("morning_news: lỗi đọc feed '%s': %s", url, exc)

    if not feed_texts:
        logger.warning("morning_news: tất cả %d feed đều lỗi, không có gì để tổng hợp.", len(feeds))
        return None

    response = await orchestrator.ask(_build_prompt(feed_texts))
    body = (getattr(response, "text", None) or "").strip()
    if not body:
        return None

    now = datetime.now(_VN_TZ)
    header = f"☀️ TIN TỨC BUỔI SÁNG — {now.strftime('%d/%m/%Y')}\n\n"
    return header + body


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


async def run_once(force: bool = False) -> str | None:
    """Tạo + gửi 1 lượt bản tin sáng. `force=True` bỏ qua guard "đã gửi hôm
    nay" (dùng cho lệnh test thủ công). Trả về nội dung đã gửi, hoặc None
    nếu không có gì để gửi/đã gửi rồi hôm nay."""
    now = datetime.now(_VN_TZ)
    if not force and await _already_sent_today(now):
        return None

    content = await build_digest()
    if not content:
        logger.info("morning_news: không có nội dung để gửi (thiếu feed hoặc tất cả feed lỗi).")
        return None

    sent_zalo = await _send_to_zalo(content)
    sent_zoom = await _send_to_zoom(content)
    if sent_zalo or sent_zoom:
        await _mark_sent(now)
    else:
        logger.info("morning_news: có nội dung nhưng chưa pair Zalo lẫn Zoom, chưa gửi được đâu cả.")
    return content


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
