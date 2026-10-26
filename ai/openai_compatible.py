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
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Optional

import httpx

from ai import call_log, drafts

logger = logging.getLogger(__name__)


class OpenAICompatibleError(RuntimeError):
    """Lỗi khi gọi 1 gateway OpenAI-compatible bất kỳ (HTTP lỗi, payload rỗng).

    ``status_code``: mã HTTP (None nếu lỗi mạng/payload). ``retry_after_sec``:
    số giây provider gợi ý chờ trước khi gọi lại (đọc từ Retry-After,
    X-RateLimit-Reset hoặc câu "try again in Xs" trong body) - dùng để đặt
    cooldown đúng thay vì 1 giờ cố định cho mọi lỗi 429."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retry_after_sec: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_sec = retry_after_sec


# "Please try again in 7.66s" / "try again in 1m30.5s" / "retry in 20s" (Groq,
# OpenRouter và nhiều gateway ghi thời gian chờ ngay trong body lỗi 429).
_RETRY_IN_RE = re.compile(
    r"(?:try again|retry)\s+in\s+(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m(?!s))?"
    r"\s*(?:(\d+(?:\.\d+)?)\s*(s|ms))?",
    re.IGNORECASE,
)


def _seconds_from_body(body: str) -> Optional[float]:
    match = _RETRY_IN_RE.search(body or "")
    if not match:
        return None
    hours, minutes, value, unit = match.groups()
    if not (hours or minutes or value):
        return None
    total = float(hours or 0) * 3600 + float(minutes or 0) * 60
    if value:
        total += float(value) / 1000 if unit and unit.lower() == "ms" else float(value)
    return total


def retry_hint_from_response(response: httpx.Response, body: str = "") -> Optional[float]:
    """Số giây nên chờ, theo thứ tự tin cậy: Retry-After (giây hoặc HTTP-date),
    X-RateLimit-Reset (epoch mili-giây kiểu OpenRouter), rồi câu trong body.
    KHÔNG dùng x-ratelimit-reset-requests của Groq (đó là bucket theo NGÀY,
    có trên mọi response, không phải thời gian chờ của lỗi 429 này)."""
    headers = response.headers
    raw = (headers.get("retry-after") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                when = parsedate_to_datetime(raw)
                return max(0.0, when.timestamp() - time.time())
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
    raw = (headers.get("x-ratelimit-reset") or "").strip()
    if raw:
        try:
            value = float(raw)
            # Epoch mili-giây (OpenRouter) / epoch giây / số giây tương đối.
            if value > 1e11:
                return max(0.0, value / 1000 - time.time())
            if value > 1e9:
                return max(0.0, value - time.time())
            return max(0.0, value)
        except ValueError:
            pass
    return _seconds_from_body(body)


def _error_chain(exc: BaseException):
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def http_status(exc: BaseException) -> Optional[int]:
    """Mã HTTP của lỗi (kể cả khi bị bọc lại, vd Router9Error from exc)."""
    for item in _error_chain(exc):
        status = getattr(item, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def retry_after(exc: BaseException) -> Optional[float]:
    for item in _error_chain(exc):
        value = getattr(item, "retry_after_sec", None)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def error_text(exc: BaseException) -> str:
    """Toàn bộ thông điệp của chuỗi lỗi (để dò dấu hiệu quota theo ngày...)."""
    return " | ".join(str(item) for item in _error_chain(exc))


class NoSearchEvidenceError(OpenAICompatibleError):
    """Lượt "provider tự tra web" trả lời nhưng KHÔNG có bằng chứng đã tra web
    thật (không executed_tools/annotations/grounding metadata...) - hoặc
    provider chưa bật/không hỗ trợ tra web. Coi như provider này thất bại ở
    lượt tra cứu: không tin câu trả lời có thể bịa từ trí nhớ model, và KHÔNG
    phải lỗi sức khoẻ/quota (không cooldown, không khai tử)."""


class Response:
    """Bọc kết quả text trả về, cùng interface `.text` như FallbackResponse
    của official_client, để orchestrator dùng chung 1 contract cho mọi nhánh.

    ``searched``: provider đã TỰ tra web trong chính lượt gọi này (có bằng
    chứng) - caller dùng để biết câu trả lời dựa trên dữ liệu web ngoài."""

    def __init__(self, text: str, *, searched: bool = False) -> None:
        self.text = text
        self.searched = searched


class CompletionResult:
    """Kết quả đầy đủ của 1 lượt /chat/completions: text + bằng chứng tra web
    (``annotations`` url_citation kiểu OpenAI/OpenRouter, ``executed_tools``
    tool chạy phía server kiểu Groq browser_search)."""

    def __init__(
        self,
        text: str,
        annotations: Optional[list[dict[str, Any]]] = None,
        executed_tools: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self.text = text
        self.annotations = annotations or []
        self.executed_tools = executed_tools or []

    @property
    def url_citations(self) -> list[str]:
        urls: list[str] = []
        for item in self.annotations:
            if not isinstance(item, dict) or item.get("type") != "url_citation":
                continue
            citation = (
                item.get("url_citation") if isinstance(item.get("url_citation"), dict) else item
            )
            url = str(citation.get("url") or "")
            if url and url not in urls:
                urls.append(url)
        return urls


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
    khởi tạo lười ở lần gọi đầu (event loop chưa chạy lúc import module).

    ``timeout_sec`` là timeout từng pha của httpx (connect, đọc từng chunk) -
    response stream nhỏ giọt không bao giờ chạm tới nó. ``total_timeout_sec``
    chặn TỔNG thời gian 1 lượt gọi (kể cả chờ semaphore) qua ``bounded()``."""

    def __init__(
        self,
        timeout_sec: float,
        max_concurrency: int,
        total_timeout_sec: Optional[float] = None,
    ) -> None:
        self._timeout_sec = timeout_sec
        self._max_concurrency = max_concurrency
        self._total_timeout_sec = total_timeout_sec
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def bounded(self, label: str, call: Callable[[], Awaitable[Any]]) -> Any:
        """Chạy ``call()`` trong total_timeout_sec; quá hạn -> TimeoutError (lớp
        provider-chain coi như provider chậm, chuyển provider kế tiếp)."""
        timeout = self._total_timeout_sec
        if not timeout or timeout <= 0:
            return await call()
        try:
            return await asyncio.wait_for(call(), timeout=timeout)
        except TimeoutError as exc:
            raise TimeoutError(f"{label}: quá {timeout:g}s (timeout tổng của lượt gọi)") from exc

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
            messages.append(
                {"role": "assistant" if role == "model" else "user", "content": content}
            )
    messages.append({"role": "user", "content": prompt})
    return messages


