"""Official Google AI Studio providers used by the provider chain."""

import asyncio
import json
import logging
import mimetypes
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from ai.timeouts import (
    OFFICIAL_CHAT_TIMEOUT_SEC,
    OFFICIAL_STATUS_TIMEOUT_SEC,
    OFFICIAL_UTILITY_TIMEOUT_SEC,
    OFFICIAL_VISION_TIMEOUT_SEC,
    TurnTimeoutError,
    bounded,
    with_timeout,
)
from ai import provider_overrides
from ai.provider_state import provider_state
from core import config
from core.text_utils import strip_json_fence

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_VN_WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
# idx (1|2) -> (api_key, genai.Client): tối đa 1 client cho mỗi slot api1/api2,
# nên luân phiên api1/api2 không tạo client mới mỗi lượt; đổi key qua trang
# admin thì client cũ của ĐÚNG slot đó được đóng (giải phóng connection pool).
_official_clients: dict[int, tuple[str, object]] = {}
_PERSONA_TEMPERATURE = 0.95
_PERSONA_TOP_P = 0.95


async def api_key_for(idx: int) -> Optional[str]:
    provider = f"api{idx}"
    override = await provider_overrides.get_api_key_override(provider)
    if override:
        return override
    return config.GOOGLE_AI_STUDIO_API_KEY_1 if idx == 1 else config.GOOGLE_AI_STUDIO_API_KEY_2


async def _model_for(idx: int) -> str:
    return await provider_overrides.get_model_override(f"api{idx}") or config.GOOGLE_AI_STUDIO_MODEL


async def get_client_and_model(idx: int):
    """Truy cập genai client + tên model đã cache cho api{idx} - dùng cho
    ai/agent_service.py (vòng lặp function-calling tự làm việc trực tiếp với
    google.genai types, không đi qua generate() vì generate() không hỗ trợ
    khai báo tool tuỳ ý/nhiều lượt gọi tool)."""
    return await _get_official_client(idx), await _model_for(idx)


async def _close_client(client) -> None:
    try:
        await client.aio.aclose()
    except Exception:
        logger.debug("Đóng async client Google AI Studio cũ lỗi.", exc_info=True)
    try:
        client.close()
    except Exception:
        logger.debug("Đóng client Google AI Studio cũ lỗi.", exc_info=True)


async def _get_official_client(idx: int):
    key = await api_key_for(idx)
    if not key:
        raise RuntimeError(f"Chưa cấu hình GOOGLE_AI_STUDIO_API_KEY_{idx}")
    cached = _official_clients.get(idx)
    if cached is not None and cached[0] == key:
        return cached[1]
    from google import genai

    client = genai.Client(api_key=key)
    _official_clients[idx] = (key, client)
    if cached is not None:
        await _close_client(cached[1])
    return client


async def close() -> None:
    """Đóng mọi client Google AI Studio (gọi lúc tắt app)."""
    clients = [client for _key, client in _official_clients.values()]
    _official_clients.clear()
    for client in clients:
        await _close_client(client)


async def usable_indices() -> list[int]:
    """Các slot api1/api2 dùng được NGAY: có key, admin không tắt, không đang
    cooldown hết quota - dùng cho mọi lượt gọi ngoài provider-chain (trí nhớ,
    tool router, agent) để tôn trọng cùng công tắc/cooldown với chat chính."""
    await provider_state.ensure_loaded()
    usable = []
    for idx in (1, 2):
        name = f"api{idx}"
        if not await api_key_for(idx):
            continue
        if not await provider_overrides.is_enabled(name):
            continue
        if provider_state.api_in_cooldown(name):
            continue
        usable.append(idx)
    return usable


async def note_failure(idx: int, exc: BaseException) -> None:
    """Lỗi hết quota (429) -> đặt cooldown dùng chung với provider-chain, dài
    ngắn theo loại quota Gemini báo (theo ngày/theo phút - ai/cooldown.py)."""
    if is_quota_exhausted_error(exc):
        from ai import cooldown

        try:
            seconds, reason = cooldown.cooldown_for(f"api{idx}", exc)
            await provider_state.mark_api_exhausted(f"api{idx}", seconds, reason)
        except Exception:
            logger.warning("Không ghi được cooldown api%s.", idx, exc_info=True)


def now_vn_context() -> str:
    now = datetime.now(_VN_TZ)
    return f"[Thời điểm hiện tại: {now:%H:%M} ngày {now:%d/%m/%Y} ({_VN_WEEKDAYS[now.weekday()]}), giờ Việt Nam]"


class FallbackResponse:
    def __init__(self, text: str, *, searched: bool = False) -> None:
        self.text = text
        self.used_fallback = True
        # Gemini đã THẬT SỰ gọi Google Search trong lượt này (có grounding metadata).
        self.searched = searched


