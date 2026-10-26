"""Facade công khai của package `ai`: kết hợp nhánh 9Router (ai/router9_client.py),
Groq (ai/groq_client.py), OpenRouter (ai/openrouter_client.py) và nhánh
api1/api2 (ai/official_client.py) thành provider-chain có fallback, theo thứ
tự core.config.PROVIDER_ORDER (mặc định router9 -> groq -> openrouter -> api1
-> api2 - toàn bộ đều miễn phí, Gemini official đứng cuối làm lưới an toàn).

- 9Router chết -> chuyển hẳn sang provider kế tiếp, KHÔNG thử lại router9 mọi
  tin nhắn nữa. 3 cách quay lại router9: probe nền định kỳ, đổi
  ROUTER9_API_KEY, lệnh /userouter9 (xem
  init_provider_state()/start_background_tasks()/try_router9_now()). Ngoại lệ
  duy nhất: khi MỌI provider khác đều lỗi trong lượt, router9 (chết vì
  timeout/lỗi tạm) được thử lại 1 lần làm cứu cánh cuối. Provider đang
  cooldown vì 429 thì KHÔNG được gọi lại trong cooldown (gọi chỉ thêm 1 lỗi 429).
- Ngoài dead/alive tự động còn có cờ bật/tắt thủ công (router9_enabled, mặc
  định bật) qua lệnh /router9 on|off (set_router9_enabled()) - tắt thì
  router9 bị loại khỏi provider-chain hoàn toàn cho tới khi bật lại, kể cả
  khi đang "sống" (kể cả lượt tra web, nếu đã bật ROUTER9_WEB_SEARCH).
- router9 chỉ bị khai tử vì lỗi sức khoẻ thật: timeout, 401/403, hoặc 2 lỗi
  5xx/mất kết nối trong 5 phút. Lỗi theo request (400/413/422, nội dung bị
  lọc, output lẫn CJK, không tra được web) chỉ chuyển provider; 429 chỉ
  cooldown ngắn.
- groq/openrouter/api1/api2/router9 hết quota (429) -> cooldown theo gợi ý của
  provider (Retry-After..., xem ai/cooldown.py; hết quota NGÀY thì tới
  API_QUOTA_COOLDOWN_SEC) rồi tự thử lại; trong lúc đó dùng provider kế tiếp.
- Lượt cần thông tin mới trên web mà Tavily tắt/lỗi/mỏng: chat(search_mode=
  "native") chỉ thử provider TỰ tra web được (Gemini Google Search, Groq
  browser_search, OpenRouter/9Router nếu bật), không ai tra được thì trả lời
  qua chuỗi thường kèm ghi chú "chưa tra được" - xem chat().
"""

import asyncio
import logging
import re
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Awaitable, Callable, Iterator, Optional

from core import config, database as db, untrusted
from ai import openai_compatible, router9_client, groq_client, openrouter_client, official_client
from ai import cooldown, prompt_budget, provider_overrides
from ai import provider_state as provider_state_module
from ai.provider_state import ProviderStateSnapshot, provider_state
from ai.timeouts import TurnTimeoutError, bounded, remaining_turn_seconds, turn_deadline

__all__ = ["TurnTimeoutError", "remaining_turn_seconds", "turn_deadline"]

logger = logging.getLogger(__name__)
call_lock = asyncio.Lock()


class OutputContaminatedError(RuntimeError):
    """Output lẫn ký tự CJK (Trung/Nhật/Hàn) - dấu hiệu model fallback (thường
    gặp ở Gemini flash-lite, provider api1/api2) bị lỗi tạo văn bản, sinh ra
    câu trả lời lẫn ngôn ngữ dù prompt yêu cầu tiếng Việt. Coi như provider
    này lỗi ở lượt gọi này, KHÔNG đánh dấu hết quota/dead - để provider chain
    tự chuyển sang provider kế tiếp thử lại."""


# Phạm vi Unicode của chữ Hán, Kana (Nhật) và Hangul (Hàn). Văn bản tiếng
# Việt bình thường không chứa các ký tự này.
_CJK_RANGES = (
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs (Hán)
    (0x3040, 0x30FA),  # Hiragana + Katakana (Nhật), trừ dấu "・" "ー" hay bị dùng làm gạch đầu dòng
    (0xAC00, 0xD7A3),  # Hangul syllables (Hàn)
)
# Ngưỡng ký tự CJK để coi là output lẫn ngôn ngữ: 1-3 chữ Hán chú thích kiểu
# "chữ Nhẫn (忍)" trong câu trả lời tiếng Việt là hợp lệ, không được tính là lỗi.
_CJK_CONTAMINATION_MIN_CHARS = 4
# Đầu vào có các dấu hiệu này -> output CJK là CÓ CHỦ ĐÍCH (dịch sang tiếng
# Nhật/Trung/Hàn, hỏi về chữ Hán...), không kiểm tra. Không dùng "nhật" trơn vì
# trùng "cập nhật".
_CJK_TARGET_HINTS = (
    "tiếng nhật",
    "nhật bản",
    "tiếng trung",
    "trung quốc",
    "tiếng hoa",
    "tiếng hàn",
    "hàn quốc",
    "chữ hán",
    "hán tự",
    "kanji",
    "hiragana",
    "katakana",
    "furigana",
    "japanese",
    "chinese",
    "korean",
    "日本語",
    "中文",
    "한국어",
)
# Caller có thể chủ động cho phép output CJK (vd luồng dịch) qua allow_cjk_output().
_cjk_allowed: ContextVar[bool] = ContextVar("cjk_output_allowed", default=False)


def _count_cjk(text: str) -> int:
    return sum(1 for ch in text if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES))


def _contains_cjk(text: str) -> bool:
    return _count_cjk(text) > 0