def parse_sse_content(raw: str) -> str:
    """Gom nội dung text từ các chunk JSON trong luồng SSE (giữ cho tương
    thích cũ; bản đầy đủ text + tool_calls xem `_parse_sse_stream`)."""
    return _parse_sse_stream(raw)[0]


def note_sse_usage(raw: str) -> None:
    """Token của 1 response SSE (ai/call_log.py): OpenRouter luôn gửi ``usage`` ở
    chunk cuối; Groq chỉ gửi khi xin stream_options.include_usage, nằm trong
    ``x_groq.usage`` của chunk cuối có choices rỗng. Lấy chunk cuối có usage."""
    found: Optional[dict[str, Any]] = None
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]" or "usage" not in payload:
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if call_log.openai_usage(chunk):
            found = chunk
            break
    if found is not None:
        call_log.note_openai_usage(found)


def _note_request(messages: list[dict[str, Any]]) -> None:
    call_log.note_prompt_chars(call_log.messages_chars(messages))


def _usage_payload(include_usage: bool) -> dict[str, Any]:
    """Groq stream chỉ trả token khi xin stream_options.include_usage (OpenRouter
    luôn trả; 9Router chưa rõ nên không gửi tham số lạ cho nó)."""
    return {"stream_options": {"include_usage": True}} if include_usage else {}


