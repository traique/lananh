"""Function calling (Bước 4 trong kế hoạch cải tiến).

Thay vì dùng cơ chế function-calling NATIVE của SDK google-genai (định nghĩa
FunctionDeclaration, model tự trả function_call trong response), module này
dùng lại `official_client.generate_utility_json()` (đã có sẵn cho services/memory_service.py)
để làm 1 "router" JSON riêng: hỏi Gemini xem tin nhắn có cần gọi tool nào
không, rồi tự thực thi tool tương ứng bằng Python thuần.

Lý do KHÔNG dùng function-calling native của SDK: cơ chế đó chỉ hoạt động ổn
định ở nhánh api1/api2 (Google AI Studio SDK chính thức) - nhánh router9
(gateway OpenAI-compatible bên thứ ba) không đảm bảo hỗ trợ tương đương, trong
khi provider-chain mặc định vẫn ưu tiên router9 (xem core.config.PROVIDER_ORDER).
Nếu dùng native function-calling, chat qua router9 sẽ hoàn toàn không có tool
nào cả, tạo ra 2 hành vi khác nhau tuỳ provider đang active. Cách "router
JSON riêng" ở đây chạy qua generate_utility_json() - vốn LUÔN gọi thẳng API
chính thức bất kể provider-chain đang active cái gì - nên hoạt động NHẤT
QUÁN dù chat chính đang trả lời qua router9 hay qua api1/api2.

Đánh đổi: thêm 1 lượt gọi API phụ cho tin nhắn có khả năng là lệnh tool (lọc
từ khóa rẻ ở maybe_run_tool() đã chặn phần lớn tin phiếm; chỉ khi có API key
cấu hình - nếu không, maybe_run_tool() trả None êm, tool tự tắt, chat chính
không ảnh hưởng) - chấp nhận được vì đây là bot phục vụ 1 người dùng, tần
suất thấp.

CHỦ Ý giữ nguyên `stock_analysis.wants_full_analysis()` / `wants_price_quote()`
(nhận diện hỏi giá/phân tích cổ phiếu) bằng keyword như cũ, KHÔNG gộp vào
router này: đó là fast-path lấy giá REALTIME thật từ DNSE, cố tình
deterministic (không phụ thuộc 1 lượt gọi LLM có thể lỗi/trả JSON sai định
dạng) - đổi sang function-calling sẽ đánh đổi độ tin cậy của 1 tính năng đã
chạy tốt để lấy sự "gọn" không thật sự cần thiết ở đây.
"""

import asyncio
import logging
from datetime import date as date_cls, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from stock import analysis as stock_analysis
from core import database as db
from services import lunar, weather
from services.reminder_delivery import current_target

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

