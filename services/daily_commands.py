"""Lệnh đời sống dùng chung mọi kênh, tính bằng code (không gọi AI):

- /vang [sjc|nhẫn|doji|pnj|bảo tín|thế giới] — giá vàng (vang.today, SJC dự phòng).
- /tygia [mã|số tiền mã] — tỷ giá Vietcombank, đổi tiền (/tygia 100 usd).
- /xang [xăng|dầu|diesel] — giá xăng dầu Petrolimex/PVOIL.
- /xoso [mb|mn|mt|tỉnh] [hôm nay|hôm qua|dd/mm|thứ] [số vé] — kết quả xổ số, dò vé;
  /xoso tinh — danh sách đài theo miền (đánh dấu đài quay hôm nay).
- /thoitiet [địa điểm] — thời tiết; /thoitiet nha <tỉnh> đặt thành phố mặc định
  riêng (/thoitiet nha xem, /thoitiet nha xoa).
- /bantin [on|off|giờ|tin on|off|xang on|off|xem] — bản tin sáng cá nhân.

Handler Telegram và channel_command_service (Zalo/Zoom) chỉ chuyển lệnh vào đây.
"""

from __future__ import annotations

import re

import datetime as dt

from core.text_normalize import fold
from core.text_utils import parse_on_off
from services import action_buttons, morning_brief, reply_audience, vn_daily_chat
from services import lottery_provinces as provinces
from services import weather_chat, weather_home
from services import vn_market_data as market

XOSO_USAGE = (
    "🎰 /xoso — kết quả xổ số (mặc định miền Nam, kỳ mới nhất). Ví dụ:\n"
    "/xoso mb · /xoso mt hôm qua · /xoso mn 25/10\n"
    "/xoso tây ninh · /xoso đà lạt chủ nhật · /xoso huế hôm nay · /xoso tinh (danh sách đài)\n"
    "/xoso 123456 (dò vé miền Nam) · /xoso mb 12345 (dò vé miền Bắc) · "
    "/xoso đồng nai 123456 (dò đúng đài)"
)
_PROVINCE_LIST_ARGS = {"tinh", "ds tinh", "danh sach", "dai", "ds", "lich", "lich quay", "list"}


def _today() -> dt.date:
    return vn_daily_chat._today()


# Câu do code viết dựng theo từng lượt: xưng hô theo người nhận (reply_audience).
def _source_down(what: str, source: str, example: str) -> str:
    you, me = reply_audience.forms()
    return (
        f"❌ {me.capitalize()} chưa lấy được {what} lúc này{source}. {you.capitalize()} nhắn "
        f'"{example}" để {me} tra web tham khảo nha.'
    )


async def vang(argument: str = "") -> str:
    try:
        return await vn_daily_chat.gold_text(f"giá vàng {argument}".strip())
    except market.MarketDataError:
        return _source_down(
            "giá vàng", " (nguồn vang.today và sjc.com.vn đều lỗi)", "giá vàng hôm nay"
        )


async def xang(argument: str = "") -> str:
    query = vn_daily_chat.parse_fuel(f"giá {argument or 'xăng dầu'} hôm nay")
    try:
        return await vn_daily_chat.fuel_text(query)
    except market.MarketDataError:
        return _source_down("giá xăng dầu", " (nguồn giaxanghomnay.com lỗi)", "giá xăng hôm nay")


async def tygia(argument: str) -> str:
    query = vn_daily_chat.parse_fx(f"tỷ giá {argument}".strip())
    try:
        return await vn_daily_chat.fx_text(query or vn_daily_chat.FxQuery())
    except market.MarketDataError:
        return _source_down("tỷ giá Vietcombank", "", "tỷ giá hôm nay")