def _parse_sse_stream(raw: str) -> tuple[str, list[dict[str, Any]]]:
    """Gom 1 response SSE (chat.completion.chunk chuẩn OpenAI) thành
    (text, tool_calls đã parse JSON). Tool_calls dạng delta: chunk đầu mang
    id + function.name (arguments rỗng), các chunk sau nối function.arguments
    theo index. Chunk `{"error": ...}` giữa stream (gateway forward lỗi
    upstream) được trả về qua error_msg để caller raise với thông điệp thật
    thay vì "trả kết quả rỗng"."""
    pieces: list[str] = []
    error_msg: Optional[str] = None
    calls_by_index: dict[int, dict[str, Any]] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(chunk, dict) and chunk.get("error"):
            error = chunk["error"]
            error_msg = (
                str(error.get("message") or error) if isinstance(error, dict) else str(error)
            )
            continue
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            delta_content = delta.get("content")
            if delta_content:
                pieces.append(delta_content)
            msg_content = (choice.get("message") or {}).get("content")
            if msg_content:
                pieces.append(msg_content)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index") or 0
                call = calls_by_index.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    call["id"] = str(tc["id"])
                fn = tc.get("function") or {}
                if fn.get("name"):
                    call["name"] = str(fn["name"])
                if fn.get("arguments"):
                    call["arguments"] += str(fn["arguments"])
    tool_calls = (
        _parse_tool_calls(
            [
                {
                    "id": call["id"] or f"call_{position}",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for position, call in sorted(calls_by_index.items())
            ]
        )
        if calls_by_index
        else []
    )
    text = "".join(pieces)
    if not text and error_msg:
        # Ném ở đây thay vì để caller nhận text rỗng: giữ nguyên thông điệp
        # lỗi upstream (gateway đã gói trong chunk {"error": ...}).
        raise OpenAICompatibleError(f"SSE upstream error: {error_msg[:500]}")
    return text, tool_calls


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
    extra_payload: Optional[dict[str, Any]] = None,
    include_usage: bool = False,
) -> str:
    """LUÔN xin stream (SSE) kể cả khi chỉ cần text cuối: 9Router/chatgpt-
    gateway với request stream:false phải GỘP toàn bộ stream upstream xong
    mới trả 1 phát JSON - trong suốt thời gian generation không có byte nào
    đi qua các proxy trung gian (Faable/Render), và proxy sẽ cắt kết nối
    "im lặng" quá lâu bằng HTTP 504 Gateway Timeout. Với các prompt dài
    (bước tổng hợp cuối của /phantich, generation 60-120s), 504 xuất hiện
    ỔN ĐỊNH mỗi lần - đã xác minh trên chatgpt-gateway (traique/
    chatgpt-gateway, hàm _aggregate_chat: chỉ trả JSON sau khi đọc hết
    stream, gateway không bao giờ tự trả 504). Với stream:true, gateway
    forward chunk ngay khi có delta -> proxy thấy byte chảy, không cắt.
    parse_sse_content() gom delta thành text; nếu gateway vẫn trả JSON
    thường (bỏ qua stream) thì nhánh else xử lý như cũ.

    Token (``usage``) và header x-ratelimit-* đi qua kênh phụ ai/call_log.py, kiểu
    trả về vẫn là str. ``include_usage``: xin stream_options.include_usage (Groq)."""
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        **_usage_payload(include_usage),
    }
    if extra_payload:
        payload.update(extra_payload)
    _note_request(messages)
    try:
        if drafts.partial_sink() is not None:
            # Lượt chat có bản nháp Telegram (ai/drafts.py): đọc SSE theo dòng.
            text = await _post_streaming_text(
                client, f"{base_url.rstrip('/')}/chat/completions", headers, payload
            )
        else:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            call_log.note_ratelimit_headers(response.headers)
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type or response.text.lstrip().startswith("data:"):
                text = parse_sse_content(response.text)
                note_sse_usage(response.text)
            else:
                completion_payload = response.json()
                call_log.note_openai_usage(completion_payload)
                text = ((completion_payload.get("choices") or [{}])[0].get("message") or {}).get(
                    "content", ""
                )
    except httpx.HTTPStatusError as exc:
        raise _http_error(provider_label, exc) from exc
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise OpenAICompatibleError(f"{provider_label}: {type(exc).__name__}: {exc}") from exc
    if not text:
        raise OpenAICompatibleError(f"{provider_label} trả kết quả rỗng")
    return text.strip()