_ROUTER_INSTRUCTION = """Bạn là bộ định tuyến tool (function router) nội bộ cho 1 trợ lý cá nhân qua
Telegram. Đọc tin nhắn của người dùng và quyết định có cần gọi 1 trong các
tool sau không. Trả về DUY NHẤT 1 object JSON (không markdown, không code
fence, không thêm chữ nào khác) đúng định dạng:

{"tool": "ten_tool_hoac_none", "args": {...}}

Danh sách tool khả dụng:
- "save_note": lưu 1 ghi chú tự do. args: {"content": "nội dung cần ghi nhớ"}.
  Dùng khi người dùng RÕ RÀNG yêu cầu ghi/ghi chú/note lại điều gì đó (vd
  "ghi chú giúp anh...", "note lại...", "nhớ giúp anh là...").
- "list_notes": xem lại các ghi chú gần đây. args: {} (rỗng). Dùng khi người
  dùng hỏi "em ghi chú gì rồi", "xem lại note của anh"...
- "set_reminder": đặt nhắc việc. args: {"message": "nội dung cần nhắc",
  "minutes_from_now": số phút kể từ bây giờ tới lúc cần nhắc (số, có thể là
  số thập phân)}. Dùng khi người dùng RÕ RÀNG yêu cầu nhắc việc/nhắc nhở vào
  1 thời điểm hoặc sau 1 khoảng thời gian (vd "nhắc anh 30 phút nữa uống
  thuốc", "8 giờ tối nay nhắc anh gọi điện cho mẹ"). Tự tính minutes_from_now
  dựa vào thời gian hiện tại được cung cấp bên dưới.
- "get_portfolio": xem lại danh mục đầu tư đã ghi nhận trong trí nhớ dài hạn.
  args: {}. Dùng khi người dùng hỏi "danh mục của anh gồm những gì", "em nhớ
  anh đang giữ mã nào không"...
- "get_weather": tra thời tiết thật (hiện tại + dự báo). args: {"location":
  "tên địa danh người dùng nói, để rỗng nếu không nói địa điểm", "days": số
  ngày dự báo TÍNH CẢ HÔM NAY (1 = hôm nay, 2 = tới ngày mai, cuối tuần thì
  tính tới Chủ Nhật; tối đa 7)}. Dùng khi hỏi thời tiết, mưa/nắng, nhiệt độ,
  có nên mang áo mưa... KHÔNG tự điền địa điểm khi người dùng không nói.
- "lunar_calendar": tra âm lịch. Hai dạng args:
  (a) {"date": "YYYY-MM-DD"} - ngày DƯƠNG cần xem âm lịch, can chi, giờ hoàng
  đạo, tiết khí, số ngày tới Tết ("" = hôm nay). Vd "hôm nay âm bao nhiêu",
  "20/11 là ngày mấy âm", "còn mấy ngày nữa Tết".
  (b) {"lunar_day": số, "lunar_month": số, "lunar_year": số năm âm hoặc bỏ
  trống nếu người dùng không nói năm, "leap": true nếu nói rõ tháng nhuận} -
  đổi ngày ÂM sang dương. Vd "rằm tháng 8 là ngày nào" -> lunar_day 15,
  lunar_month 8; "mùng 1 Tết" -> lunar_day 1, lunar_month 1.
- "none": tin nhắn KHÔNG cần gọi tool nào (chuyện phiếm, hỏi đáp thông
  thường, hỏi giá/phân tích cổ phiếu cụ thể - các trường hợp đó đã được xử
  lý riêng, không thuộc phạm vi router này). args: {}.

Quy tắc:
- CHỈ chọn 1 tool khi người dùng có ý định RÕ RÀNG khớp mô tả trên. Nếu
  không chắc hoặc chỉ là nhắc tới liên quan mơ hồ, chọn "none".
- KHÔNG tự bịa nội dung cho "content"/"message" - phải lấy đúng từ ý người
  dùng đã nói, có thể viết lại ngắn gọn hơn nhưng không thêm thông tin mới.
"""


async def _tool_save_note(user_id: int, content: str = "") -> str:
    content = (content or "").strip()
    if not content:
        return "Không có nội dung để ghi chú."
    await db.add_note(user_id, content, event_key=current_target(user_id).event_key)
    return f'Đã ghi chú: "{content}"'


async def _tool_list_notes(user_id: int) -> str:
    notes = await db.get_notes(user_id, limit=10)
    if not notes:
        return "Chưa có ghi chú nào được lưu."
    lines = [f"- {content} (lúc {created_at:%H:%M %d/%m})" for content, created_at in notes]
    return "Các ghi chú gần đây:\n" + "\n".join(lines)


async def _tool_set_reminder(user_id: int, message: str = "", minutes_from_now: float = 0) -> str:
    message = (message or "").strip() or "(không có nội dung cụ thể)"
    try:
        minutes = float(minutes_from_now)
    except (TypeError, ValueError):
        minutes = 0.0
    if minutes <= 0:
        minutes = 1.0  # Tối thiểu 1 phút, tránh due_at <= now() bị bỏ qua ở lần quét đầu.

    due_at = datetime.now(_VN_TZ) + timedelta(minutes=minutes)
    target = current_target(user_id)
    stored_due_at = await db.add_reminder(
        user_id,
        message,
        due_at,
        channel=target.channel,
        recipient_id=target.recipient_id,
        account_id=target.account_id,
        user_jid=target.user_jid,
        event_key=target.event_key,
    )
    due_at = (stored_due_at or due_at).astimezone(_VN_TZ)
    return f"Đã đặt nhắc việc lúc {due_at:%H:%M %d/%m} (giờ VN): {message}"


