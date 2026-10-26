"""Lệnh Telegram đổi cấu hình AI toàn cục: /model, /status, /userouter9,
/router9, /tavily và /anh (tạo ảnh Agnes AI + bật/tắt); /loi xem lỗi gom nhóm."""

import asyncio
import html
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from ai import agnes_client, orchestrator, router9_client, tavily_client
from core import config
from core.text_utils import parse_on_off
from handlers import common
from services import error_digest, provider_health, search_status


@common.restricted
async def userouter9_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Đang thử lại 9Router, chờ chút...")
    ok, detail = await orchestrator.try_router9_now()
    if ok:
        await update.message.reply_text("✅ 9Router hoạt động, đã chuyển về dùng 9Router.")
    else:
        await update.message.reply_text(f"❌ 9Router vẫn đang lỗi ({html.escape(detail)}).")


@common.restricted
async def router9_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    toggle = parse_on_off(common.extract_arg(context))
    if toggle is True:
        await orchestrator.set_router9_enabled(True)
        await update.message.reply_text("✅ Đã bật 9Router.")
        return
    if toggle is False:
        await orchestrator.set_router9_enabled(False)
        await update.message.reply_text(
            "🔴 Đã tắt 9Router. Bot sẽ bỏ qua 9Router trong provider-chain "
            "cho tới khi gõ /router9 on."
        )
        return
    enabled = orchestrator.get_provider_state_snapshot()["router9_enabled"]
    status = "🟢 đang BẬT" if enabled else "🔴 đang TẮT"
    await update.message.reply_text(
        f"9Router {status}.\nDùng /router9 on hoặc /router9 off để đổi."
    )