def _delta_text(line: str) -> str:
    """Chữ delta của 1 dòng SSE "data: {...}" (rỗng nếu không có)."""
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return ""
    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError:
        return ""
    if not isinstance(chunk, dict):
        return ""
    pieces = []
    for choice in chunk.get("choices") or []:
        content = (choice.get("delta") or {}).get("content") or (choice.get("message") or {}).get(
            "content"
        )
        if content:
            pieces.append(str(content))
    return "".join(pieces)


async def _post_streaming_text(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], payload: dict[str, Any]
) -> str:
    """Như nhánh SSE của post_chat_completion nhưng đọc từng dòng và đẩy chữ đã
    gom vào bản nháp (ai/drafts.push). Chữ trả về gom lại bằng CÙNG hàm
    _parse_sse_stream để kết quả cuối giống hệt đường đọc 1 lần."""
    async with client.stream("POST", url, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            await response.aread()
        response.raise_for_status()
        call_log.note_ratelimit_headers(response.headers)
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            body = (await response.aread()).decode("utf-8", errors="replace")
            if body.lstrip().startswith("data:"):
                note_sse_usage(body)
                return parse_sse_content(body)
            completion = json.loads(body)
            call_log.note_openai_usage(completion)
            return ((completion.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        lines: list[str] = []
        pieces: list[str] = []
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            lines.append(line)
            delta = _delta_text(line)
            if delta:
                pieces.append(delta)
                drafts.push("".join(pieces))
    raw = "\n".join(lines)
    note_sse_usage(raw)
    return parse_sse_content(raw)


def _http_error(provider_label: str, exc: httpx.HTTPStatusError) -> OpenAICompatibleError:
    body = exc.response.text[:500]
    status = exc.response.status_code
    return OpenAICompatibleError(
        f"{provider_label} HTTP {status}: {body}",
        status_code=status,
        retry_after_sec=retry_hint_from_response(exc.response, body) if status == 429 else None,
    )


def _parse_sse_extras(raw: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Gom annotations + executed_tools nằm rải rác trong các chunk SSE."""
    annotations: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        for choice in chunk.get("choices") or []:
            for part in (choice.get("delta") or {}, choice.get("message") or {}):
                annotations.extend(a for a in part.get("annotations") or [] if isinstance(a, dict))
                executed.extend(t for t in part.get("executed_tools") or [] if isinstance(t, dict))
    return annotations, executed


async def post_chat_completion_full(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    messages: list[dict[str, Any]],
    model: str,
    temperature: float,
    max_tokens: int,
    provider_label: str,
    extra_payload: Optional[dict[str, Any]] = None,
    stream: bool = False,
) -> CompletionResult:
    """Như post_chat_completion() nhưng trả cả bằng chứng tra web
    (annotations/executed_tools) - dùng cho lượt "provider tự tra web".

    ``stream=False`` mặc định: Groq/OpenRouter gọi thẳng (không qua proxy cắt
    kết nối im lặng) và trường executed_tools/annotations nằm gọn trong
    message cuối. 9Router đi qua proxy Render nên vẫn xin stream=True như
    post_chat_completion() - annotations được gom từ các chunk."""
    headers = {"Authorization": f"Bearer {api_key}"}
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if extra_payload:
        payload.update(extra_payload)
    _note_request(messages)
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload
        )
        response.raise_for_status()
        call_log.note_ratelimit_headers(response.headers)
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type or response.text.lstrip().startswith("data:"):
            text = parse_sse_content(response.text)
            annotations, executed = _parse_sse_extras(response.text)
            note_sse_usage(response.text)
        else:
            body = response.json()
            call_log.note_openai_usage(body)
            message = ((body.get("choices") or [{}])[0].get("message")) or {}
            text = message.get("content") or ""
            annotations = [a for a in message.get("annotations") or [] if isinstance(a, dict)]
            executed = [t for t in message.get("executed_tools") or [] if isinstance(t, dict)]
    except httpx.HTTPStatusError as exc:
        raise _http_error(provider_label, exc) from exc
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise OpenAICompatibleError(f"{provider_label}: {type(exc).__name__}: {exc}") from exc
    if not text:
        raise OpenAICompatibleError(f"{provider_label} trả kết quả rỗng")
    return CompletionResult(text.strip(), annotations, executed)


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
        parsed.append(
            {"id": call.get("id") or f"call_{len(parsed)}", "name": name, "arguments": args}
        )
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
    extra_payload: Optional[dict[str, Any]] = None,
    include_usage: bool = False,
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

    Cơ chế stream: giống post_chat_completion() - LUÔN xin stream để tránh
    proxy trung gian cắt kết nối im lặng (HTTP 504) khi gateway gộp stream
    xong mới trả. Tool_calls xuất hiện dạng delta trong các chunk
    (index/id/name ở chunk đầu, arguments nối dần) - `_parse_sse_stream`
    gom lại thành list hoàn chỉnh; nếu gateway trả JSON thường thì xử lý
    như cũ."""
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "tools": tools,
        **_usage_payload(include_usage),
    }
    # Groq: parallel_tool_calls=false (gpt-oss không gọi song song); OpenRouter:
    # provider.require_parameters=true (chỉ đi tới nhà cung cấp hỗ trợ tools).
    if extra_payload:
        payload.update(extra_payload)
    _note_request(messages)
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        call_log.note_ratelimit_headers(response.headers)
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type or response.text.lstrip().startswith("data:"):
            text, tool_calls = _parse_sse_stream(response.text)
            text = text.strip()
            note_sse_usage(response.text)
        else:
            completion_payload = response.json()
            call_log.note_openai_usage(completion_payload)
            message = ((completion_payload.get("choices") or [{}])[0]).get("message") or {}
            text = (message.get("content") or "").strip()
            tool_calls = _parse_tool_calls(message.get("tool_calls"))
    except httpx.HTTPStatusError as exc:
        raise _http_error(provider_label, exc) from exc
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise OpenAICompatibleError(f"{provider_label}: {type(exc).__name__}: {exc}") from exc
    if not text and not tool_calls:
        raise OpenAICompatibleError(
            f"{provider_label} trả kết quả rỗng (không text, không tool_calls)"
        )
    return ToolCallResponse(text, tool_calls)


async def post_audio_transcription(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    audio: bytes,
    filename: str,
    mime_type: str,
    model: str,
    provider_label: str,
    language: str = "",
    prompt: str = "",
) -> str:
    """POST multipart ``/audio/transcriptions`` (chuẩn OpenAI - Groq Whisper).

    Trả lời đã strip, có thể RỖNG (đoạn ghi âm im lặng) - caller tự quyết. Lỗi
    HTTP chuẩn hoá thành OpenAICompatibleError (kèm status_code/Retry-After như
    post_chat_completion) để provider-chain đặt cooldown đúng khi 429."""
    data = {"model": model, "response_format": "json", "temperature": "0"}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            data=data,
            files={"file": (filename, audio, mime_type)},
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise _http_error(provider_label, exc) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise OpenAICompatibleError(f"{provider_label}: {type(exc).__name__}: {exc}") from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise OpenAICompatibleError(f"{provider_label}: response thiếu trường text")
    return text.strip()


def is_rate_limited(exc: BaseException) -> bool:
    """True nếu lỗi là HTTP 429 (hết quota/rate-limit) - theo status_code mang
    theo lỗi, hoặc message do post_chat_completion() tự dựng (f"{label} HTTP
    {status}: ...") cho lỗi cũ/bọc lại."""
    if http_status(exc) == 429:
        return True
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
    include_usage: bool = False,
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

    async def _call() -> str:
        async with pool.get_semaphore():
            return await post_chat_completion(
                pool.get_client(),
                base_url=base_url,
                api_key=api_key,
                messages=messages,
                model=vision_model,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label=provider_label,
                include_usage=include_usage,
            )

    return Response(await pool.bounded(provider_label, _call))


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