# Open-Meteo/wttr.in tự có timeout từng request; chặn thêm tổng thời gian để
# lượt chat không phải chờ geocode + fallback cộng dồn.
_WEATHER_TIMEOUT_SEC = 20


async def _tool_get_weather(user_id: int, location: str = "", days: int = 1, **_ignored) -> str:
    try:
        data = await asyncio.wait_for(
            weather.get_weather(str(location or ""), days), _WEATHER_TIMEOUT_SEC
        )
    except (weather.WeatherError, TimeoutError):
        logger.warning("Tra thời tiết lỗi (location=%r)", location, exc_info=True)
        return (
            "Không lấy được dữ liệu thời tiết lúc này (Open-Meteo và wttr.in đều lỗi). "
            "Báo thật với người dùng, KHÔNG tự bịa số liệu thời tiết."
        )
    if data.get("error"):
        return f"{data['error']}. Hỏi lại người dùng tên tỉnh/thành phố cụ thể hơn."
    return (
        "Số liệu thời tiết thật vừa tra (dùng đúng các số này, không lấy số liệu thời "
        "tiết từ kết quả web nếu có):\n" + weather.describe_weather(data)
    )


def _parse_solar_date(raw: str) -> date_cls | None:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _tool_lunar_calendar(
    user_id: int,
    date: str = "",
    lunar_day=None,
    lunar_month=None,
    lunar_year=None,
    leap=False,
    **_ignored,
) -> str:
    today = datetime.now(_VN_TZ).date()
    ld, lm = _as_int(lunar_day), _as_int(lunar_month)
    if ld is None and lm is None:
        target = _parse_solar_date(str(date or "")) if date else today
        if target is None or not 1900 <= target.year <= 2100:
            return f"Ngày dương lịch '{date}' không hợp lệ (cần trong khoảng 1900-2100)."
        return "Thông tin lịch (tính offline, chính xác):\n" + lunar.describe_date(
            lunar.date_info(target)
        )

    if ld is None or lm is None:
        return "Ngày/tháng âm lịch không hợp lệ (ngày 1-30, tháng 1-12)."
    is_leap = leap is True or str(leap).strip().lower() in ("1", "true")
    day, year, error = lunar.find_lunar_date(ld, lm, _as_int(lunar_year), is_leap, today)
    if day is None:
        return error
    label = lunar.lunar_text(ld, lm, year, is_leap)
    return (
        f"{label[0].upper()}{label[1:]} âm lịch rơi vào (tính offline, chính xác):\n"
        + lunar.describe_date(lunar.date_info(day))
    )


async def _tool_get_portfolio(user_id: int) -> str:
    facts = await db.get_facts(user_id)
    # Tiêu chí "fact nào thuộc danh mục" lấy từ stock_analysis để chỉ có 1
    # nguồn duy nhất, thay vì copy cứng tuple từ khoá như trước đây.
    portfolio_facts = [f"{k}: {v}" for k, v in facts if stock_analysis.is_portfolio_fact(k)]
    if not portfolio_facts:
        return "Chưa ghi nhận danh mục đầu tư nào trong trí nhớ dài hạn."
    return "Danh mục đầu tư đã ghi nhận trong trí nhớ:\n" + "\n".join(portfolio_facts)


_HANDLERS = {
    "save_note": _tool_save_note,
    "list_notes": _tool_list_notes,
    "set_reminder": _tool_set_reminder,
    "get_portfolio": _tool_get_portfolio,
    "get_weather": _tool_get_weather,
    "lunar_calendar": _tool_lunar_calendar,
}


def _build_router_prompt(user_text: str) -> str:
    now = datetime.now(_VN_TZ)
    return (
        f"{_ROUTER_INSTRUCTION}\n\n"
        f"[Thời điểm hiện tại: {now:%H:%M} {lunar.WEEKDAYS[now.weekday()]} ngày "
        f"{now:%d/%m/%Y}, giờ Việt Nam]\n\n"
        f"### Tin nhắn của người dùng\n{user_text}\n\n"
        "Trả về JSON theo đúng định dạng đã mô tả, không thêm gì khác."
    )