def cjk_output_expected(*inputs: Optional[str]) -> bool:
    """True nếu đầu vào cho thấy output CJK là hợp lệ: bản thân đầu vào có chữ
    CJK (người dùng viết/dán tiếng Nhật, tài liệu tham chiếu dịch...) hoặc nhắc
    tới ngôn ngữ/chữ viết CJK làm đích."""
    for text in inputs:
        if not text:
            continue
        if _contains_cjk(text):
            return True
        lowered = text.lower()
        if any(hint in lowered for hint in _CJK_TARGET_HINTS):
            return True
    return False


@contextmanager
def allow_cjk_output() -> Iterator[None]:
    """Tắt bộ lọc CJK cho các lượt gọi trong khối (luồng dịch sang ja/zh/ko)."""
    token = _cjk_allowed.set(True)
    try:
        yield
    finally:
        _cjk_allowed.reset(token)


def _assert_output_clean(result, provider: str, *, allow_cjk: bool = False):
    if allow_cjk or _cjk_allowed.get():
        return result
    # Mọi client trả object có .text (Response/FallbackResponse); test cũ trả str.
    text = result if isinstance(result, str) else getattr(result, "text", None)
    if isinstance(text, str) and _count_cjk(text) >= _CJK_CONTAMINATION_MIN_CHARS:
        logger.warning(
            "%s trả output lẫn ký tự CJK (Trung/Nhật/Hàn) - coi như lỗi, chuyển provider kế tiếp.",
            provider,
        )
        raise OutputContaminatedError(f"{provider} trả output lẫn ký tự CJK")
    return result


# Model hay chèn số trích dẫn kiểu [1], [1][2], [1, 2] khi có grounding web;
# trong chat Telegram/Zalo chúng chỉ là rác. Không đụng link markdown [1](url).
_CITATION_RE = re.compile(r"[ \t]?\[[1-9]\d?(?:\s*[,\-–]\s*[1-9]\d?)*\](?!\()")
# Trích dẫn kiểu 【2†L6-L10】 của gpt-oss + browser_search (Groq).
_BROWSER_CITATION_RE = re.compile(r"[ \t]?【[^】\n]{0,60}†[^】\n]{0,60}】")


def _strip_citations(response):
    text = getattr(response, "text", None)
    if isinstance(text, str):
        response.text = _BROWSER_CITATION_RE.sub("", _CITATION_RE.sub("", text))
    return response


def _call_timeout_sec() -> float:
    return config.ROUTER9_CALL_TIMEOUT_SEC


async def _run_with_call_timeout(call_fn):
    # Trần riêng của 9Router, đồng thời không vượt phần còn lại của hạn chót lượt.
    return await bounded(call_fn, _call_timeout_sec())


async def init_provider_state() -> None:
    await provider_state_module.init_provider_state()


def set_alert_callback(fn) -> None:
    provider_state_module.set_alert_callback(fn)


def get_provider_state_snapshot() -> ProviderStateSnapshot:
    return provider_state.snapshot()


async def set_router9_enabled(enabled: bool) -> None:
    await provider_state.ensure_loaded()
    await provider_state.set_router9_enabled(enabled)


async def reset_api_cooldown(provider: str) -> None:
    await provider_state.ensure_loaded()
    await provider_state.reset_api_cooldown(provider)


class RealSearchUnavailableError(RuntimeError):
    """Strict grounded search has no configured provider with a real search tool."""


# ─── Provider TỰ tra web (native web search) ─────────────────────────────────
# Mỗi provider có cách tự tra web riêng NGAY TRONG lượt gọi (không qua Tavily):
#   api1/api2  : Google Search grounding (tool google_search của Gemini).
#   groq       : tool browser_search trên openai/gpt-oss-* (GROQ_WEB_SEARCH).
#   openrouter : plugin "web" - tốn credit, chỉ khi OPENROUTER_WEB_SEARCH=1.
#   router9    : web_search_options - chỉ khi ROUTER9_WEB_SEARCH=1, và chỉ tin
#                khi có annotations url_citation.
# Provider nào không chứng minh được đã tra web thì lượt đó coi như thất bại
# (NoSearchEvidenceError) - không bao giờ tin câu trả lời "tra web" bịa từ trí nhớ.
SEARCH_OFF = "off"  # không provider nào được tự tra web (/rag, đã có Tavily/số liệu chuẩn)
SEARCH_AUTO = "auto"  # chat thường: Gemini tự quyết có tra Google hay không
SEARCH_NATIVE = "native"  # câu cần tin mới mà Tavily tắt/lỗi/mỏng: provider tự tra
SEARCH_FORCED = "forced"  # giá vàng/tỷ giá/crypto...: BẮT BUỘC tra, cấm số từ trí nhớ
SEARCH_MODES = (SEARCH_OFF, SEARCH_AUTO, SEARCH_NATIVE, SEARCH_FORCED)

# Thứ tự cho lượt tra cứu BẮT BUỘC (chủ bot đã chọn): Groq -> Google Search
# grounding (api1 -> api2) -> OpenRouter -> 9Router (2 nhánh cuối chỉ khi bật).
_FORCED_SEARCH_ORDER = ("groq", "api1", "api2", "openrouter", "router9")
PROVIDER_LABELS = {
    "router9": "9Router",
    "groq": "Groq",
    "openrouter": "OpenRouter",
    "api1": "API 1",
    "api2": "API 2",
}


async def provider_can_search(provider: str) -> bool:
    """Provider có khả năng tự tra web theo cấu hình hiện tại không (có key,
    bật tính năng, model hỗ trợ). Trạng thái runtime (admin tắt, cooldown 429,
    router9 chết/tắt) do provider-chain tự xét ở lượt gọi."""
    if provider in ("api1", "api2"):
        return bool(await official_client.api_key_for(1 if provider == "api1" else 2))
    if provider == "groq":
        return groq_client.supports_web_search() and bool(await groq_client.api_key())
    if provider == "openrouter":
        return openrouter_client.supports_web_search() and bool(await openrouter_client.api_key())
    if provider == "router9":
        return router9_client.supports_web_search() and bool(await router9_client.api_key())
    return False


