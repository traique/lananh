"""Official Google AI Studio providers used by the provider chain."""

import asyncio
import json
import logging
import mimetypes
import re
import time
from datetime import datetime
from typing import Iterable, Optional
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
from ai import call_log, provider_overrides, structured
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
    def __init__(self, text: str, *, searched: bool = False, citations: Iterable[str] = ()) -> None:
        self.text = text
        self.used_fallback = True
        # Gemini đã THẬT SỰ gọi Google Search trong lượt này (có grounding metadata).
        self.searched = searched
        # Nguồn grounding theo TÊN MIỀN (web.uri là link chuyển hướng vertexaisearch,
        # không phải URL thật) - nguồn được phép của bộ lọc link câu trả lời.
        self.citations = list(citations)[:_MAX_GROUNDING_SOURCES]


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


_MAX_GROUNDING_SOURCES = 40
_HOSTNAME_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$", re.IGNORECASE
)


def _grounding_sources(response) -> list[str]:
    """``https://<tên miền>`` của từng nguồn Google Search grounding (web.domain,
    hoặc web.title khi title là tên miền - kiểu "vnexpress.net"). Bộ lọc link cho
    phép trang chủ/tên miền đó, link có đường dẫn/query vẫn phải khớp nguồn khác."""
    found: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        meta = getattr(candidate, "grounding_metadata", None)
        for chunk in getattr(meta, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            for value in (getattr(web, "domain", None), getattr(web, "title", None)):
                host = str(value or "").strip().lower().removeprefix("www.")
                if host and _HOSTNAME_RE.match(host):
                    source = f"https://{host}"
                    if source not in found:
                        found.append(source)
                    break
            if len(found) >= _MAX_GROUNDING_SOURCES:
                return found
    return found


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


# ─── Ngân sách suy luận (thinking) ───────────────────────────────────────────
# Token suy luận của Gemini tính vào quota và độ trễ: lượt JSON/tiện ích luôn 0, lượt
# chat theo GEMINI_CHAT_THINKING_BUDGET (None = mặc định của model). Model từ chối
# tham số này (400, vd model chỉ chạy ở chế độ suy luận) thì gọi lại 1 lần không kèm
# và nhớ model đó trong RAM (có trần) để lượt sau khỏi tốn request hỏng.
UTILITY_THINKING_BUDGET = 0
_THINKING_REJECTED: set[str] = set()
_THINKING_REJECTED_MAX = 8
_THINKING_ERROR_RE = re.compile(r"think", re.IGNORECASE)


def _thinking_config(model: str, budget: Optional[int]) -> dict:
    if budget is None or model in _THINKING_REJECTED:
        return {}
    from google.genai import types

    return {"thinking_config": types.ThinkingConfig(thinking_budget=budget)}


def _rejects_thinking(exc: BaseException) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code == 400 and bool(_THINKING_ERROR_RE.search(str(exc)))


def _forget_thinking(model: str, exc: BaseException) -> None:
    if len(_THINKING_REJECTED) < _THINKING_REJECTED_MAX:
        _THINKING_REJECTED.add(model)
    logger.warning("Model %s không nhận thinking_config (%s) - gọi lại không kèm.", model, exc)


async def _generate_content(
    client, model: str, contents, cfg_kwargs: dict, label: str, limit_sec: float
):
    """generate_content có trần ``limit_sec``. Tham số tuỳ chọn model từ chối (400):
    thinking_config (_forget_thinking) và response_json_schema (ai/structured) - bỏ
    đúng tham số đó, gọi lại; mỗi tham số tối đa 1 lần."""
    from google.genai import types

    async def _call(kwargs: dict):
        return await with_timeout(
            client.aio.models.generate_content(
                model=model, contents=contents, config=types.GenerateContentConfig(**kwargs)
            ),
            limit_sec,
            label,
        )

    kwargs = dict(cfg_kwargs)
    while True:
        try:
            return await _call(kwargs)
        except Exception as exc:
            if "thinking_config" in kwargs and _rejects_thinking(exc):
                _forget_thinking(model, exc)
                kwargs.pop("thinking_config")
            elif "response_json_schema" in kwargs and structured.rejected(exc):
                structured.mark_unsupported("gemini", model, exc)
                kwargs.pop("response_json_schema")
            else:
                raise


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
    max_output_tokens: Optional[int] = None,
    json_mode: bool = False,
    thinking_budget: Optional[int] = None,
) -> FallbackResponse:
    """``temperature``: ghi đè nhiệt độ persona (lượt có dữ liệu tra cứu dùng
    thấp hơn để giữ đúng số liệu). ``require_search_evidence``: lượt bắt buộc
    tự tra web - Gemini trả lời mà không có grounding metadata thì raise
    NoSearchEvidenceError (provider-chain chuyển provider kế tiếp).
    ``max_output_tokens``/``json_mode`` (orchestrator.ask cho việc phụ ngắn): trần
    token trả lời và JSON mode (response_mime_type) của Gemini.
    ``thinking_budget``: token suy luận tối đa (0 = tắt; None = mặc định của model)."""
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
        cfg_kwargs.update(temperature=config.CHAT_TEMPERATURE, top_p=_PERSONA_TOP_P)
    if temperature is not None:
        cfg_kwargs["temperature"] = temperature
    if max_output_tokens:
        cfg_kwargs["max_output_tokens"] = max_output_tokens
    if json_mode and not enable_search:
        # Gemini không cho JSON mode cùng tool google_search.
        cfg_kwargs["response_mime_type"] = "application/json"
    if enable_search:
        cfg_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

    model_name = model or await _model_for(idx)
    cfg_kwargs.update(_thinking_config(model_name, thinking_budget))

    call_log.note_prompt_chars(
        len(prompt_with_time)
        + len(system_instruction or "")
        + sum(len(content) for _role, content in history or ())
    )
    response = await _generate_content(
        client,
        model_name,
        contents,
        cfg_kwargs,
        f"api{idx} generate_content",
        OFFICIAL_CHAT_TIMEOUT_SEC,
    )
    call_log.note_gemini_usage(getattr(response, "usage_metadata", None))
    searched = bool(enable_search) and _used_google_search(response)
    if require_search_evidence and not searched:
        from ai.openai_compatible import NoSearchEvidenceError

        raise NoSearchEvidenceError(f"api{idx} trả lời nhưng không gọi Google Search")
    return FallbackResponse(
        (response.text or "").strip(),
        searched=searched,
        citations=_grounding_sources(response) if searched else (),
    )