@common.restricted
async def anh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = common.extract_arg(context).strip()
    toggle = parse_on_off(arg)
    if toggle is True:
        await agnes_client.set_enabled(True)
        await update.message.reply_text(
            "✅ Đã bật tạo ảnh (Agnes AI). Dùng /anh <mô tả> để tạo ảnh."
        )
        return
    if toggle is False:
        await agnes_client.set_enabled(False)
        await update.message.reply_text("🔴 Đã tắt tạo ảnh cho tới khi bật lại bằng /anh on.")
        return
    if not arg:
        enabled = await agnes_client.get_enabled()
        status = "🟢 đang BẬT" if enabled else "🔴 đang TẮT"
        await update.message.reply_text(
            f"Tạo ảnh (Agnes AI) {status}.\nDùng /anh <mô tả> để tạo ảnh, hoặc /anh on|off để bật/tắt."
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    try:
        image = await agnes_client.generate_image(arg)
    except agnes_client.AgnesError as exc:
        await update.message.reply_text(f"❌ Không tạo được ảnh: {exc}")
        return
    await update.message.reply_photo(photo=image.data)


@common.restricted
async def tavily_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    toggle = parse_on_off(common.extract_arg(context))
    if toggle is True:
        await tavily_client.set_enabled(True)
        await update.message.reply_text(
            f"✅ Đã bật tra web Tavily trước khi trả lời.\n{await search_status.search_mode_line()}"
        )
        return
    if toggle is False:
        await tavily_client.set_enabled(False)
        await update.message.reply_text(
            "🔴 Đã tắt Tavily. Câu cần thông tin mới em sẽ dùng web search của chính "
            f"provider đang trả lời.\n{await search_status.search_mode_line()}"
        )
        return
    enabled = await tavily_client.get_enabled()
    status = "🟢 đang BẬT" if enabled else "🔴 đang TẮT"
    await update.message.reply_text(
        f"Tavily {status}.\n{await search_status.search_mode_line()}\n"
        "Dùng /tavily on hoặc /tavily off để đổi."
    )


@common.restricted
async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = common.extract_arg(context)
    if not arg:
        current = await router9_client.get_preferred_model_name()
        try:
            names = await router9_client.list_models()
        except Exception:
            await update.message.reply_text("❌ Không lấy được danh sách model lúc này.")
            return
        lines = [
            f"🧠 Model đang dùng cho chat: <b>{html.escape(current or 'tự động')}</b>",
            "",
            "Các model khả dụng:",
        ]
        lines += [f"• {html.escape(n)}" for n in names]
        lines.extend(
            [
                "",
                "Đổi model: <code>/model tên</code>",
                "Về mặc định: <code>/model auto</code>",
                "",
                "⚠️ Model này chỉ áp dụng khi dùng 9Router. Nếu 9Router lỗi và bot tự "
                f"chuyển sang API dự phòng, API luôn dùng model mặc định ({html.escape(config.GOOGLE_AI_STUDIO_MODEL)}).",
            ]
        )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if arg.lower() in {"auto", "default", "reset"}:
        await router9_client.set_preferred_model_name(None)
        await orchestrator.reset_chat()
        await update.message.reply_text("🔄 Đã về chọn model tự động.")
        return

    try:
        model_name = await router9_client.find_model(arg)
    except Exception:
        await update.message.reply_text("❌ Không kiểm tra được model lúc này.")
        return

    if model_name is None:
        await update.message.reply_text(f'Không tìm thấy model khớp "{arg}".')
        return

    await router9_client.set_preferred_model_name(model_name)
    await orchestrator.reset_chat()
    await update.message.reply_text(f"✅ Đã đổi model chat sang: {model_name}")


def _fmt_epoch_vn(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%H:%M %d/%m")


def _cooldown_suffix(until: float, reason: str | None, now: float) -> str:
    """Dòng phụ "cooldown tới HH:MM (còn N phút - lý do)" cho /status."""
    minutes = max(1, int((until - now + 59) // 60))
    detail = f"còn {minutes} phút" + (f" - {html.escape(reason)}" if reason else "")
    return f"\n   ⥅ cooldown tới {_fmt_epoch_vn(until)} ({detail})"


async def _noop_ai_status() -> tuple[bool, str]:
    return False, "Chưa cấu hình"


_STATUS_CACHE_TTL_SECONDS = 90
_status_cache: tuple[float, str] | None = None


async def _build_status_text() -> str:
    """Ping thật cả 5 provider rồi dựng text trạng thái (HTML parse_mode)."""
    (
        (router9_ok, router9_detail),
        groq_status,
        openrouter_status,
        api1_status,
        api2_status,
    ) = await asyncio.gather(
        orchestrator.check_router9_status(),
        orchestrator.check_groq_status() if config.GROQ_API_KEY else _noop_ai_status(),
        orchestrator.check_openrouter_status() if config.OPENROUTER_API_KEY else _noop_ai_status(),
        orchestrator.check_ai_studio_status(1)
        if config.GOOGLE_AI_STUDIO_API_KEY_1
        else _noop_ai_status(),
        orchestrator.check_ai_studio_status(2)
        if config.GOOGLE_AI_STUDIO_API_KEY_2
        else _noop_ai_status(),
    )

    state = orchestrator.get_provider_state_snapshot()
    now = time.time()

    active_map = {
        "router9": "9Router",
        "groq": "Groq",
        "openrouter": "OpenRouter",
        "api1": "API 1",
        "api2": "API 2",
    }
    active_line = f"🔀 Provider đang dùng: <b>{html.escape(active_map.get(state['active_provider'], state['active_provider']))}</b>"

    router9_line = (
        "✅ 9Router: OK" if router9_ok else f"❌ 9Router: lỗi ({html.escape(router9_detail)})"
    )
    if not state["router9_enabled"]:
        router9_line += "\n   ⛔ đang TẮT thủ công (/router9 on để bật lại)"
    reasons = state.get("cooldown_reasons") or {}
    if state["router9_dead_since"]:
        router9_line += f"\n   ⥅ chết lúc {_fmt_epoch_vn(state['router9_dead_since'])}"
        if reasons.get("router9_dead"):
            router9_line += f" ({html.escape(reasons['router9_dead'])})"
    router9_until = state.get("router9_exhausted_until", 0.0)
    if router9_until > now:
        router9_line += _cooldown_suffix(router9_until, reasons.get("router9"), now)
    router9_search_until = state.get("router9_search_exhausted_until", 0.0)
    if router9_search_until > now:
        router9_line += "\n   🔎 tra web (ROUTER9_WEB_SEARCH):" + _cooldown_suffix(
            router9_search_until, reasons.get("router9_search"), now
        ).replace("\n   ⥅", "", 1)

    def _provider_line(label, key, status, exhausted_until, reason=None):
        if not key:
            return f"⚪ {label}: chưa cấu hình"
        ok, detail = status
        line = f"✅ {label}: OK" if ok else f"❌ {label}: lỗi ({html.escape(detail)})"
        if exhausted_until > now:
            line += _cooldown_suffix(exhausted_until, reason, now)
        return line

    groq_line = _provider_line(
        "Groq",
        config.GROQ_API_KEY,
        groq_status,
        state["groq_exhausted_until"],
        reasons.get("groq"),
    )
    groq_search_until = state.get("groq_search_exhausted_until", 0.0)
    if config.GROQ_API_KEY and groq_search_until > now:
        groq_line += "\n   🔎 tra web (browser_search):" + _cooldown_suffix(
            groq_search_until, reasons.get("groq_search"), now
        ).replace("\n   ⥅", "", 1)
    openrouter_line = _provider_line(
        "OpenRouter",
        config.OPENROUTER_API_KEY,
        openrouter_status,
        state["openrouter_exhausted_until"],
        reasons.get("openrouter"),
    )
    api1_line = _provider_line(
        "API 1",
        config.GOOGLE_AI_STUDIO_API_KEY_1,
        api1_status,
        state["api1_exhausted_until"],
        reasons.get("api1"),
    )
    api2_line = _provider_line(
        "API 2",
        config.GOOGLE_AI_STUDIO_API_KEY_2,
        api2_status,
        state["api2_exhausted_until"],
        reasons.get("api2"),
    )

    preferred = await router9_client.get_preferred_model_name()
    model_line = (
        f"🧠 Model chat: {html.escape(preferred or 'tự động')} "
        f"(9Router: {html.escape(config.ROUTER9_MODEL)}, Groq: {html.escape(config.GROQ_MODEL)}, "
        f"OpenRouter: {html.escape(config.OPENROUTER_MODEL)}, API: {html.escape(config.GOOGLE_AI_STUDIO_MODEL)})"
    )
    order_line = "🔢 PROVIDER_ORDER: " + " → ".join(config.PROVIDER_ORDER)
    search_line = html.escape(await search_status.search_mode_line())

    lines = [
        "📡 <b>Trạng thái bot</b>",
        "",
        active_line,
        order_line,
        search_line,
        "",
        router9_line,
        groq_line,
        openrouter_line,
        api1_line,
        api2_line,
        "",
        model_line,
    ]
    latency = await provider_health.latency_lines(use_html=True)
    if latency:
        lines += ["", *latency]
    return "\n".join(lines)


@common.restricted
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _status_cache

    now = time.time()
    if _status_cache is not None and now - _status_cache[0] <= _STATUS_CACHE_TTL_SECONDS:
        checked_at, cached_text = _status_cache
        await update.message.reply_text(
            f"{cached_text}\n\n<i>⏱️ Kiểm tra lúc {_fmt_epoch_vn(checked_at)} "
            f"({int(now - checked_at)}s trước) - gõ lại sau {_STATUS_CACHE_TTL_SECONDS}s để ping mới.</i>",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text("🔎 Đang kiểm tra provider-chain...")
    text = await _build_status_text()
    checked_at = time.time()
    _status_cache = (checked_at, text)
    await update.message.reply_text(
        f"{text}\n\n<i>⏱️ Kiểm tra lúc {_fmt_epoch_vn(checked_at)}</i>",
        parse_mode="HTML",
    )


@common.restricted
async def loi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/loi - lỗi gom theo loại hôm nay/hôm qua + số lần khởi động lại 7 ngày.
    Gửi plain text: đường dẫn code có dấu "_" sẽ bị Markdown hiểu nhầm."""
    text = await error_digest.build_report()
    await update.message.reply_text(text[: common.TELEGRAM_TEXT_MAX])
