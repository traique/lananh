"""AI agent thật: khác ai/orchestrator.py (pipeline CỐ ĐỊNH), module này để
MODEL tự quyết định có cần gọi tool nào, gọi bao nhiêu lần, theo thứ tự nào để
trả lời 1 câu hỏi (/agent - chỉ chủ bot/admin).

Thứ tự provider (ask_agent):
  1. Groq rồi OpenRouter: function-calling chuẩn OpenAI ĐA BƯỚC
     (groq_client/openrouter_client.generate_with_tools; Groq
     parallel_tool_calls=false + max_tokens thấp vì TPM free 8K; OpenRouter
     provider.require_parameters=true để chỉ tới nhà cung cấp hỗ trợ tools).
     Bỏ qua provider chưa cấu hình/đang tắt/cooldown; lỗi (kể cả 429) chỉ
     chuyển provider kế, KHÔNG đặt cooldown - chat (utility_json) không mất Groq.
  2. router9: native tool-calling (_run_router9, đang TẮT qua
     _ROUTER9_NATIVE_TOOLS_ENABLED vì router9 lờ tham số `tools`) rồi
     JSON-router 1 tool (_run_router9_json_router - chỉ cần model theo chỉ dẫn
     "trả JSON").
  3. api1/api2: Gemini function calling (`functionCall`/`functionResponse`),
     quota thấp nhất nên đứng cuối.
Mỗi vòng giới hạn MAX_AGENT_STEPS, mỗi bước qua bounded()/turn_deadline, mỗi
kết quả tool cắt ~2.5K ký tự, gọi lặp y hệt (cùng tool + tham số) bị chặn.

Persona Lan Anh rút gọn + trí nhớ + 4 dòng hội thoại gần nhất của CHÍNH người
hỏi (user_id - Zalo tách hẳn Telegram); Q&A lưu vào lịch sử chat của người đó.

Tool lấy từ danh bạ chung services/tool_registry.py (ToolSpec): tool riêng của
agent (tim_web, tim_gia, xem_thong_ke, xem_gia_co_phieu, doc_link, xem_rss) +
tool CHỈ ĐỌC của chat (ghi chú, nhắc việc, ngày nhớ, chi tiêu, thời tiết, âm
lịch, rag/). Tool GHI dữ liệu (đặt nhắc, ghi chú, chi tiêu...) không vào /agent:
duyệt bằng nút bấm cần callback_query (web.py/main.py chỉ nhận "message") và
không được chờ đồng bộ trong lượt (web.py giữ assistant_turn(OWNER) suốt lượt ->
nút bấm không bao giờ được xử lý kịp). Kết quả tool ngoài (web, link, RSS) được
rào core.untrusted và đánh dấu taint.

Cố ý KHÔNG gọi provider_state.mark_router9_dead() khi router9 lỗi trong agent:
lỗi có thể do router9 không hỗ trợ tools/JSON đúng ý (đặc thù agent), không
phải router9 sập hẳn - tránh làm chat chính mất oan router9. Agent thành công
qua router9 vẫn báo alive và tôn trọng router9_enabled/router9_dead_since.
"""

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator, Optional

from ai import call_log, official_client
from ai.official_client import is_quota_exhausted_error
from ai.timeouts import (
    OFFICIAL_CHAT_TIMEOUT_SEC,
    TurnTimeoutError,
    bounded,
    turn_deadline,
    with_timeout,
)
from core import address, config, untrusted, urls
from core.text_utils import strip_json_fence
from services import tool_registry as reg

logger = logging.getLogger(__name__)

# ─── Chống prompt injection cho doc_link/xem_rss ─────────────────────────────
# Nội dung web (kết quả tim_web, trang đọc qua doc_link) là dữ liệu KHÔNG tin
# cậy nhưng vẫn quay lại vòng lặp agent có tool. Nếu doc_link được đọc URL tuỳ
# ý, 1 trang độc có thể xúi model nhét dữ liệu nội bộ (vd kết quả xem_thong_ke)
# vào query string của 1 URL lạ để rò rỉ ra ngoài. Vì vậy mỗi lượt /agent chỉ
# cho đọc URL CÓ SẴN NGUYÊN VĂN trong câu hỏi của người dùng hoặc trong kết
# quả của tool tìm kiếm - model không thể tự dựng URL mới chứa dữ liệu.
# Tool trả về URL nguồn đáng tin để doc_link được phép đọc tiếp.
_URL_SOURCE_TOOLS = frozenset({"tim_web", "tim_gia"})
# Tool trả về nội dung lấy từ BÊN NGOÀI (web, trang, RSS): kết quả được rào bằng
# core.untrusted trước khi quay lại model - câu "bỏ qua mọi chỉ dẫn" trong trang
# chỉ còn là dữ liệu.
_UNTRUSTED_RESULT_TOOLS = frozenset({"tim_web", "tim_gia", "doc_link", "xem_rss"})
# Trần ký tự 1 kết quả tool ngoài (cắt trước khi rào để marker đóng không mất).
_TOOL_RESULT_MAX_CHARS = 8000
# Trần số URL được phép trong 1 lượt (tránh phình bộ nhớ với kết quả dài).
_MAX_ALLOWED_URLS = 200
_allowed_urls: ContextVar[Optional[set[str]]] = ContextVar("agent_allowed_urls", default=None)

