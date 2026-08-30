"""Tầng dùng chung cho các nhánh OpenAI-compatible của provider-chain
(9Router, Groq, OpenRouter): HTTP client pool, dựng messages, POST
/chat/completions (+ parse SSE dự phòng khi gateway trả stream dù request
không xin), gọi ảnh, check_status - chuẩn hoá lỗi thành OpenAICompatibleError.
router9_client/groq_client/openrouter_client chỉ còn giữ phần đặc thù riêng
(model mặc định, generate_realtime của Groq, catalog model của 9Router...).
"""
import asyncio
import base64
import json
import logging
import mimetypes
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)


class OpenAICompatibleError(RuntimeError):
    """Lỗi khi gọi 1 gateway OpenAI-compatible bất kỳ (HTTP lỗi, payload rỗng)."""


class Response:
    """Bọc kết quả text trả về, cùng interface `.text` như FallbackResponse
    của official_client, để orchestrator dùng chung 1 contract cho mọi nhánh."""

    def __init__(self, text: str) -> None:
        self.text = text


class ToolCallResponse:
    """Kết quả 1 lượt /chat/completions có gửi kèm `tools` (OpenAI function-
    calling): hoặc có `tool_calls` (model muốn gọi tool, chưa phải câu trả
    lời cuối), hoặc có `text` (câu trả lời cuối, không cần tool nào nữa).
    Dùng riêng, KHÔNG dùng chung class `Response` ở trên, vì caller (agent
    loop) cần phân biệt rõ 2 trường hợp này để quyết định lặp tiếp hay dừng."""

    def __init__(self, text: str, tool_calls: list[dict[str, Any]]) -> None:
        self.text = text
        # Mỗi item: {"id": str, "name": str, "arguments": dict}
        self.tool_calls = tool_calls


class ClientPool:
    """1 httpx.AsyncClient + 1 Semaphore dùng chung cho 1 nhánh provider,
    khởi tạo lười ở lần gọi đầu (event loop chưa chạy lúc import module)."""

    def __init__(self, timeout_sec: float, max_concurrency: int) -> None:
        self._timeout_sec = timeout_sec
        self._max_concurrency = max_concurrency
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore: Optional[asyncio.Semaphore] = None

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_sec)
        return self._client

    def get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, self._max_concurrency))
        return self._semaphore

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def build_messages(
    prompt: str,
    system_instruction: Optional[str] = None,
    history: Optional[list[tuple[str, str]]] = None,
) -> list[dict[str, Any]]:
    """role "model" (quy ước Gemini) được map sang "assistant" (chuẩn OpenAI)."""
    messages: list[dict[str, Any]] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    if history:
        for role, content in history:
            messages.append({"role": "assistant" if role == "model" else "user", "content": content})
    messages.append({"role": "user", "content": prompt})
    return messages


def parse_sse_content(raw: str) -> str:
    """Một số gateway (khi provider phía sau chỉ hỗ trợ streaming) trả về
    text/event-stream ngay cả khi request không xin stream. Gom lại nội dung
    delta/message từ các chunk JSON trong luồng SSE đó."""
    pieces: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices", []):
            delta_content = (choice.get("delta") or {}).get("content")
            if delta_content:
                pieces.append(delta_content)
            msg_content = (choice.get("message") or {}).get("content")
            if msg_content:
                pieces.append(msg_content)
    return "".join(pieces)


