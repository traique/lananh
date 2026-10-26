"""Lệnh đời sống dùng chung mọi kênh, tính bằng code (không gọi AI):

- /vang — giá vàng SJC.
- /tygia [mã|số tiền mã] — tỷ giá Vietcombank, đổi tiền (/tygia 100 usd).
- /xoso [mb|mn|mt|tỉnh] [hôm qua|dd/mm] [số vé] — kết quả xổ số, dò vé.
- /thoitiet [địa điểm] — thời tiết; /thoitiet nha <tỉnh> đặt thành phố mặc định
  riêng (/thoitiet nha xem, /thoitiet nha xoa).
- /bantin [on|off|giờ|tin on|off|xem] — bản tin sáng cá nhân.

Handler Telegram và channel_command_service (Zalo/Zoom) chỉ chuyển lệnh vào đây.
"""

from __future__ import annotations

import re

from core.text_normalize import fold
from core.text_utils import parse_on_off
from services import morning_brief, reply_audience, vn_daily_chat, weather_chat, weather_home
from services import vn_market_data as market

XOSO_USAGE = (
    "🎰 /xoso — kết quả xổ số (mặc định miền Nam, kỳ mới nhất). Ví dụ:\n"
    "/xoso mb · /xoso mt hôm qua · /xoso tiền giang · /xoso mn 25/10\n"
    "/xoso 123456 (dò vé miền Nam) · /xoso mb 12345 (dò vé miền Bắc)"
)
_GOLD_DOWN = (
    "❌ Em chưa lấy được giá vàng SJC lúc này (nguồn sjc.com.vn lỗi). Anh nhắn "
    '"giá vàng hôm nay" để em tra web tham khảo nha.'
)
_FX_DOWN = (
    "❌ Em chưa lấy được tỷ giá Vietcombank lúc này. Anh nhắn "
    '"tỷ giá hôm nay" để em tra web tham khảo nha.'
)
_LOTTERY_DOWN = (
    "❌ Em chưa lấy được kết quả xổ số lúc này (nguồn xskt.com.vn lỗi). Em không "
    "đoán số đâu ạ - anh thử lại sau ít phút nha."
)


async def vang() -> str:
    try:
        return await vn_daily_chat.gold_text()
    except market.MarketDataError:
        return _GOLD_DOWN


async def tygia(argument: str) -> str:
    query = vn_daily_chat.parse_fx(f"tỷ giá {argument}".strip())
    try:
        return await vn_daily_chat.fx_text(query or vn_daily_chat.FxQuery())
    except market.MarketDataError:
        return _FX_DOWN


async def xoso(argument: str) -> str:
    if fold(argument.strip()) in {"help", "huong dan", "?"}:
        return XOSO_USAGE
    query = vn_daily_chat.parse_lottery(f"xổ số {argument}".strip())
    if query is None:  # không xảy ra: luôn có chữ "xổ số"
        return XOSO_USAGE
    try:
        return await vn_daily_chat.lottery_text(query, argument)
    except market.MarketDataError:
        return _LOTTERY_DOWN


_HOME_CLEAR = {"xoa", "off", "bo", "huy", "tat"}
_HOME_SHOW = {"", "xem", "?"}