# Tách/chuẩn hoá URL dùng chung với bộ lọc link câu trả lời chat (core/urls.py).
_normalize_url = urls.normalize_url
extract_urls = urls.extract_urls


def _allow_urls_from(text: str) -> None:
    allowed = _allowed_urls.get()
    if allowed is None:
        return
    for url in extract_urls(text):
        if len(allowed) >= _MAX_ALLOWED_URLS:
            break
        allowed.add(url)


@contextmanager
def agent_url_scope(question: str) -> Iterator[set[str]]:
    """Phạm vi 1 lượt agent: URL trong câu hỏi được phép đọc ngay từ đầu."""
    allowed: set[str] = set(extract_urls(question)[:_MAX_ALLOWED_URLS])
    token = _allowed_urls.set(allowed)
    try:
        yield allowed
    finally:
        _allowed_urls.reset(token)


def _url_permitted(url: str) -> bool:
    allowed = _allowed_urls.get()
    # Fail closed: ngoài 1 lượt agent (không có phạm vi) thì không đọc link nào.
    return allowed is not None and _normalize_url(url) in allowed


_URL_NOT_PERMITTED = (
    "Không đọc được link: chỉ được đọc link có nguyên văn trong câu hỏi của người dùng "
    "hoặc trong kết quả tim_web/tim_gia của lượt này."
)


MAX_AGENT_STEPS = 4
# Giai đoạn ghi provider_calls cho mọi request của /agent (ai/call_log.py).
_STAGE = "agent"
# Trần ký tự 1 kết quả tool bất kỳ quay lại model (Groq free 8K TPM: 4 bước với kết
# quả 6000 ký tự là vượt trần ngay bước 2-3).
_ANY_RESULT_MAX_CHARS = 2500
# Trần số lượt gọi (tool, args) khác nhau nhớ để chặn gọi lặp y hệt trong 1 lượt.
_MAX_SEEN_CALLS = 16
_HISTORY_ROWS = 4
_HISTORY_ROW_MAX_CHARS = 500
_MEMORY_MAX_CHARS = 1500
# Tắt hẳn cả 2 nhánh router9 (native tools + JSON-router) của /agent, không
# đụng /router9 on|off của chat chính.
_ROUTER9_AGENT_ENABLED = True
# Tắt RIÊNG nhánh native tool-calling (function-calling chuẩn OpenAI) nếu
# xác nhận qua log router9 đang lờ `tools` đi - xem cảnh báo ở docstring
# module. Nhánh JSON-router (_run_router9_json_router) không bị ảnh hưởng
# bởi cờ này, vẫn thử trước khi rơi xuống api1/api2.
_ROUTER9_NATIVE_TOOLS_ENABLED = False
# Provider tool-calling đa bước thử TRƯỚC router9 (khi đã cấu hình, đang bật và
# không cooldown): Groq (gpt-oss, 1 tool/lần) rồi OpenRouter (require_parameters).
_TOOL_PROVIDERS = ("groq", "openrouter")

_STEP_LIMIT_REPLY = (
    "Xin lỗi, câu hỏi này cần nhiều bước quá, em dừng lại để không lặp vô hạn - anh hỏi "
    "cụ thể/ngắn hơn giúp em nhé."
)


def _persona(address_form: str | None = None) -> str:
    """Persona Lan Anh rút gọn cho /agent (bản đầy đủ quá dài cho TPM free của Groq)."""
    you = address.normalize(address_form) or "anh"
    me = address.self_word(you)
    return (
        f'Bạn là Lan Anh, trợ lý cá nhân của người dùng (xưng "{me}", gọi người dùng là '
        f'"{you}"), giọng nhẹ nhàng, ấm áp, nói thẳng vào việc. Bạn có các tool bên dưới để '
        "tra dữ liệu THẬT trước khi trả lời - KHÔNG tự bịa số liệu. Câu hỏi cần nhiều bước "
        "(vd so sánh 2 sản phẩm) thì gọi tool nhiều lần rồi mới tổng hợp câu trả lời cuối. "
        "Trả lời ngắn gọn bằng tiếng Việt, không cần kể đã dùng tool nào.\n" + untrusted.SYSTEM_RULE
    )


