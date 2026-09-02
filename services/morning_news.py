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
from typing import NamedTuple
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
    """Kết quả 1 lượt run_once(). `content` khác None chỉ có nghĩa là ĐÃ
    TỔNG HỢP được bản tin - phải xem sent_zalo/sent_zoom để biết có thực sự
    GỬI tới nơi hay không (xem docstring run_once)."""

    content: str | None
    sent_zalo: bool
    sent_zoom: bool


_GROUNDING = (
    "Bên dưới LUÔN có ít nhất 1 nguồn tin RSS thật với tiêu đề cụ thể - PHẢI "
    "tổng hợp thành bản tin từ đúng những gì đã cho. TUYỆT ĐỐI không được trả "
    "lời kiểu 'không có tin nào', 'không có thông tin', hay từ chối vì bất kỳ "
    "lý do gì - dữ liệu chắc chắn có sẵn ngay bên dưới, chỉ cần đọc và tóm tắt "
    "lại, không bịa thêm số liệu/sự kiện không có trong nguồn."
)
_STYLE = (
    "Viết tiếng Việt, giọng bản tin buổi sáng ngắn gọn dễ đọc trên điện thoại - "
    "như 1 người dẫn chương trình tường thuật lại bằng lời của mình, KHÔNG phải "
    "kiểu liệt kê nghiên cứu có trích dẫn. Diễn đạt lại hoàn toàn bằng câu văn "
    "của bạn, không giữ nguyên cấu trúc câu gốc.\n"
    "TUYỆT ĐỐI KHÔNG được thêm ngoặc trích dẫn/tên nguồn vào cuối câu hay cuối "
    "đoạn kiểu [CafeF], [VnExpress], [1], (theo Vietstock), 'nguồn tin cho biết'... "
    "- không nhắc tên nguồn RSS ở bất kỳ đâu trong bài, chỉ có duy nhất 1 dòng "
    "'Nguồn: ...' liệt kê tên các nguồn ở CUỐI CÙNG bài (sau khi hết tin).\n"
    "Gom các tin trùng chủ đề lại thành 1 đoạn liền mạch thay vì liệt kê rời "
    "rạc từng tin 1-2 dòng. Ưu tiên tin kinh tế/chứng khoán/thời sự quan trọng "
    "lên đầu. Không dùng markdown (**, #, bảng). Không thêm lời chào mở đầu/"
    "kết thúc."
)
# Nếu response ngắn hơn ngưỡng này dù feed_texts có dữ liệu thật, coi là AI đã
# "bịa" từ chối/nói không có tin thay vì tổng hợp đúng - không phải lỗi feed.
_MIN_VALID_DIGEST_CHARS = 80


def _build_prompt(feed_texts: list[str]) -> str:
    combined = "\n\n--- NGUỒN KHÁC ---\n\n".join(feed_texts)
    return f"Tổng hợp các tin RSS dưới đây thành 1 bản tin buổi sáng.\n{_GROUNDING}\n{_STYLE}\n\n{combined}"


async def build_digest() -> str | None:
    """Đọc các feed cấu hình, tổng hợp bằng AI. Trả None nếu chưa cấu hình
    feed nào, TẤT CẢ feed đều lỗi (không phải lỗi từng phần - đọc được ít
    nhất 1 feed là vẫn tổng hợp bình thường), hoặc model trả lời bất thường
    (quá ngắn/kiểu "không có tin" dù đã đưa dữ liệu thật - xem
    _MIN_VALID_DIGEST_CHARS)."""
    feeds = config.MORNING_NEWS_RSS_FEEDS
    if not feeds:
        return None

    feed_texts: list[str] = []
    for url in feeds:
        try:
            text = await web_reader.read_rss(url)
            feed_texts.append(text)
            logger.info("morning_news: đọc feed '%s' OK (%d ký tự).", url, len(text))
        except web_reader.WebReaderError as exc:
            logger.warning("morning_news: lỗi đọc feed '%s': %s", url, exc)

    if not feed_texts:
        logger.warning("morning_news: tất cả %d feed đều lỗi, không có gì để tổng hợp.", len(feeds))
        return None

    response = await orchestrator.ask(_build_prompt(feed_texts))
    body = (getattr(response, "text", None) or "").strip()
    if not body:
        return None
    if len(body) < _MIN_VALID_DIGEST_CHARS:
        # Đã đọc được ít nhất 1 feed thật (feed_texts không rỗng) nhưng model
        # trả lời bất thường ngắn/kiểu từ chối - coi là lỗi tổng hợp, không
        # phải lỗi feed, để không gửi 1 bản tin vô nghĩa cho người dùng.
        logger.warning(
            "morning_news: model trả lời bất thường (%d ký tự: %r) dù có %d feed đọc được - bỏ, không gửi.",
            len(body), body, len(feed_texts),
        )
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


async def run_once(force: bool = False) -> RunResult:
    """Tạo + gửi 1 lượt bản tin sáng. `force=True` bỏ qua guard "đã gửi hôm
    nay" (dùng cho lệnh test thủ công). QUAN TRỌNG: `result.content` khác
    None không có nghĩa là đã GỬI được - luôn kiểm tra `sent_zalo`/`sent_zoom`
    (bug cũ: trả content dù cả 2 kênh đều gửi thất bại, khiến /bantinsang báo
    "✅ Đã gửi" trong khi thực ra chẳng có gì tới nơi)."""
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
