"""Facade công khai của package `ai`: kết hợp nhánh 9Router (ai/router9_client.py),
Groq (ai/groq_client.py), OpenRouter (ai/openrouter_client.py) và nhánh
api1/api2 (ai/official_client.py) thành provider-chain có fallback, theo thứ
tự core.config.PROVIDER_ORDER (mặc định router9 -> groq -> openrouter -> api1
-> api2 - toàn bộ đều miễn phí, Gemini official đứng cuối làm lưới an toàn).

- 9Router chết -> chuyển hẳn sang provider kế tiếp, KHÔNG thử lại router9 mọi
  tin nhắn nữa. Chỉ 3 cách quay lại router9: probe nền định kỳ, đổi
  ROUTER9_API_KEY, lệnh /userouter9 (xem
  init_provider_state()/start_background_tasks()/try_router9_now()).
- Ngoài dead/alive tự động còn có cờ bật/tắt thủ công (router9_enabled, mặc
  định bật) qua lệnh /router9 on|off (set_router9_enabled()) - tắt thì
  router9 bị loại khỏi provider-chain hoàn toàn cho tới khi bật lại, kể cả
  khi đang "sống". Không ảnh hưởng nhánh search-only (_search_only_providers)
  vì nhánh đó đã không dùng router9.
- groq/openrouter/api1/api2 hết quota (429) -> cooldown API_QUOTA_COOLDOWN_SEC
  rồi tự thử lại; trong lúc đó dùng provider kế tiếp trong order.
"""

import asyncio
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Awaitable, Callable, Iterator, Optional

from core import config, database as db
from ai import openai_compatible, router9_client, groq_client, openrouter_client, official_client
from ai import provider_overrides
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


def _strip_citations(response):
    text = getattr(response, "text", None)
    if isinstance(text, str):
        response.text = _CITATION_RE.sub("", text)
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