# Giữ tên cũ cho chỗ khác đang import (bản không có trí nhớ/lịch sử).
_SYSTEM_INSTRUCTION = _persona()


@dataclass(frozen=True)
class _Turn:
    """Ngữ cảnh 1 lượt /agent: người hỏi (tool đọc dữ liệu riêng theo user_id này),
    vai trò, persona + trí nhớ, vài lượt hội thoại gần nhất của CHÍNH người đó."""

    user_id: int = 0
    role: str = reg.ROLE_OWNER
    system: str = _SYSTEM_INSTRUCTION
    history: tuple[tuple[str, str], ...] = ()
    seen: dict = field(default_factory=dict)


_turn: ContextVar[_Turn | None] = ContextVar("agent_turn", default=None)


def _current() -> _Turn:
    return _turn.get() or _Turn()


# ─── Tool riêng của agent ───────────────────────────────────────────────────


async def _tool_tim_gia(ten_san_pham: str = "", **_ignored) -> str:
    # Import trễ (lazy) như các tool khác: chỉ nạp module khi model thật sự gọi tool.
    from services import price_search

    if not ten_san_pham or not ten_san_pham.strip():
        return "Lỗi: thiếu tên sản phẩm."
    text, _used_fallback = await price_search.search_price(ten_san_pham.strip())
    return text or "Không tìm được giá cho sản phẩm này."


async def _tool_xem_thong_ke(so_gio: int = 168, **_ignored) -> str:
    from services import usage_stats

    so_gio = so_gio if isinstance(so_gio, int) and so_gio > 0 else 168
    return await usage_stats.build_thongke_text(so_gio, use_html=False)


async def _tool_xem_gia_co_phieu(ma_co_phieu: str = "", **_ignored) -> str:
    from stock import analysis as stock_analysis

    if not ma_co_phieu or not ma_co_phieu.strip():
        return "Lỗi: thiếu mã cổ phiếu."
    return await stock_analysis.quick_quote(ma_co_phieu.strip())


async def _tool_doc_link(url: str = "", **_ignored) -> str:
    from services import web_reader

    if not url or not url.strip():
        return "Lỗi: thiếu URL."
    if not _url_permitted(url):
        return _URL_NOT_PERMITTED
    try:
        return await web_reader.read_url(url.strip())
    except web_reader.WebReaderError as exc:
        return f"Không đọc được link: {exc}"


async def _tool_xem_rss(url: str = "", so_muc: int = 0, **_ignored) -> str:
    from services import web_reader

    if not url or not url.strip():
        return "Lỗi: thiếu URL feed."
    if not _url_permitted(url):
        return _URL_NOT_PERMITTED
    try:
        return await web_reader.read_rss(url.strip(), so_muc)
    except web_reader.WebReaderError as exc:
        return f"Không đọc được feed: {exc}"


async def _tool_tim_web(query: str = "", **_ignored) -> str:
    from services import web_search

    if not query or not query.strip():
        return "Lỗi: thiếu từ khóa tìm kiếm."
    try:
        # deep_read=True: /agent là hành động nghiên cứu chủ động (không phải
        # chat ngẫu nhiên), đáng đọc sâu 1-2 nguồn điểm cao thay vì chỉ dùng
        # snippet Tavily - xem services/web_search.py::_deep_read_top_results.
        topic, time_range = web_search._recency_params(query)
        return (
            await web_search.search_web(
                query.strip(), deep_read=True, topic=topic, time_range=time_range
            )
        ).text
    except web_search.WebSearchError as exc:
        return f"Không tìm được trên web: {exc}"


def agent_handler(fn: Callable[..., Awaitable[str]]) -> reg.ToolHandler:
    """Bọc tool không cần user_id thành handler chuẩn ToolSpec (user_id, **args)."""

    async def handler(user_id: int, **kwargs) -> str:
        return await fn(**kwargs)

    handler.__wrapped__ = fn  # type: ignore[attr-defined]
    return handler


def _agent_spec(name, description, parameters, fn, *, untrusted_output=False, staff=False):
    return reg.ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        handler=agent_handler(fn),
        roles=reg.STAFF_ROLES if staff else reg.ALL_ROLES,
        untrusted_output=untrusted_output,
        surfaces=frozenset({reg.AGENT}),
    )


