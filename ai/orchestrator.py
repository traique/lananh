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
from typing import Awaitable, Callable, Optional

from core import config, database as db
from ai import openai_compatible, router9_client, groq_client, openrouter_client, official_client
from ai import provider_overrides
from ai import provider_state as provider_state_module
from ai.provider_state import ProviderStateSnapshot, provider_state

logger = logging.getLogger(__name__)
call_lock = asyncio.Lock()


def _call_timeout_sec() -> float:
    return config.ROUTER9_CALL_TIMEOUT_SEC


async def _run_with_call_timeout(call_fn):
    return await asyncio.wait_for(call_fn(), timeout=_call_timeout_sec())


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
    """Return the provider order for require_real_search: api1 -> api2 -> openrouter.

    Mọi tìm kiếm thật (chat cần dữ liệu ngoài sàn VN, /gia) BẮT BUỘC đi qua
    Google AI Studio với Google Search tool bật sẵn (enable_search=True, xem
    ask()/chat() ở dưới) - api1 trước, lỗi/hết quota mới rớt xuống api2.
    router9/groq bị loại khỏi nhánh này để không lệ thuộc việc 9Router có tự
    bật đúng tool search hay không. openrouter đứng CUỐI làm lưới an toàn khi
    cả 2 key Google đều lỗi/hết quota - chấp nhận model ":free" không có tool
    search đảm bảo, còn hơn không trả lời được gì.
    """
    order = [
        provider
        for provider in ("api1", "api2")
        if await official_client.api_key_for(1 if provider == "api1" else 2)
    ]
    if config.OPENROUTER_API_KEY:
        order.append("openrouter")
    if not order:
        raise RealSearchUnavailableError(
            "Tác vụ yêu cầu Google Search thật nhưng chưa cấu hình "
            "GOOGLE_AI_STUDIO_API_KEY_1/2 hoặc OPENROUTER_API_KEY."
        )
    return order


_FORCED_SEARCH_DIRECTIVE = (
    "[YÊU CẦU BẮT BUỘC TỪ HỆ THỐNG]\n"
    "Câu hỏi này cần số liệu/sự kiện thực tế bên ngoài sàn chứng khoán Việt Nam "
    "(giá hàng hoá, tỷ giá, crypto, chỉ số quốc tế, tin thời sự). Hệ thống KHÔNG "
    "có sẵn dữ liệu này để cung cấp cho bạn.\n"
    "1. BẮT BUỘC dùng Google Search để tra trước khi trả lời.\n"
    "2. CHỈ được nêu con số, mốc thời gian và sự kiện có TRONG kết quả tra cứu. "
    "Kèm theo thời điểm của số liệu và tên nguồn.\n"
    "3. Nếu tra không ra dữ liệu: nói thẳng là chưa tra được và DỪNG LẠI. "
    "TUYỆT ĐỐI KHÔNG đưa ra bất kỳ con số hay sự kiện nào từ trí nhớ, không ước "
    "lượng, không suy diễn. Trả lời \"em chưa tra được\" là ĐÚNG; đoán một con "
    "số nghe hợp lý là SAI nghiêm trọng."
)


_GENERIC_PROVIDER_CONFIGURED: dict[str, Callable[[], bool]] = {
    "groq": lambda: bool(config.GROQ_API_KEY),
    "openrouter": lambda: bool(config.OPENROUTER_API_KEY),
}


async def _run_provider_chain(
    *,
    router9_call,
    api_call,
    groq_call: Optional[Callable[[], Awaitable]] = None,
    openrouter_call: Optional[Callable[[], Awaitable]] = None,
    providers_override: Optional[list[str]] = None,
):
    """Run providers in configured order with persisted health state.

    router9 dùng cơ chế dead/retry/probe riêng (mark_router9_dead/alive).
    api1/api2/groq/openrouter đều single-try + cooldown-on-quota giống nhau,
    chỉ khác api_call() nhận idx (int) còn groq_call/openrouter_call() không
    nhận tham số - do api_call() đã có contract cũ (test_provider_chain.py).
    """
    await provider_state.ensure_loaded()
    order = providers_override if providers_override is not None else config.PROVIDER_ORDER
    generic_calls: dict[str, Optional[Callable[[], Awaitable]]] = {
        "groq": groq_call,
        "openrouter": openrouter_call,
    }

    async def _attempt_router9():
        result = await _run_with_call_timeout(router9_call)
        await provider_state.mark_router9_alive()
        await provider_state.set_active_provider("router9")
        return result

    async def _attempt_api(idx: int):
        result = await api_call(idx)
        await provider_state.set_active_provider(f"api{idx}")
        return result

    async def _attempt_generic(name: str):
        result = await generic_calls[name]()
        await provider_state.set_active_provider(name)
        return result

    async def _has_any_fallback_configured() -> bool:
        if await official_client.api_key_for(1) or await official_client.api_key_for(2):
            return True
        return any(
            generic_calls.get(name) is not None and is_configured()
            for name, is_configured in _GENERIC_PROVIDER_CONFIGURED.items()
        )

    async with call_lock:
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
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "Gọi Gemini (9Router) lỗi/treo lần 1, thử lại 1 lần.",
                        exc_info=True,
                    )
                    try:
                        return await _attempt_router9()
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
            except Exception as exc:
                last_exc = exc

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            "Không có provider nào khả dụng (tất cả lỗi, chưa cấu hình, "
            "hoặc đang cooldown quota)."
        )


async def ask(
    prompt: str,
    model: Optional[str] = None,
    enable_search: bool = False,
    require_real_search: bool = False,
    providers_override: Optional[list[str]] = None,
):
    """Run a one-turn task through the provider chain.

    ``require_real_search`` forces a directive that reliably triggers real
    web search regardless of provider (see _search_only_providers): router9
    trước (đã tự bật search phía server), fail kết nối mới rơi xuống Groq
    compound-mini / Gemini grounding. Raises RealSearchUnavailableError khi
    không có provider nào cấu hình.

    ``providers_override`` cho phép caller tự chỉ định thứ tự provider thay
    vì để require_real_search tự suy ra qua _search_only_providers() (vd
    /gia muốn giới hạn nhánh Google Search tool chỉ còn api1 -> api2, không
    có openrouter, vì bước Tavily đứng trước đã là lưới an toàn đầu tiên -
    xem handlers/commands.py::_search_price).
    """
    if require_real_search and providers_override is None:
        providers_override = await _search_only_providers()
    effective_prompt = (
        f"{_FORCED_SEARCH_DIRECTIVE}\n\n{prompt}" if require_real_search else prompt
    )
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
    )


async def chat(
    user_id: int,
    prompt: str,
    grounding: str = "",
    memory_context: str = "",
    require_real_search: bool = False,
):
    """Chat with shared history/memory through the provider chain."""
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
            system_instruction=config.load_chat_skill(),
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
            system_instruction=config.load_chat_skill(),
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
            system_instruction=config.load_chat_skill(),
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
            system_instruction=config.load_chat_skill(),
            history=history,
            persona_generation_config=True,
            enable_search=True,
        )

    providers_override = await _search_only_providers() if require_real_search else None
    return await _run_provider_chain(
        router9_call=_router9_call,
        api_call=_api_call,
        groq_call=_groq_call,
        openrouter_call=_openrouter_call,
        providers_override=providers_override,
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
