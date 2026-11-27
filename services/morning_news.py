"""Bản tin RSS cho chủ bot.

- Scheduler mỗi sáng tạo ``BẢN TIN SÁNG`` và gửi đồng thời Zalo + Zoom.
- ``/bangtinsang`` chạy ngay đúng luồng scheduler (vẫn gửi cả Zalo + Zoom).
- ``/tintuc`` chỉ tạo ``ĐIỂM TIN`` rồi trả về kênh đang gọi lệnh; hàm này
  tuyệt đối không tự broadcast sang kênh còn lại.

RSS được tổng hợp bằng AI nhưng danh sách link cuối bài KHÔNG để AI tự bịa URL:
mỗi item được gắn ID nội bộ, model chỉ chọn ID đáng chú ý, còn code lấy lại đúng
tiêu đề + URL gốc từ RSS để người dùng bấm trực tiếp.

Tóm tắt dựa trên NỘI DUNG bài, không chỉ tiêu đề (tiêu đề + mô tả RSS ngắn cho ra bản
tin na ná tiêu đề): lượt AI 1 chọn 6-8 tin, code đọc phần đầu từng bài
(article_reader, có trần ký tự/thời gian/số bài đọc cùng lúc), lượt AI 2 tóm tắt nội
dung và ghi "(bài N)" khớp mục "đọc bài N". Bài không đọc được thì dùng mô tả RSS.

Bản tin sáng cá nhân (services/morning_brief.py) dùng chung, KHÔNG gọi AI thêm:
- ``shared_digest()``: bản tin sáng của hôm nay, tổng hợp TỐI ĐA 1 lần/ngày rồi
  giữ trong RAM (1 chuỗi) cho cả lượt 8h lẫn bản tin cá nhân gộp tin.
- ``today_headlines()``: danh sách tin cho thành viên - lấy lại đúng danh sách
  "đọc bài N" của bản tin AI nếu đã có, không thì chọn tất định từ RSS.
- Zalo controller / Zoom đã bật bản tin cá nhân (gộp tin) thì lượt 8h bỏ qua
  kênh đó: bản tin cá nhân mang luôn phần tin, người nhận chỉ nhận 1 tin.

Lọc tin trước khi đưa AI chọn (thuần code, không tốn lượt AI):
- Bỏ tin đăng quá ``_MAX_AGE_HOURS`` giờ (``read_rss(max_age_hours=...)``; tin không
  ghi ngày vẫn giữ) - feed ít cập nhật không còn đưa tin hôm kia vào bản tin.
- Bỏ link trùng giữa các feed và tin đã lên bản tin những ngày trước (setting
  ``morning_news:recent_urls``, giữ 3 ngày); so link bằng ``_url_key`` (bỏ utm_*,
  fbclid, zarsrc..., "www.", "/" cuối).
- Lời dặn chọn tin có thang giá trị (bỏ tin đồn, PR, khuyến nghị mua bán...); sau đó
  code giữ mỗi nhóm (kinh tế / thời sự / công nghệ, suy từ link feed) tối đa
  ``_MAX_PER_GROUP`` tin, còn chỗ thì nhóm chưa có tin nào được thêm 1 tin.
- Feed lỗi ``_FEED_DEAD_DAYS`` ngày liền thì báo chủ bot 1 lần (setting
  ``morning_news:feed_health``): báo hay đổi link RSS. Feed chỉ tạm im (cuối tuần,
  Tết) không tính là lỗi; tin mới nhất quá ``_STALE_FEED_HOURS`` giờ mới tính.
"""

import asyncio
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import NamedTuple
from urllib.parse import unquote_plus, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ai import orchestrator
from core import alerts, config, memtrim, untrusted, urls
from core import database as db
from services import article_reader, web_reader

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
    summary: str = ""  # mô tả ngắn trong RSS (dự phòng khi không đọc được bài)
    published: str = ""  # ngày đăng như feed ghi (chỉ để AI biết tin mới hay cũ)
    group: str = ""  # nhóm tin suy từ link feed (_feed_group)