async def generate_image_prompt(idx: int, instruction: str, image_path: str) -> FallbackResponse:
    from google.genai import types

    client = await _get_official_client(idx)

    def _read_bytes() -> bytes:
        with open(image_path, "rb") as file:
            return file.read()

    image_bytes = await asyncio.to_thread(_read_bytes)
    mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    call_log.note_prompt_chars(len(instruction))
    response = await with_timeout(
        client.aio.models.generate_content(
            model=await _model_for(idx),
            contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type), instruction],
        ),
        OFFICIAL_VISION_TIMEOUT_SEC,
        f"api{idx} image prompt",
    )
    call_log.note_gemini_usage(getattr(response, "usage_metadata", None))
    return FallbackResponse((response.text or "").strip())


async def generate_from_bytes(
    idx: int, instruction: str, data: bytes, mime_type: str, *, label: str
) -> FallbackResponse:
    """1 lượt Gemini nhận file inline (Part.from_bytes) + chỉ dẫn: nghe audio, đọc
    PDF. Gemini giới hạn cả request inline 20 MB - caller đã chặn kích thước
    file nhỏ hơn nhiều (VOICE_MAX_BYTES, DOCUMENT_MAX_BYTES)."""
    from google.genai import types

    client = await _get_official_client(idx)
    call_log.note_prompt_chars(len(instruction))
    response = await with_timeout(
        client.aio.models.generate_content(
            model=await _model_for(idx),
            contents=[types.Part.from_bytes(data=data, mime_type=mime_type), instruction],
        ),
        OFFICIAL_VISION_TIMEOUT_SEC,
        f"api{idx} {label}",
    )
    call_log.note_gemini_usage(getattr(response, "usage_metadata", None))
    return FallbackResponse((response.text or "").strip())