_AGENT_ONLY = (
    _agent_spec(
        "tim_web",
        "Tìm kiếm web theo từ khóa/câu hỏi, ưu tiên nguồn Việt Nam và trả kèm URL nguồn thật.",
        reg.object_schema(
            {"query": reg.string("Từ khóa hoặc câu hỏi cần tìm trên web")}, required=["query"]
        ),
        _tool_tim_web,
        untrusted_output=True,
    ),
    _agent_spec(
        "tim_gia",
        "Tìm giá bán thực tế của 1 sản phẩm tại Việt Nam (tìm web thật, không bịa).",
        reg.object_schema(
            {"ten_san_pham": reg.string("Tên sản phẩm cần tìm giá, vd 'iPhone 15 128GB'")},
            required=["ten_san_pham"],
        ),
        _tool_tim_gia,
        untrusted_output=True,
    ),
    _agent_spec(
        "xem_thong_ke",
        "Xem thống kê lượt gọi AI của bot (theo user/kênh và theo model) trong N giờ gần nhất.",
        reg.object_schema(
            {"so_gio": reg.integer("Số giờ gần nhất muốn xem, mặc định 168 (7 ngày)")}
        ),
        _tool_xem_thong_ke,
        staff=True,
    ),
    _agent_spec(
        "xem_gia_co_phieu",
        "Xem giá cổ phiếu Việt Nam hiện tại theo mã (vd 'FPT', 'VNM'), lấy trực tiếp từ DNSE.",
        reg.object_schema({"ma_co_phieu": reg.string("Mã cổ phiếu, vd 'FPT'")}, ["ma_co_phieu"]),
        _tool_xem_gia_co_phieu,
    ),
    _agent_spec(
        "doc_link",
        "Đọc nội dung 1 trang web/bài báo cụ thể theo URL người dùng đưa, trả về text sạch để tóm tắt/trả lời.",
        reg.object_schema(
            {"url": reg.string("URL đầy đủ (bắt đầu bằng http:// hoặc https://) cần đọc")},
            required=["url"],
        ),
        _tool_doc_link,
        untrusted_output=True,
    ),
    _agent_spec(
        "xem_rss",
        "Đọc các mục mới nhất từ 1 feed RSS/Atom theo URL người dùng đưa.",
        reg.object_schema(
            {
                "url": reg.string("URL feed RSS/Atom"),
                "so_muc": reg.integer("Số mục mới nhất muốn xem, mặc định 8"),
            },
            required=["url"],
        ),
        _tool_xem_rss,
        untrusted_output=True,
    ),
)
for _spec in _AGENT_ONLY:
    reg.register(_spec)

# Tool CHỈ ĐỌC của chat cũng dùng được trong /agent (danh bạ chung): ghi chú,
# nhắc việc, ngày nhớ, chi tiêu (đọc theo đúng user_id người hỏi), thời tiết, âm
# lịch, kho rag/ (chủ bot/admin). Tool GHI dữ liệu (đặt nhắc, ghi chú, chi tiêu...)
# KHÔNG vào /agent: chưa có duyệt bằng nút bấm (callback_query) - xem docstring.
_SHARED_READ_ONLY = (
    "list_notes",
    "list_reminders",
    "list_special_dates",
    "expense_summary",
    "get_weather",
    "lunar_calendar",
    "get_portfolio",
    "rag_search",
)


def _register_shared() -> None:
    from dataclasses import replace

    from services import tools as chat_tools  # noqa: F401 - đăng ký tool chat vào danh bạ

    for name in _SHARED_READ_ONLY:
        spec = reg.get(name)
        if spec is not None and not spec.side_effect and reg.AGENT not in spec.surfaces:
            reg.register(replace(spec, surfaces=spec.surfaces | {reg.AGENT}))


_register_shared()

# name -> ToolSpec của /agent (tool riêng + tool chỉ đọc dùng chung). Test thay 1
# tool bằng ``monkeypatch.setitem(agent_service._TOOLS, name, replace(spec, ...))``.
_TOOLS: dict[str, reg.ToolSpec] = {spec.name: spec for spec in reg.specs(reg.AGENT)}


def _specs() -> list[reg.ToolSpec]:
    role = _current().role
    return [spec for spec in _TOOLS.values() if role in spec.roles and not spec.side_effect]


def _build_tool_declarations():
    return reg.gemini_declarations(_specs())


def _build_openai_tools() -> list[dict]:
    """Cùng nội dung mô tả/schema với _build_tool_declarations() (1 nguồn là danh
    bạ ToolSpec), chỉ khác khuôn JSON chuẩn OpenAI function-calling."""
    return reg.openai_tools(_specs())