async def _search_capable(order) -> list[str]:
    return [provider for provider in order if await provider_can_search(provider)]


async def native_search_providers() -> list[str]:
    """Provider tự tra web được, theo PROVIDER_ORDER, bỏ provider admin đang
    tắt - dùng cho lượt "native" và cho /status, /tavily hiển thị."""
    names = []
    for provider in await _search_capable(config.PROVIDER_ORDER):
        if provider == "router9":
            if not provider_state.router9_enabled:
                continue
        elif not await provider_overrides.is_enabled(provider):
            continue
        names.append(provider)
    return names


async def _search_only_providers() -> list[str]:
    """Provider order cho require_real_search: Groq -> api1 -> api2, rồi
    OpenRouter/9Router nếu đã bật tra web cho chúng. Provider không tự tra web
    được thì KHÔNG đứng trong chuỗi này (trước đây OpenRouter free luôn đứng
    cuối dù không có tool search nào)."""
    order = await _search_capable(_FORCED_SEARCH_ORDER)
    if not order:
        raise RealSearchUnavailableError(
            "Tác vụ yêu cầu tìm kiếm web thật nhưng không provider nào tra web được "
            "(cần GROQ_API_KEY + GROQ_WEB_SEARCH, GOOGLE_AI_STUDIO_API_KEY_1/2, hoặc "
            "bật OPENROUTER_WEB_SEARCH/ROUTER9_WEB_SEARCH)."
        )
    return order


_FORCED_SEARCH_DIRECTIVE = (
    "[YÊU CẦU BẮT BUỘC TỪ HỆ THỐNG]\n"
    "Câu hỏi này cần số liệu/sự kiện thực tế bên ngoài sàn chứng khoán Việt Nam "
    "(giá hàng hoá, tỷ giá, crypto, chỉ số quốc tế, tin thời sự). Hệ thống KHÔNG "
    "có sẵn dữ liệu này để cung cấp cho bạn.\n"
    "1. BẮT BUỘC dùng công cụ tìm kiếm web thật để tra trước khi trả lời.\n"
    "2. CHỈ được nêu con số, mốc thời gian và sự kiện có TRONG kết quả tra cứu. "
    "Kèm theo thời điểm của số liệu và tên nguồn.\n"
    "3. Nếu tra không ra dữ liệu: nói thẳng là chưa tra được và DỪNG LẠI. "
    "TUYỆT ĐỐI KHÔNG đưa ra bất kỳ con số hay sự kiện nào từ trí nhớ, không ước "
    'lượng, không suy diễn. Trả lời "em chưa tra được" là ĐÚNG; đoán một con '
    "số nghe hợp lý là SAI nghiêm trọng."
)


_NATIVE_SEARCH_DIRECTIVE = (
    "[CHỈ DẪN HỆ THỐNG] Câu hỏi này cần thông tin mới trên web. Hãy dùng công cụ "
    "tìm kiếm web của bạn để tra TRƯỚC khi trả lời. Chỉ nêu số liệu, ngày tháng và "
    "sự kiện có trong kết quả tra cứu, kèm thời điểm của thông tin; nếu cần nêu "
    "nguồn thì nói tên nguồn tự nhiên trong câu. Tra không ra thì nói rõ là chưa "
    "tra được, KHÔNG đoán."
)
# Lượt cần tra web nhưng không provider nào tra được (tắt hết, lỗi, cooldown,
# không chứng minh được đã tra): vẫn trả lời qua chuỗi thường, kèm ghi chú này.
_SEARCH_UNAVAILABLE_NOTE = (
    "[GHI CHÚ HỆ THỐNG] Em vừa thử tra web cho câu này nhưng KHÔNG tra được (công "
    "cụ tìm kiếm đang lỗi/tạm hết lượt). TUYỆT ĐỐI không nêu số liệu, giá, tỷ giá, "
    "tin tức hay sự kiện mới nào từ trí nhớ như thể vừa tra được. Nói ngắn gọn là "
    "lúc này em chưa tra được thông tin mới nhất; phần kiến thức nền chung (không "
    "phụ thuộc thời điểm) vẫn có thể trả lời, và gợi ý anh hỏi lại sau ít phút."
)
# Nhiệt độ: chat tự do giữ 0.95 (giọng tự nhiên); lượt có dữ liệu tra cứu/tool/
# số liệu cổ phiếu hoặc tự tra web dùng thấp hơn để không "sáng tác" số.
_FREE_CHAT_TEMPERATURE = 0.95
_GROUNDED_TEMPERATURE = 0.55


_GENERIC_PROVIDER_CONFIGURED: dict[str, Callable[[], bool]] = {
    "groq": lambda: bool(config.GROQ_API_KEY),
    "openrouter": lambda: bool(config.OPENROUTER_API_KEY),
}


async def _effective_model(provider: str) -> str:
    """Tên model THỰC SỰ đang dùng cho provider tại thời điểm gọi - cùng
    logic web.py::_provider_info() dùng để hiển thị ở trang admin (phần
    "Providers - model & API key"), tái dùng ở đây để log_lượt gọi theo
    đúng model admin đang thấy, kể cả khi có override qua provider_overrides
    hoặc /model."""
    if provider == "router9":
        return await router9_client.get_preferred_model_name() or config.ROUTER9_MODEL
    if provider == "groq":
        return await groq_client._model()
    if provider == "openrouter":
        return await openrouter_client._model()
    if provider == "api1":
        return await official_client._model_for(1)
    if provider == "api2":
        return await official_client._model_for(2)
    return provider