def province_list(today: dt.date | None = None) -> str:
    """/xoso tinh: các đài theo miền, ⭐ = quay hôm nay (lịch cố định)."""
    today = today or _today()
    lines = [
        f"🎰 Đài xổ số theo miền (⭐ = quay hôm nay, {provinces.WEEKDAYS[today.weekday()]} "
        f"{today:%d/%m})"
    ]
    for region in provinces.REGION_ORDER:
        label = f"{provinces.REGION_LABELS[region]} ({provinces.DRAW_TIMES[region]})"
        if region == "mb":
            lines.append(f"\n{label}: quay hằng ngày - /xoso mb")
            continue
        items = []
        for p in provinces.in_region(region):
            days = "/".join(provinces.SHORT_WEEKDAYS[d] for d in p.weekdays)
            items.append(f"{'⭐ ' if p.draws_on(today) else ''}{p.name} ({days})")
        lines.append(f"\n{label}:\n" + ", ".join(items))
    lines.append(
        "\nGõ /xoso <tên đài> [hôm nay|hôm qua|dd/mm] [số vé], vd /xoso tây ninh, "
        "/xoso đà lạt 123456. Lịch có thể đổi dịp lễ Tết - kết quả luôn lấy từ nguồn."
    )
    return "\n".join(lines)


def today_hint(today: dt.date | None = None) -> str:
    """Dòng gợi ý cho Zalo/Zoom (Telegram có nút bấm thay thế)."""
    today = today or _today()
    names = [p.name for p in provinces.drawing_on(today) if p.region != "mb"]
    return (
        "👉 Xem riêng 1 đài: /xoso <tên tỉnh>"
        + (f" - hôm nay quay: {', '.join(names)}" if names else "")
        + " · /xoso tinh xem lịch các đài"
    )


def today_rows(today: dt.date | None = None) -> action_buttons.Rows:
    """Nút Telegram chọn đài quay hôm nay (callback "xs:<chỉ số đài>")."""
    return action_buttons.lottery_rows(
        [(p.name, p.index) for p in provinces.drawing_on(today or _today()) if p.region != "mb"]
    )


async def xoso(argument: str, channel: str = "telegram") -> str:
    folded = fold(argument.strip())
    if folded in {"help", "huong dan", "?"}:
        return XOSO_USAGE
    if folded in _PROVINCE_LIST_ARGS:
        return province_list()
    query = vn_daily_chat.parse_lottery(f"xổ số {argument}".strip())
    if query is None:  # không xảy ra: luôn có chữ "xổ số"
        return XOSO_USAGE
    try:
        text = await vn_daily_chat.lottery_text(query, argument)
    except market.MarketDataError:
        return vn_daily_chat.lottery_down()
    if not folded and channel != "telegram":
        text += "\n\n" + today_hint()
    return text


async def xoso_province(index: int) -> str:
    """Nút chọn đài (Telegram): kỳ mới nhất của đài thứ ``index`` trong danh mục."""
    province = provinces.by_index(index)
    if province is None:
        return "Đài này không còn trong danh mục, gõ /xoso tinh để xem lại nha."
    try:
        return await vn_daily_chat.province_text(province, None)
    except market.MarketDataError:
        return vn_daily_chat.lottery_down()


_HOME_CLEAR = {"xoa", "off", "bo", "huy", "tat"}
_HOME_SHOW = {"", "xem", "?"}