_GROUNDING = (
    "Bên dưới LUÔN có ít nhất 1 nguồn tin RSS thật với tiêu đề cụ thể. PHẢI "
    "tổng hợp từ đúng dữ liệu được cung cấp; không bịa thêm số liệu, sự kiện, "
    "nhân vật hoặc kết luận không có trong nguồn.\n"
    "- Mỗi câu phải dựa trên ít nhất 1 tin cụ thể bên dưới; không viết câu nhận định, "
    'dự đoán hay câu chung chung không có trong tin (vd "người dùng đang chờ đợi...").\n'
    "- Gọi người và tổ chức đúng như nguồn gọi. KHÔNG tự thêm chức danh, chức vụ, tuổi "
    'hay mốc thời gian theo hiểu biết riêng (hiểu biết của bạn có thể đã cũ - vd không gọi ai là "cựu ..." '
    "khi nguồn không viết vậy).\n"
    "- Chỉ dùng con số có trong nguồn; câu có số không có trong nguồn sẽ bị hệ thống xoá."
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
    "[CafeF], (theo VnExpress), và KHÔNG tự viết URL/link trong phần nội dung."
)
_SELECT_TASK = (
    "Chọn 6-8 tin đáng chú ý nhất cho bản tin sáng từ danh sách tin RSS dưới đây: ưu tiên "
    "tin mới, ảnh hưởng rộng; giữ tin kinh tế/chứng khoán nếu có, kèm thời sự trong nước, "
    "quốc tế, công nghệ; mỗi tin một sự kiện khác nhau (không chọn 2 bài cùng một chuyện, vd "
    "2 bài giá vàng cùng ngày).\n"
    "Cách cân nhắc giá trị một tin:\n"
    "- Cao nhất: chính sách lớn (lãi suất, thuế, luật mới), thị trường biến động mạnh, sự "
    "kiện ảnh hưởng tới nhiều người hoặc cả nền kinh tế.\n"
    "- Khá: kết quả kinh doanh bất ngờ hay thương vụ lớn của doanh nghiệp lớn, số liệu kinh tế "
    "đáng kể, thời sự trong nước/quốc tế có tác động rõ, công nghệ mới có ích thực tế.\n"
    "- Bỏ qua: tin đồn, bài PR/quảng cáo, khuyến nghị mua bán cổ phiếu, nhận định chung chung "
    "không có số liệu, giá 1 mã lên xuống thường ngày, con số % gây sốc mà không có mốc so "
    "sánh, tin giật gân.\n"
    "- Việc đã xảy ra ưu tiên hơn dự báo hay ý kiến.\n"
    "Chỉ dùng ID thật sự có trong dữ liệu. CHỈ trả đúng 1 dòng, "
    "không giải thích: IMPORTANT_IDS: F1I1,F2I3,F4I1"
)
_SUMMARY_TASK = (
    "Dưới đây là phần đầu các bài báo đã chọn, mỗi bài có số thứ tự. Viết bản tin tóm tắt "
    "NỘI DUNG các bài: mỗi bài 1-3 câu nêu ý chính cụ thể lấy từ thân bài (ai, làm gì, con "
    "số, vì sao đáng chú ý) - KHÔNG chép lại hay diễn đạt lại tiêu đề. Cuối ý của mỗi bài ghi "
    'số bài trong ngoặc, vd "(bài 3)", để người đọc nhắn "đọc bài 3" đọc nguyên bài. Bài chỉ '
    "có tiêu đề thì viết ngắn, không đoán thêm."
)
_EXCERPT_CHARS = 1500
_EXCERPT_TIMEOUT_SEC = 15
_EXCERPT_CONCURRENCY = 2  # bóc HTML tốn RAM (Render 512 MB): tối đa 2 bài cùng lúc
_MIN_VALID_DIGEST_CHARS = 80
_IMPORTANT_IDS_RE = re.compile(r"(?mi)^\s*IMPORTANT_IDS\s*:\s*([^\n]+)\s*$")
_ITEM_ID_RE = re.compile(r"F\d+I\d+", re.IGNORECASE)
_FEED_TITLE_RE = re.compile(r"^\[Feed:\s*(.*?)\]\s*$")
_ITEM_HEADER_RE = re.compile(r"^(\d+)\.\s+(.+)$")
# Ngày đăng read_rss nối sau tiêu đề: "(Wed, 28 Oct 2026 ...)", Tuổi Trẻ ghi tháng/ngày/năm
# kèm giờ AM/PM, ISO "(2026-10-28T08:33:00+07:00)".
_PUBLISHED_SUFFIX_RE = re.compile(
    r"\s+\((?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+[^)]*|\d{1,4}[/-]\d{1,2}[/-]\d{1,4}[^)]*"
    r"|\d{4}-\d{2}-\d{2}[T ][^)]*)\)\s*$",
    re.IGNORECASE,
)
_SECTION_HEADER_RE = re.compile(r"\n+(?=(?:📈|🇻🇳|🌍|💻|🧭) [^\n]*$)", re.MULTILINE)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
_SOURCE_LABELS = {
    "vnexpress.net": "VnExpress",
    "cafef.vn": "CafeF",
    "vietstock.vn": "Vietstock",
    "tuoitre.vn": "Tuổi Trẻ",
    "genk.vn": "GenK",
}
# Tin đăng quá 36 giờ bị bỏ. Feed im cả 36 giờ (chứng khoán sáng Chủ nhật/thứ Hai, Tết)
# chỉ vắng mặt trong bản tin hôm đó, không tính là feed lỗi (_STALE_FEED_HOURS).
_MAX_AGE_HOURS = 36
_MAX_ITEMS = 8  # tối đa 8 tin trong mục "đọc bài N"
_MIN_ITEMS = 5  # model chọn ít hơn thì code bù cho đủ
_MAX_PER_GROUP = 4  # 1 nhóm tối đa 4/8 tin: kinh tế không chiếm hết bản tin
# Nhóm tin theo link FEED (link bài VnExpress không ghi chuyên mục); không khớp thì
# là "thời sự".
_GROUP_MARKERS = (
    (
        "kinh tế",
        (
            "kinh-doanh",
            "kinh-te",
            "chung-khoan",
            "tai-chinh",
            "bat-dong-san",
            "cafef.vn",
            "cafebiz.vn",
            "vietstock.vn",
        ),
    ),
    ("công nghệ", ("genk.vn", "so-hoa", "cong-nghe", "khoa-hoc", "ictnews", "tinhte.vn")),
)
_DEFAULT_GROUP = "thời sự"
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ttclid",
        "twclid",
        "zarsrc",
        "_ga",
    }
)
# {ngày ISO: [khoá link]} của bản tin 3 ngày gần nhất - tin đã gửi hôm trước không lặp lại.
_RECENT_KEY = "morning_news:recent_urls"
_RECENT_DAYS = 3
_RECENT_PER_DAY = 40  # /tintuc gọi nhiều lần trong ngày: vẫn có trần
_recent_lock = asyncio.Lock()
# {link feed: {"since": ngày ISO lỗi đầu, "alerted": bool}} - chỉ feed đang lỗi.
_FEED_HEALTH_KEY = "morning_news:feed_health"
_FEED_DEAD_DAYS = 3
# Feed còn trả lời nhưng tin mới nhất đã quá 7 ngày: coi như bị bỏ, tính là lỗi.
_STALE_FEED_HOURS = 7 * 24