# Giai đoạn của lượt gọi (chat/ask/utility/search...) - caller đặt qua
# ``call_stage()`` để provider_calls phân biệt lượt nào đốt thời gian.
_call_stage: ContextVar[str | None] = ContextVar("provider_call_stage", default=None)


def _cooldown_key(provider: str) -> str:
    """Groq ở lượt tự tra web dùng model riêng (GROQ_REALTIME_MODEL) với bucket
    rate-limit riêng -> cooldown riêng "groq_search", không khoá Groq chat."""
    if provider == "groq" and _call_stage.get() == "search":
        return "groq_search"
    return provider


@contextmanager
def call_stage(stage: str) -> Iterator[None]:
    token = _call_stage.set(stage)
    try:
        yield
    finally:
        _call_stage.reset(token)


def _classify_outcome(exc: BaseException) -> str:
    if isinstance(exc, OutputContaminatedError):
        return "contaminated"
    if _is_no_search_evidence(exc):
        return "no_search"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    try:
        if openai_compatible.is_rate_limited(exc) or official_client.is_quota_exhausted_error(exc):
            return "rate_limited"
    except Exception:
        pass
    return "error"


# Lỗi theo REQUEST của router9: chạy lại vẫn hỏng với đúng request đó, nhưng
# router9 vẫn khoẻ cho request khác (context quá dài, nội dung bị lọc, ảnh không
# hỗ trợ...). 404 (model không có) KHÔNG nằm đây - lặp lại mọi lượt nên coi là
# lỗi sức khoẻ.
_ROUTER9_REQUEST_ERROR_STATUSES = frozenset({400, 413, 415, 422})


