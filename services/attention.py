"""/chuy - "Có gì cần anh chú ý?" (ý từ amanimran786/jarvis-ai "what needs my
attention"): MỘT tin tổng hợp tính bằng code, KHÔNG gọi AI.

Mục (section) đăng ký qua ``register_section`` như services/morning_brief - gói
sau thêm "việc nền đang chạy", "tác vụ định kỳ" mà không phải sửa file này::

    from services import attention

    @attention.register_section("viec_nen", order=80, audience="owner")
    async def _jobs(ctx: attention.AttentionContext) -> str | None:
        ...  # trả None khi không có gì

``audience``: "all" (mọi người, dữ liệu của CHÍNH người hỏi), "admin" (chủ bot +
admin Zalo), "owner" (chỉ chủ bot: Telegram/Zoom, Zalo controller). Mỗi mục có
timeout ngắn riêng; mục lỗi/hết giờ bị bỏ và ghi 1 dòng "chưa đọc được", không
làm hỏng cả tin. Không có gì thì trả 1 câu ngắn. Thành viên Zalo chỉ thấy dữ
liệu theo internal_user_id của chính mình.

Mục có sẵn: nhắc việc còn lại hôm nay + ngày nhớ 3 ngày tới; danh mục (stop/
target/giá chạm ngưỡng đã báo hôm nay, đang dưới stop nhiều phiên, sự kiện quyền
rơi vào hôm nay); hỏi thăm đang chờ; tin đang chờ gom (services/notify_hub); bài
Facebook chờ duyệt (admin); lỗi mới hôm nay, provider cooldown/quota >= 80%,
watchdog đang báo (chủ bot).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from core import address, config
from core import database as db
from core.text_normalize import fold

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
AUDIENCES = ("all", "admin", "owner")
LOOKAHEAD_DAYS = 3
_MAX_LINES = 6


def _now() -> dt.datetime:
    return dt.datetime.now(VN_TZ)


@dataclass(frozen=True)
class AttentionContext:
    user_id: int
    channel: str
    now: dt.datetime  # giờ VN (có tz)
    is_owner: bool
    is_admin: bool
    you: str = "anh"

    @property
    def today(self) -> dt.date:
        return self.now.date()

    @property
    def me(self) -> str:
        return address.self_word(self.you)


SectionBuilder = Callable[[AttentionContext], Awaitable[str | None]]


@dataclass(frozen=True)
class Section:
    key: str
    order: int
    build: SectionBuilder
    timeout: float
    audience: str
    label: str


_SECTIONS: dict[str, Section] = {}


def register_section(
    key: str, order: int, *, timeout: float = 6.0, audience: str = "all", label: str = ""
) -> Callable[[SectionBuilder], SectionBuilder]:
    """Đăng ký 1 mục (trùng ``key`` thì thay mục cũ). ``order`` nhỏ đứng trước."""
    if audience not in AUDIENCES:
        raise ValueError(f"audience phải là 1 trong {AUDIENCES}")

    def decorate(fn: SectionBuilder) -> SectionBuilder:
        _SECTIONS[key] = Section(key, order, fn, timeout, audience, label or key)
        return fn

    return decorate


def sections() -> list[Section]:
    return sorted(_SECTIONS.values(), key=lambda s: (s.order, s.key))


def _visible(section: Section, ctx: AttentionContext) -> bool:
    if section.audience == "owner":
        return ctx.is_owner
    if section.audience == "admin":
        return ctx.is_owner or ctx.is_admin
    return True


def _bullets(title: str, lines: list[str]) -> str:
    shown = lines[:_MAX_LINES]
    more = f"\n• … và {len(lines) - len(shown)} mục khác" if len(lines) > len(shown) else ""
    return "\n".join([title, *(f"• {line}" for line in shown)]) + more


def _vn(value) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(VN_TZ)


# ─── Mục có sẵn ─────────────────────────────────────────────────────────────


@register_section("reminders", 10, label="nhắc việc")
async def _reminders(ctx: AttentionContext) -> str | None:
    from services import lunar, ngay_nho

    end = dt.datetime.combine(ctx.today + dt.timedelta(days=1), dt.time(), tzinfo=VN_TZ)
    rows = await db.list_reminders_between(ctx.user_id, ctx.now, end, limit=10)
    lines = [f"{_vn(due):%H:%M} {message}" for due, message in rows]
    for item in await ngay_nho.items(ctx.user_id, ctx.today):
        if item.event is None:
            continue
        days = (item.event - ctx.today).days
        if 0 <= days <= LOOKAHEAD_DAYS:
            when = (
                "HÔM NAY"
                if days == 0
                else f"{lunar.WEEKDAYS[item.event.weekday()]} {item.event:%d/%m} (còn {days} ngày)"
            )
            lines.append(f"🗓 {item.title} - {when}")
    return _bullets("⏰ Nhắc việc còn lại hôm nay & ngày nhớ sắp tới:", lines) if lines else None


def _fmt(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")


def _event_line(ref: str, today: dt.date) -> str | None:
    """ref_key nhật ký sự kiện quyền "MÃ:CODE:YYYY-MM-DD" -> 1 dòng nếu rơi vào hôm nay."""
    parts = ref.split(":")
    if len(parts) < 3:
        return None
    try:
        day = dt.date.fromisoformat(parts[-1])
    except ValueError:
        return None
    if day != today:
        return None
    names = {"DIV": "cổ tức tiền", "ISS": "phát hành/quyền mua", "AGME": "ĐHCĐ", "EGME": "ĐHCĐ"}
    return f"📅 {parts[0]}: sự kiện quyền ({names.get(parts[1], parts[1])}) HÔM NAY"


@register_section("portfolio", 20, timeout=6, label="danh mục")
async def _portfolio(ctx: AttentionContext) -> str | None:
    from services import portfolio_alerts

    since = (ctx.today - dt.timedelta(days=7)).isoformat()
    today = ctx.today.isoformat()
    lines: list[str] = []
    seen: set[str] = set()
    for kind, ref, sent_on in await db.portfolio_alert_log_since(ctx.user_id, since):
        line = None
        if kind in ("stop", "target") and sent_on == today:
            symbol, _, level = ref.partition(":")
            try:
                level = _fmt(float(level))
            except ValueError:
                pass
            what = "chạm/thủng stop" if kind == "stop" else "đạt/vượt target"
            line = f"{'🚨' if kind == 'stop' else '🎯'} {symbol} {what} {level} (đã báo hôm nay)"
        elif kind == "corp" and sent_on == today:
            line = f"🔁 {ref.partition(':')[0]}: vị thế vừa được quy đổi theo sự kiện quyền"
        elif kind in ("event", "event_grouped"):
            line = _event_line(ref, ctx.today)
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    for alert in await db.portfolio_list_price_alerts(ctx.user_id):
        notified = alert.get("notified_at")
        if alert.get("last_state") == "crossed" and notified and _vn(notified).date() == ctx.today:
            word = "về" if alert.get("direction") == "BELOW" else "lên"
            lines.append(
                f"🔔 {alert['symbol']} đã chạm ngưỡng {ctx.you} đặt ({word} "
                f"{_fmt(float(alert['level']))}đ)"
            )
    # Đang dưới stop/trên target từ các phiên trước (trạng thái job cuối phiên).
    for symbol, entry in sorted((await portfolio_alerts.load_state(ctx.user_id)).items()):
        if not isinstance(entry, dict):
            continue
        stop = entry.get("stop")
        if isinstance(stop, dict) and int(stop.get("days") or 0) >= 2:
            lines.append(f"🚨 {symbol}: {stop['days']} phiên liền đóng cửa dưới stop")
    if not lines:
        return None
    return (
        _bullets("📊 Danh mục:", lines) + "\n(Mức do chính " + ctx.you + " đặt, không phải lệnh.)"
    )


@register_section("followups", 30, label="hỏi thăm")
async def _followups(ctx: AttentionContext) -> str | None:
    horizon = ctx.today + dt.timedelta(days=1)
    lines = []
    for event in await db.list_user_events(ctx.user_id, 20):
        day = event.get("event_date")
        if event.get("status") != "pending" or not isinstance(day, dt.date) or day > horizon:
            continue
        when = "hôm nay" if day == ctx.today else "mai" if day == horizon else f"{day:%d/%m}"
        lines.append(f"{event['title']} ({when})")
    if not lines:
        return None
    return _bullets(f"💬 Việc {ctx.you} kể, {ctx.me} định hỏi thăm:", lines)


@register_section("notify_queue", 35, label="tin chờ gom")
async def _queued(ctx: AttentionContext) -> str | None:
    from services import notify_hub

    if not notify_hub.active():
        return None
    count = await db.notify_count_pending(ctx.user_id)
    if not count:
        return None
    return f"📬 Có {count} thông báo {ctx.me} đang gom chờ gửi - {ctx.me} gửi luôn sau tin này nha."


async def _facebook_account() -> str | None:
    from channels import zalo_repository, zalo_session

    return (
        await zalo_session.load_account_id() or await zalo_repository.resolve_default_account_id()
    )


@register_section("facebook", 40, audience="admin", label="bài Facebook")
async def _facebook(ctx: AttentionContext) -> str | None:
    from channels import facebook_repository

    account_id = await _facebook_account()
    if not account_id:
        return None
    count = await facebook_repository.count_pending(account_id)
    if not count:
        return None
    return f"📝 {count} bài Facebook đang chờ duyệt - /fb_xem <số> để xem, /fb_ok <số> để đăng."


@register_section("errors", 50, audience="owner", label="lỗi")
async def _errors(ctx: AttentionContext) -> str | None:
    from services import error_digest

    entries = [e for e in error_digest.store.entries(ctx.today.isoformat()) if e.level == "ERROR"]
    if not entries:
        return None
    total = sum(e.count for e in entries)
    lines = [f"{e.count}× {e.exc_type} ở {e.where}" for e in entries[:3]]
    return _bullets(f"🐞 Lỗi hôm nay: {total} lần, {len(entries)} loại (/loi xem đủ):", lines)


@register_section("providers", 60, audience="owner", timeout=8, label="provider")
async def _providers(ctx: AttentionContext) -> str | None:
    import time

    from ai.provider_state import provider_state
    from services import quota_burn

    lines = []
    now = time.time()
    for name, until in sorted(provider_state.api_exhausted_until.items()):
        if until > now:
            reason = provider_state.cooldown_reasons.get(name, "")
            minutes = max(1, round((until - now) / 60))
            lines.append(
                f"{name} đang cooldown {minutes} phút nữa{f' ({reason})' if reason else ''}"
            )
    if provider_state.router9_dead_since is not None:
        lines.append("9Router đang bị đánh dấu lỗi (/userouter9 để thử lại)")
    threshold = quota_burn.alert_pct()
    if threshold > 0:
        try:
            burns = await asyncio.wait_for(quota_burn.snapshot(), 6)
        except Exception:
            logger.warning("attention: không đọc được quota miễn phí.", exc_info=True)
            burns = []
        for burn in burns:
            if burn.limit and burn.pct is not None and burn.pct >= threshold:
                lines.append(
                    f"⛽ {burn.label}: {burn.used}/{burn.limit} {burn.unit} ({burn.pct:.0f}%)"
                )
    return _bullets("📡 Provider AI:", lines) if lines else None


@register_section("watchdog", 70, audience="owner", timeout=6, label="giám sát")
async def _watchdog(ctx: AttentionContext) -> str | None:
    from services import watchdog

    if not watchdog.enabled():
        return None
    issues = await watchdog.active_issues(ctx.now)
    return _bullets("🛠 Giám sát đang báo:", issues) if issues else None


# ─── Soạn tin ───────────────────────────────────────────────────────────────


async def _run(section: Section, ctx: AttentionContext) -> tuple[str | None, bool]:
    try:
        return await asyncio.wait_for(section.build(ctx), section.timeout), True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("attention: mục %s lỗi/hết giờ, bỏ qua.", section.key, exc_info=True)
        return None, False


async def build(
    user_id: int,
    channel: str,
    *,
    is_owner: bool | None = None,
    is_admin: bool = False,
    address_form: str | None = None,
    now: dt.datetime | None = None,
) -> str:
    """Tin "có gì cần chú ý" của CHÍNH người hỏi. ``is_owner`` None = suy từ user_id
    (chủ bot Telegram/Zoom); Zalo controller do kênh truyền vào."""
    owner = config.is_owner_user_id(user_id) if is_owner is None else is_owner
    you = "anh" if config.is_owner_user_id(user_id) else address.normalize(address_form) or "anh"
    ctx = AttentionContext(
        user_id=user_id,
        channel=channel,
        now=(now or _now()).astimezone(VN_TZ),
        is_owner=owner,
        is_admin=is_admin or owner,
        you=you,
    )
    chosen = [s for s in sections() if _visible(s, ctx)]
    results = await asyncio.gather(*(_run(s, ctx) for s in chosen))
    parts = [text for text, _ in results if text]
    failed = [s.label for s, (_, ok) in zip(chosen, results, strict=True) if not ok]
    if not parts:
        text = f"✨ Hiện chưa có gì cần {you} chú ý đâu, {ctx.me} vẫn canh giúp {you} nha."
    else:
        head = f"👀 Việc cần {you} chú ý ({ctx.now:%H:%M} {ctx.now:%d/%m}):"
        text = "\n\n".join([head, *parts])
    if failed:
        text += f"\n\n(⚠️ {ctx.me.capitalize()} chưa đọc được: {', '.join(failed)} - thử lại sau.)"
    return text


# ─── Câu tự nhiên ───────────────────────────────────────────────────────────

_WHO = r"(?:anh|chi|em|minh|toi|tao|co|chu|bac|ong|ba|me|bo|sep)"
_TAIL = r"(?:\s+(?:khong|ko|k|ko vay|khong vay|khong em|vay|the|nhi|nhe|a|ha|em|ta|day|nao))*"
_NL_RE = re.compile(
    rf"^(?:(?:em|lan anh|bot) oi\s+)?(?:(?P<when>hom nay|bay gio|gio|sang nay|toi nay)\s+)?"
    rf"(?:co|dang co)\s+(?:gi|viec gi|chuyen gi|cai gi)\s+(?:can|phai)\s+(?:(?P<who>{_WHO})\s+)?"
    rf"(?:chu y|de y|luu y|lo|xu ly|quan tam|biet)(?:\s+(?:khong|ko|k))?{_TAIL}\s*[?.!]*$"
)


def is_attention_question(text: str) -> bool:
    """Câu hỏi rõ ràng kiểu "có gì cần anh chú ý", "hôm nay có gì cần lo" (cả câu,
    ngắn, phải có người được nhắc hoặc mốc "hôm nay/bây giờ") - không bắt câu
    thường như "có gì cần chú ý khi mua nhà" hay "có gì cần lưu ý không"."""
    folded = " ".join(fold(text or "").replace("?", " ?").split())
    if not folded or len(folded) > 80:
        return False
    match = _NL_RE.match(folded)
    return bool(match and (match.group("when") or match.group("who")))