_JSON_ROUTER_INSTRUCTION_TEMPLATE = """Bạn là bộ định tuyến tool (function router) nội bộ cho agent của lananh.
Đọc câu hỏi của người dùng và quyết định có cần gọi 1 trong các tool sau
không. Trả về DUY NHẤT 1 object JSON (không markdown, không code fence,
không thêm chữ nào khác) đúng định dạng:

{{"tool": "ten_tool_hoac_none", "args": {{...}}}}

Danh sách tool khả dụng:
{tool_list}
- "none": câu hỏi KHÔNG cần tool nào (chuyện phiếm, hỏi đáp không cần tra
  cứu dữ liệu thật). args: {{}}.

Quy tắc: CHỈ chọn 1 tool khi câu hỏi có ý định RÕ RÀNG khớp mô tả trên. Nếu
câu hỏi cần NHIỀU tool hoặc nhiều bước (vd so sánh 2 sản phẩm), vẫn chỉ được
chọn ĐÚNG 1 tool phù hợp nhất cho bước đầu tiên (nhánh này không hỗ trợ đa
bước) - hoặc chọn "none" nếu không có tool nào giải quyết được 1 phần rõ
ràng của câu hỏi.

Câu hỏi của người dùng: {question}"""


def _build_tool_list_text() -> str:
    """Liệt kê tool cho prompt JSON-router - sinh từ cùng danh bạ ToolSpec như
    _build_tool_declarations()/_build_openai_tools()."""
    return reg.router_tool_list(_specs())


def _cap(text: str) -> str:
    if len(text) <= _ANY_RESULT_MAX_CHARS:
        return text
    return text[:_ANY_RESULT_MAX_CHARS] + "\n...(đã cắt bớt)"


async def _call_tool(name: str, args: dict) -> str:
    spec = _TOOLS.get(name)
    if spec is None or _current().role not in spec.roles:
        return f"Lỗi: không có tool tên '{name}'."
    if spec.side_effect:
        return f"Lỗi: tool '{name}' ghi dữ liệu, không dùng được trong /agent."
    args = args or {}
    key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True, default=str))
    seen = _current().seen
    if key in seen:
        # Gọi lặp y hệt (vòng lặp model): không chạy lại, nhắc model dùng kết quả cũ.
        return "(Đã gọi tool này với đúng tham số ở bước trước - dùng lại kết quả đó.)"
    try:
        result = await bounded(lambda: spec.handler(_current().user_id, **spec.allowed_args(args)))
        if isinstance(result, reg.ToolResult):
            result = result.text
        result = str(result)
        if name in _URL_SOURCE_TOOLS:
            _allow_urls_from(result)
        if spec.untrusted_output or name in _UNTRUSTED_RESULT_TOOLS:
            reg.mark_tainted()
            if not untrusted.contains_untrusted(result):
                result = (
                    untrusted.wrap_untrusted(
                        result,
                        f"tool {name}",
                        max_chars=min(_TOOL_RESULT_MAX_CHARS, _ANY_RESULT_MAX_CHARS),
                    )
                    or result
                )
        else:
            result = _cap(result)
        if len(seen) < _MAX_SEEN_CALLS:
            seen[key] = True
        return result
    except TurnTimeoutError:
        raise
    except Exception as exc:
        logger.warning("Tool '%s' lỗi với args=%r", name, args, exc_info=True)
        # Trả lỗi VÀO lại cho model (không raise) - để model tự quyết định thử
        # cách khác hoặc báo người dùng, thay vì cả agent crash vì 1 tool lỗi.
        return f"Lỗi khi chạy tool '{name}': {exc}"


def _openai_messages(question: str) -> list[dict]:
    turn = _current()
    messages: list[dict] = [{"role": "system", "content": turn.system}]
    for role, content in turn.history:
        messages.append({"role": "assistant" if role == "model" else "user", "content": content})
    messages.append({"role": "user", "content": question})
    return messages


async def _run_one_provider(idx: int, question: str) -> str:
    from google.genai import types

    client, model = await official_client.get_client_and_model(idx)
    turn = _current()
    contents: list = [
        types.Content(
            role="model" if role == "model" else "user", parts=[types.Part.from_text(text=content)]
        )
        for role, content in turn.history
    ]
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=question)]))
    tools = _build_tool_declarations()
    gen_config = types.GenerateContentConfig(system_instruction=turn.system, tools=tools)

    async def _step(step: int):
        response = await bounded(
            lambda: with_timeout(
                client.aio.models.generate_content(
                    model=model, contents=contents, config=gen_config
                ),
                OFFICIAL_CHAT_TIMEOUT_SEC,
                f"agent api{idx} step {step + 1}",
            )
        )
        call_log.note_gemini_usage(getattr(response, "usage_metadata", None))
        return response

    for step in range(MAX_AGENT_STEPS):
        # Mỗi bước là 1 request tốn quota: ghi provider_calls (giai đoạn "agent").
        response = await call_log.timed(
            f"api{idx}",
            model,
            lambda step=step: _step(step),
            stage=_STAGE,
            classify_error=official_client.classify_error,
        )
        candidate = response.candidates[0] if response.candidates else None
        parts = candidate.content.parts if candidate and candidate.content else []
        function_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

        if not function_calls:
            return (response.text or "").strip() or "Xin lỗi, em chưa tìm được câu trả lời."

        # Model có thể yêu cầu nhiều tool cùng lúc trong 1 bước - chạy tuần tự
        # (đơn giản, đủ dùng cho quy mô 1 user; không cần asyncio.gather).
        contents.append(candidate.content)
        response_parts = []
        for call in function_calls:
            logger.info("Agent gọi tool '%s' với args=%s", call.name, dict(call.args or {}))
            result_text = await _call_tool(call.name, dict(call.args or {}))
            response_parts.append(
                types.Part.from_function_response(name=call.name, response={"result": result_text})
            )
        contents.append(types.Content(role="user", parts=response_parts))

    return _STEP_LIMIT_REPLY