async def thoitiet(user_id: int, argument: str) -> str:
    argument = " ".join(argument.split())
    folded = fold(argument)
    # "/thoitiet nha <tỉnh>" đặt thành phố riêng; "/thoitiet nha trang" vẫn là địa danh.
    if (folded == "nha" or folded.startswith("nha ")) and not re.match(r"nha trang\b", folded):
        rest = argument[3:].strip()
        if fold(rest) in _HOME_SHOW:
            home = await weather_home.get(user_id)
            return (
                f"📍 Thành phố mặc định của anh: {home}."
                if home
                else "📍 Anh chưa đặt thành phố riêng, em đang dùng TP.HCM. "
                "Đặt bằng /thoitiet nha <tỉnh>, vd /thoitiet nha Hà Nội."
            )
        if fold(rest) in _HOME_CLEAR:
            try:
                await weather_home.clear_home(user_id)
            except weather_home.HomeError as exc:
                return f"❌ {exc}"
            return "🗑️ Đã xoá thành phố riêng, em quay về dùng TP.HCM nha."
        try:
            stored = await weather_home.set_home(user_id, rest)
        except weather_home.HomeError as exc:
            return f"❌ {exc}"
        return (
            f'📍 Dạ em nhớ rồi: thời tiết mặc định của anh là {stored}. Hỏi "mai có mưa '
            'không", "chiều nay mấy giờ mưa" là em xem đúng nơi anh ở.'
        )
    question = f"thời tiết ở {argument}" if argument else "thời tiết hôm nay"
    reply = await weather_chat.answer(question, user_id=user_id)
    if reply is None:
        return f"Em không tìm thấy địa điểm '{argument}', anh gõ rõ tên tỉnh/thành giúp em nha."
    return reply


def _brief_status(settings: morning_brief.BriefSettings, channel: str, home: str) -> str:
    state = f"ĐANG BẬT, gửi lúc {settings.hour}h" if settings.on else "ĐANG TẮT"
    sections = [
        f"thời tiết {home or 'TP.HCM (mặc định)'}",
        "âm lịch & ngày lễ",
        "nhắc việc hôm nay",
        "vàng/tỷ giá",
        "danh mục (ngày giao dịch)",
    ]
    if settings.news_enabled(channel):
        sections.append("tin đáng chú ý")
    lines = [f"☀️ Bản tin sáng: {state}", "Gồm: " + ", ".join(sections) + "."]
    if channel == "telegram":
        lines.append("Bật trên Telegram thì bản tin thay luôn digest danh mục 8h.")
    lines.append(
        f"Lệnh: /bantin on|off · /bantin 6 (giờ {morning_brief.MIN_HOUR}-"
        f"{morning_brief.MAX_HOUR}h) · /bantin tin on|off · /bantin xem (xem thử) · "
        "/thoitiet nha <tỉnh>"
    )
    return "\n".join(lines)


async def bantin(user_id: int, argument: str, channel: str) -> str:
    arg = fold(" ".join(argument.split()))
    if arg in {"xem", "thu", "xem thu", "test"}:
        return await morning_brief.build(
            user_id, channel, preview=True, address_form=reply_audience.current().address_form
        )
    toggle = parse_on_off(arg)
    hour_match = re.fullmatch(r"(\d{1,2})\s*(?:h|g|gio|:00)?", arg)
    news_match = re.fullmatch(r"tin\s+(\S+)", arg)
    error = None
    if toggle is not None:
        error = await morning_brief.set_enabled(channel, user_id, toggle)
    elif hour_match:
        hour = int(hour_match.group(1))
        if not morning_brief.MIN_HOUR <= hour <= morning_brief.MAX_HOUR:
            return f"Giờ gửi bản tin từ {morning_brief.MIN_HOUR}h tới {morning_brief.MAX_HOUR}h anh nha."
        error = await morning_brief.set_enabled(channel, user_id, True, hour)
    elif news_match and parse_on_off(news_match.group(1)) is not None:
        await morning_brief.set_news(channel, user_id, bool(parse_on_off(news_match.group(1))))
    elif arg:
        return _brief_status(await morning_brief.get_settings(channel, user_id), channel, "").split(
            "\n"
        )[-1]
    if error:
        return f"❌ {error}"
    settings = await morning_brief.get_settings(channel, user_id)
    return _brief_status(settings, channel, await weather_home.get(user_id))


async def handle(command: str, argument: str, user_id: int, channel: str) -> list[str] | None:
    """Kết quả 1 lệnh đời sống, None nếu không phải lệnh của module này."""
    if command == "/vang":
        return [await vang()]
    if command == "/tygia":
        return [await tygia(argument)]
    if command == "/xoso":
        return [await xoso(argument)]
    if command == "/thoitiet":
        return [await thoitiet(user_id, argument)]
    if command == "/bantin":
        return [await bantin(user_id, argument, channel)]
    return None