async def _search_only_providers() -> list[str]:
    """Provider order cho require_real_search: Groq -> api1 -> api2 -> OpenRouter.

    Groq Compound và Google AI Studio đều có tool search thật. OpenRouter chỉ
    đứng cuối làm lưới an toàn vì model free không đảm bảo có web-search tool.
    """
    order: list[str] = []
    if config.GROQ_API_KEY:
        order.append("groq")
    for provider, idx in (("api1", 1), ("api2", 2)):
        if await official_client.api_key_for(idx):
            order.append(provider)
    if config.OPENROUTER_API_KEY:
        order.append("openrouter")
    if not order:
        raise RealSearchUnavailableError(
            "Tác vụ yêu cầu tìm kiếm web thật nhưng chưa cấu hình "
            "GROQ_API_KEY, GOOGLE_AI_STUDIO_API_KEY_1/2 hoặc OPENROUTER_API_KEY."
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


async def _record_provider_call(provider: str) -> None:
    """Ghi 1 lượt gọi thành công vào bảng provider_calls (thống kê "lượt gọi
    theo model" ở trang admin). Lỗi DB ở đây KHÔNG được làm hỏng câu trả lời
    thật cho người dùng - chỉ log cảnh báo rồi bỏ qua."""
    try:
        model = await _effective_model(provider)
        await db.record_provider_call(provider, model)
    except Exception:
        logger.warning("Không ghi được lượt gọi provider=%s vào DB.", provider, exc_info=True)


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
        result = await _run_with_call_timeout(router9_call)
        _assert_output_clean(result, "router9", allow_cjk=allow_cjk)
        await provider_state.mark_router9_alive()
        await provider_state.set_active_provider("router9")
        await _record_provider_call("router9")
        return result

    async def _attempt_api(idx: int):
        result = await bounded(lambda: api_call(idx))
        provider_name = f"api{idx}"
        _assert_output_clean(result, provider_name, allow_cjk=allow_cjk)
        await provider_state.set_active_provider(provider_name)
        await _record_provider_call(provider_name)
        return result

    async def _attempt_generic(name: str):
        result = await bounded(generic_calls[name])
        _assert_output_clean(result, name, allow_cjk=allow_cjk)
        await provider_state.set_active_provider(name)
        await _record_provider_call(name)
        return result

    async def _has_any_fallback_configured() -> bool:
        if await official_client.api_key_for(1) or await official_client.api_key_for(2):
            return True
        return any(
            generic_calls.get(name) is not None and is_configured()
            for name, is_configured in _GENERIC_PROVIDER_CONFIGURED.items()
        )

    last_exc: Optional[BaseException] = None
    known_bad_skipped: list[str] = []

    for provider in order:
        if provider == "router9":
            if not provider_state.router9_enabled:
                continue
            if provider_state.router9_dead_since is not None:
                known_bad_skipped.append("router9")
                continue
            has_fallback = await _has_any_fallback_configured()
            try:
                return await _attempt_router9()
            except TurnTimeoutError:
                raise
            except asyncio.TimeoutError as exc:
                # Timeout nghĩa là router9 đã treo đủ lâu - retry cùng ngân
                # sách chỉ nhân đôi thời gian chờ trước khi fallback.
                last_exc = exc
                if not has_fallback:
                    raise
                logger.warning(
                    "9Router timeout sau %ss; chuyển provider luôn.", _call_timeout_sec()
                )
                await provider_state.mark_router9_dead()
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Gọi Gemini (9Router) lỗi/treo lần 1, thử lại 1 lần.",
                    exc_info=True,
                )
                try:
                    return await _attempt_router9()
                except TurnTimeoutError:
                    raise
                except Exception as retry_exc:
                    last_exc = retry_exc
                    if not has_fallback:
                        raise
                    logger.warning(
                        "9Router vẫn lỗi sau retry; chuyển provider.",
                        exc_info=True,
                    )
                    await provider_state.mark_router9_dead()
        elif provider in ("api1", "api2"):
            idx = 1 if provider == "api1" else 2
            if not await provider_overrides.is_enabled(provider):
                continue
            if not await official_client.api_key_for(idx):
                continue
            if provider_state.api_in_cooldown(provider):
                known_bad_skipped.append(provider)
                continue
            try:
                return await _attempt_api(idx)
            except TurnTimeoutError:
                raise
            except Exception as exc:
                if official_client.is_quota_exhausted_error(exc):
                    await provider_state.mark_api_exhausted(provider)
                    last_exc = exc
                    continue
                logger.exception("%s lỗi (không phải hết quota).", provider)
                last_exc = exc
        else:
            call = generic_calls.get(provider)
            is_configured = _GENERIC_PROVIDER_CONFIGURED.get(provider)
            if call is None or (is_configured and not is_configured()):
                continue
            if not await provider_overrides.is_enabled(provider):
                continue
            if provider_state.api_in_cooldown(provider):
                known_bad_skipped.append(provider)
                continue
            try:
                return await _attempt_generic(provider)
            except TurnTimeoutError:
                raise
            except Exception as exc:
                if openai_compatible.is_rate_limited(exc):
                    await provider_state.mark_api_exhausted(provider)
                    last_exc = exc
                    continue
                logger.exception("%s lỗi (không phải hết quota).", provider)
                last_exc = exc

    for provider in known_bad_skipped:
        try:
            if provider == "router9":
                return await _attempt_router9()
            if provider in ("api1", "api2"):
                return await _attempt_api(1 if provider == "api1" else 2)
            if generic_calls.get(provider) is not None:
                return await _attempt_generic(provider)
        except TurnTimeoutError:
            raise
        except Exception as exc:
            last_exc = exc

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
    search chain (see _search_only_providers): Groq Compound first, then
    Google Search grounding (api1/api2), with OpenRouter as a final fallback.
    Raises RealSearchUnavailableError when no search provider is configured.

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
        return await router9_client.generate(effective_prompt, model=model_name)

    async def _groq_call():
        # require_real_search dùng model compound-mini (tool search tích hợp)
        # thay vì model chat thường - xem groq_client.generate_realtime.
        if require_real_search:
            return await groq_client.generate_realtime(effective_prompt)
        return await groq_client.generate(effective_prompt, model=model)

    async def _openrouter_call():
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

    return await _run_provider_chain(
        router9_call=_router9_call,
        api_call=_api_call,
        groq_call=_groq_call,
        openrouter_call=_openrouter_call,
        providers_override=providers_override,
        allow_cjk=allow_cjk,
    )