async def _run_openai_tools(
    label: str,
    call: Callable[[list[dict], list[dict]], Awaitable[Any]],
    question: str,
    own_timeout: float | None = None,
    model: str | None = None,
) -> str:
    """Vòng lặp function-calling chuẩn OpenAI (Groq, OpenRouter, router9): cùng
    MAX_AGENT_STEPS, cùng _call_tool(), mỗi bước qua bounded()/turn_deadline và
    được ghi provider_calls (ai/call_log.py, giai đoạn "agent")."""
    conversation = _openai_messages(question)
    tools = _build_openai_tools()

    for step in range(MAX_AGENT_STEPS):
        result = await call_log.timed(
            label,
            model,
            lambda: bounded(lambda: call(conversation, tools), own_timeout),
            stage=_STAGE,
        )

        if not result.tool_calls:
            return result.text or "Xin lỗi, em chưa tìm được câu trả lời."

        logger.info(
            "Agent (%s) yêu cầu %d tool call(s) ở bước %d.", label, len(result.tool_calls), step + 1
        )
        conversation.append(
            {
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": [
                    {
                        "id": tool_call["id"],
                        "type": "function",
                        "function": {
                            "name": tool_call["name"],
                            "arguments": json.dumps(tool_call["arguments"], ensure_ascii=False),
                        },
                    }
                    for tool_call in result.tool_calls
                ],
            }
        )
        for tool_call in result.tool_calls:
            logger.info(
                "Agent (%s) gọi tool '%s' với args=%s",
                label,
                tool_call["name"],
                tool_call["arguments"],
            )
            result_text = await _call_tool(tool_call["name"], tool_call["arguments"])
            conversation.append(
                {"role": "tool", "tool_call_id": tool_call["id"], "content": result_text}
            )

    return _STEP_LIMIT_REPLY


async def _run_router9(question: str) -> str:
    """Agent loop qua router9 (function-calling chuẩn OpenAI) - chỉ chạy khi bật
    _ROUTER9_NATIVE_TOOLS_ENABLED."""
    from ai import router9_client

    return await _run_openai_tools(
        "router9",
        lambda messages, tools: router9_client.generate_with_tools(messages, tools),
        question,
        config.ROUTER9_CALL_TIMEOUT_SEC,
        await _router9_model(),
    )


async def _router9_model() -> str:
    from ai import router9_client

    try:
        return await router9_client.get_preferred_model_name() or config.ROUTER9_MODEL
    except Exception:
        return config.ROUTER9_MODEL


async def _router9_generate(prompt: str, **kwargs):
    """1 lượt router9 của nhánh JSON-router: bounded() + ghi provider_calls."""
    from ai import router9_client

    return await call_log.timed(
        "router9",
        await _router9_model(),
        lambda: bounded(
            lambda: router9_client.generate(prompt, **kwargs), config.ROUTER9_CALL_TIMEOUT_SEC
        ),
        stage=_STAGE,
    )


async def _tool_provider_usable(name: str) -> bool:
    """Groq/OpenRouter dùng được cho agent: có key (env hoặc trang admin), đang bật,
    không cooldown hết quota."""
    from ai import groq_client, openrouter_client, provider_overrides
    from ai.provider_state import provider_state

    client = groq_client if name == "groq" else openrouter_client
    try:
        if not await client.api_key():
            return False
        if not await provider_overrides.is_enabled(name):
            return False
    except Exception:
        logger.warning("Agent: không đọc được cấu hình %s, bỏ qua.", name, exc_info=True)
        return False
    return not provider_state.api_in_cooldown(name)


async def _run_tool_provider(name: str, question: str) -> str:
    from ai import groq_client, openrouter_client

    client = groq_client if name == "groq" else openrouter_client
    try:
        model = await client.model_name()
    except Exception:
        model = None
    return await _run_openai_tools(name, client.generate_with_tools, question, model=model)