async def transcribe_audio(
    idx: int, audio: bytes, mime_type: str, instruction: str
) -> FallbackResponse:
    """Gemini nghe audio inline (audio/ogg, audio/m4a, audio/aac...) và chép lời."""
    return await generate_from_bytes(idx, instruction, audio, mime_type, label="audio")


INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
_INTERACTIONS_TIMEOUT_SEC = 60.0
# Lỗi KHÔNG phải quota của đường interactions (model cũ không hỗ trợ, schema đổi):
# chỉ cảnh báo 1 lần mỗi process rồi dùng generate_content (không phong cách).
# Lỗi 4xx (yêu cầu bị từ chối hẳn) thì thôi thử interactions 6 giờ để mỗi tin
# thoại không tốn thêm 1 request hỏng.
_interactions_warned = False
_INTERACTIONS_BACKOFF_SEC = 6 * 3600
_interactions_off_until = 0.0


class InteractionsHTTPError(RuntimeError):
    """Interactions API trả HTTP lỗi; ``status_code`` để is_quota_exhausted_error nhận 429."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"{status_code} {message}")
        self.status_code = status_code


def _find_audio(payload) -> tuple[str, str] | None:
    """(base64, mime) của khối âm thanh CUỐI trong JSON interactions (steps[].content[]
    theo REST, hoặc outputs/output_audio) - dò đệ quy để chịu được đổi vỏ JSON."""
    found: list[tuple[str, str]] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            data = node.get("data")
            if node.get("type") == "audio" and isinstance(data, str) and data:
                found.append((data, str(node.get("mime_type") or node.get("mimeType") or "")))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(payload)
    return found[-1] if found else None


async def _synthesize_interactions(
    idx: int, text: str, *, model: str, voice: str, style: str
) -> tuple[bytes, str]:
    """Gemini TTS 3.8 qua Interactions API (REST, theo ai.google.dev/gemini-api/docs/
    speech-generation 10/2026): ``text`` là bản chép nguyên văn, phong cách đọc đi
    trong annotation ``speech_metadata.style`` - model 3.8 KHÔNG làm theo chỉ dẫn
    viết lẫn trong chữ (sẽ đọc to cả chỉ dẫn). Gọi REST bằng httpx thay vì
    client.interactions của SDK: SDK chưa có kiểu annotation speech_metadata."""
    import base64

    import httpx

    from services import http_client

    key = await api_key_for(idx)
    if not key:
        raise RuntimeError(f"Chưa cấu hình GOOGLE_AI_STUDIO_API_KEY_{idx}")
    body = {
        "model": model,
        "input": [
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [{"type": "speech_metadata", "style": style}],
                    }
                ],
            }
        ],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice}]},
    }
    try:
        response = await http_client.get_client().post(
            INTERACTIONS_URL,
            json=body,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            timeout=_INTERACTIONS_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"api{idx} TTS interactions lỗi mạng: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise InteractionsHTTPError(response.status_code, response.text[:300])
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"api{idx} TTS interactions trả JSON hỏng") from exc
    audio = _find_audio(payload)
    if audio is None:
        raise RuntimeError(f"api{idx} TTS interactions không trả âm thanh")
    data, mime = audio
    return base64.b64decode(data), mime or "audio/wav"


async def synthesize_speech(
    idx: int, text: str, *, model: str, voice: str, style: str = ""
) -> tuple[bytes, str]:
    """Gemini TTS (model *-tts) đọc ``text`` bằng 1 giọng dựng sẵn -> (âm thanh,
    mime). Model đời 2.5/3.1 trả PCM thô ("audio/L16;codec=pcm;rate=24000"), đời
    3.8 trả WAV có header RIFF - services/voice_reply.py nhận cả hai. Text là bản
    chép nguyên văn (model TTS không làm theo chỉ dẫn trong text).

    ``style`` (VOICE_STYLE_PROMPT) khác rỗng: thử Interactions API có annotation
    phong cách trước; lỗi quota (429) thì raise như thường (cooldown TTS), lỗi khác
    thì rơi về generate_content không phong cách. Caller bọc bounded() (hạn chót lượt)."""
    global _interactions_warned, _interactions_off_until
    if style and time.monotonic() >= _interactions_off_until:
        try:
            return await _synthesize_interactions(idx, text, model=model, voice=voice, style=style)
        except Exception as exc:
            if is_quota_exhausted_error(exc):
                raise
            status = getattr(exc, "status_code", None)
            if isinstance(status, int) and 400 <= status < 500:
                _interactions_off_until = time.monotonic() + _INTERACTIONS_BACKOFF_SEC
            log = logger.info if _interactions_warned else logger.warning
            _interactions_warned = True
            log("api%s TTS có phong cách lỗi (%s) - đọc không phong cách.", idx, exc)
    from google.genai import types

    client = await _get_official_client(idx)
    response = await client.aio.models.generate_content(
        model=model,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        ),
    )
    call_log.note_gemini_usage(getattr(response, "usage_metadata", None))
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                return bytes(data), str(getattr(inline, "mime_type", "") or "")
    raise RuntimeError(f"api{idx} TTS không trả về âm thanh")


async def analyze_document(
    idx: int, instruction: str, path: str, mime_type: str = "application/pdf"
) -> FallbackResponse:
    """Gemini đọc tài liệu (PDF) từ file tạm trên đĩa - chỉ đọc vào RAM lúc gọi."""

    def _read_bytes() -> bytes:
        with open(path, "rb") as file:
            return file.read()

    data = await asyncio.to_thread(_read_bytes)
    return await generate_from_bytes(idx, instruction, data, mime_type, label="document")


async def generate_utility_json(prompt: str) -> Optional[dict]:
    """Lượt phụ trả JSON qua Gemini (dự phòng của orchestrator.utility_json). Mỗi
    key thử được ghi provider_calls (ai/call_log.py, giai đoạn mặc định "utility")
    kèm độ trễ, kết quả và token như provider-chain."""
    if not config.HAS_ANY_AI_STUDIO_KEY:
        return None
    last_exc: Optional[BaseException] = None
    for idx in await usable_indices():
        try:
            client = await _get_official_client(idx)
            model_name = await _model_for(idx)

            cfg_kwargs: dict = {
                "temperature": 0.1,
                "response_mime_type": "application/json",
                # Lượt JSON tiện ích: không tốn token/độ trễ cho suy luận.
                **_thinking_config(model_name, UTILITY_THINKING_BUDGET),
            }
            schema = structured.for_model("gemini", model_name)
            if schema is not None:
                # Caller mở ai/structured.json_schema: Gemini trả đúng khuôn đó.
                cfg_kwargs["response_json_schema"] = schema[1]

            def _call(
                client=client,
                model_name=model_name,
                cfg_kwargs=cfg_kwargs,
                label=f"api{idx} utility JSON",
            ):
                return _generate_content(
                    client, model_name, prompt, cfg_kwargs, label, OFFICIAL_UTILITY_TIMEOUT_SEC
                )

            async def _attempt(_call=_call):
                # bounded(): tôn trọng hạn chót còn lại của lượt (hoặc của task nền
                # đặt riêng) thay vì 45s cố định ngoài mọi ngân sách thời gian.
                call_log.note_prompt_chars(len(prompt))
                response = await bounded(_call, OFFICIAL_UTILITY_TIMEOUT_SEC)
                call_log.note_gemini_usage(getattr(response, "usage_metadata", None))
                return json.loads(strip_json_fence(response.text))

            return await call_log.timed(
                f"api{idx}",
                model_name,
                _attempt,
                stage=call_log.current_stage() or "utility",
                classify_error=classify_error,
            )
        except TurnTimeoutError:
            raise
        except Exception as exc:
            last_exc = exc
            await note_failure(idx, exc)
            logger.warning("generate_utility_json: api%s lỗi, thử key kế tiếp.", idx, exc_info=True)
    if last_exc is not None:
        logger.warning("generate_utility_json: hết key khả dụng, bỏ qua tác vụ.")
    return None


def classify_error(exc: BaseException) -> str:
    if isinstance(exc, TurnTimeoutError):
        return "turn_timeout"
    if is_quota_exhausted_error(exc):
        return "rate_limited"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "error"


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