async def chat(
    user_id: int,
    prompt: str,
    grounding: str = "",
    memory_context: str = "",
    require_real_search: bool = False,
):
    """Chat with shared history/memory through the provider chain."""
    # Hồ sơ chủ bot trong persona (user_profile) chỉ dành cho chủ bot; Zalo
    # member (id âm) nhận persona không có mục đó.
    persona = config.load_chat_skill(include_owner_profile=config.is_owner_user_id(user_id))
    full_prompt = prompt
    if grounding:
        full_prompt = f"{grounding}\n\n{full_prompt}"
    if memory_context:
        full_prompt = f"{memory_context}\n\n{full_prompt}"
    if require_real_search:
        full_prompt = f"{_FORCED_SEARCH_DIRECTIVE}\n\n{full_prompt}"

    async def _router9_call():
        # 9Router không giữ session/persona phía server như cookie cũ - phải
        # tự nhét system_instruction (persona) + lịch sử hội thoại từ DB vào
        # mỗi lượt gọi, giống hệt cách nhánh api1/api2 đã làm.
        history = await db.get_session_messages(
            user_id, config.CHAT_HISTORY_TURNS, config.CHAT_SESSION_TIMEOUT_SEC
        )
        prompt_with_time = f"{official_client.now_vn_context()}\n{full_prompt}"
        preferred_model = await router9_client.get_preferred_model_name()
        return await router9_client.generate(
            prompt_with_time,
            system_instruction=persona,
            history=history,
            model=preferred_model,
            temperature=0.95,
        )

    async def _groq_call():
        if require_real_search:
            return await groq_client.generate_realtime(full_prompt)
        history = await db.get_session_messages(
            user_id, config.CHAT_HISTORY_TURNS, config.CHAT_SESSION_TIMEOUT_SEC
        )
        prompt_with_time = f"{official_client.now_vn_context()}\n{full_prompt}"
        return await groq_client.generate(
            prompt_with_time,
            system_instruction=persona,
            history=history,
            temperature=0.95,
        )

    async def _openrouter_call():
        history = await db.get_session_messages(
            user_id, config.CHAT_HISTORY_TURNS, config.CHAT_SESSION_TIMEOUT_SEC
        )
        prompt_with_time = f"{official_client.now_vn_context()}\n{full_prompt}"
        return await openrouter_client.generate(
            prompt_with_time,
            system_instruction=persona,
            history=history,
            temperature=0.95,
        )

    async def _api_call(idx: int):
        history = await db.get_session_messages(
            user_id, config.CHAT_HISTORY_TURNS, config.CHAT_SESSION_TIMEOUT_SEC
        )
        # Không truyền preferred_model (tên model catalog 9Router) sang
        # official_client - xem chú thích tương tự trong ask()._api_call.
        # official_client.generate() tự dùng config.GOOGLE_AI_STUDIO_MODEL
        # khi model=None.
        return await official_client.generate(
            idx,
            full_prompt,
            system_instruction=persona,
            history=history,
            persona_generation_config=True,
            enable_search=True,
        )

    providers_override = await _search_only_providers() if require_real_search else None
    return _strip_citations(
        await _run_provider_chain(
            router9_call=_router9_call,
            api_call=_api_call,
            groq_call=_groq_call,
            openrouter_call=_openrouter_call,
            providers_override=providers_override,
            allow_cjk=cjk_output_expected(prompt, grounding),
        )
    )


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
