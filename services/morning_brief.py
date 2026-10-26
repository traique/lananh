"""Bản tin sáng CÁ NHÂN: mỗi người tối đa 1 tin/ngày, trên đúng kênh của họ.

Mục (section) đăng ký qua ``register_section`` - module khác (nhắc việc v2,
ngày nhớ, lịch...) thêm mục mới mà không phải sửa file này::

    from services import morning_brief

    @morning_brief.register_section("ngay_nho", order=35)
    async def _ngay_nho(ctx: morning_brief.BriefContext) -> str | None:
        ...  # trả None để bỏ mục

(module đăng ký phải được import lúc khởi động, vd từ bot_app). Mục có sẵn đều
tính bằng code, không gọi AI: thời tiết thành phố riêng (mưa theo giờ, cảnh báo
AQI > 100), âm lịch + lễ/rằm/mùng 1 sắp tới, nhắc việc còn lại hôm nay, vàng SJC
+ USD Vietcombank, danh mục (chỉ ngày giao dịch, build_report(digest=True)), tin.

Ai nhận: người tự bật ``/bantin`` trên kênh của mình (mặc định không ai nhận).
- Telegram chủ bot: thay digest danh mục 8h của scheduler (không gửi 2 tin);
  tin RSS TẮT mặc định - chủ bot đã chọn không nhận tin RSS qua Telegram.
- Zoom chủ bot: dùng chung dữ liệu chủ bot; bật thì gộp luôn bản tin RSS 8h.
- Zalo: chỉ dữ liệu theo internal_user_id của chính người đó (kể cả Zalo của
  chủ bot - tách hẳn Telegram). Controller bật thì gộp bản tin RSS 8h; thành
  viên nhận tin chọn tất định (không AI). Tối đa MORNING_BRIEF_MAX_MEMBERS.

Gửi: vòng lặp 15 phút, khung gửi bù 3 giờ như morning_news; ``brief_sent:*``
lưu ngày đã gửi. Bật services/notify_hub (mặc định) thì bản tin đi qua hàng đợi
gom: ngoài giờ yên lặng xả ngay (kèm tin NORMAL đang chờ), trong giờ yên lặng
(vd /bantin 5 khi yên lặng tới 6:30) chờ hết giờ mới gửi. Đường cũ (hub tắt):
Zalo đánh dấu TRƯỚC khi đưa vào outbox (INSERT không idempotent): lỡ lỗi giữa
chừng thì mất 1 bản tin chứ không gửi đôi. Chạy nền
ngoài khoá lượt; từng người lần lượt (RAM), mỗi mục có timeout riêng.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, replace
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from core import address, config, trace
from core import database as db
from services import personal_settings, reply_audience, weather_home

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
CHANNELS = ("telegram", "zalo", "zoom")
CATCHUP_HOURS = 3
MIN_HOUR, MAX_HOUR = 4, 12
_POLL_SEC = 15 * 60
_LIST_LIMIT = 200
_AI_INTRO_TIMEOUT_SEC = 45
_AI_INTRO_MAX_CHARS = 1500
_notify_callback: Callable[[int, str], Awaitable[None]] | None = None
_task: asyncio.Task | None = None


def set_notify_callback(fn: Callable[[int, str], Awaitable[None]]) -> None:
    """Hàm gửi tin Telegram chủ động (bot_app đăng ký, giống scheduler)."""
    global _notify_callback
    _notify_callback = fn


# ─── Cấu hình từng người (bảng settings) ────────────────────────────────────


@dataclass(frozen=True)
class BriefSettings:
    on: bool = False
    hour: int = 7
    news: bool | None = None  # None = theo mặc định kênh

    def news_enabled(self, channel: str) -> bool:
        # Telegram: tin RSS tắt mặc định (lựa chọn của chủ bot), bật bằng /bantin tin on.
        return self.news if self.news is not None else channel != "telegram"


def parse_settings(raw: str | None) -> BriefSettings:
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    hour = data.get("hour")
    if not isinstance(hour, int) or not MIN_HOUR <= hour <= MAX_HOUR:
        hour = config.MORNING_BRIEF_DEFAULT_HOUR
    news = data.get("news")
    return BriefSettings(
        on=data.get("on") is True, hour=hour, news=news if isinstance(news, bool) else None
    )


def dump_settings(settings: BriefSettings) -> str:
    return json.dumps({"on": settings.on, "hour": settings.hour, "news": settings.news})


async def get_settings(channel: str, user_id: int) -> BriefSettings:
    return parse_settings(await db.get_setting(personal_settings.brief_key(channel, user_id)))


async def save_settings(channel: str, user_id: int, settings: BriefSettings) -> None:
    await db.set_setting(personal_settings.brief_key(channel, user_id), dump_settings(settings))


@dataclass(frozen=True)
class BriefTarget:
    channel: str
    user_id: int
    settings: BriefSettings


async def list_targets() -> list[BriefTarget]:
    """Những người đang bật bản tin (Zalo cắt ở MORNING_BRIEF_MAX_MEMBERS)."""
    rows: list[tuple[str, str]] = []
    # Đọc theo từng kênh: nhiều dòng Zalo đã tắt không được đẩy Telegram/Zoom
    # của chủ bot ra khỏi trần LIMIT.
    for channel in CHANNELS:
        prefix = f"{personal_settings.BRIEF_PREFIX}{channel}:"
        rows.extend(await db.list_settings_with_prefix(prefix, _LIST_LIMIT))
    targets: list[BriefTarget] = []
    members = 0
    for key, value in rows:
        parts = key.split(":")
        if len(parts) != 3 or parts[1] not in CHANNELS:
            continue
        try:
            user_id = int(parts[2])
        except ValueError:
            continue
        settings = parse_settings(value)
        if not settings.on:
            continue
        if parts[1] == "zalo":
            members += 1
            if members > config.MORNING_BRIEF_MAX_MEMBERS:
                continue
        targets.append(BriefTarget(parts[1], user_id, settings))
    return targets


async def set_enabled(channel: str, user_id: int, on: bool, hour: int | None = None) -> str | None:
    """Bật/tắt (kèm giờ). Trả câu báo lỗi, None nếu đã lưu."""
    if channel not in CHANNELS:
        return "Kênh này chưa hỗ trợ bản tin sáng."
    if channel in ("telegram", "zoom") and not config.is_owner_user_id(user_id):
        return "Bản tin sáng trên kênh này chỉ dành cho chủ bot."
    current = await get_settings(channel, user_id)
    if on and channel == "zalo" and not current.on:
        if config.MORNING_BRIEF_MAX_MEMBERS <= 0:
            return "Chủ bot chưa mở bản tin sáng cho thành viên Zalo."
        others = [t for t in await list_targets() if t.channel == "zalo"]
        if len(others) >= config.MORNING_BRIEF_MAX_MEMBERS:
            you, me = reply_audience.forms()
            return (
                f"Bản tin sáng đã đủ {config.MORNING_BRIEF_MAX_MEMBERS} người nhận, "
                f"{you} nhờ chủ bot nới giới hạn giúp {me} nha."
            )
    updated = replace(current, on=on, hour=current.hour if hour is None else hour)
    await save_settings(channel, user_id, updated)
    return None


async def set_news(channel: str, user_id: int, news: bool) -> None:
    current = await get_settings(channel, user_id)
    await save_settings(channel, user_id, replace(current, news=news))


async def replaces_telegram_digest(user_id: int) -> bool:
    """Chủ bot đã bật bản tin trên Telegram -> scheduler bỏ digest danh mục 8h."""
    if not config.MORNING_BRIEF_ENABLED:
        return False
    return (await get_settings("telegram", user_id)).on


# ─── Gộp bản tin RSS 8h (Zalo controller, Zoom chủ bot) ────────────────────


async def _zalo_controller_internal_id() -> int | None:
    from channels import zalo_session, zalo_users

    controller = os.getenv("ZALO_CONTROLLER_ID", "").strip() or await zalo_session.load_controller()
    if not controller:
        return None
    user = await zalo_users.resolve(controller)
    return user.internal_user_id if user is not None and user.is_active else None


async def _absorbs_news(channel: str, user_id: int, settings: BriefSettings) -> bool:
    if not config.MORNING_BRIEF_ABSORB_NEWS or not settings.news_enabled(channel):
        return False
    if channel == "zoom":
        return config.is_owner_user_id(user_id)
    if channel == "zalo":
        return await _zalo_controller_internal_id() == user_id
    return False


async def absorbed_news_channels() -> set[str]:
    """Kênh ("zalo"/"zoom") mà bản tin cá nhân hôm nay mang luôn bản tin RSS 8h."""
    out: set[str] = set()
    if not config.MORNING_BRIEF_ENABLED or not config.MORNING_BRIEF_ABSORB_NEWS:
        return out
    owner = config.ALLOWED_USER_ID
    if owner and config.ZOOM_ENABLED:
        settings = await get_settings("zoom", owner)
        if settings.on and settings.news_enabled("zoom"):
            out.add("zoom")
    if _zalo_enabled():
        internal_id = await _zalo_controller_internal_id()
        if internal_id is not None:
            # Đúng danh sách run_due sẽ gửi (đã cắt ở MORNING_BRIEF_MAX_MEMBERS, 0 =
            # tắt Zalo): controller bị cắt mà vẫn coi là "gộp" thì mất cả 2 bản tin.
            delivered = {t.user_id: t.settings for t in await list_targets() if t.channel == "zalo"}
            settings = delivered.get(internal_id)
            if settings is not None and settings.news_enabled("zalo"):
                out.add("zalo")
    return out


# ─── Mục của bản tin ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BriefContext:
    user_id: int
    channel: str
    now: dt.datetime  # giờ VN (có tz)
    settings: BriefSettings
    is_owner: bool
    absorb_news: bool = False  # mục tin = bản tin RSS đầy đủ
    preview: bool = False  # /bantin xem: không gọi AI tổng hợp tin
    # Cách gọi người nhận (core/address.py; Zalo đặt qua /zalopair), mặc định "anh".
    address_form: str = "anh"

    @property
    def today(self) -> dt.date:
        return self.now.date()


SectionBuilder = Callable[[BriefContext], Awaitable[str | None]]


@dataclass(frozen=True)
class Section:
    key: str
    order: int
    build: SectionBuilder
    timeout: float


_SECTIONS: dict[str, Section] = {}


def register_section(
    key: str, order: int, *, timeout: float = 20.0
) -> Callable[[SectionBuilder], SectionBuilder]:
    """Đăng ký 1 mục (trùng ``key`` thì thay mục cũ). ``order`` nhỏ đứng trước."""

    def decorate(fn: SectionBuilder) -> SectionBuilder:
        _SECTIONS[key] = Section(key, order, fn, timeout)
        return fn

    return decorate


def sections() -> list[Section]:
    return sorted(_SECTIONS.values(), key=lambda s: (s.order, s.key))


@register_section("weather", 10, timeout=25)
async def _weather_section(ctx: BriefContext) -> str | None:
    from services import weather, weather_chat

    place = await weather_home.get(ctx.user_id)
    data, hourly, air = await asyncio.gather(
        weather.get_weather(place, 1),
        weather.get_hourly(place),
        weather_chat.optional_air(place),
        return_exceptions=True,
    )
    if not isinstance(data, dict) or data.get("error") or not data.get("daily"):
        return None
    n = weather._n
    day = data["daily"][0]
    lines = [
        f"🌤 Thời tiết {data.get('location')}: {day['description']}, "
        f"{n(day['min_c'])}-{n(day['max_c'])}°C, khả năng mưa {n(day['rain_chance_percent'])}%, "
        f"UV tối đa {n(day['uv_max'])}."
    ]
    if isinstance(hourly, dict) and hourly.get("hours"):
        points = [
            h for h in hourly["hours"] if h.time.date() == ctx.today and 6 <= h.time.hour < 22
        ]
        if points:
            lines.append(weather_chat.rain_line(points))
    warning = weather.air_warning(air if isinstance(air, dict) else None)
    if warning:
        lines.append(warning)
    if not place:
        me, you = address.self_word(ctx.address_form), ctx.address_form
        lines.append(f"(Mặc định TP.HCM - nhắn /thoitiet nha <tỉnh> để {me} xem đúng nơi {you} ở.)")
    return "\n".join(lines)


def _weekday_date(day: dt.date) -> str:
    from services import lunar

    return f"{lunar.WEEKDAYS[day.weekday()]} {day:%d/%m}"


def upcoming_events(today: dt.date, *, lunar_days: int = 16, holiday_days: int = 30) -> list[str]:
    """Mùng 1/rằm kế tiếp và ngày lễ gần nhất (không tính hôm nay)."""
    from services import calendar_query, lunar

    lunar_event: tuple[dt.date, str] | None = None
    for offset in range(1, lunar_days + 1):
        day = today + dt.timedelta(days=offset)
        ld, lm, _, leap = lunar.solar_to_lunar(day.day, day.month, day.year)
        if ld in (1, 15):
            name = "Mùng 1" if ld == 1 else "Rằm"
            lunar_event = (day, f"{name} tháng {lm}{' nhuận' if leap else ''}")
            break
    holiday: tuple[dt.date, str] | None = None
    tomorrow = today + dt.timedelta(days=1)
    for entry in calendar_query.HOLIDAYS:
        day = calendar_query.holiday_entry_date(entry, tomorrow)
        if day and 0 < (day - today).days <= holiday_days and (holiday is None or day < holiday[0]):
            holiday = (day, entry[0])
    out: list[str] = []
    if lunar_event and holiday and lunar_event[0] == holiday[0]:
        lunar_event = (lunar_event[0], f"{lunar_event[1]} - {holiday[1]}")
        holiday = None
    for day, name in sorted(e for e in (lunar_event, holiday) if e):
        out.append(f"{name} ({_weekday_date(day)}, còn {(day - today).days} ngày)")
    return out


@register_section("lunar", 20, timeout=5)
async def _lunar_section(ctx: BriefContext) -> str | None:
    from services import calendar_query, lunar

    info = lunar.date_info(ctx.today)
    lines = [f"📅 Âm lịch: {info['lunar']['text']} (ngày {info['lunar']['day_can_chi']})."]
    holidays = calendar_query.holidays_on(ctx.today)
    if holidays:
        lines.append(f"🎉 Hôm nay: {', '.join(holidays)}.")
    upcoming = upcoming_events(ctx.today)
    if upcoming:
        lines.append("🗓 Sắp tới: " + "; ".join(upcoming) + ".")
    return "\n".join(lines)


@register_section("reminders", 30, timeout=10)
async def _reminders_section(ctx: BriefContext) -> str | None:
    end = dt.datetime.combine(ctx.today + dt.timedelta(days=1), dt.time(), tzinfo=_VN_TZ)
    rows = await db.list_reminders_between(ctx.user_id, ctx.now, end, limit=10)
    if not rows:
        return None
    lines = ["⏰ Nhắc việc hôm nay:"]
    lines.extend(f"• {due.astimezone(_VN_TZ):%H:%M} {message}" for due, message in rows)
    return "\n".join(lines)


@register_section("market", 40, timeout=15)
async def _market_section(ctx: BriefContext) -> str | None:
    from services import vn_market_data as market

    gold, fx = await asyncio.gather(market.get_gold(), market.get_fx(), return_exceptions=True)
    lines = []
    if isinstance(gold, market.GoldTable):
        lines.append("🪙 " + market.gold_headline(gold))
    if isinstance(fx, market.FxTable) and market.fx_headline(fx):
        lines.append("💱 " + market.fx_headline(fx))
    return "\n".join(lines) or None


@register_section("portfolio", 50, timeout=40)
async def _portfolio_section(ctx: BriefContext) -> str | None:
    from stock import calendar as trading_calendar
    from stock import portfolio

    if not trading_calendar.is_trading_day(ctx.today):
        return None
    if not await portfolio.list_holdings(ctx.user_id):
        return None
    # digest=True: chỉ lãi/lỗ + SL/TP do chính người dùng đặt, không action policy.
    return await portfolio.build_report(ctx.user_id, digest=True)


@register_section("news", 90, timeout=180)
async def _news_section(ctx: BriefContext) -> str | None:
    from services import morning_news

    if not ctx.settings.news_enabled(ctx.channel):
        return None
    if ctx.absorb_news and not ctx.preview:
        content = await morning_news.shared_digest(ctx.now)
        if content:
            return content
    items, from_digest = await morning_news.today_headlines(ctx.now)
    return morning_news.format_headlines(items, from_digest) if items else None


async def _run_section(section: Section, ctx: BriefContext) -> str | None:
    try:
        return await asyncio.wait_for(section.build(ctx), section.timeout)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("morning_brief: mục %s lỗi, bỏ qua.", section.key, exc_info=True)
        return None


async def _ai_intro(parts: list[str]) -> str:
    """1 câu chào giọng Lan Anh (1 lượt AI có hạn chót); lỗi thì bỏ."""
    from ai import orchestrator
    from ai.timeouts import turn_deadline
    from core import untrusted

    # Tiêu đề RSS, nội dung nhắc việc... là dữ liệu ngoài: rào lại (như
    # morning_news._build_prompt) để không lái được câu chào. Mỗi mục 1 phần
    # ngân sách để mục tin đứng cuối không bị cắt mất hẳn.
    share = max(200, _AI_INTRO_MAX_CHARS // len(parts))
    brief = untrusted.wrap_untrusted(
        "\n\n".join(p[:share] for p in parts), "bản tin", max_chars=_AI_INTRO_MAX_CHARS
    )
    prompt = (
        "Bạn là Lan Anh, trợ lý cá nhân, xưng em gọi anh. Viết ĐÚNG 1 câu chào buổi "
        "sáng ngắn (dưới 30 từ), ấm áp, dựa trên bản tin bên dưới. KHÔNG nêu con số, "
        "không thêm thông tin ngoài bản tin, không markdown.\n\n" + brief
    )
    try:
        with turn_deadline(_AI_INTRO_TIMEOUT_SEC):
            response = await orchestrator.ask(prompt)
    except Exception:
        logger.warning("morning_brief: AI viết câu chào lỗi, bỏ qua.", exc_info=True)
        return ""
    text = (getattr(response, "text", None) or "").strip()
    return text.splitlines()[0][:300] if text else ""


async def build(
    user_id: int,
    channel: str,
    *,
    now: dt.datetime | None = None,
    settings: BriefSettings | None = None,
    preview: bool = False,
    address_form: str | None = None,
) -> str:
    """Soạn bản tin của 1 người (mục nào lỗi thì bỏ mục đó). ``address_form``:
    cách gọi riêng của thành viên Zalo (zalo_users.address_form), None = "anh"."""
    from services import lunar

    now = now or dt.datetime.now(_VN_TZ)
    settings = settings or await get_settings(channel, user_id)
    try:
        absorb = await _absorbs_news(channel, user_id, settings)
    except Exception:
        logger.warning("morning_brief: không xác định được gộp tin.", exc_info=True)
        absorb = False
    ctx = BriefContext(
        user_id=user_id,
        channel=channel,
        now=now,
        settings=settings,
        is_owner=config.is_owner_user_id(user_id),
        absorb_news=absorb,
        preview=preview,
        address_form=address.normalize(address_form) or "anh",
    )
    results = await asyncio.gather(*(_run_section(s, ctx) for s in sections()))
    parts = [r for r in results if r]
    header = f"☀️ Chào buổi sáng {ctx.address_form}! Bản tin {lunar.WEEKDAYS[now.weekday()]} {now:%d/%m/%Y}"
    if config.MORNING_BRIEF_AI_INTRO and ctx.is_owner and not preview and parts:
        intro = await _ai_intro(parts)
        if intro:
            header = f"{header}\n{intro}"
    return "\n\n".join([header, *parts])


# ─── Gửi theo lịch ──────────────────────────────────────────────────────────


def is_due(hour: int, now: dt.datetime) -> bool:
    start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return start <= now < start + dt.timedelta(hours=CATCHUP_HOURS)


def _zalo_enabled() -> bool:
    enabled = os.getenv("ZALO_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    return enabled and bool(os.getenv("ZALO_BRIDGE_SECRET", "").strip())


async def _already_sent(channel: str, user_id: int, now: dt.datetime) -> bool:
    key = personal_settings.brief_sent_key(channel, user_id)
    return await db.get_setting(key) == now.date().isoformat()


async def _mark_sent(channel: str, user_id: int, now: dt.datetime) -> None:
    await db.set_setting(personal_settings.brief_sent_key(channel, user_id), now.date().isoformat())


async def _zalo_users_by_internal_id() -> dict:
    from channels import zalo_users

    return {user.internal_user_id: user for user in await zalo_users.list_users()}


async def _offer(channel: str, user_id: int, text: str, now: dt.datetime) -> bool:
    """Bản tin là tin tổng hợp (NORMAL) qua services/notify_hub: xả ngay (gộp kèm
    tin đang chờ) hoặc chờ hết giờ yên lặng. False = gửi thẳng như cũ."""
    from services import notify_hub

    return await notify_hub.offer(
        channel,
        user_id,
        text,
        source="morning_brief",
        dedup=f"brief:{channel}:{user_id}:{now.date().isoformat()}",
        now=now,
    )


async def _deliver(target: BriefTarget, now: dt.datetime, zalo_map: dict) -> bool:
    channel, user_id = target.channel, target.user_id
    if channel == "telegram":
        if not config.is_owner_user_id(user_id) or _notify_callback is None:
            return False
        text = await build(user_id, channel, now=now, settings=target.settings)
        if await _offer(channel, user_id, text, now):
            await _mark_sent(channel, user_id, now)
            return True
        try:
            await _notify_callback(user_id, text)
        except Exception:
            logger.warning("morning_brief: gửi Telegram lỗi, thử lại lượt sau.", exc_info=True)
            return False
        await _mark_sent(channel, user_id, now)
        return True
    if channel == "zoom":
        from channels import zoom

        if not config.is_owner_user_id(user_id) or not config.ZOOM_ENABLED:
            return False
        pairing = await db.zoom_get_pairing()
        if not pairing:
            return False
        text = await build(user_id, channel, now=now, settings=target.settings)
        if await _offer(channel, user_id, text, now):
            await _mark_sent(channel, user_id, now)
            return True
        try:
            await zoom.send_message(to_jid=pairing[0], text=text)
        except Exception:
            logger.warning("morning_brief: gửi Zoom lỗi, thử lại lượt sau.", exc_info=True)
            return False
        await _mark_sent(channel, user_id, now)
        return True
    if channel == "zalo":
        from channels import zalo_repository
        from channels.zalo_text import to_plain_text

        user = zalo_map.get(user_id)
        if user is None or not user.is_active:
            return False  # đã gỡ pairing hoặc đang bị khoá: không gửi
        raw = await build(
            user_id,
            channel,
            now=now,
            settings=target.settings,
            address_form=user.address_form,
        )
        # Hàng đợi gom xả Zalo trong 1 transaction cùng zalo_outbox (dedup theo ngày):
        # đã nhận thì không bao giờ gửi đôi, đánh dấu sau cũng an toàn.
        if await _offer(channel, user_id, raw, now):
            await _mark_sent(channel, user_id, now)
            return True
        text = to_plain_text(raw)
        # Đánh dấu TRƯỚC khi đưa vào outbox (INSERT không idempotent): sập giữa
        # chừng thì mất 1 bản tin, không bao giờ gửi đôi qua Zalo.
        await _mark_sent(channel, user_id, now)
        try:
            account_id = await zalo_repository.resolve_active_account_id()
            await zalo_repository.enqueue_outbox(account_id, user.external_id, text)
        except Exception:
            logger.warning("morning_brief: đưa bản tin Zalo vào outbox lỗi.", exc_info=True)
            return False
        return True
    return False


async def run_due(now: dt.datetime | None = None) -> int:
    """Gửi bản tin cho những người tới giờ và hôm nay chưa nhận. Trả số tin đã gửi."""
    now = now or dt.datetime.now(_VN_TZ)
    targets = await list_targets()
    zalo_map: dict | None = None
    sent = 0
    for target in targets:
        if not is_due(target.settings.hour, now):
            continue
        if target.channel == "zalo" and not _zalo_enabled():
            continue
        if await _already_sent(target.channel, target.user_id, now):
            continue
        try:
            if target.channel == "zalo" and zalo_map is None:
                zalo_map = await _zalo_users_by_internal_id()
            if await _deliver(target, now, zalo_map or {}):
                sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("morning_brief: lỗi gửi bản tin %s:%s", target.channel, target.user_id)
    return sent


async def _loop() -> None:
    trace.detach()
    while True:
        try:
            with trace.job("morning_brief"):
                await run_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("morning_brief: vòng lặp bản tin sáng lỗi.")
        await asyncio.sleep(_POLL_SEC)


def start() -> None:
    global _task
    if not config.MORNING_BRIEF_ENABLED:
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
