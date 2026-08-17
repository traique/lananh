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
    OFFICIAL_EMBED_TIMEOUT_SEC,
    OFFICIAL_STATUS_TIMEOUT_SEC,
    OFFICIAL_UTILITY_TIMEOUT_SEC,
    OFFICIAL_VISION_TIMEOUT_SEC,
    with_timeout,
)
from core import config

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_VN_WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
_official_clients: dict[int, object] = {}
_PERSONA_TEMPERATURE = 0.95
_PERSONA_TOP_P = 0.95
_EMBEDDING_MODEL = "text-embedding-004"


def api_key_for(idx: int) -> Optional[str]:
    return config.GOOGLE_AI_STUDIO_API_KEY_1 if idx == 1 else config.GOOGLE_AI_STUDIO_API_KEY_2


def _get_official_client(idx: int):
    if idx not in _official_clients:
        from google import genai

        key = api_key_for(idx)
        if not key:
            raise RuntimeError(f"Chưa cấu hình GOOGLE_AI_STUDIO_API_KEY_{idx}")
        _official_clients[idx] = genai.Client(api_key=key)
    return _official_clients[idx]


def now_vn_context() -> str:
    now = datetime.now(_VN_TZ)
    return f"[Thời điểm hiện tại: {now:%H:%M} ngày {now:%d/%m/%Y} ({_VN_WEEKDAYS[now.weekday()]}), giờ Việt Nam]"


class FallbackResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.used_fallback = True


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
) -> FallbackResponse:
    from google.genai import types

    client = _get_official_client(idx)
    prompt_with_time = f"{now_vn_context()}\n{prompt}"
    if history:
        contents = [
            types.Content(
                role="model" if role == "model" else "user",
                parts=[types.Part.from_text(text=content)],
            )
            for role, content in history
        ]
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt_with_time)]))
    else:
        contents = prompt_with_time

    cfg_kwargs = {"system_instruction": system_instruction or None}
    if persona_generation_config:
        cfg_kwargs.update(temperature=_PERSONA_TEMPERATURE, top_p=_PERSONA_TOP_P)
    if enable_search:
        cfg_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

    response = await with_timeout(
        client.aio.models.generate_content(
            model=model or config.GOOGLE_AI_STUDIO_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        ),
        OFFICIAL_CHAT_TIMEOUT_SEC,
        f"api{idx} generate_content",
    )
    return FallbackResponse((response.text or "").strip())


async def generate_image_prompt(idx: int, instruction: str, image_path: str) -> FallbackResponse:
    from google.genai import types

    client = _get_official_client(idx)

    def _read_bytes() -> bytes:
        with open(image_path, "rb") as file:
            return file.read()

    image_bytes = await asyncio.to_thread(_read_bytes)
    mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    response = await with_timeout(
        client.aio.models.generate_content(
            model=config.GOOGLE_AI_STUDIO_MODEL,
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
    for idx in (1, 2):
        if not api_key_for(idx):
            continue
        try:
            client = _get_official_client(idx)
            response = await with_timeout(
                client.aio.models.generate_content(
                    model=config.GOOGLE_AI_STUDIO_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type="application/json",
                    ),
                ),
                OFFICIAL_UTILITY_TIMEOUT_SEC,
                f"api{idx} utility JSON",
            )
            raw = (response.text or "").strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            return json.loads(raw)
        except Exception as exc:
            last_exc = exc
            logger.warning("generate_utility_json: api%s lỗi, thử key kế tiếp.", idx, exc_info=True)
    if last_exc is not None:
        logger.warning("generate_utility_json: hết key khả dụng, bỏ qua tác vụ.")
    return None


async def embed_text(text: str) -> Optional[list[float]]:
    if not config.HAS_ANY_AI_STUDIO_KEY:
        return None
    for idx in (1, 2):
        if not api_key_for(idx):
            continue
        try:
            client = _get_official_client(idx)
            result = await with_timeout(
                client.aio.models.embed_content(model=_EMBEDDING_MODEL, contents=text),
                OFFICIAL_EMBED_TIMEOUT_SEC,
                f"api{idx} embedding",
            )
            embeddings = getattr(result, "embeddings", None)
            if not embeddings:
                return None
            values = getattr(embeddings[0], "values", None)
            return list(values) if values else None
        except Exception:
            logger.warning("embed_text: api%s lỗi, thử key kế tiếp.", idx, exc_info=True)
    return None


async def check_ai_studio_status(idx: int) -> tuple[bool, str]:
    key = api_key_for(idx)
    if not key:
        return False, f"Chưa cấu hình GOOGLE_AI_STUDIO_API_KEY_{idx}"
    try:
        from google.genai import types
    except ImportError:
        return False, "Chưa cài package google-genai"
    try:
        client = _get_official_client(idx)
        await with_timeout(
            client.aio.models.generate_content(
                model=config.GOOGLE_AI_STUDIO_MODEL,
                contents="ping",
                config=types.GenerateContentConfig(max_output_tokens=1),
            ),
            OFFICIAL_STATUS_TIMEOUT_SEC,
            f"api{idx} status check",
        )
        return True, "OK"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