async def _run_router9_json_router(question: str) -> Optional[str]:
    """Lưới an toàn TRUNG GIAN (Hướng A) - KHÔNG dùng tham số `tools` của API,
    chỉ hỏi router9 bằng 1 prompt yêu cầu trả JSON (cùng kiểu
    services/tools.py::maybe_run_tool), nên không phụ thuộc gateway có hỗ trợ
    đúng function-calling chuẩn OpenAI hay không. Tối đa 1 tool (không đa
    bước). Trả về None nếu không xác định được tool hợp lệ hoặc lỗi bất kỳ
    bước nào - caller (ask_agent) coi None là "nhánh này không giải quyết
    được", rơi tiếp xuống api1/api2."""
    system = _current().system
    router_prompt = _JSON_ROUTER_INSTRUCTION_TEMPLATE.format(
        tool_list=_build_tool_list_text(), question=question
    )
    try:
        router_response = await _router9_generate(router_prompt, temperature=0.1)
    except TurnTimeoutError:
        raise
    except Exception:
        logger.warning(
            "Agent (router9 JSON-router): lỗi khi hỏi định tuyến, bỏ qua nhánh này.", exc_info=True
        )
        return None

    raw = strip_json_fence(router_response.text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "Agent (router9 JSON-router): model không trả JSON hợp lệ, bỏ qua nhánh này. raw=%r",
            raw[:200],
        )
        return None

    tool_name = str(data.get("tool", "none")).strip() if isinstance(data, dict) else "none"

    if tool_name == "none" or tool_name not in {spec.name for spec in _specs()}:
        # Không cần tool - hỏi router9 trả lời trực tiếp bằng văn phong tự
        # nhiên (KHÔNG dùng lại text ép JSON ở trên).
        try:
            reply = await _router9_generate(
                question, system_instruction=system, history=list(_current().history)
            )
            return (reply.text or "").strip() or None
        except TurnTimeoutError:
            raise
        except Exception:
            logger.warning(
                "Agent (router9 JSON-router): lỗi khi trả lời trực tiếp, bỏ qua nhánh này.",
                exc_info=True,
            )
            return None

    args = data.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    logger.info("Agent (router9 JSON-router) gọi tool '%s' với args=%s", tool_name, args)
    tool_result = await _call_tool(tool_name, args)

    synth_prompt = (
        f"Câu hỏi của người dùng: {question}\n\n"
        f"Kết quả tool '{tool_name}' vừa chạy để trả lời câu hỏi trên:\n{tool_result}\n\n"
        "Dựa DUY NHẤT vào kết quả tool ở trên, trả lời người dùng bằng văn phong tự nhiên, ngắn gọn, tiếng Việt. "
        "Không tự bịa thêm thông tin ngoài kết quả tool."
    )
    try:
        final = await _router9_generate(synth_prompt, system_instruction=system)
        return (final.text or "").strip() or tool_result
    except TurnTimeoutError:
        raise
    except Exception:
        logger.warning(
            "Agent (router9 JSON-router): lỗi khi tổng hợp câu trả lời cuối, trả thẳng kết quả tool thô.",
            exc_info=True,
        )
        return tool_result


async def _build_turn(user_id: int | None, question: str, address_form: str | None) -> _Turn:
    """Persona + trí nhớ + 4 dòng hội thoại gần nhất của ĐÚNG user_id người hỏi
    (Zalo, kể cả Zalo của chủ bot, tách hẳn Telegram). Lỗi DB -> lượt chạy không
    có ngữ cảnh, không chặn /agent."""
    system = _persona(address_form)
    if not user_id:
        return _Turn(system=system)
    role = reg.ROLE_OWNER if config.is_owner_user_id(user_id) else reg.ROLE_ADMIN
    history: tuple[tuple[str, str], ...] = ()
    try:
        from core import database as db

        rows = await db.get_session_messages(
            user_id, _HISTORY_ROWS // 2, config.CHAT_SESSION_TIMEOUT_SEC
        )
        history = tuple(
            (role_, content[:_HISTORY_ROW_MAX_CHARS]) for role_, content in rows[-_HISTORY_ROWS:]
        )
    except Exception:
        logger.warning("Agent: không đọc được lịch sử hội thoại.", exc_info=True)
    try:
        from services import memory_service

        memory = (await memory_service.build_memory_context(user_id, question) or "").strip()
        if memory:
            system += (
                "\n\nĐiều đã biết về người dùng (trí nhớ dài hạn):\n" + memory[:_MEMORY_MAX_CHARS]
            )
    except Exception:
        logger.warning("Agent: không đọc được trí nhớ dài hạn.", exc_info=True)
    return _Turn(user_id=user_id, role=role, system=system, history=history)