async def thoitiet(user_id: int, argument: str) -> str:
    argument = " ".join(argument.split())
    folded = fold(argument)
    you, me = reply_audience.forms()
    # "/thoitiet nha <tỉnh>" đặt thành phố riêng; "/thoitiet nha trang" vẫn là địa danh.
    if (folded == "nha" or folded.startswith("nha ")) and not re.match(r"nha trang\b", folded):
        rest = argument[3:].strip()
        if fold(rest) in _HOME_SHOW:
            home = await weather_home.get(user_id)
            return (
                f"📍 Thành phố mặc định của {you}: {home}."
                if home
                else f"📍 {you.capitalize()} chưa đặt thành phố riêng, {me} đang dùng TP.HCM. "
                "Đặt bằng /thoitiet nha <tỉnh>, vd /thoitiet nha Hà Nội."
            )
        if fold(rest) in _HOME_CLEAR:
            try:
                await weather_home.clear_home(user_id)
            except weather_home.HomeError as exc:
                return f"❌ {exc}"
            return f"🗑️ Đã xoá thành phố riêng, {me} quay về dùng TP.HCM nha."
        try:
            stored = await weather_home.set_home(user_id, rest)
        except weather_home.HomeError as exc:
            return f"❌ {exc}"
        return (
            f'📍 Dạ {me} nhớ rồi: thời tiết mặc định của {you} là {stored}. Hỏi "mai có mưa '
            f'không", "chiều nay mấy giờ mưa" là {me} xem đúng nơi {you} ở.'
        )
    question = f"thời tiết ở {argument}" if argument else "thời tiết hôm nay"
    reply = await weather_chat.answer(question, user_id=user_id)
    if reply is None:
        return (
            f"{me.capitalize()} không tìm thấy địa điểm '{argument}', {you} gõ rõ tên "
            f"tỉnh/thành giúp {me} nha."
        )
    return reply


def _brief_status(settings: morning_brief.BriefSettings, channel: str, home: str) -> str:
    state = f"ĐANG BẬT, gửi lúc {settings.hour}h" if settings.on else "ĐANG TẮT"
    sections = [
        f"thời tiết {home or 'TP.HCM (mặc định)'}",
        "âm lịch & ngày lễ",
        "ngày nhớ 7 ngày tới",
        "nhắc việc hôm nay",
        "vàng/tỷ giá",
        "danh mục (ngày giao dịch)",
        "chi tiêu tháng (nếu có ghi)",
    ]
    if settings.fuel:
        sections.append("giá xăng dầu")
    if settings.news_enabled(channel):
        sections.append("tin đáng chú ý")
    lines = [f"☀️ Bản tin sáng: {state}", "Gồm: " + ", ".join(sections) + "."]
    if channel == "telegram":
        lines.append("Bật trên Telegram thì bản tin thay luôn digest danh mục 8h.")
    lines.append(
        f"Lệnh: /bantin on|off · /bantin 6 (giờ {morning_brief.MIN_HOUR}-"
        f"{morning_brief.MAX_HOUR}h) · /bantin tin on|off · /bantin xang on|off (giá xăng) · "
        "/bantin xem (xem thử) · /thoitiet nha <tỉnh>"
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
    fuel_match = re.fullmatch(r"(?:xang|xang dau|gia xang)\s+(\S+)", arg)
    error = None
    if toggle is not None:
        error = await morning_brief.set_enabled(channel, user_id, toggle)
    elif hour_match:
        hour = int(hour_match.group(1))
        if not morning_brief.MIN_HOUR <= hour <= morning_brief.MAX_HOUR:
            you = reply_audience.forms()[0]
            return (
                f"Giờ gửi bản tin từ {morning_brief.MIN_HOUR}h tới {morning_brief.MAX_HOUR}h "
                f"{you} nha."
            )
        error = await morning_brief.set_enabled(channel, user_id, True, hour)
    elif news_match and parse_on_off(news_match.group(1)) is not None:
        await morning_brief.set_news(channel, user_id, bool(parse_on_off(news_match.group(1))))
    elif fuel_match and parse_on_off(fuel_match.group(1)) is not None:
        await morning_brief.set_fuel(channel, user_id, bool(parse_on_off(fuel_match.group(1))))
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
        return [await vang(argument)]
    if command == "/tygia":
        return [await tygia(argument)]
    if command == "/xang":
        return [await xang(argument)]
    if command == "/xoso":
        return [await xoso(argument, channel)]
    if command == "/thoitiet":
        return [await thoitiet(user_id, argument)]
    if command == "/bantin":
        return [await bantin(user_id, argument, channel)]
    return None