# Bộ lọc rẻ trước khi gọi LLM: các tool đều dùng những từ này khi người dùng
# diễn đạt ý định thật (ghi chú/nhắc việc/xem danh mục/thời tiết/âm lịch), nên
# tin nhắn không khớp từ nào coi như không cần tool - tiết kiệm 1 lượt gọi utility API cho
# phần lớn tin chat phiếm. Đánh đổi: bỏ lỡ câu lệnh diễn đạt lạ hoàn toàn
# không chứa từ khóa nào ở dưới.
_TOOL_INTENT_KEYWORDS = (
    "ghi chú",
    "ghi chu",
    "ghi lại",
    "ghi lai",
    "note",
    "nhắc",
    "nhac",
    "nhớ",
    "nho",
    "lưu lại",
    "luu lai",
    "danh mục",
    "danh muc",
    "đang giữ",
    "portfolio",
    "đặt lịch",
    "dat lich",
    "hẹn giờ",
    "hen gio",
    "remind",
    "todo",
    "thời tiết",
    "thoi tiet",
    "mưa",
    "nắng",
    "nhiệt độ",
    "nhiet do",
    "dự báo",
    "du bao",
    "bão",
    "âm lịch",
    "am lich",
    "lịch âm",
    "ngày âm",
    "ngay am",
    "tết",
    "rằm",
    "mùng",
    "can chi",
    "hoàng đạo",
    "tiết khí",
)


def _looks_like_tool_intent(user_text: str) -> bool:
    lower = user_text.lower()
    # Câu web_search đã bỏ qua vì là hỏi thời tiết thì router PHẢI chạy, nếu
    # không lượt chat chẳng có số liệu nào.
    return any(kw in lower for kw in _TOOL_INTENT_KEYWORDS) or (
        weather.looks_like_weather_question(lower)
    )


async def maybe_run_tool(user_id: int, user_text: str) -> Optional[str]:
    """Hỏi Gemini xem tin nhắn này có cần gọi tool nào không; nếu có, chạy
    tool và trả về text kết quả (để chèn làm `grounding` cho ai.orchestrator.chat(),
    giúp Gemini biết tool đã chạy và kết quả ra sao khi soạn câu trả lời tự
    nhiên). Trả về None nếu không cần tool, hoặc lỗi/chưa cấu hình API key
    (graceful - KHÔNG được raise, không được làm gián đoạn chat chính)."""
    if not _looks_like_tool_intent(user_text):
        return None
    # Khởi tạo TRƯỚC block try: nếu chính generate_utility_json() raise
    # TypeError thì block `except TypeError` bên dưới vẫn cần đọc được 2 biến
    # này. Nếu để chúng chỉ được gán bên trong try, except sẽ ném
    # UnboundLocalError và lỗi gốc biến mất hoàn toàn (không log, không traceback).
    tool_name = "?"
    args: dict = {}
    try:
        from ai import orchestrator

        data = await orchestrator.utility_json(_build_router_prompt(user_text))
        if not data:
            return None

        tool_name = str(data.get("tool", "none")).strip()
        if tool_name == "none" or tool_name not in _HANDLERS:
            return None

        args = data.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        handler = _HANDLERS[tool_name]
        result = await handler(user_id, **args)
        logger.info("Function calling: đã chạy tool '%s' cho user_id=%s", tool_name, user_id)
        return f"[Kết quả tool '{tool_name}' vừa chạy - dùng thông tin này để trả lời tự nhiên]\n{result}"
    except TypeError:
        logger.warning(
            "Lỗi TypeError khi chạy tool '%s' (có thể do Gemini truyền sai args). Args: %s",
            tool_name,
            args,
            exc_info=True,
        )
        return None
    except Exception:
        logger.warning(
            "Lỗi khi chạy function-calling router (bỏ qua, chat chính vẫn tiếp tục bình thường).",
            exc_info=True,
        )
        return None