async def _save_turn(user_id: int | None, question: str, answer: str) -> None:
    """Lưu câu hỏi/đáp /agent vào lịch sử chat của người hỏi (best-effort) qua
    conversation_log.record_exchange: kind 'agent' (trí nhớ chỉ đọc lời người
    dùng của lượt này), câu trả lời được rút gọn như mọi lượt khác."""
    if not user_id or not answer:
        return
    try:
        from services import conversation_log

        await conversation_log.record_exchange(
            user_id, f"/agent {question}", answer, conversation_log.KIND_AGENT
        )
    except Exception:
        logger.warning("Agent: không lưu được lịch sử hội thoại.", exc_info=True)


async def ask_agent(
    question: str, *, user_id: int | None = None, address_form: str | None = None
) -> tuple[str, str]:
    """Chạy vòng lặp agent cho 1 câu hỏi, trả về (câu trả lời, provider đã dùng).

    Thứ tự: Groq rồi OpenRouter (function-calling đa bước, bỏ qua nếu chưa cấu
    hình/đang tắt/cooldown; lỗi kể cả 429 thì chuyển tiếp mà KHÔNG đặt cooldown
    cho chat) -> router9 (native tools nếu bật, rồi JSON-router 1 tool) ->
    api1/api2 (Gemini function calling; hết quota thì rớt api2).

    ``user_id`` (khuyến nghị): persona Lan Anh + trí nhớ + 4 dòng hội thoại gần
    nhất của chính người đó; tool chỉ đọc dữ liệu của người đó; Q&A được lưu vào
    lịch sử. ``address_form``: cách gọi riêng thành viên Zalo (core/address.py).

    Cả lượt chạy trong hạn chót CHAT_TURN_TIMEOUT_SEC (TurnTimeoutError khi
    hết), doc_link/xem_rss chỉ đọc URL có sẵn trong câu hỏi/kết quả tìm kiếm,
    tool ghi dữ liệu không bao giờ chạy trong /agent."""
    with turn_deadline(), agent_url_scope(question), reg.taint_scope():
        turn = await _build_turn(user_id, question, address_form)
        token = _turn.set(turn)
        try:
            text, provider = await _ask_agent(question)
        finally:
            _turn.reset(token)
    await _save_turn(user_id, question, text)
    return text, provider


async def _ask_agent(question: str) -> tuple[str, str]:
    for name in _TOOL_PROVIDERS:
        if not await _tool_provider_usable(name):
            continue
        try:
            return await _run_tool_provider(name, question), name
        except TurnTimeoutError:
            raise
        except Exception as exc:
            # Cố ý KHÔNG mark_api_exhausted: 429 của agent (TPM nhỏ) không được làm
            # utility_json/chat mất Groq - agent chỉ chuyển sang provider kế.
            logger.warning("Agent: %s (tools) lỗi (%s), thử provider kế.", name, exc)

    if _ROUTER9_AGENT_ENABLED:
        from ai.provider_state import provider_state

        await provider_state.ensure_loaded()
        if provider_state.router9_enabled and provider_state.router9_dead_since is None:
            if _ROUTER9_NATIVE_TOOLS_ENABLED:
                try:
                    text = await _run_router9(question)
                    await provider_state.mark_router9_alive()
                    return text, "router9"
                except TurnTimeoutError:
                    raise
                except Exception as exc:
                    # Cố ý KHÔNG mark_router9_dead() - xem docstring module.
                    logger.warning(
                        "Agent: router9 (native tools) lỗi (%s), thử JSON-router.",
                        exc,
                        exc_info=True,
                    )

            try:
                text = await _run_router9_json_router(question)
                if text:
                    await provider_state.mark_router9_alive()
                    return text, "router9"
                logger.warning(
                    "Agent: router9 (JSON-router) không xác định được tool/trả lời, chuyển sang api1/api2."
                )
            except TurnTimeoutError:
                raise
            except Exception as exc:
                logger.warning(
                    "Agent: router9 (JSON-router) lỗi ngoài dự kiến (%s), chuyển sang api1/api2.",
                    exc,
                    exc_info=True,
                )

    last_exc: Exception | None = None
    for idx in await official_client.usable_indices():
        name = f"api{idx}"
        try:
            text = await _run_one_provider(idx, question)
            return text, name
        except TurnTimeoutError:
            raise
        except Exception as exc:
            last_exc = exc
            if is_quota_exhausted_error(exc):
                await official_client.note_failure(idx, exc)
                logger.warning("Agent: %s hết quota, thử provider kế tiếp.", name)
                continue
            logger.exception("Agent: lỗi không phải quota ở %s.", name)
            continue
    if last_exc is None:
        raise RuntimeError(
            "Cả router9, api1 và api2 đều lỗi cho agent: không có API dự phòng khả dụng "
            "(chưa cấu hình, đang tắt hoặc đang cooldown hết quota)."
        )
    raise RuntimeError(f"Cả router9, api1 và api2 đều lỗi cho agent: {last_exc}")