def _feed_group(feed_url: str) -> str:
    low = feed_url.lower()
    for group, markers in _GROUP_MARKERS:
        if any(marker in low for marker in markers):
            return group
    return _DEFAULT_GROUP


def _is_tracking_param(name: str) -> bool:
    name = unquote_plus(name).lower()
    return name.startswith("utm_") or name in _TRACKING_PARAMS


def _url_key(url: str) -> str:
    """Khoá so trùng link: cùng 1 bài ở 2 feed (hoặc 2 ngày) có thể khác nhau ở tham
    số theo dõi (utm_*, fbclid, zarsrc...), "www.", "/" cuối, #fragment, http/https."""
    normalized = urls.normalize_url(url)
    if not normalized:
        return url.strip()
    parts = urlsplit(normalized)
    query = "&".join(
        part
        for part in parts.query.split("&")
        if part and not _is_tracking_param(part.partition("=")[0])
    )
    return urlunsplit(("https", parts.netloc.removeprefix("www."), parts.path, query, ""))


def _feed_source(feed_text: str, url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if host in _SOURCE_LABELS:
        return _SOURCE_LABELS[host]

    first = feed_text.splitlines()[0].strip() if feed_text.splitlines() else ""
    match = _FEED_TITLE_RE.match(first)
    return (match.group(1).strip() if match else host or url)[:80]


def _parse_feed_items(
    feed_text: str, feed_index: int, source: str, group: str = _DEFAULT_GROUP
) -> list[_NewsItem]:
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
        raw_title = header.group(2).strip()
        published = _PUBLISHED_SUFFIX_RE.search(raw_title)
        title = raw_title[: published.start()].strip() if published else raw_title
        url = next(
            (
                line
                for line in reversed(block_lines[1:])
                if line.startswith(("https://", "http://"))
            ),
            "",
        )
        if not url:
            continue
        summary = " ".join(
            line for line in block_lines[1:] if not line.startswith(("https://", "http://"))
        )
        items.append(
            _NewsItem(
                item_id=f"F{feed_index}I{ordinal}",
                title=title or "Tin đáng chú ý",
                url=url,
                source=source,
                summary=summary[:400],
                published=published.group(0).strip()[1:-1] if published else "",
                group=group,
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


def _build_prompt(feed_texts: list[str], now: datetime | None = None) -> str:
    annotated = [_annotate_feed(text, i) for i, text in enumerate(feed_texts, start=1)]
    combined = "\n\n--- NGUỒN KHÁC ---\n\n".join(annotated)
    # Nội dung RSS là dữ liệu ngoài: rào lại (core.untrusted) để câu lệnh cài
    # trong tiêu đề/tóm tắt tin không điều khiển được bản tin.
    fenced = untrusted.wrap_untrusted(combined, "tin RSS")
    return (
        f"{_today_line(now)} Tổng hợp các tin RSS dưới đây thành bản tin.\n"
        f"{_GROUNDING}\n{_STYLE}\n\n{fenced}"
    )


def _today_line(now: datetime | None) -> str:
    return f"Hôm nay là {(now or datetime.now(_VN_TZ)).strftime('%d/%m/%Y')} (giờ Việt Nam)."


def _selection_list(items: list[_NewsItem]) -> str:
    """Danh sách tin cho lượt chọn: dựng từ tin đã lọc (không dùng nguyên chữ feed)
    để tin trùng/tin đã gửi hôm trước không còn trong prompt."""
    blocks = []
    for item in items:
        meta = f"{item.source}, {item.published}" if item.published else item.source
        block = f"[ID:{item.item_id}] {item.title} ({meta})"
        blocks.append(f"{block}\n{item.summary}" if item.summary else block)
    return "\n\n".join(blocks)


async def _choose_items(items: list[_NewsItem]) -> list[_NewsItem]:
    """Lượt AI 1: chọn tin. Lỗi hoặc không ra ID hợp lệ thì chọn tất định."""
    fenced = untrusted.wrap_untrusted(_selection_list(items), "tin RSS")
    ids: list[str] = []
    try:
        # Việc phụ 1 dòng (danh sách ID): chuỗi tiện ích Groq -> Gemini, ít token, nhiệt độ
        # thấp - không chạy cả provider-chain chat (9Router trước, 4096 token).
        response = await orchestrator.ask_utility(
            f"{_today_line(None)} {_SELECT_TASK}\n\n{fenced}", max_tokens=200
        )
        _, ids = _extract_important_ids(getattr(response, "text", None) or "")
    except Exception:
        logger.warning("morning_news: lượt chọn tin lỗi, chọn tất định.", exc_info=True)
    return _select_important_items(items, ids)


async def _excerpt(item: _NewsItem) -> str:
    article = await article_reader.read_article(item.url)
    return " ".join(p for p in (article.sapo, *article.paragraphs) if p)[:_EXCERPT_CHARS]


async def _read_excerpts(items: list[_NewsItem]) -> dict[str, str]:
    """Phần đầu từng bài (item_id -> chữ); bài lỗi/quá giờ thì bỏ trống."""
    gate = asyncio.Semaphore(_EXCERPT_CONCURRENCY)

    async def one(item: _NewsItem) -> tuple[str, str]:
        async with gate:
            try:
                return item.item_id, await asyncio.wait_for(_excerpt(item), _EXCERPT_TIMEOUT_SEC)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("morning_news: không đọc được bài '%s': %r", item.url, exc)
                return item.item_id, ""

    return dict(await asyncio.gather(*(one(item) for item in items)))


def _build_summary_prompt(
    items: list[_NewsItem], excerpts: dict[str, str], now: datetime | None = None
) -> str:
    blocks = [
        f"Bài {number} - {item.title} ({item.source})\n"
        + (excerpts.get(item.item_id) or item.summary or "(chỉ có tiêu đề)")
        for number, item in enumerate(items, 1)
    ]
    fenced = untrusted.wrap_untrusted("\n\n".join(blocks), "bài báo")
    return f"{_today_line(now)} {_SUMMARY_TASK}\n{_GROUNDING}\n{_STYLE}\n\n{fenced}"


def _digits(text: str) -> set[str]:
    """Các con số trong câu, bỏ dấu chấm/phẩy phân cách: "4.060" thành "4060"."""
    return {re.sub(r"[.,]", "", number) for number in _NUMBER_RE.findall(text)}


def _drop_unsourced_numbers(body: str, feed_texts: list[str]) -> str:
    """Bỏ câu có con số không xuất hiện trong RSS (model tự thêm "VN-Index giảm 35 điểm"
    khi nguồn không có), rồi bỏ mục chỉ còn tiêu đề. Lời dặn thôi chưa đủ chặn."""
    known = _digits("\n".join(feed_texts))
    lines: list[str] = []
    for line in body.splitlines():
        sentences = _SENTENCE_SPLIT_RE.split(line)
        kept = [s for s in sentences if _digits(s) <= known]
        if len(kept) < len(sentences):
            logger.warning(
                "morning_news: bỏ câu có số không có trong RSS: %r",
                [s for s in sentences if s not in kept],
            )
            if not kept:
                continue
        lines.append(" ".join(kept))
    tidy: list[str] = []
    for i, line in enumerate(lines):
        is_header = bool(_SECTION_HEADER_RE.match("\n" + line))
        following = next((x for x in lines[i + 1 :] if x.strip()), "")
        if is_header and (not following or _SECTION_HEADER_RE.match("\n" + following)):
            continue  # mục rỗng sau khi bỏ câu
        tidy.append(line)
    return "\n".join(tidy).strip()


def _drop_invented_former(body: str, feed_texts: list[str]) -> str:
    """Model hay gọi người đương chức là "cựu ..." theo hiểu biết đã cũ (bản tin 28/10/2026
    viết "cựu Tổng thống Donald Trump"). Nguồn không có chữ "cựu" thì chữ đó là bịa."""
    if "cựu" in "\n".join(feed_texts).lower():
        return body
    body = re.sub(r"\bCựu\s+(\w)", lambda m: m.group(1).upper(), body)  # đầu câu
    return re.sub(r"\bcựu\s+", "", body)


def _tidy_sections(body: str) -> str:
    """Mỗi tiêu đề mục cách đoạn trước đúng 1 dòng trống (model hay quên)."""
    return _SECTION_HEADER_RE.sub("\n\n", body).strip()


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


def _select_important_items(
    all_items: list[_NewsItem], requested_ids: list[str]
) -> list[_NewsItem]:
    """Tin model chọn (đúng thứ tự model ghi), mỗi nhóm tối đa ``_MAX_PER_GROUP`` tin
    khi có từ 2 nhóm trở lên; còn chỗ thì nhóm chưa có tin nào được 1 tin (không đẩy
    tin model đã chọn ra); chưa đủ ``_MIN_ITEMS`` thì bù tất định, bỏ qua trần nhóm."""
    by_id = {item.item_id.upper(): item for item in all_items}
    selected: list[_NewsItem] = []
    seen_urls: set[str] = set()
    per_group: Counter[str] = Counter()
    group_cap = _MAX_PER_GROUP if len({item.group for item in all_items}) > 1 else _MAX_ITEMS

    def add(item: _NewsItem | None, capped: bool = True) -> None:
        if item is None or len(selected) >= _MAX_ITEMS:
            return
        key = _url_key(item.url)
        if key in seen_urls or (capped and per_group[item.group] >= group_cap):
            return
        seen_urls.add(key)
        per_group[item.group] += 1
        selected.append(item)

    for item_id in requested_ids:
        add(by_id.get(item_id.upper()))
    for item in all_items:
        if per_group[item.group] == 0:
            add(item)

    # Nếu model quên/ghi sai ID, vẫn bảo đảm cuối bài có link thật. Ưu tiên lấy
    # ít nhất một tin từ mỗi nguồn trước, sau đó mới bù theo thứ tự RSS.
    if len(selected) < _MIN_ITEMS:
        seen_sources: set[str] = set()
        for item in all_items:
            if item.source in seen_sources:
                continue
            add(item, capped=False)
            seen_sources.add(item.source)
            if len(selected) >= _MIN_ITEMS:
                break
    if len(selected) < _MIN_ITEMS:
        for item in all_items:
            add(item, capped=False)
            if len(selected) >= _MIN_ITEMS:
                break
    return selected


def _fresh_items(items: list[_NewsItem], sent_before: set[str]) -> list[_NewsItem]:
    """Bỏ link trùng giữa các feed (giữ bản ở feed đứng trước) và tin đã lên bản tin
    những ngày trước. Toàn tin đã gửi thì giữ cả (thà lặp còn hơn không có bản tin)."""
    unique: dict[str, _NewsItem] = {}
    for item in items:
        unique.setdefault(_url_key(item.url), item)
    fresh = [item for key, item in unique.items() if key not in sent_before]
    if len(fresh) < len(unique):
        logger.info(
            "morning_news: bỏ %d tin đã lên bản tin những ngày trước.", len(unique) - len(fresh)
        )
    return fresh or list(unique.values())


async def _load_recent() -> dict[str, list[str]]:
    try:
        data = json.loads(await db.get_setting(_RECENT_KEY) or "{}")
    except Exception:
        logger.warning("morning_news: không đọc được danh sách tin đã gửi.", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        day: [key for key in keys if isinstance(key, str)]
        for day, keys in data.items()
        if isinstance(day, str) and isinstance(keys, list)
    }


def _sent_before(recent: dict[str, list[str]], today: str) -> set[str]:
    return {key for day, keys in recent.items() if day < today for key in keys}


async def _save_recent(today: str, items: list[_NewsItem]) -> None:
    # Đọc-gộp-ghi dưới khoá: 2 lượt /tintuc cùng lúc (khoá lượt theo người dùng không
    # chặn nhau) không xoá link của nhau. App chạy 1 worker nên khoá trong tiến trình đủ.
    async with _recent_lock:
        recent = await _load_recent()
        today_keys = [*recent.get(today, []), *(_url_key(item.url) for item in items)]
        data = {today: list(dict.fromkeys(today_keys))[-_RECENT_PER_DAY:]}
        oldest = (date.fromisoformat(today) - timedelta(days=_RECENT_DAYS - 1)).isoformat()
        data.update({day: keys for day, keys in recent.items() if oldest <= day < today})
        try:
            await db.set_setting(_RECENT_KEY, json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.warning("morning_news: không lưu được danh sách tin đã gửi.", exc_info=True)


async def _track_feed_health(results: dict[str, str | None], now: datetime) -> None:
    """``results``: link feed -> None (đọc được) hoặc lỗi. Feed lỗi (hay không còn tin
    mới) ``_FEED_DEAD_DAYS`` ngày liền thì báo chủ bot 1 lần; đọc được 1 lần là xoá
    trạng thái. Chỉ lưu feed đang lỗi có trong cấu hình hiện tại. Không bao giờ raise."""
    today = now.date()
    try:
        raw = await db.get_setting(_FEED_HEALTH_KEY)
        state = json.loads(raw) if raw else {}
    except Exception:
        logger.warning("morning_news: không đọc được trạng thái feed.", exc_info=True)
        return
    if not isinstance(state, dict):
        state = {}
    new_state: dict[str, dict] = {}
    for url, error in results.items():
        if error is None:
            continue
        old = state.get(url) if isinstance(state.get(url), dict) else {}
        try:
            since = date.fromisoformat(str(old.get("since")))
        except ValueError:
            since = today
        alerted = bool(old.get("alerted"))
        days = (today - since).days + 1
        if days >= _FEED_DEAD_DAYS and not alerted:
            alerted = alerts.send(
                f"⚠️ Bản tin sáng: feed RSS lỗi {days} ngày liền (từ {since:%d/%m}).\n{url}\n"
                f"Lỗi gần nhất: {error}\n"
                "Kiểm tra lại link hoặc bỏ feed này khỏi MORNING_NEWS_RSS_FEEDS.",
                key=f"morning_news_feed:{url}",
            )
        new_state[url] = {"since": since.isoformat(), "alerted": alerted}
    if new_state == state:
        return
    try:
        await db.set_setting(_FEED_HEALTH_KEY, json.dumps(new_state, ensure_ascii=False))
    except Exception:
        logger.warning("morning_news: không lưu được trạng thái feed.", exc_info=True)


async def _read_feed(url: str) -> str:
    return await web_reader.read_rss(url, max_age_hours=_MAX_AGE_HOURS)


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
    for number, item in enumerate(items, 1):
        lines.append(f"{number}. {item.title}\n   {item.url}")
    lines.append('💬 Nhắn "đọc bài 1" (hoặc gửi link) để đọc nguyên bài ngay trong chat.')
    return "\n".join(lines)


_MORNING_HEADER = "☀️ BẢN TIN SÁNG — "


def without_header(content: str) -> str:
    """Bản tin sáng bỏ dòng tiêu đề - để gộp vào bản tin cá nhân đã có tiêu đề riêng."""
    if content.startswith(_MORNING_HEADER):
        return content.split("\n\n", 1)[1] if "\n\n" in content else ""
    return content


def _header(mode: str, now: datetime) -> str:
    if mode == "on_demand":
        return f"📰 ĐIỂM TIN — {now.strftime('%d/%m/%Y %H:%M')}"
    return f"{_MORNING_HEADER}{now.strftime('%d/%m/%Y')}"


async def build_digest(mode: str = "morning") -> str | None:
    """Đọc RSS và tổng hợp.

    ``mode='morning'`` dùng cho scheduler và ``/bangtinsang``.
    ``mode='on_demand'`` dùng cho ``/tintuc`` và chỉ khác tiêu đề; việc gửi
    đi đâu do caller quyết định.
    """
    feeds = config.MORNING_NEWS_RSS_FEEDS
    if not feeds:
        return None

    now = datetime.now(_VN_TZ)
    today = now.date().isoformat()
    feed_texts: list[str] = []
    all_items: list[_NewsItem] = []
    successful_sources: list[str] = []
    feed_errors: dict[str, str | None] = {}
    for url in feeds:
        try:
            text = await _read_feed(url)
            feed_texts.append(text)
            feed_index = len(feed_texts)
            source = _feed_source(text, url)
            successful_sources.append(source)
            all_items.extend(_parse_feed_items(text, feed_index, source, _feed_group(url)))
            feed_errors[url] = None
            logger.info("morning_news: đọc feed '%s' OK (%d ký tự).", url, len(text))
        except web_reader.FeedStaleError as exc:
            # Feed im (cuối tuần, Tết) chưa phải feed chết; im quá 7 ngày mới tính là lỗi.
            feed_errors[url] = str(exc) if exc.newest_age_hours > _STALE_FEED_HOURS else None
            logger.info("morning_news: feed '%s' không có tin mới: %s", url, exc)
        except web_reader.WebReaderError as exc:
            feed_errors[url] = str(exc)
            logger.warning("morning_news: lỗi đọc feed '%s': %s", url, exc)
    await _track_feed_health(feed_errors, now)

    if not feed_texts:
        logger.warning("morning_news: tất cả %d feed đều lỗi, không có gì để tổng hợp.", len(feeds))
        return None

    known_texts = list(feed_texts)  # chữ nguồn để soát số và chức danh trong bản AI viết
    if all_items:
        sent_before = _sent_before(await _load_recent(), today)
        important_items = await _choose_items(_fresh_items(all_items, sent_before))
        excerpts = await _read_excerpts(important_items)
        # Tải RSS + bóc HTML các bài (cả /tintuc): dọn heap khi xong (core/memtrim.py).
        memtrim.request("tin sáng")
        logger.info(
            "morning_news: đọc được %d/%d bài để tóm tắt.",
            sum(1 for text in excerpts.values() if text),
            len(important_items),
        )
        known_texts += [*excerpts.values(), " ".join(str(n) for n in range(1, 9))]  # "(bài N)"
        response = await orchestrator.ask(_build_summary_prompt(important_items, excerpts))
    else:
        # Feed lạ, không tách được tin có link: tổng hợp thẳng từ chữ RSS như trước.
        important_items = []
        response = await orchestrator.ask(_build_prompt(feed_texts))
    raw_body = (getattr(response, "text", None) or "").strip()
    if not raw_body:
        return None

    body, _ = _extract_important_ids(raw_body)
    body = _drop_invented_former(_drop_unsourced_numbers(body, known_texts), known_texts)
    body = _tidy_sections(body)
    if len(body) < _MIN_VALID_DIGEST_CHARS:
        logger.warning(
            "morning_news: model trả lời bất thường (%d ký tự: %r) dù có %d feed đọc được - bỏ, không gửi.",
            len(body),
            body,
            len(feed_texts),
        )
        return None

    if important_items:
        await article_reader.save_digest_links([(i.title, i.url) for i in important_items])
        await _save_recent(today, important_items)
        _remember_digest_items(now, important_items)
    parts = [_header(mode, now), body]
    sources = _format_sources(successful_sources)
    if sources:
        parts.append(sources)
    links = _format_important_links(important_items)
    if links:
        parts.append(links)
    return "\n\n".join(parts)


# ─── Dùng chung cho bản tin sáng cá nhân (services/morning_brief.py) ────────

HEADLINE_COUNT = 5
_HEADLINE_BUDGET_SEC = 45
# (ngày ISO, tin) của bản tin AI gần nhất - đúng danh sách "đọc bài N" đã lưu.
_digest_items: tuple[str, list[_NewsItem]] | None = None
# (ngày ISO, nội dung) bản tin sáng hôm nay: lượt 8h và bản tin cá nhân gộp tin
# dùng chung, tổng hợp AI tối đa 1 lần/ngày. Chỉ 1 chuỗi vài KB trong RAM.
_shared: tuple[str, str] | None = None
_shared_lock = asyncio.Lock()
# (ngày ISO, tin chọn tất định từ RSS khi chưa có bản tin AI)
_headlines: tuple[str, list[_NewsItem]] | None = None
_headlines_lock = asyncio.Lock()


def _remember_digest_items(now: datetime, items: list[_NewsItem]) -> None:
    global _digest_items
    _digest_items = (now.date().isoformat(), list(items[:8]))


def reset_shared_state() -> None:
    """Quên bản tin/tin đã giữ trong RAM (test và khi đổi ngày)."""
    global _digest_items, _shared, _headlines
    _digest_items = _shared = _headlines = None


async def shared_digest(now: datetime | None = None) -> str | None:
    """Bản tin sáng của hôm nay; tổng hợp (1 lượt AI) nếu hôm nay chưa có."""
    global _shared
    today = (now or datetime.now(_VN_TZ)).date().isoformat()
    async with _shared_lock:
        if _shared is not None and _shared[0] == today:
            return _shared[1]
        content = await build_digest()
        if content:
            _shared = (today, content)
        return content


async def _read_feed_items() -> list[_NewsItem]:
    """Mọi tin trong các feed RSS cấu hình (theo thứ tự feed), có trần thời gian."""
    deadline = time.monotonic() + _HEADLINE_BUDGET_SEC
    all_items: list[_NewsItem] = []
    for index, url in enumerate(config.MORNING_NEWS_RSS_FEEDS, start=1):
        if time.monotonic() > deadline:
            break
        try:
            text = await _read_feed(url)
        except web_reader.WebReaderError as exc:
            logger.warning("morning_news: tin cho bản tin cá nhân - lỗi feed '%s': %s", url, exc)
            continue
        all_items.extend(_parse_feed_items(text, index, _feed_source(text, url), _feed_group(url)))
    return all_items


async def _read_headlines() -> list[_NewsItem]:
    """Chọn tin tất định từ RSS (không AI): ưu tiên mỗi nguồn 1 tin."""
    return _select_important_items(await _read_feed_items(), [])[:HEADLINE_COUNT]


async def rss_headlines(limit: int = 10) -> list[_NewsItem]:
    """Tin mới nhất từ RSS lúc này cho màn hình thông tin của app
    (services/app_dashboard.py tự cache có hạn): lần lượt mỗi nguồn 1 tin theo
    thứ tự trong feed, bỏ link trùng. Không AI, không giữ trong RAM ở đây."""
    by_source: dict[str, list[_NewsItem]] = {}
    for item in await _read_feed_items():
        by_source.setdefault(item.source, []).append(item)
    picked: list[_NewsItem] = []
    seen: set[str] = set()
    queues = list(by_source.values())
    while queues and len(picked) < limit:
        for queue in list(queues):
            if not queue:
                queues.remove(queue)
                continue
            item = queue.pop(0)
            key = _url_key(item.url)
            if key not in seen:
                seen.add(key)
                picked.append(item)
            if len(picked) >= limit:
                break
    return picked


async def today_headlines(now: datetime | None = None) -> tuple[list[_NewsItem], bool]:
    """(tin hôm nay, có phải đúng danh sách "đọc bài N" của bản tin AI không).

    Không bao giờ ghi đè danh sách "đọc bài N" chung: danh sách đó chỉ do bản
    tin AI ghi (1 lần/ngày), tin chọn tất định đi kèm link để gửi lại đọc."""
    global _headlines
    today = (now or datetime.now(_VN_TZ)).date().isoformat()
    if _digest_items is not None and _digest_items[0] == today and _digest_items[1]:
        return list(_digest_items[1][:HEADLINE_COUNT]), True
    async with _headlines_lock:
        if _headlines is not None and _headlines[0] == today:
            return list(_headlines[1]), False
        items = await _read_headlines()
        if items:
            _headlines = (today, items)
        return items, False


def format_headlines(items: list[_NewsItem], from_digest: bool) -> str:
    lines = ["📰 TIN ĐÁNG CHÚ Ý"]
    for number, item in enumerate(items, 1):
        lines.append(f"{number}. {item.title} ({item.source})\n   {item.url}")
    lines.append(
        '💬 Nhắn "đọc bài 1" (hoặc gửi link) để đọc nguyên bài ngay trong chat.'
        if from_digest
        else "💬 Gửi lại link để em đọc nguyên bài ngay trong chat."
    )
    return "\n".join(lines)


async def _absorbed_channels() -> set[str]:
    """Kênh mà bản tin cá nhân đã mang phần tin hôm nay (lượt 8h bỏ qua)."""
    if not config.MORNING_BRIEF_ABSORB_NEWS:
        return set()
    from services import morning_brief

    try:
        return await asyncio.wait_for(morning_brief.absorbed_news_channels(), 15)
    except Exception:
        logger.warning("morning_news: không kiểm tra được bản tin cá nhân gộp tin.", exc_info=True)
        return set()


async def _send_to_zalo(content: str) -> bool:
    from channels import zalo_repository, zalo_session

    account_id = await zalo_repository.resolve_active_account_id()
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

    # /bangtinsang (force) vẫn gửi thẳng cả 2 kênh như cũ; lượt tự động bỏ qua
    # kênh mà bản tin cá nhân đã gộp phần tin (1 tin/ngày thay vì 2).
    absorbed = set() if force else await _absorbed_channels()
    if {"zalo", "zoom"} <= absorbed:
        logger.info("morning_news: Zalo lẫn Zoom đều nhận tin qua bản tin cá nhân, bỏ lượt 8h.")
        await _mark_sent(now)
        return RunResult(content=None, sent_zalo=False, sent_zoom=False)

    content = await build_digest() if force else await shared_digest(now)
    if not content:
        logger.info("morning_news: không có nội dung để gửi (thiếu feed hoặc tất cả feed lỗi).")
        return RunResult(content=None, sent_zalo=False, sent_zoom=False)

    sent_zalo = "zalo" not in absorbed and await _send_to_zalo(content)
    sent_zoom = "zoom" not in absorbed and await _send_to_zoom(content)
    if sent_zalo or sent_zoom or absorbed:
        await _mark_sent(now)
    else:
        logger.info(
            "morning_news: có nội dung nhưng chưa pair Zalo lẫn Zoom, chưa gửi được đâu cả."
        )
    return RunResult(content=content, sent_zalo=sent_zalo, sent_zoom=sent_zoom)


def _seconds_until_next_hour(hour: int) -> float:
    now = datetime.now(_VN_TZ)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# Lỡ giờ (Render ngủ/redeploy đúng 8h) thì gửi bù trong khung này; sau khung
# thì thôi (bản tin sáng gửi lúc 14h không còn là bản tin sáng).
CATCHUP_HOURS = 3
_POLL_SEC = 15 * 60


def is_due(now: datetime) -> bool:
    start = now.replace(hour=config.MORNING_NEWS_HOUR_VN, minute=0, second=0, microsecond=0)
    return start <= now < start + timedelta(hours=CATCHUP_HOURS)


async def _loop() -> None:
    while True:
        try:
            # run_once tự bỏ qua nếu hôm nay đã gửi (guard _LAST_SENT_KEY trong DB).
            if is_due(datetime.now(_VN_TZ)):
                await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("morning_news: lỗi khi tạo/gửi bản tin sáng.")
        await asyncio.sleep(_POLL_SEC)


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