async def post_chat_completion(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    messages: list[dict[str, Any]],
    model: str,
    temperature: float,
    max_tokens: int,
    provider_label: str,
) -> str:
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type or response.text.lstrip().startswith("data:"):
            text = parse_sse_content(response.text)
        else:
            completion_payload = response.json()
            text = ((completion_payload.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        raise OpenAICompatibleError(f"{provider_label} HTTP {exc.response.status_code}: {body}") from exc
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise OpenAICompatibleError(f"{provider_label}: {type(exc).__name__}: {exc}") from exc
    if not text:
        raise OpenAICompatibleError(f"{provider_label} trả kết quả rỗng")
    return text.strip()


def _parse_tool_calls(raw_tool_calls: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Chuyển `message.tool_calls` chuẩn OpenAI (mỗi item có `function.name` +
    `function.arguments` là JSON string) thành list dict gọn: {id, name,
    arguments (đã parse JSON)}. Bỏ qua item thiếu name hoặc arguments không
    parse được (coi như [] cho item đó, không raise - để 1 tool_call hỏng
    không làm hỏng cả response)."""
    if not raw_tool_calls:
        return []
    parsed: list[dict[str, Any]] = []
    for call in raw_tool_calls:
        function = call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        raw_args = function.get("arguments")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else {}
        except json.JSONDecodeError:
            args = {}
        parsed.append({"id": call.get("id") or f"call_{len(parsed)}", "name": name, "arguments": args})
    return parsed


async def post_chat_completion_with_tools(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    temperature: float,
    max_tokens: int,
    provider_label: str,
) -> ToolCallResponse:
    """Giống post_chat_completion() nhưng gửi kèm `tools` (function-calling
    chuẩn OpenAI) và trả về ToolCallResponse (text + tool_calls) thay vì chỉ
    str. Tách riêng khỏi post_chat_completion() thay vì thêm tham số optional
    vào đó - contract trả về khác hẳn nhau (str vs có tool_calls), gộp lại sẽ
    bắt mọi caller cũ (groq_client/openrouter_client/router9_client.generate)
    phải xử lý thêm 1 nhánh không liên quan tới họ.

    KHÔNG xử lý nhánh SSE dự phòng như post_chat_completion() (tool_calls
    xuất hiện dạng chunk rời rạc trong stream, ghép lại phức tạp và ít gateway
    stream khi đã tắt `stream`) - nếu gateway trả SSE dù đã xin JSON thường,
    `response.json()` sẽ raise ValueError -> OpenAICompatibleError, caller tự
    fallback provider kế tiếp như mọi lỗi khác.

    CẢNH BÁO QUAN TRỌNG (đọc kỹ trước khi tin tưởng nhánh này ở production):
    một số gateway OpenAI-compatible bên thứ ba (vd 9Router) có thể ÂM THẦM
    bỏ qua `tools` (không lỗi, chỉ trả lời chữ bình thường) nếu backend thật
    sự đứng sau không hỗ trợ function-calling. Hàm này không có cách nào tự
    phát hiện được sự khác biệt giữa "model chủ động thấy không cần tool" và
    "gateway lờ tools đi" - cả 2 đều trả tool_calls rỗng. Xem log ở
    ai/agent_service.py::_run_router9 để tự kiểm chứng bằng traffic thật.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "tools": tools,
    }
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        completion_payload = response.json()
        message = ((completion_payload.get("choices") or [{}])[0]).get("message") or {}
        text = (message.get("content") or "").strip()
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        raise OpenAICompatibleError(f"{provider_label} HTTP {exc.response.status_code}: {body}") from exc
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise OpenAICompatibleError(f"{provider_label}: {type(exc).__name__}: {exc}") from exc
    if not text and not tool_calls:
        raise OpenAICompatibleError(f"{provider_label} trả kết quả rỗng (không text, không tool_calls)")
    return ToolCallResponse(text, tool_calls)


def is_rate_limited(exc: BaseException) -> bool:
    """True nếu lỗi là HTTP 429 (hết quota/rate-limit) - dựa trên message do
    post_chat_completion() tự dựng ở trên (f"{label} HTTP {status}: ...")."""
    return isinstance(exc, OpenAICompatibleError) and "HTTP 429" in str(exc)


async def generate_image_prompt(
    pool: ClientPool,
    *,
    base_url: str,
    api_key: str,
    vision_model: str,
    provider_label: str,
    instruction: str,
    image_path: str,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> Response:
    """Gửi ảnh dạng base64 data URL theo chuẩn nội dung đa phương tiện OpenAI
    (image_url). Chỉ hoạt động nếu vision_model hỗ trợ vision - nếu không,
    gateway/model phía sau sẽ tự trả lỗi rõ ràng."""

    def _read_bytes() -> bytes:
        with open(image_path, "rb") as file:
            return file.read()

    image_bytes = await asyncio.to_thread(_read_bytes)
    mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    async with pool.get_semaphore():
        text = await post_chat_completion(
            pool.get_client(),
            base_url=base_url,
            api_key=api_key,
            messages=messages,
            model=vision_model,
            temperature=temperature,
            max_tokens=max_tokens,
            provider_label=provider_label,
        )
    return Response(text)


async def check_status(
    generate_fn: Callable[..., Awaitable[Any]],
    *,
    api_key: str,
    missing_key_msg: str,
    expected_error: type[BaseException] = OpenAICompatibleError,
) -> tuple[bool, str]:
    """Ping generate_fn("ping", max_tokens=8) và chuẩn hoá kết quả cho lệnh
    /status - dùng chung cho router9/groq/openrouter. `expected_error` là lỗi
    "đã biết" của nhánh gọi (router9_client bọc thành Router9Error thay vì
    OpenAICompatibleError thẳng) - báo str(exc) gọn, không kèm tên class."""
    if not api_key:
        return False, missing_key_msg
    try:
        await generate_fn("ping", max_tokens=8)
        return True, "OK"
    except expected_error as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