def _router9_failure_kind(exc: BaseException) -> str:
    """timeout | auth | rate_limited | request | health (xem
    _run_provider_chain_within_deadline._attempt_router9_with_health)."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, OutputContaminatedError) or _is_no_search_evidence(exc):
        return "request"
    status = openai_compatible.http_status(exc)
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limited"
    if status in _ROUTER9_REQUEST_ERROR_STATUSES:
        return "request"
    return "health"


def _is_no_search_evidence(exc: BaseException) -> bool:
    return any(
        isinstance(item, openai_compatible.NoSearchEvidenceError)
        for item in openai_compatible._error_chain(exc)
    )


def _short_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _log_provider_failure(provider: str, exc: BaseException) -> None:
    if _is_no_search_evidence(exc):
        # Không tra web được ở lượt bắt buộc tra: chuyện thường, không cần traceback.
        logger.warning("%s không tra web được lượt này: %s", provider, _short_error(exc))
        return
    logger.error("%s lỗi (không phải hết quota).", provider, exc_info=exc)


async def _record_provider_call(
    provider: str,
    *,
    outcome: str = "ok",
    latency_ms: int | None = None,
) -> None:
    """Ghi 1 lượt gọi (thành công lẫn lỗi/timeout/429) vào provider_calls kèm độ
    trễ và giai đoạn. Lỗi DB ở đây KHÔNG được làm hỏng câu trả lời thật cho
    người dùng - chỉ log cảnh báo rồi bỏ qua."""
    try:
        model = await _effective_model(provider)
        await db.record_provider_call(
            provider,
            model,
            outcome=outcome,
            latency_ms=latency_ms,
            stage=_call_stage.get(),
        )
    except Exception:
        logger.warning("Không ghi được lượt gọi provider=%s vào DB.", provider, exc_info=True)


async def _assert_clean_recorded(result, provider: str, allow_cjk: bool, latency_ms: int):
    try:
        _assert_output_clean(result, provider, allow_cjk=allow_cjk)
    except OutputContaminatedError:
        await _record_provider_call(provider, outcome="contaminated", latency_ms=latency_ms)
        raise


async def _timed(provider: str, call: Callable[[], Awaitable]):
    """Chạy ``call()``, đo độ trễ và ghi provider_calls cho CẢ lượt lỗi (outcome
    phân loại theo exception). Lượt thành công do caller ghi sau khi kiểm output."""
    import time

    started = time.monotonic()
    try:
        result = await call()
    except TurnTimeoutError:
        await _record_provider_call(
            provider, outcome="turn_timeout", latency_ms=int((time.monotonic() - started) * 1000)
        )
        raise
    except Exception as exc:
        await _record_provider_call(
            provider,
            outcome=_classify_outcome(exc),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        raise
    return result, int((time.monotonic() - started) * 1000)


async def _run_provider_chain(
    *,
    router9_call,
    api_call,
    groq_call: Optional[Callable[[], Awaitable]] = None,
    openrouter_call: Optional[Callable[[], Awaitable]] = None,
    providers_override: Optional[list[str]] = None,
    allow_cjk: bool = False,
):
    """Run providers in configured order with persisted health state.

    router9 dùng cơ chế dead/retry/probe riêng (mark_router9_dead/alive).
    api1/api2/groq/openrouter đều single-try + cooldown-on-quota giống nhau,
    chỉ khác api_call() nhận idx (int) còn groq_call/openrouter_call() không
    nhận tham số - do api_call() đã có contract cũ (test_provider_chain.py).

    Cả chuỗi chạy trong hạn chót CHAT_TURN_TIMEOUT_SEC (hoặc hạn sớm hơn do
    caller đặt qua turn_deadline()); hết hạn -> TurnTimeoutError, không thử
    provider kế tiếp nữa.
    """
    with turn_deadline():
        return await _run_provider_chain_within_deadline(
            router9_call=router9_call,
            api_call=api_call,
            groq_call=groq_call,
            openrouter_call=openrouter_call,
            providers_override=providers_override,
            allow_cjk=allow_cjk,
        )


async def _run_provider_chain_within_deadline(
    *,
    router9_call,
    api_call,
    groq_call: Optional[Callable[[], Awaitable]],
    openrouter_call: Optional[Callable[[], Awaitable]],
    providers_override: Optional[list[str]],
    allow_cjk: bool,
):
    await provider_state.ensure_loaded()
    order = providers_override if providers_override is not None else config.PROVIDER_ORDER
    generic_calls: dict[str, Optional[Callable[[], Awaitable]]] = {
        "groq": groq_call,
        "openrouter": openrouter_call,
    }

    async def _attempt_router9():
        result, latency_ms = await _timed("router9", lambda: _run_with_call_timeout(router9_call))
        await _assert_clean_recorded(result, "router9", allow_cjk, latency_ms)
        await provider_state.mark_router9_alive()
        await provider_state.set_active_provider("router9")
        await _record_provider_call("router9", latency_ms=latency_ms)
        return result

    async def _attempt_api(idx: int):
        provider_name = f"api{idx}"
        result, latency_ms = await _timed(provider_name, lambda: bounded(lambda: api_call(idx)))
        await _assert_clean_recorded(result, provider_name, allow_cjk, latency_ms)
        await provider_state.set_active_provider(provider_name)
        await _record_provider_call(provider_name, latency_ms=latency_ms)
        return result

    async def _attempt_generic(name: str):
        result, latency_ms = await _timed(name, lambda: bounded(generic_calls[name]))
        await _assert_clean_recorded(result, name, allow_cjk, latency_ms)
        await provider_state.set_active_provider(name)
        await _record_provider_call(name, latency_ms=latency_ms)
        return result

    async def _has_any_fallback_configured() -> bool:
        if await official_client.api_key_for(1) or await official_client.api_key_for(2):
            return True
        return any(
            generic_calls.get(name) is not None and is_configured()
            for name, is_configured in _GENERIC_PROVIDER_CONFIGURED.items()
        )

    async def _attempt_router9_with_health(has_fallback: bool):
        """Gọi router9 và phân loại lỗi (xem _router9_failure_kind):
        - timeout / 401-403: khai tử ngay, không retry (timeout retry chỉ nhân
          đôi thời gian chờ - quyết định có chủ đích, có test riêng);
        - 429: cooldown ngắn theo Retry-After, KHÔNG khai tử;
        - lỗi theo request (400/413/422, nội dung bị lọc, ảnh không hỗ trợ,
          output lẫn CJK): chuyển provider, không đổi trạng thái sức khoẻ;
        - 5xx/mất kết nối/lỗi lạ: retry 1 lần; khai tử khi đủ 2 lỗi trong 5 phút.
        Không có provider dự phòng nào thì raise lỗi gốc, không khai tử."""
        for attempt in (1, 2):
            try:
                return await _attempt_router9()
            except TurnTimeoutError:
                raise
            except Exception as exc:
                kind = _router9_failure_kind(exc)
                if kind == "request":
                    logger.warning(
                        "9Router từ chối request này (%s) - chuyển provider, giữ trạng thái.",
                        _short_error(exc),
                    )
                    raise
                if kind == "rate_limited":
                    seconds, reason = cooldown.cooldown_for("router9", exc)
                    await provider_state.mark_api_exhausted("router9", seconds, reason)
                    raise
                if kind in ("timeout", "auth"):
                    if not has_fallback:
                        raise
                    logger.warning(
                        "9Router %s; chuyển provider luôn.",
                        f"timeout sau {_call_timeout_sec()}s"
                        if kind == "timeout"
                        else "từ chối key",
                    )
                    await provider_state.mark_router9_dead(
                        "timeout" if kind == "timeout" else "API key bị từ chối (401/403)"
                    )
                    raise
                should_die = provider_state.note_router9_health_failure()
                if should_die or attempt == 2:
                    if not has_fallback:
                        raise
                    logger.warning("9Router vẫn lỗi (%s); chuyển provider.", _short_error(exc))
                    await provider_state.mark_router9_dead("lỗi máy chủ/kết nối 2 lần liên tiếp")
                    raise
                logger.warning("Gọi 9Router lỗi lần 1 (%s), thử lại 1 lần.", _short_error(exc))
        raise RuntimeError("unreachable")  # pragma: no cover

    last_exc: Optional[BaseException] = None
    known_bad_skipped: list[str] = []
    cooling_skipped: list[str] = []

    for provider in order:
        if provider == "router9":
            if not provider_state.router9_enabled:
                continue
            if provider_state.router9_dead_since is not None:
                known_bad_skipped.append("router9")
                continue
            if provider_state.api_in_cooldown("router9"):
                # 429 của router9 chỉ là cooldown ngắn (Retry-After), không phải
                # chết - không gọi lại kể cả ở lượt cứu cánh cuối.
                cooling_skipped.append("router9")
                continue
            has_fallback = await _has_any_fallback_configured()
            try:
                return await _attempt_router9_with_health(has_fallback)
            except TurnTimeoutError:
                raise
            except Exception as exc:
                last_exc = exc
        elif provider in ("api1", "api2"):
            idx = 1 if provider == "api1" else 2
            if not await provider_overrides.is_enabled(provider):
                continue
            if not await official_client.api_key_for(idx):
                continue
            if provider_state.api_in_cooldown(provider):
                cooling_skipped.append(provider)
                continue
            try:
                return await _attempt_api(idx)
            except TurnTimeoutError:
                raise
            except Exception as exc:
                if official_client.is_quota_exhausted_error(exc):
                    seconds, reason = cooldown.cooldown_for(provider, exc)
                    await provider_state.mark_api_exhausted(provider, seconds, reason)
                    last_exc = exc
                    continue
                _log_provider_failure(provider, exc)
                last_exc = exc
        else:
            call = generic_calls.get(provider)
            is_configured = _GENERIC_PROVIDER_CONFIGURED.get(provider)
            if call is None or (is_configured and not is_configured()):
                continue
            if not await provider_overrides.is_enabled(provider):
                continue
            cooldown_key = _cooldown_key(provider)
            if provider_state.api_in_cooldown(cooldown_key):
                cooling_skipped.append(cooldown_key)
                continue
            try:
                return await _attempt_generic(provider)
            except TurnTimeoutError:
                raise
            except Exception as exc:
                if openai_compatible.is_rate_limited(exc):
                    seconds, reason = cooldown.cooldown_for(provider, exc)
                    await provider_state.mark_api_exhausted(cooldown_key, seconds, reason)
                    last_exc = exc
                    continue
                _log_provider_failure(provider, exc)
                last_exc = exc

    # Cứu cánh cuối: chỉ router9 (chết vì timeout/lỗi tạm, không phải quota).
    # Provider đang cooldown 429 bị bỏ qua thật sự - gọi lại chỉ tốn thời gian
    # của lượt và thêm 1 lỗi 429 vào quota đang cạn.
    if "router9" in known_bad_skipped:
        try:
            return await _attempt_router9()
        except TurnTimeoutError:
            raise
        except Exception as exc:
            last_exc = exc
    skipped = known_bad_skipped + cooling_skipped
    if last_exc is None and skipped:
        last_exc = RuntimeError("Mọi provider đang cooldown/chết: " + ", ".join(skipped))

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(
        "Không có provider nào khả dụng (tất cả lỗi, chưa cấu hình, hoặc đang cooldown quota)."
    )


async def ask(
    prompt: str,
    model: Optional[str] = None,
    enable_search: bool = False,
    require_real_search: bool = False,
    providers_override: Optional[list[str]] = None,
):
    """Run a one-turn task through the provider chain.

    ``require_real_search`` forces a directive that triggers the dedicated
    search chain (see _search_only_providers): Groq browser_search first, then
    Google Search grounding (api1/api2), then OpenRouter/9Router only when their
    web search is enabled. Raises RealSearchUnavailableError when no search
    provider is configured.

    ``providers_override`` lets callers pin a smaller provider order instead
    of using _search_only_providers() (for example /gia excludes OpenRouter
    because Tavily already runs before the grounded fallback).
    """
    if require_real_search and providers_override is None:
        providers_override = await _search_only_providers()
    allow_cjk = cjk_output_expected(prompt)
    effective_prompt = f"{_FORCED_SEARCH_DIRECTIVE}\n\n{prompt}" if require_real_search else prompt
    model_name = model or await router9_client.get_preferred_model_name()

    async def _router9_call():
        if require_real_search and router9_client.supports_web_search():
            return await router9_client.generate_search(effective_prompt, model=model_name)
        return await router9_client.generate(effective_prompt, model=model_name)

    async def _groq_call():
        # require_real_search: Groq tự tra web bằng browser_search (model
        # GROQ_REALTIME_MODEL) thay vì model chat thường - xem
        # groq_client.generate_realtime.
        if require_real_search:
            return await groq_client.generate_realtime(effective_prompt)
        return await groq_client.generate(effective_prompt, model=model)

    async def _openrouter_call():
        if require_real_search and openrouter_client.supports_web_search():
            return await openrouter_client.generate_search(effective_prompt)
        return await openrouter_client.generate(effective_prompt, model=model)

    async def _api_call(idx: int):
        # KHÔNG truyền model_name (tên model của catalog 9Router, có thể mang
        # tiền tố nhà cung cấp khác như "notion/...") sang official_client -
        # SDK Google AI Studio chỉ hiểu tên model CHÍNH THỨC của Google. Nếu
        # người dùng chỉ định `model` tường minh (tham số hàm này, không phải
        # preferred_model_name lấy từ router9), vẫn tôn trọng - chỉ bỏ qua
        # phần suy ra từ router9_client.get_preferred_model_name().
        return await official_client.generate(
            idx,
            effective_prompt,
            model=model,
            enable_search=enable_search or require_real_search,
        )

    with call_stage("search") if require_real_search else nullcontext():
        return await _run_provider_chain(
            router9_call=_router9_call,
            api_call=_api_call,
            groq_call=_groq_call,
            openrouter_call=_openrouter_call,
            providers_override=providers_override,
            allow_cjk=allow_cjk,
        )


async def _read_history(user_id: int) -> list[tuple[str, str]]:
    """Lịch sử phiên cho lượt chat; DB lỗi thì trả [] (trả lời không kèm lịch sử)
    thay vì làm hỏng cả lượt - _with_reconnect đã tự thử lại 1 lỗi kết nối lẻ."""
    try:
        return await db.get_session_messages(
            user_id, config.CHAT_HISTORY_TURNS, config.CHAT_SESSION_TIMEOUT_SEC
        )
    except Exception:
        logger.warning(
            "Không đọc được lịch sử chat user_id=%s (DB lỗi) - trả lời không kèm lịch sử.",
            user_id,
            exc_info=True,
        )
        return []


def _resolve_search_mode(
    search_mode: Optional[str], require_real_search: bool, web_grounded: bool
) -> str:
    """Tương thích tham số cũ: require_real_search -> "forced", web_grounded
    (đã có kết quả Tavily) -> "off"; không truyền gì -> "auto"."""
    if require_real_search:
        return SEARCH_FORCED
    if search_mode in SEARCH_MODES:
        return search_mode
    if search_mode:
        logger.warning("search_mode=%r không hợp lệ, dùng 'auto'.", search_mode)
    return SEARCH_OFF if web_grounded else SEARCH_AUTO


def _persona_variant(user_id: int, is_admin: bool) -> str:
    """Hồ sơ chủ bot trong persona (user_profile) chỉ dành cho chủ bot
    (ALLOWED_USER_ID); mọi id Zalo (âm) nhận biến thể không có mục đó - admin
    Zalo có thể là người khác chủ bot (/zaloadmin), nên không bao giờ nhận
    biến thể "owner" chỉ vì có quyền admin."""
    if config.is_owner_user_id(user_id):
        return config.PERSONA_OWNER
    return config.PERSONA_ZALO_ADMIN if is_admin else config.PERSONA_MEMBER


async def chat(
    user_id: int,
    prompt: str,
    grounding: str = "",
    memory_context: str = "",
    require_real_search: bool = False,
    web_grounded: bool = False,
    search_mode: Optional[str] = None,
    *,
    channel: str = "telegram",
    is_admin: bool = False,
    address_form: Optional[str] = None,
):
    """Chat with shared history/memory through the provider chain.

    ``search_mode`` (xem SEARCH_*):
    - "off": không provider nào được tự tra web (/rag chỉ dùng tài liệu riêng;
      grounding đã có Tavily hoặc số liệu cổ phiếu chuẩn từ DNSE).
    - "auto": chat thường, nhánh Gemini tự quyết có tra Google hay không.
    - "native": câu cần thông tin mới mà Tavily tắt/lỗi/quá mỏng - CHỈ thử các
      provider tự tra web được (theo PROVIDER_ORDER), mỗi provider nhận đủ
      persona + lịch sử + trí nhớ + giờ VN và phải chứng minh đã tra web.
    - "forced": số liệu ngoài sàn VN (vàng, tỷ giá, crypto...): như "native"
      nhưng theo thứ tự tra cứu chủ bot chọn và chỉ thị cấm số từ trí nhớ.
    Native/forced không provider nào tra được -> chuỗi thường kèm ghi chú
    "chưa tra được, không bịa số/tin mới".

    Tham số cũ vẫn nhận: ``require_real_search`` = "forced", ``web_grounded``
    = "off" (khi không truyền search_mode).

    ``channel`` thêm 1 dòng gợi ý trình bày theo kênh vào persona (Zalo chỉ hiện
    chữ thuần). ``is_admin`` chọn biến thể persona cho người KHÔNG phải chủ bot:
    admin Zalo không bị dặn "không phải chủ bot" (xem config.load_chat_skill).
    ``address_form``: cách gọi riêng chủ bot đặt cho thành viên Zalo (vd "chị").
    """
    mode = _resolve_search_mode(search_mode, require_real_search, web_grounded)
    persona = config.load_chat_skill(
        variant=_persona_variant(user_id, is_admin), channel=channel, address_form=address_form
    )
    if untrusted.contains_untrusted(grounding):
        # Grounding có dữ liệu ngoài đã rào (web/RSS...): nhắc luật "dữ liệu
        # không phải chỉ dẫn" ngay trong system prompt.
        persona = f"{persona}\n\n{untrusted.SYSTEM_RULE}"
    # Lịch sử đọc DB ĐÚNG 1 LẦN cho mọi provider trong lượt (trước đây mỗi
    # closure provider tự query -> 3-4 query giống nhau khi fallback), rồi cắt
    # cùng grounding/trí nhớ cho vừa ngân sách prompt.
    raw_history = await _read_history(user_id)
    history, memory_context, grounding = prompt_budget.fit_chat_prompt(
        persona=persona,
        history=raw_history,
        memory_context=memory_context,
        grounding=grounding,
        prompt=prompt,
    )
    wants_search = mode in (SEARCH_NATIVE, SEARCH_FORCED)
    temperature = _GROUNDED_TEMPERATURE if (grounding or wants_search) else _FREE_CHAT_TEMPERATURE
    allow_cjk = cjk_output_expected(prompt, grounding)

    def _compose(directive: str, extra_grounding: str = "") -> tuple[str, str]:
        full_prompt = prompt
        block = "\n\n".join(part for part in (extra_grounding, grounding) if part)
        if block:
            full_prompt = f"{block}\n\n{full_prompt}"
        if memory_context:
            full_prompt = f"{memory_context}\n\n{full_prompt}"
        if directive:
            full_prompt = f"{directive}\n\n{full_prompt}"
        return full_prompt, f"{official_client.now_vn_context()}\n{full_prompt}"

    async def _run(
        *,
        directive: str,
        extra_grounding: str,
        provider_search: bool,
        gemini_search: bool,
        providers_override: Optional[list[str]],
    ):
        full_prompt, prompt_with_time = _compose(directive, extra_grounding)

        async def _router9_call():
            # 9Router không giữ session/persona phía server như cookie cũ - phải
            # tự nhét system_instruction (persona) + lịch sử hội thoại từ DB vào
            # mỗi lượt gọi, giống hệt cách nhánh api1/api2 đã làm.
            preferred_model = await router9_client.get_preferred_model_name()
            generate = (
                router9_client.generate_search if provider_search else router9_client.generate
            )
            return await generate(
                prompt_with_time,
                system_instruction=persona,
                history=history,
                model=preferred_model,
                temperature=temperature,
            )

        async def _groq_call():
            generate = groq_client.generate_realtime if provider_search else groq_client.generate
            return await generate(
                prompt_with_time,
                system_instruction=persona,
                history=history,
                temperature=temperature,
            )

        async def _openrouter_call():
            generate = (
                openrouter_client.generate_search if provider_search else openrouter_client.generate
            )
            return await generate(
                prompt_with_time,
                system_instruction=persona,
                history=history,
                temperature=temperature,
            )

        async def _api_call(idx: int):
            # Không truyền preferred_model (tên model catalog 9Router) sang
            # official_client - xem chú thích tương tự trong ask()._api_call.
            # official_client.generate() tự dùng config.GOOGLE_AI_STUDIO_MODEL
            # khi model=None. official_client tự thêm dòng giờ VN.
            return await official_client.generate(
                idx,
                full_prompt,
                system_instruction=persona,
                history=history,
                persona_generation_config=True,
                temperature=temperature,
                enable_search=provider_search or gemini_search,
                require_search_evidence=provider_search,
            )

        return _strip_citations(
            await _run_provider_chain(
                router9_call=_router9_call,
                api_call=_api_call,
                groq_call=_groq_call,
                openrouter_call=_openrouter_call,
                providers_override=providers_override,
                allow_cjk=allow_cjk,
            )
        )

    if wants_search:
        providers = (
            await _search_capable(_FORCED_SEARCH_ORDER)
            if mode == SEARCH_FORCED
            else await _search_capable(config.PROVIDER_ORDER)
        )
        if providers:
            try:
                with call_stage("search"):
                    response = await _run(
                        directive=(
                            _FORCED_SEARCH_DIRECTIVE
                            if mode == SEARCH_FORCED
                            else _NATIVE_SEARCH_DIRECTIVE
                        ),
                        extra_grounding="",
                        provider_search=True,
                        gemini_search=True,
                        providers_override=providers,
                    )
                try:
                    response.searched = True  # bằng chứng đã kiểm ở từng provider
                except AttributeError:
                    pass
                return response
            except TurnTimeoutError:
                raise
            except Exception as exc:
                logger.warning(
                    "Lượt cần tra web (%s): không provider nào tra được (%s) - trả lời "
                    "qua chuỗi thường kèm ghi chú chưa tra được.",
                    mode,
                    _short_error(exc),
                )
        else:
            logger.warning("Lượt cần tra web (%s) nhưng chưa provider nào tự tra web được.", mode)
        with call_stage("chat"):
            return await _run(
                directive="",
                extra_grounding=_SEARCH_UNAVAILABLE_NOTE,
                provider_search=False,
                gemini_search=False,
                providers_override=None,
            )

    with call_stage("chat"):
        return await _run(
            directive="",
            extra_grounding="",
            provider_search=False,
            gemini_search=mode == SEARCH_AUTO,
            providers_override=None,
        )


async def utility_json(prompt: str) -> Optional[dict]:
    """Lượt phụ trả JSON (trích trí nhớ, router tool): Groq (json_object, nhanh,
    free, không đụng quota Gemini) trước, Gemini utility làm dự phòng. Không
    raise - trả None khi mọi đường đều lỗi (caller coi là "bỏ qua lượt này")."""
    if (
        _GENERIC_PROVIDER_CONFIGURED["groq"]()
        and await provider_overrides.is_enabled("groq")
        and not provider_state.api_in_cooldown("groq")
    ):
        try:
            with call_stage("utility"):
                data, latency_ms = await _timed("groq", lambda: groq_client.generate_json(prompt))
            if isinstance(data, dict):
                await _record_provider_call("groq", latency_ms=latency_ms)
                return data
        except TurnTimeoutError:
            raise
        except Exception as exc:
            if openai_compatible.is_rate_limited(exc):
                seconds, reason = cooldown.cooldown_for("groq", exc)
                await provider_state.mark_api_exhausted("groq", seconds, reason)
            logger.info("utility_json: Groq lỗi, dùng Gemini utility: %r", exc)
    with call_stage("utility"):
        return await official_client.generate_utility_json(prompt)


async def reset_chat() -> None:
    # 9Router không giữ ChatSession phía server (không như cookie cũ) - lịch
    # sử hội thoại hoàn toàn nằm trong DB, xoá qua core.database.clear_chat()
    # (gọi riêng ở nơi gọi reset_chat(), xem handlers/commands.py). Hàm này
    # giữ lại để tương thích chữ ký cũ và dự phòng cho state tương lai.
    async with call_lock:
        return


async def analyze_image(instruction: str, image_path: str):
    async def _router9_call():
        return await router9_client.generate_image_prompt(instruction, image_path)

    async def _groq_call():
        return await groq_client.generate_image_prompt(instruction, image_path)

    async def _openrouter_call():
        return await openrouter_client.generate_image_prompt(instruction, image_path)

    async def _api_call(idx: int):
        return await official_client.generate_image_prompt(idx, instruction, image_path)

    return await _run_provider_chain(
        router9_call=_router9_call,
        api_call=_api_call,
        groq_call=_groq_call,
        openrouter_call=_openrouter_call,
        allow_cjk=cjk_output_expected(instruction),
    )


async def check_router9_status() -> tuple[bool, str]:
    try:
        return await _run_with_call_timeout(router9_client.check_status)
    except asyncio.TimeoutError:
        timeout_sec = _call_timeout_sec()
        logger.warning("Probe 9Router quá %ss.", timeout_sec)
        return False, f"TimeoutError: ping 9Router quá {timeout_sec}s"


async def check_groq_status() -> tuple[bool, str]:
    return await groq_client.check_status()


async def check_openrouter_status() -> tuple[bool, str]:
    return await openrouter_client.check_status()


async def check_ai_studio_status(idx: int) -> tuple[bool, str]:
    return await official_client.check_ai_studio_status(idx)


async def try_router9_now() -> tuple[bool, str]:
    await provider_state.ensure_loaded()
    async with call_lock:
        ok, detail = await router9_client.check_status()
        if ok:
            await provider_state.mark_router9_alive()
            await provider_state.set_active_provider("router9")
        return ok, detail


_probe_task: Optional[asyncio.Task] = None


async def _router9_probe_loop() -> None:
    while True:
        await asyncio.sleep(config.ROUTER9_PROBE_INTERVAL_SEC)
        await provider_state.ensure_loaded()
        if provider_state.router9_dead_since is None:
            continue
        try:
            async with call_lock:
                ok, _ = await router9_client.check_status()
                if ok:
                    await provider_state.mark_router9_alive()
                    await provider_state.set_active_provider("router9")
        except Exception:
            logger.warning("Lỗi khi probe 9Router nền.", exc_info=True)


def start_background_tasks() -> None:
    global _probe_task
    if _probe_task is None or _probe_task.done():
        _probe_task = asyncio.create_task(_router9_probe_loop())