def _used_google_search(response) -> bool:
    """True nếu response có grounding metadata của Google Search (truy vấn đã
    chạy hoặc trang nguồn) - bằng chứng Gemini đã tra web, không chỉ "được phép"
    tra (bật tool google_search thì model vẫn tự quyết có tra hay không)."""
    for candidate in getattr(response, "candidates", None) or []:
        meta = getattr(candidate, "grounding_metadata", None)
        if meta is None:
            continue
        if getattr(meta, "web_search_queries", None) or getattr(meta, "grounding_chunks", None):
            return True
    return False


def is_quota_exhausted_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code == 429:
        return True
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status == 429:
        return True
    if "ResourceExhausted" in type(exc).__name__:
        return True
    message = str(exc)
    return "RESOURCE_EXHAUSTED" in message or (
        "429" in message and ("quota" in message.lower() or "rate" in message.lower())
    )


async def generate(
    idx: int,
    prompt: str,
    *,
    system_instruction: Optional[str] = None,
    history: Optional[list[tuple[str, str]]] = None,
    persona_generation_config: bool = False,
    enable_search: bool = False,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    require_search_evidence: bool = False,
) -> FallbackResponse:
    """``temperature``: ghi đè nhiệt độ persona (lượt có dữ liệu tra cứu dùng
    thấp hơn để giữ đúng số liệu). ``require_search_evidence``: lượt bắt buộc
    tự tra web - Gemini trả lời mà không có grounding metadata thì raise
    NoSearchEvidenceError (provider-chain chuyển provider kế tiếp)."""
    from google.genai import types

    client = await _get_official_client(idx)
    prompt_with_time = f"{now_vn_context()}\n{prompt}"
    if history:
        contents = [
            types.Content(
                role="model" if role == "model" else "user",
                parts=[types.Part.from_text(text=content)],
            )
            for role, content in history
        ]
        contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=prompt_with_time)])
        )
    else:
        contents = prompt_with_time

    cfg_kwargs = {"system_instruction": system_instruction or None}
    if persona_generation_config:
        cfg_kwargs.update(temperature=_PERSONA_TEMPERATURE, top_p=_PERSONA_TOP_P)
    if temperature is not None:
        cfg_kwargs["temperature"] = temperature
    if enable_search:
        cfg_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

    response = await with_timeout(
        client.aio.models.generate_content(
            model=model or await _model_for(idx),
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        ),
        OFFICIAL_CHAT_TIMEOUT_SEC,
        f"api{idx} generate_content",
    )
    searched = bool(enable_search) and _used_google_search(response)
    if require_search_evidence and not searched:
        from ai.openai_compatible import NoSearchEvidenceError

        raise NoSearchEvidenceError(f"api{idx} trả lời nhưng không gọi Google Search")
    return FallbackResponse((response.text or "").strip(), searched=searched)


async def generate_image_prompt(idx: int, instruction: str, image_path: str) -> FallbackResponse:
    from google.genai import types

    client = await _get_official_client(idx)

    def _read_bytes() -> bytes:
        with open(image_path, "rb") as file:
            return file.read()

    image_bytes = await asyncio.to_thread(_read_bytes)
    mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    response = await with_timeout(
        client.aio.models.generate_content(
            model=await _model_for(idx),
            contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type), instruction],
        ),
        OFFICIAL_VISION_TIMEOUT_SEC,
        f"api{idx} image prompt",
    )
    return FallbackResponse((response.text or "").strip())


async def generate_utility_json(prompt: str) -> Optional[dict]:
    if not config.HAS_ANY_AI_STUDIO_KEY:
        return None
    from google.genai import types

    last_exc: Optional[BaseException] = None
    for idx in await usable_indices():
        try:
            client = await _get_official_client(idx)
            model_name = await _model_for(idx)

            def _call(client=client, model_name=model_name):
                return client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type="application/json",
                    ),
                )

            # bounded(): tôn trọng hạn chót còn lại của lượt (hoặc của task nền
            # đặt riêng) thay vì 45s cố định ngoài mọi ngân sách thời gian.
            response = await bounded(_call, OFFICIAL_UTILITY_TIMEOUT_SEC)
            return json.loads(strip_json_fence(response.text))
        except TurnTimeoutError:
            raise
        except Exception as exc:
            last_exc = exc
            await note_failure(idx, exc)
            logger.warning("generate_utility_json: api%s lỗi, thử key kế tiếp.", idx, exc_info=True)
    if last_exc is not None:
        logger.warning("generate_utility_json: hết key khả dụng, bỏ qua tác vụ.")
    return None


async def check_ai_studio_status(idx: int) -> tuple[bool, str]:
    key = await api_key_for(idx)
    if not key:
        return False, f"Chưa cấu hình GOOGLE_AI_STUDIO_API_KEY_{idx}"
    try:
        from google.genai import types
    except ImportError:
        return False, "Chưa cài package google-genai"
    try:
        client = await _get_official_client(idx)
        await with_timeout(
            client.aio.models.generate_content(
                model=await _model_for(idx),
                contents="ping",
                config=types.GenerateContentConfig(max_output_tokens=1),
            ),
            OFFICIAL_STATUS_TIMEOUT_SEC,
            f"api{idx} status check",
        )
        return True, "OK"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


model_for = _model_for
